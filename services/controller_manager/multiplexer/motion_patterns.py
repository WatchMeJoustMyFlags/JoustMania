"""Parametric motion functions for pattern-driven mock games (UC-046, #1258).

An *archetype* is one parameter object (an object variant of the agent.json flag
``experiment.mock_archetype``) that defines the motion of every player of a
pattern game. Player i (slot i) reports the acceleration ``(0, 0, intensity_i(t))``
g on its k-th delivered gameplay frame, with game time ``t = k / r`` (UC-046
BR-001) and

    intensity_i(t) = clamp(base_i + drift_i * t
                           + amp_i * sin(2 pi (t / period_s + phase_i))
                           + burst_i(t), 0, 10)

``burst_i`` is a rectangular pulse of height ``burst_h`` and width BURST_WIDTH_S
starting every ``burst_period_s`` seconds; per-slot modifiers shift base, drift,
amp and phase (BR-002, BR-003). No rule forces the game to end: its length emerges
from the parameters, the coordinator's thresholds, the tempo and interventions.

An archetype may also carry a ``trigger_policy`` (UC-046 BR-018, #2096): the
controllers then press the trigger by themselves, ``burst`` during the burst
windows, ``threshold`` while the intensity reached ``level`` within the last
``hold_frames`` frames after a ``reaction_frames`` delay. ``trigger(slot, k)`` is
a pure function of the archetype and k, like the intensity.

The controller manager never reads flagd for this: the agent resolves and pins the
archetype and passes its parameters in ``AddControllers``. This module is pure:
validation, evaluation and the signal hash. The mock adapter owns runtime state.
"""

from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass, field

# --- Constants of UC-046 BR-002 / BR-003 ---

MIN_PLAYERS = 2
MAX_PLAYERS = 8
STREAM_RATES_HZ = (15, 30, 60)
MIN_G = 0.0
MAX_G = 10.0
BURST_WIDTH_S = 0.2
# Rounding guard so a pulse spans exactly round(0.2 * r) frames at every rate
# (k / r and the modulo are inexact in binary floating point).
_BURST_EPS = 1e-9

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Field -> (low, high, high_exclusive). Every field is required (BR-003).
_FIELDS: dict[str, tuple[float, float, bool]] = {
    "players": (MIN_PLAYERS, MAX_PLAYERS, False),
    "stream_rate_hz": (15, 60, False),
    "base": (0.0, 10.0, False),
    "drift": (-1.0, 1.0, False),
    "amp": (0.0, 10.0, False),
    "period_s": (0.0, math.inf, False),
    "phase": (0.0, 1.0, True),
    "burst_h": (0.0, 10.0, False),
    "burst_period_s": (0.0, math.inf, False),
    "d_base": (-1.0, 1.0, False),
    "d_drift": (-1.0, 1.0, False),
    "d_amp": (-1.0, 1.0, False),
    "d_phase": (0.0, 1.0, True),
}
PARAM_FIELDS = tuple(_FIELDS)

# UC-046 BR-018: the optional, non-numeric trigger policy (#2096).
TRIGGER_POLICY_KEY = "trigger_policy"
POLICY_NONE = "none"
POLICY_BURST = "burst"
POLICY_THRESHOLD = "threshold"
POLICY_LEVEL_MAX = 10.0
POLICY_REACTION_MAX = 60
# At least two frames, so a press survives one coalesced frame.
POLICY_HOLD_FRAMES_MIN = 2
POLICY_HOLD_FRAMES_MAX = 120

_FNV64_OFFSET = 0xCBF29CE484222325
_FNV64_PRIME = 0x100000001B3
_MASK64 = 0xFFFFFFFFFFFFFFFF


@dataclass(frozen=True)
class SlotFunction:
    """intensity_i(t) of one player, with the slot modifiers already applied."""

    base: float
    drift: float
    amp: float
    period_s: float
    phase: float
    burst_h: float
    burst_period_s: float

    def sine(self, t: float) -> float:
        return self.amp * math.sin(2.0 * math.pi * (t / self.period_s + self.phase))

    def burst(self, t: float) -> float:
        """Rectangular pulse: on while (s - floor(s)) * T < BURST_WIDTH_S for s = t/T + phase >= 1."""
        if self.burst_h <= 0.0:
            return 0.0
        s = t / self.burst_period_s + self.phase
        if s < 1.0:
            return 0.0
        return self.burst_h if (s - math.floor(s)) * self.burst_period_s < BURST_WIDTH_S - _BURST_EPS else 0.0

    def raw(self, t: float) -> float:
        return self.base + self.drift * t + self.sine(t) + self.burst(t)

    def value(self, t: float) -> float:
        """intensity_i(t) clamped to MIN_G..MAX_G (BR-002)."""
        v = self.raw(t)
        if not math.isfinite(v):
            return MAX_G
        return min(MAX_G, max(MIN_G, v))


@dataclass(frozen=True)
class TriggerPolicy:
    """How a pattern controller presses its trigger (UC-046 BR-018); ``none`` is represented by None."""

    kind: str
    level: float = 0.0
    reaction_frames: int = 0
    hold_frames: int = 0


