"""
Base class for controller effects - shared by real and mock controller managers.

This class contains all the animation logic for controller LED effects (FLASH, PULSE,
RAINBOW, FADE_OUT, FADE_IN). The only hardware-specific method is _set_led_color(),
which must be implemented by subclasses.

Phase 40: Refactoring to eliminate code duplication between real and mock servers.
"""

import asyncio
import colorsys
import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from lib.clock import Clock, RealClock

logger = logging.getLogger(__name__)


@dataclass
class ActiveEffect:
    """Tracks an active effect task and its type for a single controller."""

    task: asyncio.Task
    effect_type: int  # GameEffect enum value


# Agent shield LED schedule (#1301, UC-040 BR-008 / BR-009). Pure function of the
# elapsed time so it is testable without a clock and identical on real and mock
# controllers.
SHIELD_BLINK_WINDOW_S = 1.0  # last second blinks
SHIELD_BLINK_HZ = 4.0
SHIELD_FADE_FLOOR = 0.30  # full shield fades 100% -> 30% before the blink window
TOUGHNESS_BOOST_BREATH_HZ = 0.5
TOUGHNESS_BOOST_MIN = 0.10
TOUGHNESS_BOOST_MAX = 0.50

# Shield charge (#1837, UC-040 BR-021): the held, not yet activated shield breathes the
# player's colour; the activation swell is a rumble ramp. Pure functions, like shield_frame.
CHARGE_BREATH_HZ = 0.5
CHARGE_MIN = 0.60
CHARGE_MAX = 1.00
CHARGE_MAX_DURATION_MS = 120_000  # the game re-sends while the charge is held
CHARGE_RENDER_INTERVAL_S = 0.1  # 10 Hz is plenty for a 0.5 Hz breath (battery)
SWELL_MS = 600
SWELL_PEAK = 180
SWELL_STEP_S = 0.05

# Charge rumble pattern (#2094, UC-040 BR-021): a moving player must feel the charge without
# looking at the controller. Defaults of the four ``game`` flags (flags.md §4), read per charge.
CHARGE_GRANT_PULSES = 3
CHARGE_PRE_EXPIRY_PULSES = 2
CHARGE_PULSE_GAP_MS = 100
DEFAULT_CHARGE_RUMBLE_INTENSITY = 255
DEFAULT_CHARGE_RUMBLE_PULSE_MS = 150
DEFAULT_CHARGE_REMINDER_S = 5.0
DEFAULT_CHARGE_PRE_EXPIRY_S = 3.0
CHARGE_PULSE_MS_RANGE = (50, 500)
CHARGE_REMINDER_S_RANGE = (1.0, 30.0)  # 0 stays 0 (off)
# Below the coordinator's 10 s re-send margin (SHIELD_CHARGE_RESEND_MARGIN_SECONDS), so a
# layer that is re-sent (expiry never, or more than 120 s ahead) never gets an early pulse.
CHARGE_PRE_EXPIRY_MAX_S = 8.0
# Shield ending soon (#2094, UC-040 BR-021): a double pulse this long before an active
# (full) shield layer ends; ``shield.ending_rumble_seconds``, read per layer start.
DEFAULT_SHIELD_ENDING_LEAD_S = 1.0
SHIELD_ENDING_LEAD_MAX_S = 3.0
SHIELD_ENDING_PULSES = 2
CHARGE_SLOT_WAIT_S = 1.0  # a cue waits this long for a busy rumble slot, then is skipped
CHARGE_SLOT_POLL_S = 0.05

# Shield hold (#2096, UC-040 BR-026): the idle energy layer shows four states in the
# player's colour, the running activation is white by the energy left. Pure functions.
HOLD_STATE_EMPTY = "empty"
HOLD_STATE_LOW = "low"
HOLD_STATE_CHARGED = "charged"
HOLD_STATE_FULL = "full"
HOLD_LOW_FRACTION = 1.0 / 3.0  # below a third of the cap is "low"
HOLD_FULL_EPSILON_S = 0.05  # the coordinator's pool-full room (SHIELD_HOLD_POOL_FULL_ROOM_SECONDS)
HOLD_LOW_MIN = 0.20
HOLD_LOW_HZ = 1.0
HOLD_FULL_WHITE_BLEND = 0.50
HOLD_FULL_HZ = 0.5
HOLD_ACTIVE_FLOOR = 0.30
HOLD_COLOR = (255, 255, 255)  # protected, as every shield (BR-008)
DEFAULT_HOLD_RUMBLE_INTENSITY = 70
HOLD_RUMBLE_MAX = 150
DEFAULT_EMPTY_PULSE_MS = 400
EMPTY_PULSE_MS_RANGE = (100, 800)


