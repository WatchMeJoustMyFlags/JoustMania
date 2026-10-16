"""Deterministic motion patterns for pattern-driven mock games (UC-046, #1258).

A *parameter set* is one immutable JSON file under
``services/controller_manager/patterns/<name>_v<version>.json``. It holds one
motion function per controller *slot*: an ordered list of time segments, each one
of ``constant``, ``ramp`` or ``sine`` (UC-046 BR-002). A pattern controller reports
the acceleration ``(0, 0, f(k / r))`` g on its k-th delivered gameplay frame, where
``r`` is the set's pinned ``stream_rate_hz`` (BR-001), so the signal depends only
on the frame index, never on wall time.

This module is pure: parsing, validation (BR-003 V1..V9), evaluation and the
signal hash. The mock adapter owns the per-controller runtime state.

The death-window rule (V9) needs the game coordinator's smoothing at its default
calibration. The controller-manager image does not ship the coordinator code, so
the constants below MIRROR ``services/game_coordinator/games/base.py``
(``_update_ema``, ``SLOW_MAX`` / ``FAST_MAX``); ``tests/test_motion_patterns.py``
imports the coordinator's values and fails on any drift.
"""

from __future__ import annotations

import bisect
import json
import math
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

# --- Constants of UC-046 BR-003 (accepted by the maintainer review of #1302) ---

MIN_SLOTS = 2
MAX_SLOTS = 8
STREAM_RATES_HZ = (15, 30, 60)
MIN_GAME_SECONDS = 10
MAX_GAME_SECONDS = 60
MIN_G = 0.0
MAX_G = 10.0
IDLE_LEVEL_G = 1.0
IDLE_PRELUDE_S = 4.0
TERMINAL_LEVEL_G = 4.0
TERMINAL_LEAD_S = 1.0
WINDOW_SEPARATION_S = 0.5
MAX_DESCRIPTION = 500
MAX_SEGMENTS = 32

# --- Mirror of the coordinator's default calibration (drift-tested) ---

# services/game_coordinator/games/base.py SLOW_MAX / FAST_MAX (death thresholds).
COORDINATOR_SLOW_MAX = (1.3, 1.5, 1.8, 2.5, 3.2)
COORDINATOR_FAST_MAX = (1.6, 1.8, 2.8, 3.2, 3.5)
DEFAULT_SENSITIVITY = 2  # MEDIUM
DEFAULT_EMA_WEIGHT = 4.0  # perception.ema_weight default
WINDOW_START_G = COORDINATOR_SLOW_MAX[DEFAULT_SENSITIVITY]  # 1.8 g
WINDOW_END_G = COORDINATOR_FAST_MAX[DEFAULT_SENSITIVITY]  # 2.8 g

PATTERNS_DIR = Path(__file__).resolve().parent.parent / "patterns"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SET_ID_RE = re.compile(r"^([a-z][a-z0-9_]*)_v([1-9][0-9]*)$")

_TOP_LEVEL_KEYS = {"name", "version", "description", "stream_rate_hz", "max_game_seconds", "slots"}
_FUNCTION_PARAMS = {
    "constant": ("level",),
    "ramp": ("start", "slope"),
    "sine": ("mean", "amplitude", "period_s", "phase"),
}
# Per-parameter bounds from the entity model (PATTERN_SEGMENT).
_PARAM_BOUNDS = {
    "level": (0.0, 10.0),
    "start": (0.0, 10.0),
    "slope": (-10.0, 10.0),
    "mean": (0.0, 10.0),
    "amplitude": (0.0, 10.0),
    "period_s": (0.1, 60.0),
}

_FNV64_OFFSET = 0xCBF29CE484222325
_FNV64_PRIME = 0x100000001B3
_MASK64 = 0xFFFFFFFFFFFFFFFF


@dataclass(frozen=True)
class Segment:
    """One time segment of a slot's motion function (UC-046 BR-002)."""

    from_s: float
    function: str
    params: tuple[float, ...]

    def value(self, t: float) -> float:
        """f at game time t (seconds), evaluated in local time tau = t - from_s."""
        tau = t - self.from_s
        if self.function == "constant":
            return self.params[0]
        if self.function == "ramp":
            start, slope = self.params
            return start + slope * tau
        mean, amplitude, period_s, phase = self.params
        return mean + amplitude * math.sin(2.0 * math.pi * (tau / period_s + phase))

    def to_dict(self) -> dict:
        names = _FUNCTION_PARAMS[self.function]
        out: dict = {"from_s": self.from_s, "function": self.function}
        out.update(dict(zip(names, self.params, strict=True)))
        return out


