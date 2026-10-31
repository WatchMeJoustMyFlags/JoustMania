"""
Agent shield effects of the FeedbackManager (#1301, #1837, #2094, #2096; UC-040 A16 / A17, BR-008..BR-026).

``ShieldEffectsMixin`` holds the shield LED layers, the charge / hold / ending rumble cues and the
activation swell. It was moved out of ``feedback_manager.py`` unchanged (#2354); the state it uses
(``_shield_layers`` ...) is created in ``FeedbackManager.__init__``.
"""

import asyncio
import contextlib
import logging
import math
from dataclasses import dataclass

from opentelemetry.context import Context

from lib.colors import Colors
from lib.telemetry import get_tracer
from proto import controller_manager_pb2
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
    DEFAULT_EMPTY_PULSE_MS,
    DEFAULT_HOLD_RUMBLE_INTENSITY,
    DEFAULT_SHIELD_ENDING_LEAD_S,
    HOLD_COLOR,
    SHIELD_BLINK_WINDOW_S,
    SHIELD_ENDING_PULSES,
    SWELL_MS,
    SWELL_STEP_S,
    ChargeCues,
    HoldCues,
    HoldEnergy,
    charge_frame,
    hold_active_frame,
    hold_energy_frame,
    hold_energy_state,
    resolve_charge_cues,
    resolve_empty_pulse_ms,
    resolve_hold_rumble_intensity,
    resolve_shield_ending_lead,
    shield_frame,
    swell_intensity,
)

logger = logging.getLogger(__name__)

# Lazy telemetry initialization - defers OTLP setup until first span
tracer = get_tracer(__name__)

# Span attribute keys (S1192 - avoid duplicate string literals)
CONTROLLER_SERIAL_ATTR = "controller.serial"


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


def read_hold_cues() -> HoldCues:
    """Fallback cue values of a hold layer when a command carries none (UC-040 BR-026, #2096).

    The coordinator ships the values its game latched (``ShieldCueParams``); this read, with
    no game context, only fills fields a command leaves out.
    """
    from lib.feature_flags import read_int_flag

    return HoldCues(
        pulses=read_charge_cues(),
        ending_lead_s=read_shield_ending_lead(),
        hold_rumble=resolve_hold_rumble_intensity(
            read_int_flag("game", "shield.hold_rumble_intensity", DEFAULT_HOLD_RUMBLE_INTENSITY)
        ),
        empty_pulse_ms=resolve_empty_pulse_ms(read_int_flag("game", "shield.empty_pulse_ms", DEFAULT_EMPTY_PULSE_MS)),
    )


_HOLD_CUE_FIELDS = ("rumble_intensity", "pulse_ms", "ending_lead_ms", "hold_rumble_intensity", "empty_pulse_ms")