@dataclass(frozen=True)
class ChargeCues:
    """The charge rumble pattern of one charge, fixed when its layer starts (UC-040 BR-021)."""

    intensity: int = DEFAULT_CHARGE_RUMBLE_INTENSITY
    pulse_ms: int = DEFAULT_CHARGE_RUMBLE_PULSE_MS
    reminder_s: float = DEFAULT_CHARGE_REMINDER_S
    pre_expiry_s: float = DEFAULT_CHARGE_PRE_EXPIRY_S

    @property
    def enabled(self) -> bool:
        return self.intensity > 0

    def burst_s(self, pulses: int) -> float:
        """Length of ``pulses`` pulses with their gaps."""
        return (pulses * self.pulse_ms + (pulses - 1) * CHARGE_PULSE_GAP_MS) / 1000.0


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def resolve_charge_cues(intensity: object, pulse_ms: object, reminder_s: object, pre_expiry_s: object) -> ChargeCues:
    """Validate the four charge rumble flag values (flags.md §4); a bad value gives its default."""
    level = _finite_number(intensity)
    level = DEFAULT_CHARGE_RUMBLE_INTENSITY if level is None or level < 0 else min(255.0, level)
    pulse = _finite_number(pulse_ms)
    pulse = DEFAULT_CHARGE_RUMBLE_PULSE_MS if pulse is None or pulse <= 0 else pulse
    pulse = max(CHARGE_PULSE_MS_RANGE[0], min(CHARGE_PULSE_MS_RANGE[1], pulse))
    reminder = _finite_number(reminder_s)
    if reminder is None or reminder < 0:
        reminder = DEFAULT_CHARGE_REMINDER_S
    elif reminder > 0:
        reminder = max(CHARGE_REMINDER_S_RANGE[0], min(CHARGE_REMINDER_S_RANGE[1], reminder))
    lead = _finite_number(pre_expiry_s)
    lead = DEFAULT_CHARGE_PRE_EXPIRY_S if lead is None or lead < 0 else min(CHARGE_PRE_EXPIRY_MAX_S, lead)
    return ChargeCues(intensity=int(level), pulse_ms=int(pulse), reminder_s=reminder, pre_expiry_s=lead)