@dataclass(frozen=True)
class Archetype:
    """A validated archetype (entity MOTION_ARCHETYPE)."""

    name: str
    params: dict = field(compare=False)
    slots: tuple[SlotFunction, ...]
    trigger_policy: TriggerPolicy | None = None

    @property
    def has_trigger_policy(self) -> bool:
        return self.trigger_policy is not None

    @property
    def players(self) -> int:
        return len(self.slots)

    @property
    def stream_rate_hz(self) -> int:
        return int(self.params["stream_rate_hz"])

    def game_time(self, k: int) -> float:
        """UC-046 BR-001: game time of delivered frame k is t = k / r."""
        return k / self.stream_rate_hz

    def value(self, slot: int, k: int) -> float:
        """intensity_slot(k / r) in g."""
        return self.slots[slot].value(k / self.stream_rate_hz)

    def trigger(self, slot: int, k: int) -> bool:
        """UC-046 BR-018: whether slot presses the trigger on delivered frame k (pure in the archetype and k).

        ``burst``: during a burst window of the slot. ``threshold``: some frame j in
        [k - reaction - hold + 1, k - reaction], j >= 0, has intensity_slot(j / r) >= level.
        """
        policy = self.trigger_policy
        if policy is None:
            return False
        if policy.kind == POLICY_BURST:
            return self.slots[slot].burst(k / self.stream_rate_hz) > 0.0
        newest = k - policy.reaction_frames
        oldest = max(0, newest - policy.hold_frames + 1)
        return any(self.value(slot, j) >= policy.level for j in range(oldest, newest + 1))

    def canonical_json(self) -> str:
        """The parameter object as canonical JSON (sorted keys, no whitespace) for ``pattern.params``."""
        return canonical_json(self.params)


def canonical_json(params: dict) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def _is_number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def slot_function(params: dict, slot: int) -> SlotFunction:
    """Apply the per-slot modifiers of BR-003 to the game parameters."""
    return SlotFunction(
        base=params["base"] + slot * params["d_base"],
        drift=params["drift"] + slot * params["d_drift"],
        amp=params["amp"] + slot * params["d_amp"],
        period_s=params["period_s"],
        phase=math.fmod(params["phase"] + slot * params["d_phase"], 1.0),
        burst_h=params["burst_h"],
        burst_period_s=params["burst_period_s"],
    )


def _whole_number(v: object, low: int, high: int) -> int | None:
    if not _is_number(v) or not math.isfinite(v) or v != int(v) or not (low <= v <= high):
        return None
    return int(v)


def validate_trigger_policy(raw: object) -> tuple[TriggerPolicy | None, list[str]]:
    """Validate a ``trigger_policy`` object (UC-046 BR-018). Returns (policy or None for none, failed rules)."""
    if not isinstance(raw, dict):
        return None, ["trigger_policy is not a JSON object"]
    kind = raw.get("kind")
    allowed = {"kind"}
    if kind == POLICY_THRESHOLD:
        allowed |= {"level", "reaction_frames", "hold_frames"}
    elif kind not in (POLICY_NONE, POLICY_BURST):
        return None, [f"trigger_policy.kind={kind!r} is not one of ['none', 'burst', 'threshold']"]
    errors: list[str] = []
    unknown = sorted(set(raw) - allowed)
    if unknown:
        errors.append(f"trigger_policy has unknown fields {unknown}")
    if kind != POLICY_THRESHOLD:
        return (TriggerPolicy(kind) if kind == POLICY_BURST else None), errors
    level = raw.get("level")
    if not _is_number(level) or not math.isfinite(level) or not (0.0 <= level <= POLICY_LEVEL_MAX):
        errors.append(f"trigger_policy.level={level!r} is not a number in 0..{POLICY_LEVEL_MAX:g}")
    reaction = _whole_number(raw.get("reaction_frames"), 0, POLICY_REACTION_MAX)
    if reaction is None:
        errors.append(
            f"trigger_policy.reaction_frames={raw.get('reaction_frames')!r} "
            f"is not a whole number in 0..{POLICY_REACTION_MAX}"
        )
    hold = _whole_number(raw.get("hold_frames"), POLICY_HOLD_FRAMES_MIN, POLICY_HOLD_FRAMES_MAX)
    if hold is None:
        errors.append(
            f"trigger_policy.hold_frames={raw.get('hold_frames')!r} is not a whole number in "
            f"{POLICY_HOLD_FRAMES_MIN}..{POLICY_HOLD_FRAMES_MAX}"
        )
    if errors:
        return None, errors
    return TriggerPolicy(POLICY_THRESHOLD, float(level), reaction, hold), []


