"""
Feedback manager for ControllerManager.

Handles LED colors, vibration, and visual effects on controllers.
Provides both internal methods and effect management with auto-restore.

Phase 46: Internal feedback methods.
Phase 57: Backend abstraction for platform independence.
"""

import asyncio
import contextlib
import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opentelemetry.context import Context

from lib.colors import Colors
from lib.telemetry import extract_trace_context, get_tracer
from proto import controller_manager_pb2
from services.controller_manager import metrics
from services.controller_manager.effects_base import (
    CHARGE_GRANT_PULSES,
    CHARGE_MAX_DURATION_MS,
    CHARGE_PRE_EXPIRY_PULSES,
    CHARGE_PULSE_GAP_MS,
    CHARGE_RENDER_INTERVAL_S,
    CHARGE_SLOT_POLL_S,
    CHARGE_SLOT_WAIT_S,
    DEFAULT_CHARGE_PRE_EXPIRY_S,
    DEFAULT_CHARGE_REMINDER_S,
    DEFAULT_CHARGE_RUMBLE_INTENSITY,
    DEFAULT_CHARGE_RUMBLE_PULSE_MS,
    DEFAULT_SHIELD_ENDING_LEAD_S,
    SHIELD_BLINK_WINDOW_S,
    SHIELD_ENDING_PULSES,
    SWELL_MS,
    SWELL_STEP_S,
    ActiveEffect,
    ChargeCues,
    ControllerEffectsBase,
    charge_frame,
    resolve_charge_cues,
    resolve_shield_ending_lead,
    shield_frame,
    swell_intensity,
)

if TYPE_CHECKING:
    from services.controller_manager.backend import ControllerBackend

logger = logging.getLogger(__name__)

# Lazy telemetry initialization - defers OTLP setup until first span
tracer = get_tracer(__name__)

# Span attribute keys (S1192 - avoid duplicate string literals)
CONTROLLER_SERIAL_ATTR = "controller.serial"

# Identify a controller (UC-004 BR-001): white 2 Hz flash plus a short rumble.
IDENTIFY_DEFAULT_MS = 3000
IDENTIFY_MIN_MS = 500
IDENTIFY_MAX_MS = 10_000
IDENTIFY_FLASH_HZ = 2
IDENTIFY_RUMBLE_INTENSITY = 150
IDENTIFY_RUMBLE_MS = 500


def clamp_identify_duration(duration_ms: int) -> int:
    """Identify duration: 0 or negative means the default, else clamped (UC-004 BR-001)."""
    if duration_ms <= 0:
        return IDENTIFY_DEFAULT_MS
    return max(IDENTIFY_MIN_MS, min(IDENTIFY_MAX_MS, duration_ms))


# Module-level cache for winner rainbow duration from flagd
_winner_rainbow_duration_ms: int | None = None

# Warning and death effect timings (milliseconds), UC-030 steps 7 and 9.
WARNING_FLASH_MS = 200
WARNING_VIBRATION_MS = 200
DEATH_RUMBLE_MS = 1000  # ~1 s, distinct from the 200 ms warning: with the LED off the rumble is the signal (#1832)
DEATH_RED_HOLD_MS = 300
DEATH_FADE_MS = 700


def get_winner_rainbow_duration_ms() -> int:
    """Get winner rainbow duration from flagd game domain.

    Falls back to 3000ms if flagd is unavailable.
    The value is cached and updated via _on_game_flags_changed().
    """
    if _winner_rainbow_duration_ms is not None:
        return _winner_rainbow_duration_ms
    return 3000


def init_game_settings_listener() -> None:
    """Initialize flagd game domain and register change listener.

    Called from server.py during startup to set up dynamic flag-based
    winner_rainbow_duration_ms configuration.
    """
    global _winner_rainbow_duration_ms
    try:
        from openfeature import api
        from openfeature.evaluation_context import EvaluationContext
        from openfeature.provider import ProviderEvent

        from lib.feature_flags import get_flag_client, init_flag_domain

        init_flag_domain("game")
        client = get_flag_client("game")

        # Initial read
        _winner_rainbow_duration_ms = client.get_integer_value("winner_rainbow_duration_ms", 3000, EvaluationContext())
        logger.info(f"winner_rainbow_duration_ms initialized from flagd: {_winner_rainbow_duration_ms}ms")

        # Register for changes
        api.add_handler(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, _on_game_flags_changed)

    except Exception as e:
        logger.warning(f"Could not initialize game flags, using defaults: {e}")


def _on_game_flags_changed(event_details) -> None:
    """Update winner_rainbow_duration_ms when flagd config changes."""
    global _winner_rainbow_duration_ms

    # Skip if the change is not for our flag
    changed_flags = getattr(event_details, "flags_changed", [])
    if changed_flags and "winner_rainbow_duration_ms" not in changed_flags:
        return

    try:
        from openfeature.evaluation_context import EvaluationContext

        from lib.feature_flags import get_flag_client

        client = get_flag_client("game")
        new_val = client.get_integer_value("winner_rainbow_duration_ms", 3000, EvaluationContext())
        if new_val != _winner_rainbow_duration_ms:
            logger.info(f"winner_rainbow_duration_ms updated from flagd: {_winner_rainbow_duration_ms} -> {new_val}ms")
            _winner_rainbow_duration_ms = new_val
    except Exception as e:
        logger.warning(f"Failed to refresh winner_rainbow_duration_ms from flagd: {e}")


def read_charge_cues() -> ChargeCues:
    """The charge rumble pattern for a new charge, from the four ``game`` flags (flags.md §4, #2094).

    Read when a charge layer starts (never per frame or per pulse), so a stage flip applies
    from the next charge; any failure gives the defaults.
    """
    from lib.feature_flags import read_float_flag, read_int_flag

    return resolve_charge_cues(
        read_int_flag("game", "shield.charge_rumble_intensity", DEFAULT_CHARGE_RUMBLE_INTENSITY),
        read_int_flag("game", "shield.charge_rumble_pulse_ms", DEFAULT_CHARGE_RUMBLE_PULSE_MS),
        read_float_flag("game", "shield.charge_reminder_seconds", DEFAULT_CHARGE_REMINDER_S),
        read_float_flag("game", "shield.charge_pre_expiry_seconds", DEFAULT_CHARGE_PRE_EXPIRY_S),
    )


def read_shield_ending_lead() -> float:
    """Seconds before an active shield ends for the "ending soon" pulse (flags.md §4, #2094); read per layer start."""
    from lib.feature_flags import read_float_flag

    return resolve_shield_ending_lead(
        read_float_flag("game", "shield.ending_rumble_seconds", DEFAULT_SHIELD_ENDING_LEAD_S)
    )


@dataclass
class ShieldLayer:
    """One agent shield on one controller (#1301, UC-040 BR-008..BR-010).

    The shield is a persistent LED LAYER, not a replaceable effect: short LED
    effects overlay it and it resumes after them (``shield_frame`` is a pure
    function of the time since ``start``).
    """

    start: float
    total_s: float
    color: tuple[int, int, int]
    kind: str = "full"  # "full" | "toughness_boost" | "charge" (#1837)
    blink_reported: bool = False

    @property
    def toughness_boost(self) -> bool:
        return self.kind == "toughness_boost"

    def remaining(self, now: float) -> float:
        return self.start + self.total_s - now