def hold_cues_from(state) -> HoldCues:
    """The cue values of a ``ShieldHoldState``: the shipped ones, the flag read for any field left out."""
    params = state.cues if state is not None and state.HasField("cues") else None
    shipped = {f: getattr(params, f) for f in _HOLD_CUE_FIELDS if params is not None and params.HasField(f)}
    # Read the flags only when a field is missing; a fully shipped command reads nothing.
    fallback = HoldCues() if len(shipped) == len(_HOLD_CUE_FIELDS) else read_hold_cues()
    return HoldCues(
        pulses=resolve_charge_cues(
            shipped.get("rumble_intensity", fallback.pulses.intensity),
            shipped.get("pulse_ms", fallback.pulses.pulse_ms),
            0,
            0,
        ),
        ending_lead_s=resolve_shield_ending_lead(shipped.get("ending_lead_ms", fallback.ending_lead_s * 1000) / 1000.0),
        hold_rumble=resolve_hold_rumble_intensity(shipped.get("hold_rumble_intensity", fallback.hold_rumble)),
        empty_pulse_ms=resolve_empty_pulse_ms(shipped.get("empty_pulse_ms", fallback.empty_pulse_ms)),
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
    kind: str = "full"  # "full" | "toughness_boost" | "charge" (#1837) | "hold" | "energy" (#2096)
    blink_reported: bool = False
    cap_s: float = 0.0  # hold: the energy cap, for the brightness by energy left

    @property
    def toughness_boost(self) -> bool:
        return self.kind == "toughness_boost"

    def remaining(self, now: float) -> float:
        return self.start + self.total_s - now


_SHIELD_KIND_BY_EFFECT = {
    controller_manager_pb2.GAME_EFFECT_SHIELD: "full",
    controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST: "toughness_boost",
    controller_manager_pb2.GAME_EFFECT_SHIELD_CHARGE: "charge",
    controller_manager_pb2.GAME_EFFECT_SHIELD_HOLD: "hold",
    controller_manager_pb2.GAME_EFFECT_SHIELD_ENERGY: "energy",
}
# Paint order of the shield layers: the full shield over the hold activation (#2096) over the
# toughness boost over a charge over the idle hold energy.
_SHIELD_LAYER_ORDER = ("full", "hold", "toughness_boost", "charge", "energy")
# Layers rendered at the charge rate (10 Hz, only on a visible change or a 1 s keepalive).
_SLOW_LAYERS = frozenset({"charge", "energy"})
LED_KEEPALIVE_S = 1.0
# The activation swell lives in the rumble slot (vibration_tasks), so any later rumble replaces it (#1837).
SWELL_TASK_NAME = "shield_swell"
# The death rumble owns the slot for its full duration; the swell and the charge pulses never replace it.
DEATH_RUMBLE_TASK_NAME = "death_rumble"
# A charge pulse burst (#2094) plays only in a free rumble slot; any later rumble replaces it.
CHARGE_PULSE_TASK_NAME = "charge_pulse"
# The "ending soon" double pulse of an active shield (#2094), same slot rules.
SHIELD_ENDING_PULSE_TASK_NAME = "shield_ending_pulse"
# Every pulse of the hold feedback (#2096): ending soon, empty, full, top-up; same slot rules.
HOLD_PULSE_TASK_NAME = "hold_pulse"
_SHIELD_PULSE_TASK_NAMES = (CHARGE_PULSE_TASK_NAME, SHIELD_ENDING_PULSE_TASK_NAME, HOLD_PULSE_TASK_NAME)
_CUE_EPSILON_S = 1e-6


class ShieldEffectsMixin:
    """Shield layers, cues and swell of the FeedbackManager (state is set up in its __init__)."""

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
        for kind in _SHIELD_LAYER_ORDER:
            if kind in layers:
                return layers[kind]
        return None

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
        self._ensure_shield_renderer(serial, kind, trace_context, duration_ms)
        if newly_charged:
            # The charge rumble pattern starts with the layer, not with a re-send (UC-040 BR-021, #2094).
            await self._stop_charge_cues(serial)
            self._charge_cue_tasks[serial] = asyncio.create_task(self._charge_cue_loop(serial))
        elif kind == "full":
            # Each (re)start of the protection moves the "ending soon" pulse to the new end (#2094).
            await self._stop_shield_pulses(serial, self._shield_ending_tasks, SHIELD_ENDING_PULSE_TASK_NAME)
            self._shield_ending_tasks[serial] = asyncio.create_task(self._shield_ending_loop(serial))

    def _ensure_shield_renderer(
        self, serial: str, kind: str, trace_context: Context | None, duration_ms: int | float = 0
    ) -> None:
        """Start the one renderer task of the controller, unless it runs (it picks new layers up)."""
        task = self._shield_tasks.get(serial)
        if task is None or task.done():
            self.backend.set_effect_active(serial, True)
            self._shield_tasks[serial] = asyncio.create_task(self._shield_render_loop(serial, kind, trace_context))
        else:
            logger.debug(f"Shield layer {kind} (re)started on {serial} for {duration_ms}ms")

    async def _shield_render_loop(self, serial: str, kind: str, trace_context: Context | None) -> None:
        """Paint the top shield layer at 20 Hz (a charge at 10 Hz, on change) while no overlay effect owns the LED."""
        interval = 0.05
        with tracer.start_as_current_span("controller.shield_effect", context=trace_context) as span:
            span.set_attribute(CONTROLLER_SERIAL_ATTR, serial)
            span.set_attribute("shield.kind", kind)
            first = self._shield_layers.get(serial, {}).get(kind)
            if first is not None and math.isfinite(first.total_s):
                span.set_attribute("effect.duration_ms", int(first.total_s * 1000))
            end_reason = "cancelled"
            last_write = -math.inf
            try:
                while True:
                    now = self._clock.time()
                    layer = self._top_shield(serial, now)
                    if serial in self._background_rumble and "hold" not in self._shield_layers.get(serial, {}):
                        # The activation ran out without its end command: its rumble stops too (#2096).
                        await self._set_background_rumble(serial, 0)
                    if layer is None:
                        end_reason = "expired"
                        break
                    elapsed = now - layer.start
                    slow = layer.kind in _SLOW_LAYERS
                    if (
                        not slow
                        and not layer.blink_reported
                        and elapsed >= max(0.0, layer.total_s - SHIELD_BLINK_WINDOW_S)
                    ):
                        layer.blink_reported = True
                        span.add_event("shield_blink", {"shield.kind": layer.kind})
                    if not self._overlay_active(serial):
                        frame = self._shield_layer_frame(serial, layer, now, elapsed)
                        if (
                            slow
                            and frame is not None
                            and frame == self.current_colors.get(serial)
                            and now - last_write < LED_KEEPALIVE_S
                        ):
                            frame = None  # no visible change: skip the HID write (a 1 s keepalive stays)
                        if frame is not None:
                            await self._set_led_color(serial, frame)
                            last_write = now
                    await self._clock.sleep(CHARGE_RENDER_INTERVAL_S if slow else interval)
            finally:
                span.add_event("shield_end", {"shield.end_reason": end_reason})
                if self._shield_tasks.get(serial) is asyncio.current_task():
                    self._shield_tasks.pop(serial, None)
        # Expired naturally: hand the LED back, unless an overlay owns it (its
        # own finish restores the base colour then).
        self._shield_layers.pop(serial, None)
        if not self._overlay_active(serial):
            await self._finish_effect(serial, restore=True)

    def _shield_layer_frame(
        self, serial: str, layer: ShieldLayer, now: float, elapsed: float
    ) -> tuple[int, int, int] | None:
        """The LED frame of the top shield layer now; None paints nothing this tick."""
        # The player's current base colour, so a team/colour change is followed.
        base = self.base_colors.get(serial) or layer.color
        if layer.kind == "charge":
            return charge_frame(elapsed, base)
        if layer.kind == "energy":
            energy = self._hold_energy.get(serial)
            if energy is None:
                return None
            level = energy.level(now)
            state = hold_energy_state(level, energy.cap_s, energy.full_s, energy.min_burst_s)
            return hold_energy_frame(elapsed, state, base)
        if layer.kind == "hold":
            return hold_active_frame(layer.remaining(now), layer.cap_s)
        return shield_frame(elapsed, layer.total_s, layer.color, layer.toughness_boost)

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
            if "hold" in kinds:
                await self._end_hold_activation(serial)
            if "energy" in kinds:
                await self._stop_hold_cues(serial)
                self._hold_energy.pop(serial, None)
            if layers:
                return True  # another shield layer keeps painting
        self._shield_layers.pop(serial, None)
        await self._stop_charge_cues(serial)
        await self._stop_shield_pulses(serial, self._shield_ending_tasks, SHIELD_ENDING_PULSE_TASK_NAME)
        await self._end_hold_activation(serial)
        await self._stop_hold_cues(serial)
        self._hold_energy.pop(serial, None)
        self._hold_cues.pop(serial, None)
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
        self,
        serial: str,
        effect: int,
        duration_ms: int,
        color: tuple | None,
        trace_context: Context | None,
        shield_hold=None,
    ) -> None:
        kind = _SHIELD_KIND_BY_EFFECT[effect]
        if duration_ms > 0 and kind == "hold":
            await self._start_hold_layer(serial, duration_ms, shield_hold, trace_context)
            return
        if duration_ms > 0 and kind == "energy":
            await self._start_energy_layer(serial, shield_hold, trace_context)
            return
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
    # Shield hold (#2096, UC-040 BR-026)
    # =========================================================================

    async def _motor_off(self, serial: str) -> None:
        """End a one-shot rumble: back to the background level of a running hold, else off."""
        await self.backend.set_rumble(serial, self._background_rumble.get(serial, 0))

    async def _set_background_rumble(self, serial: str, level: int) -> None:
        """Set (or clear, 0) the background rumble; a running one-shot keeps the motor until it ends."""
        if level > 0:
            self._background_rumble[serial] = level
        elif self._background_rumble.pop(serial, None) is None:
            return
        if not self._rumble_slot_busy(serial):
            await self.backend.set_rumble(serial, level)

    async def _start_hold_layer(self, serial: str, duration_ms: int, state, trace_context: Context | None) -> None:
        """Start or restart the hold activation layer: white by the energy left, background rumble, ending pulse.

        A restart (a grant during the hold) moves the end and the "ending soon" pulse and
        plays the grant's cue; the background rumble starts only with a new activation.
        """
        now = self._clock.time()
        cues = hold_cues_from(state)
        self._hold_cues[serial] = cues
        layers = self._shield_layers.setdefault(serial, {})
        running = layers.get("hold")
        newly = running is None or running.remaining(now) <= 0
        cap_s = state.cap_ms / 1000.0 if state is not None and state.cap_ms > 0 else duration_ms / 1000.0
        layers["hold"] = ShieldLayer(
            start=now, total_s=duration_ms / 1000.0, color=HOLD_COLOR, kind="hold", cap_s=cap_s
        )
        self._ensure_shield_renderer(serial, "hold", trace_context, duration_ms)
        if newly:
            # The idle pool does not recharge during an activation: its cues wait for the next SHIELD_ENERGY.
            await self._stop_hold_cues(serial)
            await self._set_background_rumble(serial, cues.hold_rumble)
        await self._stop_shield_pulses(serial, self._hold_ending_tasks, HOLD_PULSE_TASK_NAME)
        self._hold_ending_tasks[serial] = asyncio.create_task(self._hold_ending_loop(serial))
        cue = state.cue if state is not None else controller_manager_pb2.SHIELD_HOLD_CUE_NONE
        if cue in (controller_manager_pb2.SHIELD_HOLD_CUE_FULL, controller_manager_pb2.SHIELD_HOLD_CUE_TOP_UP):
            await self._stop_hold_cues(serial)
            self._hold_cue_tasks[serial] = asyncio.create_task(self._hold_cue_loop(serial, cue, "hold"))

    async def _start_energy_layer(self, serial: str, state, trace_context: Context | None) -> None:
        """Show (or update) the idle energy layer and play its cue, then the recharge-to-full pattern."""
        now = self._clock.time()
        self._hold_cues[serial] = hold_cues_from(state)
        if state is not None:
            self._hold_energy[serial] = HoldEnergy(
                energy_s=max(0, state.energy_ms) / 1000.0,
                cap_s=max(0, state.cap_ms) / 1000.0,
                full_s=max(0, state.full_ms) / 1000.0,
                min_burst_s=max(0, state.min_burst_ms) / 1000.0,
                rate_per_s=max(0, state.rate_ms_per_s) / 1000.0,
                start=now,
            )
        else:
            self._hold_energy.setdefault(serial, HoldEnergy(0.0, 0.0, 0.0, 0.0, 0.0, now))
        layers = self._shield_layers.setdefault(serial, {})
        if "energy" not in layers:
            color = self.base_colors.get(serial) or self.current_colors.get(serial) or HOLD_COLOR
            layers["energy"] = ShieldLayer(start=now, total_s=math.inf, color=color, kind="energy")
        self._ensure_shield_renderer(serial, "energy", trace_context)
        await self._stop_hold_cues(serial)
        cue = state.cue if state is not None else controller_manager_pb2.SHIELD_HOLD_CUE_NONE
        self._hold_cue_tasks[serial] = asyncio.create_task(self._hold_cue_loop(serial, cue, "energy"))

    async def _end_hold_activation(self, serial: str) -> None:
        """The activation layer ended: its ending pulse and its background rumble stop."""
        await self._stop_shield_pulses(serial, self._hold_ending_tasks, HOLD_PULSE_TASK_NAME)
        await self._set_background_rumble(serial, 0)

    async def _stop_hold_cues(self, serial: str) -> None:
        await self._stop_shield_pulses(serial, self._hold_cue_tasks, HOLD_PULSE_TASK_NAME)

    async def _hold_cue_loop(self, serial: str, cue: int, kind: str) -> None:
        """Play the command's cue, then, on the energy layer, the grant pattern when the recharge fills the pool.

        Full: the #2094 grant pattern; top-up: one pulse; empty: one pulse of ``empty_pulse_ms``.
        The recharge-to-full pattern plays once per fill, detected from the level and rate the
        coordinator sent, so a recharge needs no command (UC-040 A38 step 3).
        """
        me = asyncio.current_task()
        try:
            cues = self._hold_cues.get(serial, HoldCues())
            if cues.pulses.enabled:
                if cue == controller_manager_pb2.SHIELD_HOLD_CUE_FULL:
                    await self._play_charge_cue(
                        serial, CHARGE_GRANT_PULSES, cues.pulses, kind=kind, task_name=HOLD_PULSE_TASK_NAME
                    )
                elif cue == controller_manager_pb2.SHIELD_HOLD_CUE_TOP_UP:
                    await self._play_charge_cue(serial, 1, cues.pulses, kind=kind, task_name=HOLD_PULSE_TASK_NAME)
                elif cue == controller_manager_pb2.SHIELD_HOLD_CUE_EMPTY:
                    await self._play_charge_cue(serial, 1, cues.empty_pulse, kind=kind, task_name=HOLD_PULSE_TASK_NAME)
            energy = self._hold_energy.get(serial)
            full_at = energy.full_at() if kind == "energy" and energy is not None else None
            if full_at is None or not cues.pulses.enabled:
                return
            while (now := self._clock.time()) < full_at - _CUE_EPSILON_S:
                await self._clock.sleep(max(CHARGE_SLOT_POLL_S, full_at - now))
            if self._hold_energy.get(serial) is energy and "hold" not in self._shield_layers.get(serial, {}):
                await self._play_charge_cue(
                    serial, CHARGE_GRANT_PULSES, cues.pulses, kind="energy", task_name=HOLD_PULSE_TASK_NAME
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Hold cue on {serial} stopped: {e}")
        finally:
            if self._hold_cue_tasks.get(serial) is me:
                del self._hold_cue_tasks[serial]

    async def _hold_ending_loop(self, serial: str) -> None:
        """The "ending soon" double pulse ``ending_lead_s`` before a held activation runs out of energy."""
        me = asyncio.current_task()
        try:
            cues = self._hold_cues.get(serial, HoldCues())
            lead = cues.ending_lead_s
            if lead <= 0 or not cues.pulses.enabled:
                return
            while (layer := self._live_layer(serial, "hold")) is not None:
                now = self._clock.time()
                pulse_at = layer.start + layer.total_s - lead
                if pulse_at <= layer.start + _CUE_EPSILON_S:
                    return  # an activation no longer than the lead gets no pulse
                if now >= pulse_at - _CUE_EPSILON_S:
                    await self._play_charge_cue(
                        serial, SHIELD_ENDING_PULSES, cues.pulses, kind="hold", task_name=HOLD_PULSE_TASK_NAME
                    )
                    return
                await self._clock.sleep(max(CHARGE_SLOT_POLL_S, pulse_at - now))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Hold ending pulse on {serial} stopped: {e}")
        finally:
            if self._hold_ending_tasks.get(serial) is me:
                del self._hold_ending_tasks[serial]

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
                    await self._motor_off(serial)

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
                await self._motor_off(serial)
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
            await self._motor_off(serial)
        finally:
            # Only the slot's owner clears it: a replacing rumble has already taken the slot.
            if self.vibration_tasks.get(serial) is me:
                del self.vibration_tasks[serial]