def _scale(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(int(round(c * factor)) for c in color)  # type: ignore[return-value]


@dataclass(frozen=True)
class HoldCues:
    """The cue values of a hold game, as the coordinator latched them (UC-040 BR-026)."""

    pulses: ChargeCues = ChargeCues()
    ending_lead_s: float = DEFAULT_SHIELD_ENDING_LEAD_S
    hold_rumble: int = DEFAULT_HOLD_RUMBLE_INTENSITY
    empty_pulse_ms: int = DEFAULT_EMPTY_PULSE_MS

    @property
    def empty_pulse(self) -> ChargeCues:
        """One pulse of ``empty_pulse_ms`` at the cue intensity."""
        return ChargeCues(intensity=self.pulses.intensity, pulse_ms=self.empty_pulse_ms)


def resolve_hold_rumble_intensity(value: object) -> int:
    """Validate ``shield.hold_rumble_intensity`` (flags.md §4): bad -> 70, clamped to 0..150."""
    level = _finite_number(value)
    if level is None or level < 0:
        return DEFAULT_HOLD_RUMBLE_INTENSITY
    return int(min(HOLD_RUMBLE_MAX, level))


def resolve_empty_pulse_ms(value: object) -> int:
    """Validate ``shield.empty_pulse_ms`` (flags.md §4): bad -> 400, clamped to 100..800."""
    pulse = _finite_number(value)
    if pulse is None or pulse <= 0:
        return DEFAULT_EMPTY_PULSE_MS
    return int(max(EMPTY_PULSE_MS_RANGE[0], min(EMPTY_PULSE_MS_RANGE[1], pulse)))


@dataclass(frozen=True)
class HoldEnergy:
    """A player's idle shield energy as last sent by the coordinator (UC-040 BR-024, BR-026).

    ``start`` is the controller clock at receipt; the level recharges at ``rate_per_s``
    up to ``full_s`` (``min(cap, budget remaining)``), so it is a pure function of time.
    """

    energy_s: float
    cap_s: float
    full_s: float
    min_burst_s: float
    rate_per_s: float
    start: float

    def level(self, now: float) -> float:
        energy = self.energy_s
        if self.rate_per_s > 0 and energy < self.full_s:
            energy = min(self.full_s, energy + self.rate_per_s * max(0.0, now - self.start))
        return max(0.0, energy)

    def is_full(self, energy: float) -> bool:
        return energy >= self.full_s - HOLD_FULL_EPSILON_S and energy >= self.min_burst_s

    def full_at(self) -> float | None:
        """Controller time the recharge fills the pool; None when it never does or is full already."""
        if self.rate_per_s <= 0 or self.is_full(self.energy_s) or self.full_s < self.min_burst_s:
            return None
        return self.start + max(0.0, self.full_s - HOLD_FULL_EPSILON_S - self.energy_s) / self.rate_per_s


def hold_energy_state(energy_s: float, cap_s: float, full_s: float, min_burst_s: float) -> str:
    """The idle energy state (UC-040 BR-026): empty, low, charged or full."""
    if energy_s < min_burst_s - 1e-9:
        return HOLD_STATE_EMPTY  # cannot activate (also once the budget is used up)
    if energy_s >= full_s - HOLD_FULL_EPSILON_S:
        return HOLD_STATE_FULL
    if energy_s < cap_s * HOLD_LOW_FRACTION:
        return HOLD_STATE_LOW
    return HOLD_STATE_CHARGED


def hold_energy_frame(elapsed_s: float, state: str, color: tuple[int, int, int]) -> tuple[int, int, int]:
    """LED colour of the idle energy layer in ``state`` (UC-040 BR-026); never red, off or plain white.

    - empty: the base colour, steady (as in a game without a shield);
    - low: the base colour breathing 20 % to 100 % at 1 Hz;
    - charged: the base colour breathing 60 % to 100 % at 0.5 Hz (the charge look, BR-021);
    - full: breathing between the base colour and a 50 % white blend at 0.5 Hz.
    """
    elapsed_s = max(0.0, elapsed_s)
    if state == HOLD_STATE_LOW:
        wave = (1 - math.cos(2 * math.pi * HOLD_LOW_HZ * elapsed_s)) / 2
        return _scale(color, 1.0 - (1.0 - HOLD_LOW_MIN) * wave)
    if state == HOLD_STATE_CHARGED:
        return charge_frame(elapsed_s, color)
    if state == HOLD_STATE_FULL:
        wave = (1 - math.cos(2 * math.pi * HOLD_FULL_HZ * elapsed_s)) / 2
        blend = HOLD_FULL_WHITE_BLEND * wave
        return tuple(int(round(c + (255 - c) * blend)) for c in color)  # type: ignore[return-value]
    return tuple(color)  # type: ignore[return-value]


def hold_active_frame(remaining_s: float, cap_s: float) -> tuple[int, int, int] | None:
    """LED colour of a running hold activation with ``remaining_s`` of energy left (UC-040 BR-026).

    White at 30 % + 70 % x (energy left / cap); the last second blinks at 4 Hz like a
    shield (BR-008). None once the energy is gone: the layer below shows.
    """
    if remaining_s <= 0:
        return None
    if remaining_s <= SHIELD_BLINK_WINDOW_S:
        phase = ((SHIELD_BLINK_WINDOW_S - remaining_s) * SHIELD_BLINK_HZ) % 1.0
        return (0, 0, 0) if phase >= 0.5 else HOLD_COLOR
    fill = min(1.0, remaining_s / cap_s) if cap_s > 0 else 1.0
    return _scale(HOLD_COLOR, HOLD_ACTIVE_FLOOR + (1.0 - HOLD_ACTIVE_FLOOR) * fill)


def shield_frame(
    elapsed_s: float, total_s: float, color: tuple[int, int, int], toughness_boost: bool = False
) -> tuple[int, int, int] | None:
    """LED colour of a shield effect ``elapsed_s`` into a ``total_s`` shield.

    Returns None once the shield is over (``elapsed_s >= total_s``): the caller
    restores the player's base colour.

    - Full shield (BR-008): brightness falls linearly 100% -> 30% over the first
      ``total_s - 1`` seconds, then the last second blinks at 4 Hz between full
      colour and off. A shield of 1 s or less blinks for its whole duration.
    - Toughness boost (BR-009): breathes at 0.5 Hz between 10% and 50% until the
      last second, which blinks at 4 Hz between 50% and off.
    """
    if elapsed_s >= total_s:
        return None
    elapsed_s = max(0.0, elapsed_s)
    blink_start = max(0.0, total_s - SHIELD_BLINK_WINDOW_S)
    if elapsed_s >= blink_start:
        # On for the first half of each blink period, off for the second half.
        phase = ((elapsed_s - blink_start) * SHIELD_BLINK_HZ) % 1.0
        if phase >= 0.5:
            return (0, 0, 0)
        return _scale(color, TOUGHNESS_BOOST_MAX if toughness_boost else 1.0)
    if toughness_boost:
        # Sine breathing that starts at the bottom of the range: 10% -> 50% -> 10%.
        wave = (1 - math.cos(2 * math.pi * TOUGHNESS_BOOST_BREATH_HZ * elapsed_s)) / 2
        return _scale(color, TOUGHNESS_BOOST_MIN + (TOUGHNESS_BOOST_MAX - TOUGHNESS_BOOST_MIN) * wave)
    fraction = elapsed_s / blink_start  # blink_start > 0 here
    return _scale(color, 1.0 - (1.0 - SHIELD_FADE_FLOOR) * fraction)


def charge_frame(elapsed_s: float, color: tuple[int, int, int]) -> tuple[int, int, int]:
    """LED colour of a shield charge ``elapsed_s`` after it started (UC-040 BR-021).

    Breathes the colour between 60% and 100% at 0.5 Hz (a 2 s period), starting at 60%.
    """
    wave = (1 - math.cos(2 * math.pi * CHARGE_BREATH_HZ * max(0.0, elapsed_s))) / 2
    return _scale(color, CHARGE_MIN + (CHARGE_MAX - CHARGE_MIN) * wave)


def swell_intensity(elapsed_s: float) -> int:
    """Rumble of the activation swell: 0 -> 180 -> 0 over 600 ms (a half sine), 0 outside it."""
    if elapsed_s < 0 or elapsed_s >= SWELL_MS / 1000.0:
        return 0
    return int(round(SWELL_PEAK * math.sin(math.pi * elapsed_s / (SWELL_MS / 1000.0))))


class ControllerEffectsBase(ABC):
    """Base class providing controller effect animations.

    Subclasses must implement _set_led_color() to control actual hardware or mock state.

    Args:
        clock: Optional Clock instance for time operations. Defaults to RealClock.
               Inject a FakeClock in tests to control time without patching.
    """

    def __init__(self, clock: Clock | None = None):
        """Initialize effect tracking.

        Args:
            clock: Clock instance for time/sleep operations. Defaults to RealClock.
        """
        # Track active effects per controller: {serial: ActiveEffect}
        self.active_effects: dict[str, ActiveEffect] = {}
        self._clock = clock or RealClock()

    @abstractmethod
    async def _set_led_color(self, serial: str, color: tuple[int, int, int]):
        """Set LED color on a controller (async).

        Args:
            serial: Controller serial number
            color: RGB tuple (0-255, 0-255, 0-255)

        Subclasses must implement this to control hardware or mock state.
        """
        pass

    async def _effect_flash(self, serial: str, color: tuple[int, int, int], duration_ms: int, speed: int):
        """FLASH effect: rapid on/off blinking (Phase 31).

        Args:
            serial: Controller serial
            color: RGB color to flash
            duration_ms: Total effect duration in milliseconds
            speed: Flash frequency (1-10 Hz)
        """
        interval = 1.0 / max(1, speed)  # seconds per flash cycle
        end_time = self._clock.time() + (duration_ms / 1000.0)

        try:
            while self._clock.time() < end_time:
                await self._set_led_color(serial, color)
                await self._clock.sleep(interval / 2)
                await self._set_led_color(serial, (0, 0, 0))  # Off
                await self._clock.sleep(interval / 2)
        except asyncio.CancelledError:
            logger.debug(f"FLASH effect cancelled for {serial}")
            raise
        finally:
            # Restore color at end
            await self._set_led_color(serial, color)

    async def _effect_pulse(self, serial: str, color: tuple[int, int, int], duration_ms: int, speed: int):
        """PULSE effect: smooth breathing/fade (Phase 31).

        Uses sine wave for smooth brightness transitions.

        Args:
            serial: Controller serial
            color: RGB color to pulse
            duration_ms: Total effect duration in milliseconds
            speed: Pulse frequency (1-10 Hz)
        """
        interval = 0.05  # 20 Hz update rate for smooth animation
        cycle_duration = 1.0 / max(1, speed)  # seconds per pulse cycle
        start = self._clock.time()
        end_time = start + (duration_ms / 1000.0)

        try:
            while self._clock.time() < end_time:
                elapsed = self._clock.time() - start
                # Sine wave: 0 → 1 → 0 (smooth breathing)
                brightness = (math.sin(2 * math.pi * elapsed / cycle_duration) + 1) / 2

                scaled_color = tuple(int(c * brightness) for c in color)
                await self._set_led_color(serial, scaled_color)
                await self._clock.sleep(interval)
        except asyncio.CancelledError:
            logger.debug(f"PULSE effect cancelled for {serial}")
            raise
        finally:
            # Restore full brightness at end
            await self._set_led_color(serial, color)

    async def _effect_rainbow(self, serial: str, duration_ms: int, speed: int):
        """RAINBOW effect: cycle through color spectrum (Phase 31).

        Uses HSV color space for smooth color transitions.

        Args:
            serial: Controller serial
            duration_ms: Total effect duration in milliseconds
            speed: Rotation speed (1-10 Hz)
        """
        interval = 0.05  # 20 Hz update rate
        cycle_duration = 1.0 / max(1, speed)  # seconds per full rainbow cycle
        start = self._clock.time()
        end_time = start + (duration_ms / 1000.0)

        try:
            while self._clock.time() < end_time:
                elapsed = self._clock.time() - start
                # HSV color space: rotate hue 0 → 1
                hue = (elapsed / cycle_duration) % 1.0

                rgb = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
                color = tuple(int(c * 255) for c in rgb)
                await self._set_led_color(serial, color)
                await self._clock.sleep(interval)
        except asyncio.CancelledError:
            logger.debug(f"RAINBOW effect cancelled for {serial}")
            raise

    async def _effect_fade_out(self, serial: str, color: tuple[int, int, int], duration_ms: int):
        """FADE_OUT effect: linear fade to black (Phase 31).

        Args:
            serial: Controller serial
            color: Starting RGB color
            duration_ms: Fade duration in milliseconds
        """
        interval = 0.05  # 20 Hz update rate
        steps = int((duration_ms / 1000.0) / interval)
        steps = max(1, steps)

        try:
            for step in range(steps):
                brightness = 1.0 - (step / steps)
                scaled_color = tuple(int(c * brightness) for c in color)
                await self._set_led_color(serial, scaled_color)
                await self._clock.sleep(interval)
        except asyncio.CancelledError:
            logger.debug(f"FADE_OUT effect cancelled for {serial}")
            raise
        finally:
            # End at black
            await self._set_led_color(serial, (0, 0, 0))

    async def _effect_fade_in(self, serial: str, color: tuple[int, int, int], duration_ms: int):
        """FADE_IN effect: linear fade from black to color (Phase 31).

        Args:
            serial: Controller serial
            color: Target RGB color
            duration_ms: Fade duration in milliseconds
        """
        interval = 0.05  # 20 Hz update rate
        steps = int((duration_ms / 1000.0) / interval)
        steps = max(1, steps)

        try:
            for step in range(steps + 1):
                brightness = step / steps
                scaled_color = tuple(int(c * brightness) for c in color)
                await self._set_led_color(serial, scaled_color)
                await self._clock.sleep(interval)
        except asyncio.CancelledError:
            logger.debug(f"FADE_IN effect cancelled for {serial}")
            raise
        finally:
            # End at full brightness
            await self._set_led_color(serial, color)

    def cancel_controller_effect(self, serial: str):
        """Cancel any active effect on a controller.

        Call this when a controller disconnects or is removed.

        Args:
            serial: Controller serial number
        """
        if serial in self.active_effects:
            self.active_effects[serial].task.cancel()
            del self.active_effects[serial]
            logger.debug(f"Cancelled effect for {serial}")


def resolve_shield_ending_lead(value: object) -> float:
    """Validate ``shield.ending_rumble_seconds`` (flags.md §4): bad -> 1 s, clamped to 0..3, 0 = off."""
    lead = _finite_number(value)
    if lead is None or lead < 0:
        return DEFAULT_SHIELD_ENDING_LEAD_S
    return min(SHIELD_ENDING_LEAD_MAX_S, lead)