# Effects that never touch the LED: they neither overlay nor end a shield, and
# skip the active-effect placeholder entirely (#1301).
NON_LED_EFFECTS = frozenset(
    {controller_manager_pb2.GAME_EFFECT_RUMBLE, controller_manager_pb2.GAME_EFFECT_SHIELD_SWELL}
)
SHIELD_EFFECTS = frozenset(
    {
        controller_manager_pb2.GAME_EFFECT_SHIELD,
        controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST,
        controller_manager_pb2.GAME_EFFECT_SHIELD_CHARGE,
    }
)
_SHIELD_KIND_BY_EFFECT = {
    controller_manager_pb2.GAME_EFFECT_SHIELD: "full",
    controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST: "toughness_boost",
    controller_manager_pb2.GAME_EFFECT_SHIELD_CHARGE: "charge",
}
# The activation swell lives in the rumble slot (vibration_tasks), so any later rumble replaces it (#1837).
SWELL_TASK_NAME = "shield_swell"
# The death rumble owns the slot for its full duration; the swell and the charge pulses never replace it.
DEATH_RUMBLE_TASK_NAME = "death_rumble"
# A charge pulse burst (#2094) plays only in a free rumble slot; any later rumble replaces it.
# Effects a GAME_EFFECT_PULSE (the lobby mode pulse, UC-015 BR-015) must not take over (UC-004 BR-005).
PULSE_PROOF_EFFECTS = frozenset(
    {controller_manager_pb2.GAME_EFFECT_IDENTIFY, controller_manager_pb2.GAME_EFFECT_SHOW_BATTERY}
)
CHARGE_PULSE_TASK_NAME = "charge_pulse"
# The "ending soon" double pulse of an active shield (#2094), same slot rules.
SHIELD_ENDING_PULSE_TASK_NAME = "shield_ending_pulse"
_SHIELD_PULSE_TASK_NAMES = (CHARGE_PULSE_TASK_NAME, SHIELD_ENDING_PULSE_TASK_NAME)
_CUE_EPSILON_S = 1e-6