@dataclass(frozen=True)
class SlotPattern:
    """The motion function of one slot: ordered segments, the last lasts forever."""

    segments: tuple[Segment, ...]
    _starts: tuple[float, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_starts", tuple(s.from_s for s in self.segments))

    def value(self, t: float) -> float:
        # Last segment whose start is <= t (segments start at 0, so idx >= 0 for t >= 0).
        idx = bisect.bisect_right(self._starts, t) - 1
        return self.segments[max(idx, 0)].value(t)

    def canonical_json(self) -> str:
        """Segments as canonical JSON (sorted keys, no whitespace) for ``pattern.params``."""
        return json.dumps([s.to_dict() for s in self.segments], sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PatternSet:
    """A named, versioned parameter set (entity MOTION_PATTERN_SET)."""

    name: str
    version: int
    description: str
    stream_rate_hz: int
    max_game_seconds: int
    slots: tuple[SlotPattern, ...]

    @property
    def set_id(self) -> str:
        return f"{self.name}_v{self.version}"

    @property
    def slot_count(self) -> int:
        return len(self.slots)

    @property
    def horizon_frames(self) -> int:
        """K = ceil(T * r): the last frame index the validation rules cover."""
        return math.ceil(self.max_game_seconds * self.stream_rate_hz)

    def game_time(self, k: int) -> float:
        """UC-046 BR-001: game time of delivered frame k is t = k / r."""
        return k / self.stream_rate_hz

    def value(self, slot: int, k: int) -> float:
        """f_slot(k / r) in g."""
        return self.slots[slot].value(k / self.stream_rate_hz)


@dataclass
class LoadResult:
    """Outcome of resolving a set id (``GetMotionPatternSet``)."""

    set_id: str
    found: bool
    pattern_set: PatternSet | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.found and self.pattern_set is not None and not self.errors


# --------------------------------------------------------------------------
# Parsing (structure + V1, V2, V3, V4, V5)
# --------------------------------------------------------------------------


def _is_number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _parse_segment(slot: int, pos: int, raw: object, errors: list[str]) -> Segment | None:
    where = f"slot {slot} segment {pos}"
    if not isinstance(raw, dict):
        errors.append(f"V5: {where} is not an object")
        return None
    function = raw.get("function")
    if function not in _FUNCTION_PARAMS:
        errors.append(f"V5: {where} function {function!r} is not one of constant, ramp, sine")
        return None
    names = _FUNCTION_PARAMS[function]
    extra = set(raw) - {"from_s", "function", *names}
    if extra:
        errors.append(f"V5: {where} has parameters not of its function: {sorted(extra)}")
    from_s = raw.get("from_s")
    if not _is_number(from_s) or not math.isfinite(from_s):
        errors.append(f"V5: {where} from_s must be a finite number")
        return None
    values: list[float] = []
    for name in names:
        v = raw.get(name)
        if not _is_number(v) or not math.isfinite(v):
            errors.append(f"V5: {where} {name} must be a finite number")
            return None
        values.append(float(v))
    params = dict(zip(names, values, strict=True))
    for name, v in params.items():
        bounds = _PARAM_BOUNDS.get(name)
        if bounds and not (bounds[0] <= v <= bounds[1]):
            errors.append(f"V5: {where} {name}={v} outside {bounds[0]}..{bounds[1]}")
    if function == "sine":
        if params["period_s"] <= 0:
            errors.append(f"V5: {where} period_s must be > 0")
        if not (0.0 <= params["phase"] < 1.0):
            errors.append(f"V5: {where} phase={params['phase']} outside 0 <= phase < 1")
    return Segment(from_s=float(from_s), function=function, params=tuple(values))


def parse_pattern_set(data: object, file_stem: str | None = None) -> tuple[PatternSet | None, list[str]]:
    """Parse a set document. Returns (set or None, structural errors).

    ``file_stem`` (the file name without ``.json``) enables the file-name half of V1.
    A set is returned whenever its structure is usable, even with rule errors, so
    the caller can report every failed rule at once.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return None, ["V1: set document is not a JSON object"]

    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        errors.append(f"V1: unknown top-level keys {sorted(unknown)}")

    # V1 identity
    name = data.get("name")
    version = data.get("version")
    description = data.get("description")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        errors.append(f"V1: name {name!r} does not match [a-z][a-z0-9_]*")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append(f"V1: version {version!r} is not an integer >= 1")
    if not isinstance(description, str) or not (1 <= len(description) <= MAX_DESCRIPTION):
        errors.append(f"V1: description must hold 1 to {MAX_DESCRIPTION} characters")
    if (
        file_stem is not None
        and isinstance(name, str)
        and isinstance(version, int)
        and file_stem != f"{name}_v{version}"
    ):
        errors.append(f"V1: file name {file_stem}.json does not match {name}_v{version}.json")

    # V3 rate, V4 duration
    rate = data.get("stream_rate_hz")
    if rate not in STREAM_RATES_HZ or isinstance(rate, bool):
        errors.append(f"V3: stream_rate_hz {rate!r} is not one of {list(STREAM_RATES_HZ)}")
    horizon = data.get("max_game_seconds")
    if (
        not isinstance(horizon, int)
        or isinstance(horizon, bool)
        or not (MIN_GAME_SECONDS <= horizon <= MAX_GAME_SECONDS)
    ):
        errors.append(f"V4: max_game_seconds {horizon!r} is not an integer in {MIN_GAME_SECONDS}..{MAX_GAME_SECONDS}")

    # V2 slots
    raw_slots = data.get("slots")
    slots: list[SlotPattern] = []
    if not isinstance(raw_slots, list) or not (MIN_SLOTS <= len(raw_slots) <= MAX_SLOTS):
        count = len(raw_slots) if isinstance(raw_slots, list) else raw_slots
        errors.append(f"V2: slot count {count!r} is not in {MIN_SLOTS}..{MAX_SLOTS}")
    if isinstance(raw_slots, list):
        for i, raw_slot in enumerate(raw_slots):
            if not isinstance(raw_slot, list) or not raw_slot:
                errors.append(f"V2: slot {i} has no segments")
                continue
            if len(raw_slot) > MAX_SEGMENTS:
                errors.append(f"V5: slot {i} has {len(raw_slot)} segments, more than {MAX_SEGMENTS}")
            segs = [_parse_segment(i, p, raw, errors) for p, raw in enumerate(raw_slot)]
            if any(s is None for s in segs):
                continue
            starts = [s.from_s for s in segs]
            if starts[0] != 0:
                errors.append(f"V5: slot {i} first segment starts at {starts[0]}, not 0")
            if any(b <= a for a, b in zip(starts, starts[1:], strict=False)):
                errors.append(f"V5: slot {i} segment starts do not strictly increase")
            slots.append(SlotPattern(segments=tuple(segs)))  # type: ignore[arg-type]

    structurally_usable = (
        isinstance(name, str)
        and isinstance(version, int)
        and rate in STREAM_RATES_HZ
        and isinstance(horizon, int)
        and isinstance(raw_slots, list)
        and len(slots) == len(raw_slots)
        and len(slots) >= 1
    )
    if not structurally_usable:
        return None, errors
    return (
        PatternSet(
            name=name,  # type: ignore[arg-type]
            version=version,  # type: ignore[arg-type]
            description=description if isinstance(description, str) else "",
            stream_rate_hz=rate,  # type: ignore[arg-type]
            max_game_seconds=horizon,  # type: ignore[arg-type]
            slots=tuple(slots),
        ),
        errors,
    )


# --------------------------------------------------------------------------
# Frame-level rules (V6, V7, V8, V9)
# --------------------------------------------------------------------------


def slot_values(ps: PatternSet, slot: int) -> list[float]:
    """f_slot(k / r) for k = 0..K (every frame the validation covers)."""
    return [ps.value(slot, k) for k in range(ps.horizon_frames + 1)]


def terminal_frame(values: list[float], rate: int) -> int | None:
    """First frame k_term from which f >= TERMINAL_LEVEL_G holds up to K, if k_term <= K - r.

    Returns None when the slot is not terminating (V8).
    """
    horizon = len(values) - 1
    k_term = None
    for k in range(horizon, -1, -1):
        if values[k] >= TERMINAL_LEVEL_G:
            k_term = k
        else:
            break
    if k_term is None or k_term > horizon - rate:
        return None
    return k_term


def death_window(values: list[float], ema_weight: float = DEFAULT_EMA_WEIGHT) -> tuple[int, int] | None:
    """V9 death window at the default calibration, in frame indices.

    The EMA is primed at f(0) and updated once per frame as the coordinator does
    (``smoothed = (smoothed * w + raw) / (w + 1)``). The window starts at the first
    frame whose EMA exceeds WINDOW_START_G (slow table) and ends at the first frame
    whose EMA exceeds WINDOW_END_G (high-tempo table), or at K. None when the EMA
    never exceeds WINDOW_START_G (the slot cannot die at the default calibration).
    """
    ema = values[0]
    start = None
    for k, v in enumerate(values):
        if k > 0:
            ema = (ema * ema_weight + v) / (ema_weight + 1)
        if start is None and ema > WINDOW_START_G:
            start = k
        if start is not None and ema > WINDOW_END_G:
            return start, k
    if start is None:
        return None
    return start, len(values) - 1


def validate_frames(ps: PatternSet) -> list[str]:
    """Rules V2 (slot count), V6, V7, V8 and V9 over every frame k = 0..K."""
    errors: list[str] = []
    n = ps.slot_count
    r = ps.stream_rate_hz
    if not (MIN_SLOTS <= n <= MAX_SLOTS):
        errors.append(f"V2: slot count {n} is not in {MIN_SLOTS}..{MAX_SLOTS}")

    terminating = 0
    windows: list[tuple[int, int, int]] = []
    for slot in range(n):
        values = slot_values(ps, slot)
        for k, v in enumerate(values):
            if not math.isfinite(v) or not (MIN_G <= v <= MAX_G):
                errors.append(f"V6: slot {slot} frame {k} value {v:.4f} g outside {MIN_G}..{MAX_G} g")
                break
        for k, v in enumerate(values):
            if k / r >= IDLE_PRELUDE_S:
                break
            if v != IDLE_LEVEL_G:
                errors.append(
                    f"V7: slot {slot} frame {k} (t={k / r:.3f} s) is {v:.4f} g, "
                    f"not {IDLE_LEVEL_G} g during the {IDLE_PRELUDE_S} s idle prelude"
                )
                break
        if terminal_frame(values, r) is not None:
            terminating += 1
        window = death_window(values)
        if window is not None:
            windows.append((window[0], window[1], slot))

    if terminating < n - 1:
        errors.append(
            f"V8: only {terminating} of {n} slots are terminating; at least {n - 1} must reach "
            f">= {TERMINAL_LEVEL_G} g by {ps.max_game_seconds - TERMINAL_LEAD_S:.0f} s and hold it to "
            f"{ps.max_game_seconds} s"
        )

    windows.sort()
    gap = WINDOW_SEPARATION_S * r
    for (s1, e1, slot1), (s2, _e2, slot2) in zip(windows, windows[1:], strict=False):
        if s2 - e1 < gap:
            errors.append(
                f"V9: death windows of slot {slot1} [{s1 / r:.3f}, {e1 / r:.3f}] s and slot {slot2} "
                f"(start {s2 / r:.3f} s) are less than {WINDOW_SEPARATION_S} s apart"
            )
    return errors


def validate_pattern_set(data: object, file_stem: str | None = None) -> tuple[PatternSet | None, list[str]]:
    """Parse and run every rule V1..V9. Returns (set or None, all failed rules)."""
    ps, errors = parse_pattern_set(data, file_stem)
    if ps is None:
        return None, errors
    # Frame rules evaluate f; with a broken parameter (V5) f may be undefined.
    if any(e.startswith("V5") for e in errors):
        return ps, errors
    errors.extend(validate_frames(ps))
    return ps, errors


def load_pattern_set(set_id: str, directory: Path | None = None) -> LoadResult:
    """Resolve ``<directory>/<set_id>.json`` and validate it (BR-003)."""
    directory = directory or PATTERNS_DIR
    if not _SET_ID_RE.match(set_id or ""):
        return LoadResult(set_id=set_id, found=False, errors=[f"V1: set id {set_id!r} is not <name>_v<version>"])
    path = directory / f"{set_id}.json"
    if not path.is_file():
        return LoadResult(set_id=set_id, found=False, errors=[f"unknown parameter set {set_id!r}"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return LoadResult(set_id=set_id, found=True, errors=[f"V1: {path.name} is not valid JSON: {e}"])
    ps, errors = validate_pattern_set(data, file_stem=path.stem)
    return LoadResult(set_id=set_id, found=True, pattern_set=ps, errors=errors)


def list_pattern_set_ids(directory: Path | None = None) -> list[str]:
    """Ids of every set file in the patterns directory, sorted."""
    directory = directory or PATTERNS_DIR
    return sorted(p.stem for p in directory.glob("*.json"))


# --------------------------------------------------------------------------
# Signal hash (telemetry.md §9)
# --------------------------------------------------------------------------


def milli_g(value: float) -> int:
    """round(f * 1000), half up, as the signed 32-bit integer the hash encodes."""
    return int(math.floor(value * 1000.0 + 0.5))


class SignalHasher:
    """FNV-1a/64 over delivered frames, each the int32 little-endian milli-g value.

    ``checkpoints`` holds the hash after every ``rate`` frames (one per second of
    game time), so two runs whose slots stopped receiving frames at slightly
    different moments can still be compared on their common prefix.
    """

    __slots__ = ("_hash", "frames", "checkpoints", "_rate")

    def __init__(self, rate: int) -> None:
        self._hash = _FNV64_OFFSET
        self.frames = 0
        self.checkpoints: list[str] = []
        self._rate = rate

    def update(self, value: float) -> None:
        h = self._hash
        for byte in struct.pack("<i", milli_g(value)):
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