def _field_errors(params: dict) -> list[str]:
    """BR-003 per-field rules: unknown fields, then each field present, finite and in bounds."""
    errors: list[str] = []
    unknown = sorted(set(params) - set(_FIELDS) - {TRIGGER_POLICY_KEY})
    if unknown:
        errors.append(f"unknown fields {unknown}")
    for key, (low, high, high_exclusive) in _FIELDS.items():
        v = params.get(key)
        if v is None:
            errors.append(f"{key} is missing")
            continue
        if not _is_number(v) or not math.isfinite(v):
            errors.append(f"{key}={v!r} is not a finite number")
            continue
        too_high = v >= high if high_exclusive else v > high
        if v < low or too_high:
            bound = f"< {high}" if high_exclusive else f"<= {high}"
            errors.append(f"{key}={v} outside {low} <= {key} {bound}")
    return errors


def _cross_field_errors(params: dict, policy: TriggerPolicy | None) -> list[str]:
    """BR-003 / BR-018 rules across fields, for parameters whose fields each passed."""
    errors: list[str] = []
    if params["players"] != int(params["players"]):
        errors.append(f"players={params['players']} is not an integer")
    if params["stream_rate_hz"] not in STREAM_RATES_HZ:
        errors.append(f"stream_rate_hz={params['stream_rate_hz']} is not one of {list(STREAM_RATES_HZ)}")
    if params["period_s"] <= 0:
        errors.append("period_s must be > 0")
    if params["burst_h"] > 0 and params["burst_period_s"] <= 0:
        errors.append("burst_period_s must be > 0 when burst_h > 0")
    if policy is not None and policy.kind == POLICY_BURST and params["burst_h"] <= 0:
        errors.append("trigger_policy burst needs burst_h > 0 (it would never press)")
    return errors


def _slot_errors(slots: tuple[SlotFunction, ...]) -> list[str]:
    """BR-003: every slot's base and amplitude stay inside MIN_G..MAX_G."""
    errors: list[str] = []
    for i, s in enumerate(slots):
        if not (MIN_G <= s.base <= MAX_G):
            errors.append(f"slot {i} base {s.base:.4f} g outside {MIN_G}..{MAX_G}")
        if not (MIN_G <= s.amp <= MAX_G):
            errors.append(f"slot {i} amp {s.amp:.4f} g outside {MIN_G}..{MAX_G}")
    return errors


def validate_archetype(name: str, params: object) -> tuple[Archetype | None, list[str]]:
    """Validate an archetype (UC-046 BR-003). Returns (archetype or None, failed rules).

    ``params`` is the parameter object or its JSON text.
    """
    errors: list[str] = []
    if not isinstance(name, str) or not _NAME_RE.match(name):
        errors.append(f"name {name!r} does not match [a-z][a-z0-9_]*")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError as e:
            return None, [*errors, f"parameters are not valid JSON: {e}"]
    if not isinstance(params, dict):
        return None, [*errors, "parameters are not a JSON object"]

    errors.extend(_field_errors(params))
    policy = None
    if TRIGGER_POLICY_KEY in params:
        policy, policy_errors = validate_trigger_policy(params[TRIGGER_POLICY_KEY])
        errors.extend(policy_errors)
    if errors:
        return None, errors

    errors = _cross_field_errors(params, policy)
    if errors:
        return None, errors

    players = int(params["players"])
    slots = tuple(slot_function(params, i) for i in range(players))
    errors = _slot_errors(slots)
    if errors:
        return None, errors
    return Archetype(name=name, params=dict(params), slots=slots, trigger_policy=policy), []


# --------------------------------------------------------------------------
# Signal hash (telemetry.md §9)
# --------------------------------------------------------------------------


def milli_g(value: float) -> int:
    """floor(f * 1000 + 0.5) (milli-g, rounded half up), the int32 the hash encodes."""
    return int(math.floor(value * 1000.0 + 0.5))


class SignalHasher:
    """FNV-1a/64 over delivered frames, each the int32 little-endian milli-g value.

    For an archetype with a trigger policy (UC-046 BR-018) each frame also folds one
    byte after the value: 1 when the trigger is pressed on that frame, else 0. An
    archetype without a policy passes ``trigger=None`` and hashes as before.

    ``checkpoints`` holds the hash after every ``rate`` frames (one per second of
    game time). Total delivered frames depend on wall-clock lag after a death or at
    game end, so replays compare checkpoints on their common prefix (NFR-040).
    """

    __slots__ = ("_hash", "frames", "checkpoints", "_rate")

    def __init__(self, rate: int) -> None:
        self._hash = _FNV64_OFFSET
        self.frames = 0
        self.checkpoints: list[str] = []
        self._rate = rate

    def update(self, value: float, trigger: bool | None = None) -> None:
        h = self._hash
        data = struct.pack("<i", milli_g(value))
        if trigger is not None:
            data += b"\x01" if trigger else b"\x00"
        for byte in data:
            h ^= byte
            h = (h * _FNV64_PRIME) & _MASK64
        self._hash = h
        self.frames += 1
        if self.frames % self._rate == 0:
            self.checkpoints.append(self.hexdigest())

    def hexdigest(self) -> str:
        return f"{self._hash:016x}"

    @property
    def value(self) -> int:
        return self._hash