class FeedbackManager(ControllerEffectsBase):
    """
    Manages LED colors, vibration, and visual effects on controllers.

    Inherits from ControllerEffectsBase for effect animation methods.
    """

    def __init__(
        self,
        backend: "ControllerBackend",
        tracked_controllers: dict[str, dict],
    ):
        """
        Initialize feedback manager.

        Args:
            backend: Controller backend for hardware control
            tracked_controllers: Shared dict of tracked controller info
        """
        super().__init__()  # Initialize ControllerEffectsBase

        self.backend = backend
        self.tracked_controllers = tracked_controllers

        # Effect lock for async effect management
        self.effect_lock = asyncio.Lock()

        # Base colors per controller (for auto-restore after effects)
        self.base_colors: dict[str, tuple[int, int, int]] = {}

        # Effects that can be cancelled by a base_color message
        self.cancellable_effects: set[int] = {
            controller_manager_pb2.GAME_EFFECT_FORCE_START_CHARGE,
        }

        # Agent shield layers per controller ("full" / "toughness_boost" / "charge") and the one
        # renderer task per controller that paints the top layer (#1301). The
        # shield is NOT an active_effects entry: other effects overlay it, a new
        # base colour only changes what is restored after it (UC-040 BR-010).
        self._shield_layers: dict[str, dict[str, ShieldLayer]] = {}
        self._shield_tasks: dict[str, asyncio.Task] = {}
        # One charge rumble pattern task per controller while a charge layer is held (#2094).
        self._charge_cue_tasks: dict[str, asyncio.Task] = {}
        # One "ending soon" task per controller while a full shield layer runs (#2094).
        self._shield_ending_tasks: dict[str, asyncio.Task] = {}

        # Colour shown before an identify started, for controllers without a base colour (UC-004 BR-002)
        self._identify_fallback: dict[str, tuple[int, int, int] | None] = {}

        # Vibration duration tasks
        self.vibration_tasks: dict[str, asyncio.Task] = {}

        # Cached metric label children (Issue #542: avoid label lookups per LED update)
        self._color_gauges: dict[str, dict] = {}

        # The LED colour last written to each controller, effect frames included.
        # Streamed as ControllerState.color so the dashboard (and anyone without
        # a real PS Move) sees what the LED shows, e.g. the shield fade (#1301).
        self.current_colors: dict[str, tuple[int, int, int]] = {}

    def _get_color_gauges(self, serial: str) -> dict:
        """Get or create cached color metric gauges for a controller."""
        gauges = self._color_gauges.get(serial)
        if gauges is None:
            gauges = {
                "r": metrics.controller_color_r.labels(serial=serial),
                "g": metrics.controller_color_g.labels(serial=serial),
                "b": metrics.controller_color_b.labels(serial=serial),
                "hex": metrics.controller_color_hex.labels(serial=serial),
            }
            self._color_gauges[serial] = gauges
        return gauges

    async def _set_led_color(self, serial: str, color: tuple[int, int, int]) -> None:
        """
        Set LED color on a controller (implements abstract from ControllerEffectsBase).

        Args:
            serial: Controller serial number
            color: RGB tuple (0-255, 0-255, 0-255)
        """
        if serial not in self.tracked_controllers:
            return

        try:
            await self.backend.set_led_color(serial, color[0], color[1], color[2])
            self.current_colors[serial] = (color[0], color[1], color[2])
            # Update color metrics for Grafana dashboard (Issue #542: cached gauges)
            gauges = self._get_color_gauges(serial)
            gauges["r"].set(color[0])
            gauges["g"].set(color[1])
            gauges["b"].set(color[2])
            gauges["hex"].set((color[0] << 16) | (color[1] << 8) | color[2])
        except Exception as e:
            logger.error(f"Error setting LED color on {serial}: {e}", exc_info=True)

    async def set_controller_color(self, serial: str, color_rgb: tuple[int, int, int]) -> bool:
        """
        Set controller color (internal method).

        Can be called from both SetControllerColor RPC and stream-based ColorCommand.

        Args:
            serial: Controller serial number
            color_rgb: RGB color tuple (r, g, b)

        Returns:
            True if successful, False otherwise
        """
        try:
            if serial not in self.tracked_controllers:
                logger.warning(f"Controller {serial} not found for color change")
                return False

            success = await self.backend.set_led_color(serial, color_rgb[0], color_rgb[1], color_rgb[2])
            if success:
                logger.debug(f"Set color on {serial}: RGB{color_rgb}")
                self.current_colors[serial] = (color_rgb[0], color_rgb[1], color_rgb[2])
                # Emit color metrics for Grafana dashboard (Issue #542: cached gauges)
                gauges = self._get_color_gauges(serial)
                gauges["r"].set(color_rgb[0])
                gauges["g"].set(color_rgb[1])
                gauges["b"].set(color_rgb[2])
                gauges["hex"].set((color_rgb[0] << 16) | (color_rgb[1] << 8) | color_rgb[2])
            else:
                logger.warning(f"Failed to set color on {serial}")
            return success

        except Exception as e:
            logger.error(f"Error setting color on {serial}: {e}", exc_info=True)
            return False

    async def set_vibration(self, serial: str, intensity: int, duration_ms: int) -> bool:
        """
        Set controller vibration (internal method).

        Called from stream-based VibrationCommand.

        Args:
            serial: Controller serial number
            intensity: Vibration intensity (0-255)
            duration_ms: Duration in milliseconds

        Returns:
            True if successful, False otherwise
        """
        try:
            if serial not in self.tracked_controllers:
                logger.warning(f"Controller {serial} not found for vibration")
                return False

            will_schedule = duration_ms > 0 and intensity > 0
            previous = self.vibration_tasks.get(serial)
            if previous is not None and (
                will_schedule or previous.get_name() in (SWELL_TASK_NAME, *_SHIELD_PULSE_TASK_NAMES)
            ):
                # Cancel BEFORE writing: a running swell step must not overwrite the new rumble (#1837).
                previous.cancel()
                del self.vibration_tasks[serial]

            success = await self.backend.set_rumble(serial, intensity)
            if success:
                logger.debug(f"Set vibration on {serial}: intensity={intensity}")

                # Schedule vibration stop if duration is specified
                if will_schedule:
                    # Store task reference to prevent garbage collection
                    self.vibration_tasks[serial] = asyncio.create_task(
                        self._delayed_stop_vibration(serial, duration_ms)
                    )
            else:
                logger.warning(f"Failed to set vibration on {serial}")
            return success

        except Exception as e:
            logger.error(f"Error setting vibration on {serial}: {e}", exc_info=True)
            return False

    async def _delayed_stop_vibration(self, serial: str, duration_ms: int) -> None:
        """Stop vibration on a controller after async delay."""
        try:
            await asyncio.sleep(duration_ms / 1000.0)
            await self.backend.set_rumble(serial, 0)
            logger.debug(f"Vibration stopped on {serial} (duration expired)")
        except Exception as e:
            logger.error(f"Error in delayed vibration stop for {serial}: {e}")

    async def play_effect_with_restore(
        self,
        serial: str,
        effect_type: str,
        color: tuple[int, int, int],
        duration_ms: int,
        speed: int,
        restore_color: tuple[int, int, int] | None,
    ) -> None:
        """
        Play an effect and restore to base color when done.

        Args:
            serial: Controller serial
            effect_type: "flash", "pulse", "rainbow", "fade_out", "fade_in"
            color: RGB color for effect
            duration_ms: Effect duration
            speed: Effect speed (1-10)
            restore_color: Color to restore to after effect (None = no restore)
        """
        # Cancel any existing effect, preserving the effect_type if already set
        # (handle_game_effect sets effect_type before calling handler)
        current_effect_type = await self._take_over_effect(serial)

        # Mark effect as active (polling skips LED refresh)
        self.backend.set_effect_active(serial, True)

        # Create wrapper that restores color after effect
        async def effect_with_restore():
            try:
                match effect_type:
                    case "flash":
                        await self._effect_flash(serial, color, duration_ms, speed)
                    case "pulse":
                        await self._effect_pulse(serial, color, duration_ms, speed)
                    case "rainbow":
                        await self._effect_rainbow(serial, duration_ms, speed)
                    case "fade_out":
                        await self._effect_fade_out(serial, color, duration_ms)
                    case "fade_in":
                        await self._effect_fade_in(serial, color, duration_ms)
            except asyncio.CancelledError:
                raise
            finally:
                # Remove from active_effects so new base colors can be applied immediately,
                # unless another effect already owns the LED (#1430).
                if await self._release_effect(serial):
                    # Restore the current base colour, or hand the LED back to an
                    # active agent shield (#1301, UC-040 BR-010).
                    fallback = restore_color if isinstance(restore_color, tuple) else None
                    await self._finish_effect(serial, bool(restore_color), fallback)

        task = asyncio.create_task(effect_with_restore())
        async with self.effect_lock:
            self.active_effects[serial] = ActiveEffect(task=task, effect_type=current_effect_type)

    async def _take_over_effect(self, serial: str) -> int:
        """Cancel and await the running effect so a new one owns the LED.

        Returns the effect_type of the replaced entry (handle_game_effect types
        the entry before calling the handler), 0 when there was none.
        """
        existing_task = None
        effect_type = 0
        async with self.effect_lock:
            active = self.active_effects.pop(serial, None)
            if active is not None:
                effect_type = active.effect_type
                existing_task = active.task
                existing_task.cancel()
        # Await cancelled task outside lock to avoid deadlock with task's finally block
        if existing_task:
            with contextlib.suppress(asyncio.CancelledError):
                await existing_task
        return effect_type

    async def _release_effect(self, serial: str) -> bool:
        """At an effect task's end: drop its entry if it still owns the LED.

        False when another effect replaced it; that effect owns the LED, so the
        ending task must neither pop its entry nor restore the colour (#1430).
        """
        async with self.effect_lock:
            active = self.active_effects.get(serial)
            if active is not None and active.task is not asyncio.current_task():
                return False
            self.active_effects.pop(serial, None)
            return True

    # =========================================================================
    # Individual effect handlers
    # =========================================================================

    async def _effect_none(self, serial: str, **_kwargs) -> None:
        """No-op - clear tracking."""
        # Note: async kept for interface consistency with other effect handlers
        async with self.effect_lock:
            self.active_effects.pop(serial, None)

    async def _effect_player_warning(
        self, serial: str, restore_color: tuple | None, trace_context: Context | None = None, **_kwargs
    ) -> None:
        """White flash + vibrate, restore to base color.

        Always pass truthy value to ensure restoration to base_colors at effect end.
        """
        flash_ms = WARNING_FLASH_MS
        vibration_ms = WARNING_VIBRATION_MS
        with tracer.start_as_current_span("effect_player_warning", context=trace_context) as span:
            span.set_attribute(CONTROLLER_SERIAL_ATTR, serial)

            span.add_event("flash_start", {"color": "white", "duration_ms": flash_ms})
            await self.play_effect_with_restore(serial, "flash", Colors.White.value, flash_ms, 5, restore_color or True)

            span.add_event("vibration", {"intensity": 100, "duration_ms": vibration_ms})
            await self.set_vibration(serial, 100, vibration_ms)

    async def _effect_player_death(self, serial: str, trace_context: Context | None = None, **_kwargs) -> None:
        """Red + vibrate, then fade to off to signal player is out.

        Transition: ~1 s rumble + solid red briefly + fade to black (LED stays off afterwards).
        This feels more immediate and satisfying than slow blinking.
        """
        rumble_ms = DEATH_RUMBLE_MS
        red_hold_ms = DEATH_RED_HOLD_MS
        fade_ms = DEATH_FADE_MS
        with tracer.start_as_current_span("effect_player_death", context=trace_context) as span:
            span.set_attribute(CONTROLLER_SERIAL_ATTR, serial)

            # Cancel any active effect (e.g., warning flash) and the agent shield
            # immediately for a clean transition (#1301, UC-040 A17)
            span.add_event("cancel_existing_effect")
            await self.end_shield_effect(serial, restore=False)
            await self.cancel_effect(serial)

            # Long rumble (~1 s, UC-030 step 9): clearly different from the 200 ms warning. It runs on its
            # own stop timer, so the red hold, the fade and the end of the game do not cut it short.
            span.add_event("vibration", {"intensity": 255, "duration_ms": rumble_ms})
            await self.set_vibration(serial, 255, rumble_ms)
            stop_timer = self.vibration_tasks.get(serial)
            if stop_timer is not None:
                stop_timer.set_name(DEATH_RUMBLE_TASK_NAME)  # the swell and the charge pulses leave it alone

            # Solid red briefly (default 300ms) then fade to black (default 700ms)
            # This replaces the awkward slow blink (1Hz over 1500ms)
            span.add_event("led_red")
            await self._set_led_color(serial, Colors.Red.value)
            await asyncio.sleep(red_hold_ms / 1000.0)

            span.add_event("fade_to_black", {"duration_ms": fade_ms})
            # Set base color to black BEFORE spawning the fade so a concurrent
            # base_color command (e.g., menu lobby reset) that lands during the
            # effect setup can't be clobbered afterwards — apply_base_color
            # writes win, and the fade's restore picks up the latest value (#757).
            self.base_colors[serial] = Colors.Black.value
            await self.play_effect_with_restore(serial, "fade_out", Colors.Red.value, fade_ms, 1, Colors.Black.value)

    async def _effect_player_respawn(self, serial: str, **_kwargs) -> None:
        """White during spawn protection."""
        async with self.effect_lock:
            self.active_effects.pop(serial, None)
        await self.set_controller_color(serial, Colors.White.value)
        self.base_colors[serial] = Colors.White.value

    async def _effect_winner_rainbow(
        self, serial: str, restore_color: tuple | None, trace_context: Context | None = None, **_kwargs
    ) -> None:
        """Rainbow effect, restore to base color.

        Always pass truthy value to ensure restoration to base_colors at effect end.
        (base_colors may be updated DURING the effect, e.g., menu sends lobby color)
        """
        duration_ms = get_winner_rainbow_duration_ms()
        # The winner rainbow ends an agent shield (#1301, UC-040 A17).
        await self.end_shield_effect(serial, restore=False)
        with tracer.start_as_current_span("effect_winner_rainbow", context=trace_context) as span:
            span.set_attribute(CONTROLLER_SERIAL_ATTR, serial)
            span.set_attribute("effect.duration_ms", duration_ms)

            span.add_event("rainbow_start", {"duration_ms": duration_ms})
            await self.play_effect_with_restore(
                serial, "rainbow", Colors.White.value, duration_ms, 1, restore_color or True
            )
            span.add_event("rainbow_end")

    async def _effect_countdown(
        self,
        serial: str,
        restore_color: tuple | None,
        trace_context: Context | None = None,
        duration_ms: int = 0,
        **_kwargs,
    ) -> None:
        """Full countdown sequence: Red → Yellow → Green."""
        # Use provided duration or default to 750ms per phase
        phase_duration_ms = duration_ms if duration_ms > 0 else 750
        # A running effect (e.g. an identify, UC-004) must not keep painting over the countdown.
        await self._take_over_effect(serial)
        task = asyncio.create_task(self._countdown_sequence(serial, restore_color, trace_context, phase_duration_ms))
        async with self.effect_lock:
            self.active_effects[serial] = ActiveEffect(
                task=task, effect_type=controller_manager_pb2.GAME_EFFECT_COUNTDOWN
            )

    async def _effect_admin_enter(self, serial: str, **_kwargs) -> None:
        """White flash, then persistent white."""
        self.base_colors[serial] = Colors.White.value
        await self.play_effect_with_restore(serial, "flash", Colors.White.value, 600, 5, Colors.White.value)

    async def _effect_admin_exit(self, serial: str, restore_color: tuple | None, **_kwargs) -> None:
        """Restore to base color (instant)."""
        async with self.effect_lock:
            self.active_effects.pop(serial, None)
        if restore_color:
            await self.set_controller_color(serial, restore_color)

    async def _effect_low_battery(self, serial: str, restore_color: tuple | None, **_kwargs) -> None:
        """Red flash 2x warning."""
        for _ in range(2):
            await self.set_controller_color(serial, Colors.Red.value)
            await asyncio.sleep(0.15)
            await self.set_controller_color(serial, Colors.Red20.value)
            await asyncio.sleep(0.15)
        async with self.effect_lock:
            self.active_effects.pop(serial, None)
        if restore_color:
            await self.set_controller_color(serial, restore_color)

    async def _effect_force_start_charge(self, serial: str, **_kwargs) -> None:
        """Fade from white to dim over 2s."""
        await self.play_effect_with_restore(serial, "fade_out", Colors.White.value, 2000, 5, None)

    async def _effect_show_battery(self, serial: str, restore_color: tuple | None, **_kwargs) -> None:
        """Show battery level for 1 second, then restore.

        Always pass truthy value to ensure restoration to base_colors at effect end.
        """
        battery = self.tracked_controllers.get(serial, {}).get("battery", 0)
        battery_color = self._get_battery_color(battery)
        await self.play_effect_with_restore(serial, "flash", battery_color, 1000, 1, restore_color or True)
        logger.debug("Battery display: %s level=%d%% color=%s", serial, battery, battery_color)

    async def _effect_rumble(self, serial: str, duration_ms: int = 0, speed: int = 0, **_kwargs) -> None:
        """Vibrate only (for secret signaling like traitor/werewolf)."""
        effect_duration = duration_ms if duration_ms > 0 else 2000
        effect_speed = speed if speed > 0 else 5
        intensity = min(255, effect_speed * 50)
        await self.set_vibration(serial, intensity, effect_duration)

    async def _game_effect_pulse(
        self,
        serial: str,
        restore_color: tuple | None,
        color: tuple | None = None,
        duration_ms: int = 0,
        speed: int = 0,
        **_kwargs,
    ) -> None:
        """Pulse with color, restore to base."""
        effect_color = color if color else Colors.White.value
        effect_duration = duration_ms if duration_ms > 0 else 600
        effect_speed = speed if speed > 0 else 3
        await self.play_effect_with_restore(serial, "pulse", effect_color, effect_duration, effect_speed, restore_color)

    async def _game_effect_flash(
        self,
        serial: str,
        restore_color: tuple | None,
        color: tuple | None = None,
        duration_ms: int = 0,
        speed: int = 0,
        **_kwargs,
    ) -> None:
        """Brief flash with color, restore to base."""
        effect_color = color if color else Colors.White.value
        effect_duration = duration_ms if duration_ms > 0 else 400
        effect_speed = speed if speed > 0 else 5
        await self.play_effect_with_restore(serial, "flash", effect_color, effect_duration, effect_speed, restore_color)

    def _pulse_yields(self, serial: str, effect: int) -> bool:
        """A PULSE that would take over a running identify or battery display (caller holds effect_lock)."""
        if effect != controller_manager_pb2.GAME_EFFECT_PULSE:
            return False
        active = self.active_effects.get(serial)
        return active is not None and active.effect_type in PULSE_PROOF_EFFECTS and not active.task.done()

    def _identify_running(self, serial: str) -> bool:
        active = self.active_effects.get(serial)
        return (
            active is not None
            and active.effect_type == controller_manager_pb2.GAME_EFFECT_IDENTIFY
            and not active.task.done()
        )

    async def start_identify(self, serial: str, duration_ms: int = 0) -> bool:
        """Identify (UC-004 BR-001, BR-002): white 2 Hz flash + short rumble, then restore.

        Only the IdentifyController RPC starts it; GAME_EFFECT_IDENTIFY is not a
        stream GameEffectCommand. Restores the base colour; a controller without
        one gets back the colour its LED showed when the first identify started
        (a repeat press, A5, keeps that colour instead of a flash frame).
        """
        if serial not in self.tracked_controllers:
            return False
        effect_duration = clamp_identify_duration(duration_ms)
        async with self.effect_lock:
            if not self._identify_running(serial):
                self._identify_fallback[serial] = self.current_colors.get(serial)
            active = self.active_effects.get(serial)
            if active is not None:
                active.effect_type = controller_manager_pb2.GAME_EFFECT_IDENTIFY
            else:
                placeholder = asyncio.get_running_loop().create_future()
                placeholder.set_result(None)
                self.active_effects[serial] = ActiveEffect(
                    task=placeholder, effect_type=controller_manager_pb2.GAME_EFFECT_IDENTIFY
                )
        restore_to = self.base_colors.get(serial) or self._identify_fallback.get(serial) or True
        await self.set_vibration(serial, IDENTIFY_RUMBLE_INTENSITY, min(IDENTIFY_RUMBLE_MS, effect_duration))
        async with self.effect_lock:
            active = self.active_effects.get(serial)
            claimed = active is None or active.effect_type != controller_manager_pb2.GAME_EFFECT_IDENTIFY
        if claimed:
            # Cancelled while starting (a game claimed the controller, UC-004 BR-003).
            await self._stop_identify_rumble(serial)
            return False
        await self.play_effect_with_restore(
            serial, "flash", Colors.White.value, effect_duration, IDENTIFY_FLASH_HZ, restore_to
        )
        return True

    async def cancel_identify(self, serial: str) -> bool:
        """End a running identify and its rumble at once (UC-004 BR-003: a game claims the controller).

        Also ends one that is still starting (its entry is the IDENTIFY placeholder).
        """
        active = self.active_effects.get(serial)
        if active is None or active.effect_type != controller_manager_pb2.GAME_EFFECT_IDENTIFY:
            return False
        await self.cancel_effect(serial)
        await self._stop_identify_rumble(serial)
        return True

    async def _stop_identify_rumble(self, serial: str) -> None:
        vibration = self.vibration_tasks.pop(serial, None)
        if vibration is not None:
            vibration.cancel()
        await self.backend.set_rumble(serial, 0)

    # =========================================================================
    # Agent shield layer (#1301, UC-040 A16 / A17, BR-008..BR-010)
    # =========================================================================

    def shield_active(self, serial: str) -> bool:
        """Whether a shield layer is live (painted, or waiting under an overlay)."""
        return bool(self._shield_layers.get(serial))

    def _overlay_active(self, serial: str) -> bool:
        """A real (not placeholder) effect currently owns the LED."""
        active = self.active_effects.get(serial)
        return active is not None and not active.task.done()

    def _top_shield(self, serial: str, now: float) -> ShieldLayer | None:
        """The shield layer to paint now: full over toughness_boost over charge; expired layers are dropped."""
        layers = self._shield_layers.get(serial)
        if not layers:
            return None
        for kind in [k for k, layer in layers.items() if layer.remaining(now) <= 0]:
            del layers[kind]
        return layers.get("full") or layers.get("toughness_boost") or layers.get("charge")

    async def _finish_effect(self, serial: str, restore: bool, fallback: tuple[int, int, int] | None = None) -> None:
        """After an effect: hand the LED back to the shield, else restore the base colour.

        The current base colour wins (one set during the effect included); without
        one, ``fallback`` (the colour captured when the effect started) is shown.
        """
        if self.shield_active(serial):
            # The shield renderer paints its current frame on its next tick.
            return
        self.backend.set_effect_active(serial, False)
        current_restore = (self.base_colors.get(serial) or fallback) if restore else None
        if current_restore:
            await self._set_led_color(serial, current_restore)

    async def start_shield_effect(
        self,
        serial: str,
        duration_ms: int,
        toughness_boost: bool = False,
        color: tuple[int, int, int] | None = None,
        trace_context: Context | None = None,
        charge: bool = False,
    ) -> None:
        """Start or restart (extend) a shield layer; its schedule starts again at t=0.

        A charge layer (#1837) is capped at 120 s and a re-send while it is held only
        extends it: the breathing phase continues and the grant pattern is not repeated.
        """
        kind = "charge" if charge else ("toughness_boost" if toughness_boost else "full")
        now = self._clock.time()
        layers = self._shield_layers.setdefault(serial, {})
        newly_charged = False
        if charge:
            duration_ms = min(duration_ms, CHARGE_MAX_DURATION_MS)
            held = layers.get("charge")
            if held is not None and held.remaining(now) > 0:
                held.total_s = (now - held.start) + duration_ms / 1000.0
            else:
                newly_charged = True
        if kind != "charge" or newly_charged:
            layers[kind] = ShieldLayer(
                start=now,
                total_s=duration_ms / 1000.0,
                color=color if color else Colors.White.value,
                kind=kind,
            )
        task = self._shield_tasks.get(serial)
        if task is None or task.done():
            self.backend.set_effect_active(serial, True)
            self._shield_tasks[serial] = asyncio.create_task(self._shield_render_loop(serial, kind, trace_context))
        else:
            # The running renderer picks the new layer up on its next tick.
            logger.debug(f"Shield layer {kind} (re)started on {serial} for {duration_ms}ms")
        if newly_charged:
            # The charge rumble pattern starts with the layer, not with a re-send (UC-040 BR-021, #2094).
            await self._stop_charge_cues(serial)
            self._charge_cue_tasks[serial] = asyncio.create_task(self._charge_cue_loop(serial))
        elif kind == "full":
            # Each (re)start of the protection moves the "ending soon" pulse to the new end (#2094).
            await self._stop_shield_pulses(serial, self._shield_ending_tasks, SHIELD_ENDING_PULSE_TASK_NAME)
            self._shield_ending_tasks[serial] = asyncio.create_task(self._shield_ending_loop(serial))

    async def _shield_render_loop(self, serial: str, kind: str, trace_context: Context | None) -> None:
        """Paint the top shield layer at 20 Hz (a charge at 10 Hz, on change) while no overlay effect owns the LED."""
        interval = 0.05
        with tracer.start_as_current_span("controller.shield_effect", context=trace_context) as span:
            span.set_attribute(CONTROLLER_SERIAL_ATTR, serial)
            span.set_attribute("shield.kind", kind)
            first = self._shield_layers.get(serial, {}).get(kind)
            if first is not None:
                span.set_attribute("effect.duration_ms", int(first.total_s * 1000))
            end_reason = "cancelled"
            try:
                while True:
                    now = self._clock.time()
                    layer = self._top_shield(serial, now)
                    if layer is None:
                        end_reason = "expired"
                        break
                    elapsed = now - layer.start
                    is_charge = layer.kind == "charge"
                    if (
                        not is_charge
                        and not layer.blink_reported
                        and elapsed >= max(0.0, layer.total_s - SHIELD_BLINK_WINDOW_S)
                    ):
                        layer.blink_reported = True
                        span.add_event("shield_blink", {"shield.kind": layer.kind})
                    if not self._overlay_active(serial):
                        if is_charge:
                            # The player's current base colour, so a team/colour change is followed.
                            frame = charge_frame(elapsed, self.base_colors.get(serial) or layer.color)
                            if frame == self.current_colors.get(serial):
                                frame = None  # no visible change: skip the HID write
                        else:
                            frame = shield_frame(elapsed, layer.total_s, layer.color, layer.toughness_boost)
                        if frame is not None:
                            await self._set_led_color(serial, frame)
                    await self._clock.sleep(CHARGE_RENDER_INTERVAL_S if layer.kind == "charge" else interval)
            finally:
                span.add_event("shield_end", {"shield.end_reason": end_reason})
                if self._shield_tasks.get(serial) is asyncio.current_task():
                    self._shield_tasks.pop(serial, None)
        # Expired naturally: hand the LED back, unless an overlay owns it (its
        # own finish restores the base colour then).
        self._shield_layers.pop(serial, None)
        if not self._overlay_active(serial):
            await self._finish_effect(serial, restore=True)

    async def end_shield_effect(
        self,
        serial: str,
        toughness_boost_only: bool = False,
        restore: bool = True,
        kinds: frozenset[str] | None = None,
    ) -> bool:
        """End shield layers now (#1301, UC-040 A17); other effects are untouched.

        ``kinds`` (or ``toughness_boost_only``) ends just those layers (a full shield keeps
        painting under a charge end). With no layer left the renderer is cancelled and, if
        ``restore`` and no overlay owns the LED, the base colour is shown.
        Returns True if a layer was ended.
        """
        layers = self._shield_layers.get(serial)
        if not layers:
            return False
        if toughness_boost_only:
            kinds = frozenset({"toughness_boost"})
        if kinds is not None:
            ended = [layers.pop(kind, None) for kind in kinds]
            if all(layer is None for layer in ended):
                return False
            if "charge" in kinds:
                await self._stop_charge_cues(serial)
            if layers:
                return True  # another shield layer keeps painting
        self._shield_layers.pop(serial, None)
        await self._stop_charge_cues(serial)
        await self._stop_shield_pulses(serial, self._shield_ending_tasks, SHIELD_ENDING_PULSE_TASK_NAME)
        task = self._shield_tasks.pop(serial, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if not self._overlay_active(serial):
            if restore:
                await self._finish_effect(serial, restore=True)
            else:
                self.backend.set_effect_active(serial, False)
        return True

    async def _handle_shield_command(
        self, serial: str, effect: int, duration_ms: int, color: tuple | None, trace_context: Context | None
    ) -> None:
        kind = _SHIELD_KIND_BY_EFFECT[effect]
        if duration_ms <= 0:
            # SHIELD 0 ends every shield layer (game end); TOUGHNESS_BOOST 0 / SHIELD_CHARGE 0 only their own layer.
            await self.end_shield_effect(serial, kinds=None if kind == "full" else frozenset({kind}))
            return
        await self.start_shield_effect(
            serial,
            duration_ms,
            toughness_boost=kind == "toughness_boost",
            color=color,
            trace_context=trace_context,
            charge=kind == "charge",
        )

    # =========================================================================
    # Charge rumble pattern (#2094, UC-040 BR-021)
    # =========================================================================

    def _rumble_slot_busy(self, serial: str) -> bool:
        task = self.vibration_tasks.get(serial)
        return task is not None and not task.done()

    def _held_charge(self, serial: str) -> ShieldLayer | None:
        return self._live_layer(serial, "charge")

    def _live_layer(self, serial: str, kind: str) -> ShieldLayer | None:
        layer = self._shield_layers.get(serial, {}).get(kind)
        if layer is None or layer.remaining(self._clock.time()) <= 0 or serial not in self.tracked_controllers:
            return None
        return layer

    async def _stop_charge_cues(self, serial: str) -> None:
        """Stop the charge rumble pattern now, a pulse in progress included (UC-040 A32)."""
        await self._stop_shield_pulses(serial, self._charge_cue_tasks, CHARGE_PULSE_TASK_NAME)

    async def _stop_shield_pulses(self, serial: str, cue_tasks: dict[str, asyncio.Task], pulse_name: str) -> None:
        """Cancel a cue task and its pulse in progress; a cut pulse turns the motor off unless a rumble took over."""
        task = cue_tasks.pop(serial, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        pulse = self.vibration_tasks.get(serial)
        if pulse is not None and pulse.get_name() == pulse_name:
            del self.vibration_tasks[serial]
            if not pulse.done():
                pulse.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pulse
                # A rumble that took the slot during the await keeps its motor and its own stop timer.
                if self.vibration_tasks.get(serial) is None:
                    await self.backend.set_rumble(serial, 0)

    async def _charge_cue_loop(self, serial: str) -> None:
        """Grant pattern, reminders and the pre-expiry double pulse of one held charge.

        Sleeps until the next cue (seconds apart), so a held charge costs one idle task and
        nothing in the 100 Hz state loop. Ends when the charge layer ends; ``end_shield_effect``
        cancels it at once.
        """
        me = asyncio.current_task()
        try:
            cues = read_charge_cues()
            if not cues.enabled:
                return
            start = self._clock.time()
            await self._play_charge_cue(serial, CHARGE_GRANT_PULSES, cues)
            next_reminder = start + cues.reminder_s if cues.reminder_s > 0 else math.inf
            pre_expiry_pending = cues.pre_expiry_s > 0
            first_pass = True
            while (layer := self._held_charge(serial)) is not None:
                now = self._clock.time()
                end = layer.start + layer.total_s
                pre_expiry_at = end - cues.pre_expiry_s if pre_expiry_pending else math.inf
                if now >= pre_expiry_at - _CUE_EPSILON_S:
                    pre_expiry_pending = False
                    if first_pass:
                        continue  # shorter than the grant pattern plus the lead: no pre-expiry pulse
                    await self._play_charge_cue(serial, CHARGE_PRE_EXPIRY_PULSES, cues)
                    return  # the last cue: no reminder after it
                first_pass = False
                if now >= next_reminder - _CUE_EPSILON_S:
                    await self._play_charge_cue(serial, 1, cues)
                    while next_reminder <= self._clock.time() + _CUE_EPSILON_S:
                        next_reminder += cues.reminder_s
                    continue
                wake = min(pre_expiry_at, next_reminder, end)
                await self._clock.sleep(max(CHARGE_SLOT_POLL_S, wake - now))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Charge rumble pattern on {serial} stopped: {e}")
        finally:
            if self._charge_cue_tasks.get(serial) is me:
                del self._charge_cue_tasks[serial]

    async def _shield_ending_loop(self, serial: str) -> None:
        """The "ending soon" double pulse of an active (full) shield, ``lead`` s before its end (#2094)."""
        me = asyncio.current_task()
        try:
            lead = read_shield_ending_lead()
            if lead <= 0:
                return
            while (layer := self._live_layer(serial, "full")) is not None:
                now = self._clock.time()
                pulse_at = layer.start + layer.total_s - lead
                if pulse_at <= layer.start + _CUE_EPSILON_S:
                    return  # a shield no longer than the lead gets no pulse
                if now >= pulse_at - _CUE_EPSILON_S:
                    await self._play_charge_cue(
                        serial, SHIELD_ENDING_PULSES, ChargeCues(), kind="full", task_name=SHIELD_ENDING_PULSE_TASK_NAME
                    )
                    return
                await self._clock.sleep(max(CHARGE_SLOT_POLL_S, pulse_at - now))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Shield ending pulse on {serial} stopped: {e}")
        finally:
            if self._shield_ending_tasks.get(serial) is me:
                del self._shield_ending_tasks[serial]

    async def _play_charge_cue(
        self,
        serial: str,
        pulses: int,
        cues: ChargeCues,
        kind: str = "charge",
        task_name: str = CHARGE_PULSE_TASK_NAME,
    ) -> bool:
        """Play ``pulses`` charge pulses once the rumble slot is free; False if skipped.

        A busy slot (warning, death rumble, swell, agent rumble) keeps priority: the cue waits
        up to ``CHARGE_SLOT_WAIT_S`` and is skipped if the slot is still taken.
        """
        deadline = self._clock.time() + CHARGE_SLOT_WAIT_S
        while self._rumble_slot_busy(serial):
            if self._clock.time() >= deadline - _CUE_EPSILON_S:
                return False
            await self._clock.sleep(CHARGE_SLOT_POLL_S)
        if self._live_layer(serial, kind) is None:
            return False
        burst = asyncio.create_task(self._charge_pulse_loop(serial, pulses, cues), name=task_name)
        self.vibration_tasks[serial] = burst
        # wait() does not raise when a later rumble cancels the burst, and a cancel of this
        # loop leaves the burst to _stop_charge_cues, which also switches the motor off.
        await asyncio.wait({burst})
        return True

    async def _charge_pulse_loop(self, serial: str, pulses: int, cues: ChargeCues) -> None:
        me = asyncio.current_task()
        try:
            for index in range(pulses):
                if index:
                    await self._clock.sleep(CHARGE_PULSE_GAP_MS / 1000.0)
                await self.backend.set_rumble(serial, cues.intensity)
                await self._clock.sleep(cues.pulse_ms / 1000.0)
                await self.backend.set_rumble(serial, 0)
        finally:
            # Only the slot's owner clears it: a replacing rumble has already taken the slot.
            if self.vibration_tasks.get(serial) is me:
                del self.vibration_tasks[serial]

    def _death_rumble_running(self, serial: str) -> bool:
        task = self.vibration_tasks.get(serial)
        return task is not None and not task.done() and task.get_name() == DEATH_RUMBLE_TASK_NAME

    async def _effect_shield_swell(self, serial: str, **_kwargs) -> None:
        """Activation swell (#1837, UC-040 BR-021): rumble 0 -> 180 -> 0 over 600 ms, no LED.

        It owns the rumble slot (``vibration_tasks``), so a later rumble (warning, death)
        cancels it and runs on its own timer.
        """
        if self._death_rumble_running(serial):
            return  # the death rumble is never shortened (UC-040 BR-021)
        previous = self.vibration_tasks.pop(serial, None)
        if previous is not None:
            previous.cancel()
        self.vibration_tasks[serial] = asyncio.create_task(self._swell_loop(serial), name=SWELL_TASK_NAME)

    async def _swell_loop(self, serial: str) -> None:
        me = asyncio.current_task()
        try:
            start = self._clock.time()
            while (elapsed := self._clock.time() - start) < SWELL_MS / 1000.0 - 1e-9:
                await self.backend.set_rumble(serial, swell_intensity(elapsed))
                await self._clock.sleep(SWELL_STEP_S)
            await self.backend.set_rumble(serial, 0)
        finally:
            # Only the slot's owner clears it: a replacing rumble has already taken the slot.
            if self.vibration_tasks.get(serial) is me:
                del self.vibration_tasks[serial]

    # =========================================================================
    # Effect dispatch
    # =========================================================================

    # Map GameEffect enum to handler method
    _EFFECT_HANDLERS: dict = {}  # Populated in __init__ or lazily

    def _get_effect_handlers(self) -> dict:
        """Get mapping of GameEffect enum to handler methods."""
        return {
            controller_manager_pb2.GAME_EFFECT_NONE: self._effect_none,
            controller_manager_pb2.GAME_EFFECT_PLAYER_WARNING: self._effect_player_warning,
            controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH: self._effect_player_death,
            controller_manager_pb2.GAME_EFFECT_PLAYER_RESPAWN: self._effect_player_respawn,
            controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW: self._effect_winner_rainbow,
            controller_manager_pb2.GAME_EFFECT_COUNTDOWN: self._effect_countdown,
            controller_manager_pb2.GAME_EFFECT_ADMIN_ENTER: self._effect_admin_enter,
            controller_manager_pb2.GAME_EFFECT_ADMIN_EXIT: self._effect_admin_exit,
            controller_manager_pb2.GAME_EFFECT_LOW_BATTERY: self._effect_low_battery,
            controller_manager_pb2.GAME_EFFECT_FORCE_START_CHARGE: self._effect_force_start_charge,
            controller_manager_pb2.GAME_EFFECT_SHOW_BATTERY: self._effect_show_battery,
            controller_manager_pb2.GAME_EFFECT_RUMBLE: self._effect_rumble,
            controller_manager_pb2.GAME_EFFECT_PULSE: self._game_effect_pulse,
            controller_manager_pb2.GAME_EFFECT_FLASH: self._game_effect_flash,
            controller_manager_pb2.GAME_EFFECT_SHIELD_SWELL: self._effect_shield_swell,
        }

    async def handle_game_effect(
        self,
        serial: str,
        effect: int,
        _subscriber_id: str = "",
        color: tuple[int, int, int] | None = None,
        duration_ms: int = 0,
        speed: int = 0,
        trace_parent: str = "",
        trace_state: str = "",
    ) -> bool:
        """
        Handle semantic game effect with auto-restore to base color.

        Args:
            serial: Controller serial (empty = all controllers for broadcast)
            effect: GameEffect enum value
            subscriber_id: For logging
            color: Optional override color (r, g, b) for effects that support it
            duration_ms: Optional override duration (0 = use default)
            speed: Optional override speed (0 = use default)
            trace_parent: W3C traceparent for distributed tracing
            trace_state: W3C tracestate for distributed tracing

        Returns:
            True if successful, False otherwise
        """
        try:
            # Extract trace context if provided
            parent_context = None
            if trace_parent:
                parent_context = extract_trace_context(trace_parent, trace_state)

            # Determine target controllers
            if serial:
                serials = [serial] if serial in self.tracked_controllers else []
            else:
                serials = list(self.tracked_controllers.keys())

            if not serials:
                logger.warning("No controllers found for game effect")
                return False

            # The agent shield is a layer, not an active effect: route it before
            # the placeholder below, which re-types a running effect (#1301,
            # UC-040 BR-010).
            if effect in SHIELD_EFFECTS:
                for target_serial in serials:
                    await self._handle_shield_command(target_serial, effect, duration_ms, color, parent_context)
                return True

            handlers = self._get_effect_handlers()
            handler = handlers.get(effect)

            if not handler:
                effect_name = controller_manager_pb2.GameEffect.Name(effect)
                logger.warning(f"Unknown game effect: {effect_name}")
                return False

            for target_serial in serials:
                restore_color = self.base_colors.get(target_serial)
                if effect in NON_LED_EFFECTS:
                    # Rumble never touches the LED: it must not re-type, pop or
                    # orphan whatever effect is running (#1301).
                    await handler(serial=target_serial, duration_ms=duration_ms, speed=speed)
                    continue
                # Store effect_type; if there's already an ActiveEffect, update its type.
                # The handler will replace the full ActiveEffect when it creates its task.
                async with self.effect_lock:
                    if self._pulse_yields(target_serial, effect):
                        # UC-004 BR-005 / UC-015 BR-015 (#2105): a pulse never cuts short an identify
                        # or a battery display; the base colour still shows when that effect ends.
                        logger.debug(f"Pulse for {target_serial} skipped: identify / battery display running")
                        continue
                    if target_serial in self.active_effects:
                        self.active_effects[target_serial].effect_type = effect
                    else:
                        # Placeholder with no-op task; handler will replace with real task
                        done_task = asyncio.get_running_loop().create_future()
                        done_task.set_result(None)
                        self.active_effects[target_serial] = ActiveEffect(task=done_task, effect_type=effect)

                await handler(
                    serial=target_serial,
                    restore_color=restore_color,
                    color=color,
                    duration_ms=duration_ms,
                    speed=speed,
                    trace_context=parent_context,
                )

            return True

        except Exception as e:
            logger.error(f"Error handling game effect: {e}", exc_info=True)
            return False

    async def cancel_effect(self, serial: str) -> None:
        """Cancel any active effect on a controller."""
        task = None
        async with self.effect_lock:
            if serial in self.active_effects:
                task = self.active_effects[serial].task
                task.cancel()
                del self.active_effects[serial]
                self.backend.set_effect_active(serial, False)
        # Await cancelled task outside lock to avoid deadlock with task's finally block
        if task:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def cancel_if_cancellable(self, serial: str) -> bool:
        """
        Cancel effect only if it's marked as cancellable.

        Args:
            serial: Controller serial

        Returns:
            True if effect was cancelled, False otherwise
        """
        task = None
        async with self.effect_lock:
            if serial in self.active_effects:
                active = self.active_effects[serial]
                if active.effect_type in self.cancellable_effects:
                    task = active.task
                    task.cancel()
                    del self.active_effects[serial]
                    self.backend.set_effect_active(serial, False)
                    logger.debug(f"Cancelled cancellable effect for {serial}")
        # Await cancelled task outside lock to avoid deadlock with task's finally block
        if task:
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return True
        return False

    async def apply_base_color(self, serial: str, color: tuple[int, int, int], label: str = "") -> None:
        """Cancel cancellable effects and apply base color for a controller.

        Combines cancel_if_cancellable + store-and-apply-under-lock into one call.
        Used by both StreamButtonEvents and StreamGameplayData to handle base_color commands.

        Args:
            serial: Controller serial number
            color: RGB tuple (r, g, b)
            label: Stream label for log messages (e.g. "ButtonStream", "GameplayStream")
        """
        await self.cancel_if_cancellable(serial)
        async with self.effect_lock:
            self.base_colors[serial] = color
            if self.shield_active(serial):
                # Restored when the agent shield ends (#1301, UC-040 BR-010).
                logger.info(f"[{label}] Base color for {serial} stored; shown after the agent shield")
            elif serial not in self.active_effects:
                await self.set_controller_color(serial, color)
                logger.info(f"[{label}] Applied base color for {serial}: {color}")
            else:
                active = self.active_effects[serial]
                logger.warning(f"[{label}] Base color for {serial} blocked by effect: {active.effect_type}")

    def clear_controller(self, serial: str) -> None:
        """Clear feedback state for a disconnected controller."""
        # Note: Keep base_colors[serial] so we can restore on reconnect
        # Cancel the task before removing to avoid orphaned coroutines
        if serial in self.active_effects:
            self.active_effects[serial].task.cancel()
            del self.active_effects[serial]
        # A stale stop timer must not cut the rumble of a reconnected controller (#1832: 1 s window)
        vibration_task = self.vibration_tasks.pop(serial, None)
        if vibration_task is not None:
            vibration_task.cancel()
        self._shield_layers.pop(serial, None)
        shield_task = self._shield_tasks.pop(serial, None)
        if shield_task is not None:
            shield_task.cancel()
        for cue_tasks in (self._charge_cue_tasks, self._shield_ending_tasks):
            cue_task = cue_tasks.pop(serial, None)
            if cue_task is not None:
                cue_task.cancel()
        self.current_colors.pop(serial, None)
        self._identify_fallback.pop(serial, None)
        self._color_gauges.pop(serial, None)

    def _get_battery_color(self, battery_percent: int) -> tuple[int, int, int]:
        """
        Get LED color for battery level.

        Color scheme matches original JoustMania:
        - 100%: Green
        - 80%: Turquoise
        - 60%: Blue
        - 40%: Yellow
        - <40%: Red

        Args:
            battery_percent: Battery level as percentage (0-100)

        Returns:
            RGB tuple for battery indicator color
        """
        if battery_percent >= 100:
            return Colors.Green.value  # Green - full
        if battery_percent >= 80:
            return Colors.Turquoise.value  # Turquoise - 80%
        if battery_percent >= 60:
            return Colors.Blue.value  # Blue - 60%
        if battery_percent >= 40:
            return Colors.Yellow.value  # Yellow - 40%
        return Colors.Red.value  # Red - low battery

    async def _countdown_sequence(
        self,
        serial: str,
        restore_color: tuple[int, int, int] | None,
        trace_context: Context | None = None,
        phase_duration_ms: int = 750,
    ) -> None:
        """
        Run the full countdown sequence on a single controller.

        Red → Yellow → Green → restore to base color

        Args:
            serial: Controller serial
            restore_color: Color to restore to after sequence (base color)
            trace_context: Optional trace context for distributed tracing
            phase_duration_ms: Duration of each phase in milliseconds (default 750ms)
        """
        phase_duration_sec = phase_duration_ms / 1000.0

        with tracer.start_as_current_span("effect_countdown", context=trace_context) as span:
            span.set_attribute(CONTROLLER_SERIAL_ATTR, serial)

            try:
                # Mark effect as active (polling skips LED refresh)
                self.backend.set_effect_active(serial, True)

                # Red - "3"
                span.add_event("countdown_red", {"phase": 3, "duration_ms": phase_duration_ms})
                await self.set_controller_color(serial, Colors.Red.value)
                await asyncio.sleep(phase_duration_sec)

                # Yellow - "2"
                span.add_event("countdown_yellow", {"phase": 2, "duration_ms": phase_duration_ms})
                await self.set_controller_color(serial, Colors.Yellow.value)
                await asyncio.sleep(phase_duration_sec)

                # Green - "1" / GO!
                span.add_event("countdown_green", {"phase": 1, "duration_ms": phase_duration_ms})
                await self.set_controller_color(serial, Colors.Green.value)
                await asyncio.sleep(phase_duration_sec)

                span.add_event("countdown_complete")

            except asyncio.CancelledError:
                span.add_event("countdown_cancelled")
                raise
            finally:
                # Remove from active_effects so new base colors can be applied immediately,
                # unless another effect already owns the LED (#1430).
                if await self._release_effect(serial):
                    # Restore to base color (or hand back to an agent shield, #1301)
                    await self._finish_effect(serial, bool(restore_color))
