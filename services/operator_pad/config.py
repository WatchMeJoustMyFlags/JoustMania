"""Configuration of the operator pad from the `operator` flag set (UC-015, flags.md §10a).

Every key is read at use time through the in-process OpenFeature client; a key the client cannot
resolve, or a malformed value, falls back to the code default below (the `operator.json` baseline).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

FLAG_DOMAIN = "operator"

# Logical key names of the pad (UC-015 BR-011).
K1, K2, K3, K4, K5, K6, K7, K8 = "K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8"
KEYS = (K1, K2, K3, K4, K5, K6, K7, K8)
KNOB_PRESS = "KNOB_PRESS"
KNOB_TURN_A = "KNOB_TURN_A"  # counter-clockwise (left): down
KNOB_TURN_B = "KNOB_TURN_B"  # clockwise (right): up
KNOB_NAMES = (KNOB_PRESS, KNOB_TURN_A, KNOB_TURN_B)

DEFAULT_DEVICE = "cb00:1337"
DEFAULT_TAP_MS = 400
DEFAULT_HOLD_AGENT_MS = 1000
DEFAULT_HOLD_END_MS = 1500
DEFAULT_HOLD_LOCK_MS = 3000
DEFAULT_KNOB_EVENTS_PER_STEP = 4
KNOB_EVENTS_PER_STEP_RANGE = (1, 12)
DEFAULT_PREVIEW_DEBOUNCE_MS = 150  # BR-017: wait after the last knob step before the preview speaks
PREVIEW_DEBOUNCE_MS_RANGE = (0, 1000)
DEFAULT_KEYMAP = (
    "K1=F13,K2=F14,K3=F18,K4=F19,K5=F20,K6=F21,K7=F22,K8=F23,KNOB_PRESS=F15,KNOB_TURN_A=F16,KNOB_TURN_B=F17"
)

# Linux input key codes of the function keys (linux/input-event-codes.h).
_FKEY_CODES = {
    **{f"F{n}": 58 + n for n in range(1, 11)},  # F1=59 .. F10=68
    "F11": 87,
    "F12": 88,
    **{f"F{n}": 170 + n for n in range(13, 25)},  # F13=183 .. F24=194
}

_DEVICE_RE = re.compile(r"^([0-9a-fA-F]{4}):([0-9a-fA-F]{4})$")


def key_code(token: str) -> int | None:
    """``F13`` -> 183, ``KEY_F13`` -> 183, ``183`` -> 183; anything else -> None."""
    token = token.strip().upper()
    token = token.removeprefix("KEY_")
    if token.isdigit():
        return int(token)
    return _FKEY_CODES.get(token)


def parse_keymap(raw: str) -> dict[int, str] | None:
    """``K1=F13,...`` -> {183: "K1", ...}; None when malformed, a name is unknown or a code repeats."""
    mapping: dict[int, str] = {}
    for part in raw.split(","):
        name, sep, token = part.partition("=")
        name = name.strip().upper()
        code = key_code(token) if sep else None
        if code is None or (name not in KEYS and name not in KNOB_NAMES) or code in mapping:
            return None
        if name in mapping.values():
            return None
        mapping[code] = name
    return mapping


def parse_device(raw: str) -> tuple[str, str] | None:
    """``cb00:1337`` -> ("cb00", "1337"); "" (variant ``unset``) or malformed -> None (match nothing)."""
    match = _DEVICE_RE.match(raw.strip())
    if match is None:
        return None
    return match.group(1).lower(), match.group(2).lower()


@dataclass(frozen=True)
class Timings:
    """Gesture timings in ms (UC-015 BR-007, BR-011)."""

    tap_ms: int = DEFAULT_TAP_MS
    hold_ms: dict[str, int] = field(
        default_factory=lambda: {K3: DEFAULT_HOLD_AGENT_MS, K4: DEFAULT_HOLD_END_MS, K8: DEFAULT_HOLD_LOCK_MS}
    )
    knob_events_per_step: int = DEFAULT_KNOB_EVENTS_PER_STEP  # BR-005: raw knob events per step


class PadConfig:
    """Reads the `operator` flag set at use time (flags.md §10a)."""

    def __init__(self, client) -> None:
        self._client = client

    def _int(self, key: str, default: int) -> int:
        try:
            value = self._client.get_integer_value(key, default)
        except Exception as e:  # an unreadable flag keeps the default
            logger.warning(f"Reading {key} failed, using {default}: {e}")
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            logger.warning(f"Malformed {key}={value!r}, using {default}")
            return default
        return value

    def knob_events_per_step(self) -> int:
        """Raw knob events that make one step (BR-005); the default outside 1 to 12 or when malformed."""
        value = self._int("operator_pad.knob_events_per_step", DEFAULT_KNOB_EVENTS_PER_STEP)
        low, high = KNOB_EVENTS_PER_STEP_RANGE
        if not low <= value <= high:
            logger.warning(
                f"operator_pad.knob_events_per_step={value} outside {low} to {high}, "
                f"using {DEFAULT_KNOB_EVENTS_PER_STEP}"
            )
            return DEFAULT_KNOB_EVENTS_PER_STEP
        return value

    def _str(self, key: str, default: str) -> str:
        try:
            value = self._client.get_string_value(key, default)
        except Exception as e:
            logger.warning(f"Reading {key} failed, using the default: {e}")
            return default
        return value if isinstance(value, str) else default

    def enabled(self) -> bool:
        try:
            return bool(self._client.get_boolean_value("operator_pad.enabled", True))
        except Exception as e:
            logger.warning(f"Reading operator_pad.enabled failed, keeping the pad on: {e}")
            return True

    def preview_enabled(self) -> bool:
        """`operator_pad.preview_enabled` (BR-017); unreadable keeps the preview on (it writes nothing)."""
        try:
            return bool(self._client.get_boolean_value("operator_pad.preview_enabled", True))
        except Exception as e:
            logger.warning(f"Reading operator_pad.preview_enabled failed, keeping the preview on: {e}")
            return True

    def preview_debounce_ms(self) -> int:
        """`operator_pad.preview_debounce_ms` (BR-017); 0 is valid, the default outside 0 to 1000 or malformed."""
        key, default = "operator_pad.preview_debounce_ms", DEFAULT_PREVIEW_DEBOUNCE_MS
        try:
            value = self._client.get_integer_value(key, default)
        except Exception as e:
            logger.warning(f"Reading {key} failed, using {default}: {e}")
            return default
        low, high = PREVIEW_DEBOUNCE_MS_RANGE
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            logger.warning(f"Malformed or out of range {key}={value!r} (valid {low} to {high}), using {default}")
            return default
        return value

    def device(self) -> tuple[str, str] | None:
        raw = self._str("operator_pad.device", DEFAULT_DEVICE)
        parsed = parse_device(raw)
        if parsed is None and raw:
            logger.warning(f"Malformed operator_pad.device={raw!r}: no device matches")
        return parsed

    def keymap(self) -> dict[int, str]:
        raw = self._str("operator_pad.keymap", DEFAULT_KEYMAP)
        parsed = parse_keymap(raw)
        if parsed is None:
            logger.warning(f"Malformed operator_pad.keymap={raw!r}, using the default keymap")
            parsed = parse_keymap(DEFAULT_KEYMAP)
            assert parsed is not None
        return parsed

    def timings(self) -> Timings:
        """The timings in force; the defaults when a hold time is not above the tap limit (BR-007)."""
        knob = self.knob_events_per_step()
        timings = Timings(
            knob_events_per_step=knob,
            tap_ms=self._int("operator_pad.tap_ms", DEFAULT_TAP_MS),
            hold_ms={
                K3: self._int("operator_pad.hold_agent_ms", DEFAULT_HOLD_AGENT_MS),
                K4: self._int("operator_pad.hold_end_ms", DEFAULT_HOLD_END_MS),
                K8: self._int("operator_pad.hold_lock_ms", DEFAULT_HOLD_LOCK_MS),
            },
        )
        if any(hold <= timings.tap_ms for hold in timings.hold_ms.values()):
            logger.warning(
                f"operator_pad timings invalid (tap_ms={timings.tap_ms}, hold={timings.hold_ms}): "
                "every hold must be longer than the tap limit; using the default timings"
            )
            return Timings(knob_events_per_step=knob)
        return timings
