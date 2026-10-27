"""
BaseGameMode - Abstract base class for all game modes

Phase 36b: Extracts common patterns from FFA, Teams, Random Teams, and Nonstop Joust.
Provides consistent span hierarchy orchestration and common game operations.

Uses Template Method pattern:
- run() orchestrates the entire game lifecycle with spans
- Concrete methods implement shared behavior (settings, countdown, game loop)
- Abstract methods define game-specific behavior (team assignment, win conditions, etc.)

Phase 70: Added dynamic music tempo system.
"""

import asyncio
import contextlib
import inspect
import logging
import math
import random
import statistics
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

import grpc
from openfeature.evaluation_context import EvaluationContext
from opentelemetry import context as otel_context
from opentelemetry import trace

from lib.controller_constants import battery_to_pct
from lib.feature_flags import (
    GAME_KIND_SHADOW,
    build_session_kind_context,
    read_float_flag,
    read_int_flag,
    read_object_flag,
    read_string_flag,
    set_game_transaction_context,
)
from lib.telemetry import inject_trace_context
from lib.types import GameEvent, Sensitivity, Sound
from services.game_coordinator import metrics
from services.game_coordinator.runtime_config import get_config_manager

if TYPE_CHECKING:
    from services.game_coordinator.games.analytics import PlayerAnalytics

tracer = trace.get_tracer(__name__)
logger = logging.getLogger(__name__)

# Game constants (Phase 43: Now uses runtime config for dynamic adjustment)
# Phase 72: Increased from 30Hz to 60Hz for better responsiveness
UPDATE_FREQUENCY = 60  # Hz - default, overridden by runtime config
COUNTDOWN_BEEP_COUNT = 3  # Number of beeps (Red/Yellow/Green)

# Phase 70: Music tempo constants (from original JoustMania)
SLOW_MUSIC_SPEED = 1.0  # Normal playback
FAST_MUSIC_SPEED = 1.3  # 30% faster

# #1833: the tempo INTENSITY ramp (UC-031 step 3, BR-005). Each game owns one
# intensity i in [0, 1] (0 = slow table / 1.0x, 1 = fast table / 1.3x) that moves
# toward its target at a bounded rate: a full swing takes ``tempo.ramp_up_seconds``
# going up and ``tempo.ramp_down_seconds`` going down (game flags, read at each
# ramp start, never per frame). Thresholds read i every frame; the music is
# derived from it (one ChangeTempo glide per segment), so it can only trail.
DEFAULT_RAMP_UP_SECONDS = 3.0  # The maintainer's "slow increase"
DEFAULT_RAMP_DOWN_SECONDS = 1.5  # The original JoustMania glide, until measured on a Pi
RAMP_SECONDS_MIN = 0.1  # Flag clamp, matches the registry bounds (flags.md §13.3)
RAMP_SECONDS_MAX = 10.0
# Shortest glide sent to the audio service: a transition_duration of 0 means 1.0 s there.
MIN_GLIDE_SECONDS = 0.05
# Deadline for one ChangeTempo, so a wedged audio service never stalls the music loop.
AUDIO_TEMPO_RPC_TIMEOUT_SECONDS = 0.5
# Back-off between resends after a ChangeTempo error (doubles per failure).
AUDIO_TEMPO_RETRY_MIN_SECONDS = 0.5
AUDIO_TEMPO_RETRY_MAX_SECONDS = 5.0
# A persistent failing music-loop tick logs once, then at most this often (#1841).
MUSIC_TICK_LOG_INTERVAL_SECONDS = 5.0
# Track id the audio service returns for PlayMusic while play_audio is off (UC-031 A1).
MUTED_TRACK_ID = "muted"

# #1109: tempo-schedule SOURCE selector (``game.tempo_schedule_mode`` flag).
# The tempo-change decision ALWAYS flows through the single pacing-policy seam
# (``_decide_next_change_delay``); this flag selects which policy that seam runs:
#   * "rule"  — the rule engine owns the decision. The DEFAULT rule reproduces
#     the historical random-window timing (``_rng.uniform`` within the lerped
#     ``music_windows``) byte-for-byte, so the unset/default behavior is
#     unchanged WITHOUT a second legacy code path. A deterministic non-random
#     rule is a follow-up policy (#1108) — the default rule simply keeps the RNG.
#   * "agent" — the seam yields the next-change decision to agent tempo
#     directives (#1103 ramp_tempo, a follow-up). Until that primitive lands the
#     mode is a clean STUB: with no directive present it falls back to the
#     default rule, so selecting "agent" early is safe (never freezes pacing).
# There is intentionally NO "random" mode: randomness survives as the default
# RULE, not as a separate mode (maintainer re-scope, #1109).
TEMPO_MODE_RULE = "rule"
TEMPO_MODE_AGENT = "agent"
# Unset / unknown values resolve to the rule engine's default rule => back-compat.
TEMPO_SCHEDULE_MODE_DEFAULT = TEMPO_MODE_RULE
_VALID_TEMPO_SCHEDULE_MODES = (TEMPO_MODE_RULE, TEMPO_MODE_AGENT)


def resolve_tempo_schedule_mode(value: object) -> str:
    """Validate the ``game.tempo_schedule_mode`` flag value.

    Any value other than the known modes (``rule``/``agent``) — including a
    missing flag, a non-string, or a stale ``random`` left over from an older
    config — resolves to :data:`TEMPO_SCHEDULE_MODE_DEFAULT` (``rule``), whose
    default policy reproduces today's behavior. This keeps the seam default-safe.
    """
    if isinstance(value, str) and value in _VALID_TEMPO_SCHEDULE_MODES:
        return value
    return TEMPO_SCHEDULE_MODE_DEFAULT


# #1117 (#1103 MVP action 2): ``ramp_tempo`` — a scheduled tempo CURVE that fills
# the #1114 ``agent``-mode seam. The agent emits a single string directive
# ``"<target>:<seconds>:<curve>"`` (e.g. ``"1.3:8:linear"``); the game-side records
# a RampDescriptor and ``_check_music_speed`` interpolates the live tempo toward
# the target over the duration (the SAME single tempo-owner seam the override /
# ``agent`` mode uses — no competing tempo writer), holding at target when done.
#
# Bounds (validated at parse time so a malformed value degrades to a safe no-op,
# never dispatches garbage): target is clamped to the valid music-speed range
# [SLOW_MUSIC_SPEED, FAST_MUSIC_SPEED]; seconds must be > 0 and is capped at
# RAMP_TEMPO_MAX_SECONDS so a stray huge value can't freeze pacing for the whole
# round; curve is one of the closed vocabulary {linear, ease}.
RAMP_CURVE_LINEAR = "linear"
RAMP_CURVE_EASE = "ease"
_VALID_RAMP_CURVES = (RAMP_CURVE_LINEAR, RAMP_CURVE_EASE)
# Upper bound on a single ramp's duration (seconds, #1840). A Free-for-All game lasts
# 2 to 3 minutes, so a longer curve would hold the pacing for too much of it; a
# malformed huge value is capped here too.
RAMP_TEMPO_MAX_SECONDS = 15.0

# Per-step audio transition for an ``ease`` ramp (#1122 review): the ease curve
# applies a small tempo step every ~100ms loop tick, so each step's ChangeTempo
# glides over roughly one tick rather than a full ramp — otherwise every tick
# stacks an overlapping long transition chasing a stale target. A ``linear`` ramp
# is one intensity segment with one glide instead (#1833).
RAMP_TEMPO_STEP_TRANSITION = 0.12


def _shape_progress(progress: float, curve: str) -> float:
    """Map linear progress in (0, 1) through the ramp curve (linear or smoothstep ease)."""
    if curve == RAMP_CURVE_EASE:
        return progress * progress * (3.0 - 2.0 * progress)
    return progress


def tempo_from_intensity(intensity: float) -> float:
    """Music ratio for an intensity (UC-031 BR-005): 1.0 + 0.3 * i, i clamped to [0, 1]."""
    clamped = max(0.0, min(1.0, intensity))
    return SLOW_MUSIC_SPEED + (FAST_MUSIC_SPEED - SLOW_MUSIC_SPEED) * clamped


def intensity_from_tempo(tempo: float) -> float:
    """Inverse of :func:`tempo_from_intensity`, clamped to [0, 1]."""
    speed_range = FAST_MUSIC_SPEED - SLOW_MUSIC_SPEED
    if speed_range <= 0:
        return 0.0
    return max(0.0, min(1.0, (tempo - SLOW_MUSIC_SPEED) / speed_range))


def resolve_ramp_seconds(value: object, default: float) -> float:
    """Validate a ``tempo.ramp_*_seconds`` flag value (#1833).

    A non-number, a bool, NaN / infinity or a value <= 0 is malformed and gives
    ``default``; any other value is clamped to [RAMP_SECONDS_MIN, RAMP_SECONDS_MAX].
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0.0:
        return default
    return max(RAMP_SECONDS_MIN, min(RAMP_SECONDS_MAX, seconds))


# Player-activated shield charge (#1837, FR-160; UC-040 A30-A32, BR-019-BR-021;
# UC-030 A6, BR-005). The game flag ``shield.activation`` picks how a grant_shield
# lands: ``instant`` (today's shield) or ``trigger`` (a charge the player activates
# with the trigger). Both flags are read per grant, never per frame.
SHIELD_ACTIVATION_INSTANT = "instant"
SHIELD_ACTIVATION_TRIGGER = "trigger"
# Hold mode (#2096, FR-161; UC-030 A7, UC-040 A36-A39, BR-024-BR-027): a pool of
# shield energy that protects while the trigger is held. Latched per game.
SHIELD_ACTIVATION_HOLD = "hold"
DEFAULT_SHIELD_ACTIVATION = SHIELD_ACTIVATION_INSTANT
DEFAULT_CHARGE_EXPIRY_SECONDS = 20.0
CHARGE_EXPIRY_MAX_SECONDS = 120.0
# The controller manager caps a charge layer at 120 s (CHARGE_MAX_DURATION_MS);
# the coordinator re-sends the layer this long before it runs out (BR-021).
SHIELD_CHARGE_LAYER_MAX_SECONDS = 120.0
SHIELD_CHARGE_RESEND_MARGIN_SECONDS = 10.0
# shield.charge_outcome values (telemetry.md §3.2) and player_shield_charge_ended reasons.
CHARGE_OUTCOME_ACTIVATED = "activated"
CHARGE_OUTCOME_EXPIRED = "expired"
CHARGE_OUTCOME_DEATH = "death"
CHARGE_OUTCOME_GAME_END = "game_end"


def resolve_shield_activation(value: object) -> str:
    """Validate a ``shield.activation`` flag value: ``trigger``, ``hold`` or else ``instant`` (UC-040 BR-019)."""
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in (SHIELD_ACTIVATION_TRIGGER, SHIELD_ACTIVATION_HOLD):
            return mode
    return SHIELD_ACTIVATION_INSTANT


def resolve_charge_expiry(value: object) -> float:
    """Validate a ``shield.charge_expiry_seconds`` flag value (flags.md §4).

    A non-number, a bool, NaN or a negative value gives the default (20 s); any
    other value is clamped to [0, CHARGE_EXPIRY_MAX_SECONDS]. 0 means never.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return DEFAULT_CHARGE_EXPIRY_SECONDS
    seconds = float(value)
    if math.isnan(seconds) or seconds < 0.0:
        return DEFAULT_CHARGE_EXPIRY_SECONDS
    return min(CHARGE_EXPIRY_MAX_SECONDS, seconds)


# Hold-mode flags (flags.md §4, UC-040 BR-024): key -> (default, min, max). The
# ranges equal the registry bounds (flags.md §13.3), so a stage overlay the
# registry accepts is never clamped here.
SHIELD_HOLD_FLAGS: dict[str, tuple[float, float, float]] = {
    "shield.energy_max_seconds": (5.0, 1.0, 10.0),
    "shield.energy_budget_seconds": (15.0, 3.0, 30.0),
    "shield.energy_initial_seconds": (0.0, 0.0, 5.0),
    "shield.recharge_per_second": (0.0, 0.0, 0.5),
    "shield.min_burst_seconds": (0.3, 0.1, 1.0),
}
# A grant that could add less than this is refused as shield_pool_full (A37).
SHIELD_HOLD_POOL_FULL_ROOM_SECONDS = 0.05
# shield.hold_outcome values (telemetry.md §3.2) and game_shield_hold_activations_total{outcome}.
HOLD_OUTCOME_RELEASED = "released"
HOLD_OUTCOME_EMPTY = "empty"
HOLD_OUTCOME_BUDGET_EXHAUSTED = "budget_exhausted"
HOLD_OUTCOME_DEATH = "death"
HOLD_OUTCOME_GAME_END = "game_end"
SHIELD_REFUSED_POOL_FULL = "shield_pool_full"
SHIELD_REFUSED_BUDGET_EXHAUSTED = "shield_budget_exhausted"


def resolve_hold_value(value: object, default: float, low: float, high: float) -> float:
    """Validate a hold-mode flag value (flags.md §4): malformed gives ``default``, else clamped to [low, high]."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    number = float(value)
    if not math.isfinite(number):
        return default
    return max(low, min(high, number))


@dataclass(frozen=True)
class ShieldHoldParams:
    """The hold-mode values a game latched at the start of its loop (UC-040 BR-024, BR-027)."""

    cap: float
    budget: float
    initial: float
    recharge: float
    min_burst: float


# Agent shield limits per player per game (#2044, UC-040 A34, BR-022): the
# coordinator's backstop. Operator flags in the `agent` set, read per grant.
DEFAULT_MAX_SHIELDS_PER_PLAYER_PER_GAME = 3
DEFAULT_SHIELD_COOLDOWN_SECONDS = 15
SHIELD_REFUSED_CAP = "shield_cap_reached"
SHIELD_REFUSED_COOLDOWN = "shield_cooldown"


def resolve_shield_limit(value: object, default: int) -> int:
    """Validate a shield-limit flag value (flags.md §3): a negative or non-int value gives the default."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


@dataclass(frozen=True)
class IntensityRamp:
    """One segment of the tempo intensity (#1833, UC-031 BR-005). Immutable; replaced per segment.

    ``value_at`` is a pure function of the clock, so thresholds read it every frame
    without awaiting anything, and a stalled music loop can never stall them.
    """

    start: float
    target: float
    start_time: float
    duration: float
    curve: str = RAMP_CURVE_LINEAR

    @classmethod
    def hold(cls, intensity: float, now: float = 0.0) -> "IntensityRamp":
        """A finished ramp that holds ``intensity`` (clamped to [0, 1])."""
        clamped = max(0.0, min(1.0, intensity))
        return cls(start=clamped, target=clamped, start_time=now, duration=0.0)

    @property
    def end_time(self) -> float:
        return self.start_time + self.duration

    def value_at(self, now: float) -> float:
        """Intensity at ``now``: start before the segment, target after it, clamped to [0, 1]."""
        if self.duration <= 0:
            value = self.target
        else:
            progress = (now - self.start_time) / self.duration
            if progress <= 0.0:
                value = self.start
            elif progress >= 1.0:
                value = self.target
            else:
                value = self.start + (self.target - self.start) * _shape_progress(progress, self.curve)
        return max(0.0, min(1.0, value))

    def is_complete(self, now: float) -> bool:
        """True once the segment has reached (and holds) its target."""
        return self.duration <= 0 or now >= self.end_time


@dataclass(frozen=True)
class RampDescriptor:
    """A scheduled tempo curve (#1117). Immutable; replaced wholesale on a new ramp.

    Attributes:
        start_tempo: Music speed when the ramp began (the interpolation origin).
        target_tempo: Clamped destination tempo, held once reached.
        start_time: ``time.time()`` when the ramp began.
        duration: Ramp length in seconds (> 0, capped at RAMP_TEMPO_MAX_SECONDS).
        curve: Interpolation shape — ``linear`` or ``ease`` (smoothstep).
    """

    start_tempo: float
    target_tempo: float
    start_time: float
    duration: float
    curve: str

    def tempo_at(self, now: float) -> float:
        """Interpolated tempo at ``now``; clamped to the target once complete."""
        if self.duration <= 0:
            return self.target_tempo
        progress = (now - self.start_time) / self.duration
        if progress <= 0.0:
            return self.start_tempo
        if progress >= 1.0:
            return self.target_tempo
        # ease = smoothstep: ease in and out, monotone, hits 0/1 at the endpoints.
        return self.start_tempo + (self.target_tempo - self.start_tempo) * _shape_progress(progress, self.curve)

    def is_complete(self, now: float) -> bool:
        """True once the ramp has reached (and should hold at) its target."""
        return self.duration <= 0 or (now - self.start_time) >= self.duration


def parse_ramp_tempo_value(value: object) -> RampDescriptor | None:
    """Parse a ``ramp_tempo`` directive ``"<target>:<seconds>:<curve>"`` (#1117).

    Returns a partially-filled :class:`RampDescriptor` (``start_tempo`` /
    ``start_time`` are 0.0 placeholders — the handler fills them from the live
    game) on a VALID directive, or ``None`` for any malformed / neutral input so
    the caller degrades to a safe no-op (never dispatches garbage). Validation:

    * exactly three ``:``-delimited fields;
    * target is a finite number, clamped to ``[SLOW_MUSIC_SPEED, FAST_MUSIC_SPEED]``;
    * seconds is a finite number ``> 0``, capped at :data:`RAMP_TEMPO_MAX_SECONDS`;
    * curve is one of :data:`_VALID_RAMP_CURVES`.

    ``"none"`` / ``""`` (the neutral value) and any parse failure return ``None``.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text == "" or text.lower() == "none":
        return None
    parts = text.split(":")
    if len(parts) != 3:
        return None
    target_raw, seconds_raw, curve_raw = (p.strip() for p in parts)
    curve = curve_raw.lower()
    if curve not in _VALID_RAMP_CURVES:
        return None
    try:
        target = float(target_raw)
        seconds = float(seconds_raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(target) or not math.isfinite(seconds):
        return None
    if seconds <= 0.0:
        return None
    seconds = min(seconds, RAMP_TEMPO_MAX_SECONDS)
    target = max(SLOW_MUSIC_SPEED, min(FAST_MUSIC_SPEED, target))
    return RampDescriptor(
        start_tempo=0.0,  # filled by the handler from the live music_speed
        target_tempo=target,
        start_time=0.0,  # filled by the handler from time.time()
        duration=seconds,
        curve=curve,
    )


# Span attribute keys (S1192 - avoid duplicate string literals)
GAME_MODE_ATTR = "game.mode"

# Log messages (S1192 - avoid duplicate strings)
_MSG_COUNTDOWN_INTERRUPTED = "Countdown interrupted - game no longer running"

# Threshold Scaling: LERP approach (matches original JoustMania)
# ===============================================================
# Uses linear interpolation between slow/fast threshold arrays based on music speed.
# This allows fine-tuned per-sensitivity-level behavior as music tempo changes.
#
# Formula: threshold = lerp(SLOW[sens], FAST[sens], intensity)
# Where intensity is the game's tempo intensity in [0, 1] (#1833, UC-031 BR-005);
# the music ratio is derived from it: 1.0 + 0.3 * intensity.
#
# Example at MEDIUM (sens=2), music at 1.15x (50% between slow/fast):
#   warning = lerp(1.6, 1.9, 0.5) = 1.75g
#   death   = lerp(1.8, 2.8, 0.5) = 2.3g

# Music timing intervals (seconds) - how long before next tempo change
MIN_MUSIC_FAST_TIME = 4  # Minimum time at fast speed
MAX_MUSIC_FAST_TIME = 8  # Maximum time at fast speed
MIN_MUSIC_SLOW_TIME = 10  # Minimum time at slow speed
MAX_MUSIC_SLOW_TIME = 23  # Maximum time at slow speed

# End game timing (more frequent changes as game progresses)
END_MIN_MUSIC_FAST_TIME = 6
END_MAX_MUSIC_FAST_TIME = 10
END_MIN_MUSIC_SLOW_TIME = 8
END_MAX_MUSIC_SLOW_TIME = 12

# Volume levels — module constants double as flagd defaults and as the
# revert target for the volume_override intervention (interventions.py).
GAME_VOLUME = 0.7
COUNTDOWN_MUSIC_VOLUME = 0.15  # Soft background music during countdown
# Upper bound on the wait for a spoken instruction clip (UC-020 A5), so a bad
# duration from the audio service can never stall a game start.
MAX_INSTRUCTIONS_WAIT_SECONDS = 60.0
# How often the instruction wait checks for a force-end.
INSTRUCTIONS_POLL_SECONDS = 0.1
# Deadline for StopSound after a force-end, so a wedged audio service cannot stall teardown (#1586).
STOP_SOUND_TIMEOUT_SECONDS = 2.0


def _read_volume_flag(flag_name: str, default: float) -> float:
    """Read a per-channel volume from the flagd user domain (F7, #766).

    Validates the value is a number in [0.0, 1.0]; falls back to the hardcoded
    default on malformed values or any read error, so promotion stays
    behavior-neutral. Read at game init only (init-frozen).
    """
    try:
        from openfeature.evaluation_context import EvaluationContext

        from lib.feature_flags import get_flag_client, init_flag_domain

        init_flag_domain("user")
        client = get_flag_client("user")
        value = client.get_float_value(flag_name, default, EvaluationContext())
        if not isinstance(value, int | float) or isinstance(value, bool) or not (0.0 <= value <= 1.0):
            logger.warning(f"Malformed volume {flag_name}={value!r}, using default {default}")
            return default
        return float(value)
    except Exception as e:
        logger.warning(f"Failed to read volume {flag_name}, using default {default}: {e}")
        return default


# Threshold arrays from original JoustMania (in g-force units, 1.0 = 1g)
# PSMove accelerometer returns g-force values (standing still = ~1.0 on Z axis)
#
# Uses LERP between slow/fast thresholds based on music speed:
#   threshold = lerp(SLOW[sens], FAST[sens], music_speed_percent)
#
# Index: 0=ULTRA_SLOW, 1=SLOW, 2=MEDIUM, 3=FAST, 4=ULTRA_FAST
SLOW_WARNING = [1.2, 1.3, 1.6, 2.0, 2.5]  # Warning thresholds when music is slow
SLOW_MAX = [1.3, 1.5, 1.8, 2.5, 3.2]  # Death thresholds when music is slow
FAST_WARNING = [1.4, 1.6, 1.9, 2.7, 2.8]  # Warning thresholds when music is fast
FAST_MAX = [1.6, 1.8, 2.8, 3.2, 3.5]  # Death thresholds when music is fast

# Number of sensitivity levels (0=ULTRA_SLOW .. 4=ULTRA_FAST); every threshold
# array must carry exactly this many entries.
SENSITIVITY_LEVELS = 5


def _valid_threshold_row(row) -> bool:
    """A threshold array must hold exactly SENSITIVITY_LEVELS numeric entries."""
    if not isinstance(row, (list, tuple)) or len(row) != SENSITIVITY_LEVELS:
        return False
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in row)


def resolve_base_thresholds(flag_value: dict) -> dict[str, list]:
    """
    Validate the ``game.thresholds`` object flag and resolve the four arrays.

    Each of slow_warning/slow_max/fast_warning/fast_max must be a 5-element
    numeric array, and per sensitivity row warning < max (slow and fast). On
    ANY malformed entry the corresponding hardcoded module constant is used as
    the fallback source of truth, keeping the promotion behavior-neutral.

    Args:
        flag_value: Object value from the ``thresholds`` flag (may be partial/invalid)

    Returns:
        Dict with keys slow_warning, slow_max, fast_warning, fast_max -> lists.
    """
    defaults = {
        "slow_warning": list(SLOW_WARNING),
        "slow_max": list(SLOW_MAX),
        "fast_warning": list(FAST_WARNING),
        "fast_max": list(FAST_MAX),
    }
    if not isinstance(flag_value, dict):
        return defaults

    resolved = {}
    for key in ("slow_warning", "slow_max", "fast_warning", "fast_max"):
        candidate = flag_value.get(key)
        resolved[key] = list(candidate) if _valid_threshold_row(candidate) else defaults[key]

    # Per-row sanity: warning must be strictly below max at every sensitivity.
    # If any row violates it, fall back to the matching default pair so we never
    # ship an unkillable or instant-death configuration.
    for warn_key, max_key in (("slow_warning", "slow_max"), ("fast_warning", "fast_max")):
        if any(w >= m for w, m in zip(resolved[warn_key], resolved[max_key], strict=False)):
            resolved[warn_key] = defaults[warn_key]
            resolved[max_key] = defaults[max_key]

    return resolved


def resolve_mode_thresholds(flag_value: dict, default_table: dict) -> dict:
    """
    Validate a per-mode override flag (zombie/werewolf) -> ``{Sensitivity: (warn, max)}``.

    The flag carries two 5-element arrays ``warning`` and ``max`` (indexed by
    sensitivity 0..4). On any malformed entry or warning>=max row, the supplied
    ``default_table`` (the mode's hardcoded dict) is returned unchanged.

    Args:
        flag_value: Object value from ``<mode>.thresholds`` flag
        default_table: Hardcoded ``{Sensitivity: (warn, max)}`` fallback

    Returns:
        A ``{Sensitivity: (warn, max)}`` dict.
    """
    if not isinstance(flag_value, dict):
        return default_table

    warning = flag_value.get("warning")
    death = flag_value.get("max")
    if not _valid_threshold_row(warning) or not _valid_threshold_row(death):
        return default_table
    if any(w >= m for w, m in zip(warning, death, strict=False)):
        return default_table

    return {Sensitivity(i): (warning[i], death[i]) for i in range(SENSITIVITY_LEVELS)}


def resolve_non_negative_duration(value, default: float) -> float:
    """
    Validate a promoted duration flag (#766 F2): finite, numeric, ``>= 0``.

    Durations of exactly ``0`` are allowed (e.g. "no grace", "no spawn
    protection"); negatives, non-numbers and NaN/inf fall back to ``default``
    so the promotion stays behavior-neutral.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return float(value) if value >= 0 else default


@dataclass
class MusicWindows:
    """Tempo-change scheduling windows (seconds) for the music loop.

    Each field is the min/max delay before the next tempo change in a given
    phase. The loop draws a uniform delay in ``[min, max]`` (lerping between the
    early-game and end-game pairs as players are eliminated). #766 F3 promoted
    these from the module constants to the ``game.windows`` object flag.

    #766 F6 seam (LIVE ``pacing_profile`` intervention): the resolved windows
    live on ``self.music_windows`` (one ``MusicWindows`` instance). F6's live
    handler must build a new ``MusicWindows`` from the chosen preset and assign
    it atomically — ``self.music_windows = new_windows`` — so the music loop,
    which reads ``self.music_windows`` fresh on each ``_get_music_change_time``
    call, picks up the swap on the next tempo change without tearing (the field
    reference flips in a single bytecode store; never mutate the fields in
    place). Init reads it once (init-frozen calibration); F6 owns the live path.
    """

    fast_min: float
    fast_max: float
    slow_min: float
    slow_max: float
    end_fast_min: float
    end_fast_max: float
    end_slow_min: float
    end_slow_max: float

    @classmethod
    def defaults(cls) -> "MusicWindows":
        """Build a MusicWindows from the hardcoded module constants."""
        return cls(
            fast_min=MIN_MUSIC_FAST_TIME,
            fast_max=MAX_MUSIC_FAST_TIME,
            slow_min=MIN_MUSIC_SLOW_TIME,
            slow_max=MAX_MUSIC_SLOW_TIME,
            end_fast_min=END_MIN_MUSIC_FAST_TIME,
            end_fast_max=END_MAX_MUSIC_FAST_TIME,
            end_slow_min=END_MIN_MUSIC_SLOW_TIME,
            end_slow_max=END_MAX_MUSIC_SLOW_TIME,
        )


# Maps each MusicWindows field to its game.windows flag key (snake_case after
# the module constants) and the constant used as the per-field fallback.
_MUSIC_WINDOW_FIELDS = (
    ("fast_min", "min_music_fast_time", MIN_MUSIC_FAST_TIME),
    ("fast_max", "max_music_fast_time", MAX_MUSIC_FAST_TIME),
    ("slow_min", "min_music_slow_time", MIN_MUSIC_SLOW_TIME),
    ("slow_max", "max_music_slow_time", MAX_MUSIC_SLOW_TIME),
    ("end_fast_min", "end_min_music_fast_time", END_MIN_MUSIC_FAST_TIME),
    ("end_fast_max", "end_max_music_fast_time", END_MAX_MUSIC_FAST_TIME),
    ("end_slow_min", "end_min_music_slow_time", END_MIN_MUSIC_SLOW_TIME),
    ("end_slow_max", "end_max_music_slow_time", END_MAX_MUSIC_SLOW_TIME),
)

# (min_field, max_field) pairs that must satisfy min <= max for the windows to
# be usable. If any pair is violated after resolution we discard the whole flag
# value and fall back to the defaults (keeping promotion behavior-neutral).
_MUSIC_WINDOW_PAIRS = (
    ("fast_min", "fast_max"),
    ("slow_min", "slow_max"),
    ("end_fast_min", "end_fast_max"),
    ("end_slow_min", "end_slow_max"),
)


def _positive_number(value) -> bool:
    """A music window must be a finite, strictly-positive (>0) number, not bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return False
    return value > 0


def resolve_music_windows(flag_value: dict) -> MusicWindows:
    """
    Validate the ``game.windows`` object flag and resolve a :class:`MusicWindows`.

    Each of the eight window values must be a finite, strictly-positive number,
    and every (min, max) pair must satisfy ``min <= max``. On ANY malformed
    value or violated ordering the ENTIRE flag value is rejected in favour of
    the module-constant defaults, keeping the promotion behavior-neutral (a
    half-applied window set could distort the fast/slow rhythm unexpectedly).

    Args:
        flag_value: Object value from the ``windows`` flag (may be partial/invalid)

    Returns:
        A validated :class:`MusicWindows` (defaults on any failure).
    """
    defaults = MusicWindows.defaults()
    if not isinstance(flag_value, dict):
        return defaults

    resolved: dict[str, float] = {}
    for field, key, _fallback in _MUSIC_WINDOW_FIELDS:
        candidate = flag_value.get(key)
        if not _positive_number(candidate):
            return defaults  # any malformed value -> whole-set fallback
        resolved[field] = float(candidate)

    for min_field, max_field in _MUSIC_WINDOW_PAIRS:
        if resolved[min_field] > resolved[max_field]:
            return defaults  # any inverted window -> whole-set fallback

    return MusicWindows(**resolved)


# Warning feedback duration (seconds) - flash + rumble time
# This is purely visual feedback, NOT protection (player can still die during warning)
WARNING_DURATION = 0.5

# UC-020 BR-002: death_grace_period_seconds has no effect in FFA (no respawn).
# Post-respawn/conversion invincibility window (no death or warning during this
# time). This is a RESPAWN-MODE concept ONLY: it protects a player who has just
# respawned, been revived, or been converted (e.g. zombie infection) from being
# instantly re-killed by the same motion spike. FFA (ffa.py) has NO respawn — a
# death is permanent elimination — so this knob and the ``game.death_grace_period_seconds``
# flag that overrides it have NO EFFECT on FFA gameplay. Do not treat
# ``death_grace_period_seconds`` as an FFA difficulty/pacing lever (see #1090).
# The unrelated STARTUP grace (residual-motion guard at game start) is a separate,
# fixed 0.3s window applied to every mode and is intentionally NOT derived from this.
DEATH_GRACE_PERIOD = 2.0  # seconds of invincibility after respawn (respawn modes only)

# Log at import time to verify correct version is deployed
logger.info(f"base.py loaded: WARNING_DURATION={WARNING_DURATION}s")


def player_identity_attributes(serial: str, name: str = "") -> dict[str, str]:
    """Span-event attributes naming the player an intervention was applied to (#1295).

    ``player.serial`` is the same key the player span carries (and the agent puts on
    its targeted ``agent.decision`` spans), so one Jaeger search finds both sides;
    ``player.name`` is the display name (e.g. "Calm Lion") and is omitted until the
    controller reported one.
    """
    attrs = {"player.serial": serial}
    if name:
        attrs["player.name"] = name
    return attrs


@dataclass
class Player:
    """Represents a player in the game."""

    serial: str
    name: str = ""  # Human-readable controller name (e.g., "Blue Phoenix")
    team: int = 0
    alive: bool = True
    color: tuple = (255, 255, 255)
    last_accel_mag: float = 0.0
    # Latest battery percentage (0-100) seen on this player's gameplay frames,
    # or None until the first frame carrying a battery reading arrives. Sourced
    # from GameplayData.battery and normalized via lib.controller_constants
    # (#798). Read by the InterventionManager battery guard.
    battery_pct: float | None = None
    # Exponential moving average of acceleration (from original JoustMania)
    # EMA smooths sensor noise and prevents false positives from single-frame spikes
    smoothed_accel: float = 0.0
    span: trace.Span | None = None  # OpenTelemetry span for this player's lifecycle
    # Grace period: no death or warning checks until this timestamp
    # Used for respawn modes (e.g., after team swap or zombie respawn)
    grace_until: float = 0.0
    # Warning state: when > 0, player is in warning feedback (flash + rumble)
    # This is purely visual - player CAN still die during warning (matches original)
    warning_until: float = 0.0
    # Analytics tracker for this player (initialized when game starts)
    analytics: "PlayerAnalytics | None" = None
    # Per-player sensitivity multiplier (Phase 3: Per-Player Sensitivity Infrastructure)
    # 1.0 = default, >1.0 = more sensitive (easier to die), <1.0 = less sensitive (harder to die)
    # Thresholds are divided by this factor: higher factor = lower threshold = easier to trigger
    sensitivity_factor: float = 1.0
    # Per-player MULTIPLICATIVE handicap on the death/warning threshold (#1107,
    # #1103 MVP action 1). A SEPARATE agent-only knob that COMPOSES with (does not
    # replace) sensitivity_factor. Intent-framed and OPPOSITE in direction to
    # sensitivity_factor: >1.0 raises the effective threshold (harder to die ->
    # "help"); <1.0 lowers it (easier to die -> "rein in"). Clamped [0.5, 2.0] in
    # _compute_effective_thresholds. 1.0 = neutral (no effect). Shadow-game-only
    # initially via the interventions_allowed allow-list gate.
    handicap_factor: float = 1.0
    # Time-boxed toughness boost (#1129, #1103 Phase 2). When ``toughness_boost_until``
    # is in the future, ``toughness_boost_factor`` acts as a TEMPORARY handicap
    # override that COMPOSES with ``handicap_factor`` by taking the MAX of the two
    # (so a standing set_player_handicap is never weakened, only strengthened for
    # the window) — see ``_compute_effective_thresholds``. A large boost (~2.0)
    # makes the player MUCH harder to eliminate but NOT immune (vs grant_shield's
    # total grace_until immunity): the combined factor is still clamped [0.5, 2.0].
    # On expiry the boost simply stops applying (no reset task needed) and the
    # effective handicap falls back to the standing ``handicap_factor``. 0.0 / past
    # deadline = inactive (no effect). Shadow-game-only via the allow-list gate.
    toughness_boost_until: float = 0.0
    toughness_boost_factor: float = 1.0
    # Time-boxed SOFT-PENALTY tighten (#1134, #1103 Phase 3). The deliberate
    # MIRROR of toughness_boost: where toughness_boost STRENGTHENS protection (max,
    # factor >= 1.0), a tighten WEAKENS it for a short window (factor < 1.0),
    # making the player TEMPORARILY EASIER to eliminate — graduated pressure, not
    # guaranteed elimination. While ``soft_penalty_until`` is in the future,
    # ``soft_penalty_factor`` composes into the effective handicap by taking the
    # MIN (so it can cut through even a toughness boost — pressure overrides
    # protection), then the existing [0.5, 2.0] clamp keeps the threshold finite
    # (never instant-death). On expiry the factor simply stops applying (no reset
    # task needed) and the effective handicap falls back to the standing/shielded
    # value. 1.0 / past deadline = inactive (no effect). Shadow-game-only via the
    # allow-list gate. See ``_compute_effective_thresholds`` for the precedence.
    soft_penalty_until: float = 0.0
    soft_penalty_factor: float = 1.0
    # Time-boxed AUTO-RUBBERBAND boost (#1143, #1103 Phase 3). A SINGLE agent
    # decision (auto_rubberband=gentle|strong) is expanded by the coordinator
    # across players based on the LIVE skill gap: the trailing player(s) — those
    # closest to their death threshold — receive a temporary handicap BOOST that
    # COMPRESSES the gap to the leader. Like toughness_boost it STRENGTHENS (boost
    # >= 1.0, composed by MAX), but unlike it the boost is computed per-player
    # from the gap and is CAPPED so the boosted laggard can never overtake the
    # current leader (never inverts standings). While ``rubberband_until`` is in
    # the future, ``rubberband_boost`` composes into the effective handicap by the
    # MAX (alongside toughness_boost), still clamped [0.5, 2.0] so even a maxed
    # boost is "harder to die", never immune / never instant-death. On expiry the
    # boost silently stops applying (no reset task needed). 1.0 / past deadline =
    # inactive. Shadow-game-only via the allow-list gate.
    rubberband_until: float = 0.0
    rubberband_boost: float = 1.0
    # Shield charge (#1837, UC-040 A30-A32, BR-020): at most one per player.
    # ``shield_charge_seconds`` > 0 means a charge is held; it becomes the shield
    # duration on activation. The expiry is fixed at grant time (0 = never).
    # ``shield_charge_layer_until`` is when the last SHIELD_CHARGE LED command runs
    # out on the controller, so the frame path re-sends it in time (BR-021).
    shield_charge_seconds: float = 0.0
    shield_charge_granted_at: float = 0.0
    shield_charge_expires_at: float = 0.0
    shield_charge_layer_until: float = 0.0
    _shield_charge_span: trace.Span | None = None
    # Trigger edge detection (UC-030 BR-005): the trigger of the previous frame,
    # and whether the trigger read released once in the game loop (so a press held
    # or latched from the lobby is never a press).
    trigger_prev: bool = False
    trigger_armed: bool = False
    # Shield hold (#2096, UC-030 A7, UC-040 BR-024): only used in a game latched to
    # hold. The energy is a function of time, so nothing ticks: ``hold_energy`` is
    # the stored value at ``hold_energy_at`` (0 = the recharge clock has not
    # started). While an activation runs (``hold_started_at`` > 0) the energy and
    # the usable energy at its start are kept, and ``hold_until`` is the protection
    # deadline: the energy's end while held, start + cost after the release.
    hold_energy: float = 0.0
    hold_energy_at: float = 0.0
    hold_budget_used: float = 0.0
    hold_started_at: float = 0.0
    hold_until: float = 0.0
    hold_pressed: bool = False
    hold_energy_start: float = 0.0
    hold_budget_start: float = 0.0
    hold_usable_start: float = 0.0
    hold_last_pressed_at: float = 0.0
    hold_min_burst_applied: bool = False
    # UC-030 BR-009: the first unprotected frame after an activation primes the EMA
    # with min(raw, warning threshold) instead of the raw sample.
    hold_prime_cap: bool = False
    hold_budget_exhausted_noted: bool = False
    _shield_hold_span: trace.Span | None = None
    # Per-player countdown span (created before countdown, ended after)
    _countdown_span: trace.Span | None = None
    # Health tracking (#571: controller health on player_lifecycle spans)
    total_poll_drops: int = 0
    total_poll_errors: int = 0
    total_led_failures: int = 0
    _poll_degraded_span: trace.Span | None = None
    _led_degraded_span: trace.Span | None = None
    # Rolling window rate detection for poll drops
    _health_window_start: float | None = None  # monotonic time at window start
    _health_window_drops: int = 0  # cumulative drops at window start
    _health_drop_rate: float = 0.0  # current calculated drop rate (drops/sec)


@dataclass
class Phase:
    """Represents a game phase with a name and execution method."""

    name: str
    execute: callable


class PollDegradationError(Exception):
    """Synthetic exception for poll degradation span events.

    Not raised for control flow — only passed to span.record_exception()
    so failure analysis can group and alert on poll health issues
    by exception type (PollDegradationError) and message.
    """


class GameState(Enum):
    """Game lifecycle states."""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    ENDING = "ending"
    ENDED = "ended"


class BaseGameMode(ABC):
    """
    Abstract base class for all game modes.

    Provides:
    - Template method run() for consistent game lifecycle orchestration
    - Concrete methods for shared operations (settings, countdown, controller processing)
    - Abstract methods for game-specific behavior (team assignment, win conditions, etc.)
    - Consistent OpenTelemetry span hierarchy across all game modes

    Subclasses must implement:
    - get_game_name() - return game mode identifier
    - _initialize_players_impl() - assign players to teams
    - _create_player_spans() - create lifecycle spans (flat vs hierarchical)
    - _check_win_condition() - determine if game should end
    - _kill_player_impl() - handle player death (stay dead vs respawn)
    - _get_additional_phases() - return extra phases (e.g., team_formation)
    - _end_game_impl() - cleanup and declare winner
    """

    # UC-040 BR-019: ``shield.activation = trigger`` is an allow-list. Only a mode
    # that sets this (Free-for-All) turns a grant_shield into a charge; every other
    # mode, and every shadow game, always takes the instant path.
    SHIELD_TRIGGER_ALLOWED: bool = False

    def __init__(
        self,
        controller_manager_client: Any,  # controller_manager_pb2_grpc.ControllerManagerServiceStub
        event_publisher: Callable[[str, dict[str, str]], Any],
        audio_client: Any | None = None,  # audio_pb2_grpc.AudioServiceStub
        game_id: str = "",
        initial_players: list | None = None,  # List of Player protobuf messages
        sensitivity: int = 2,  # 0-4, passed from StartGameConfig (default MEDIUM)
        rng_seed: int = 0,  # #1003 paired-CRN seed; 0 => entropy (today's behavior)
    ) -> None:
        """
        Initialize base game mode (Phase 33 - added type hints).

        Args:
            controller_manager_client: gRPC stub for ControllerManager service
            event_publisher: Async callback function to publish game events (event_type, data)
            audio_client: gRPC stub for Audio service (Phase 29)
            initial_players: Optional list of Player protobuf messages from StartGame RPC
            game_id: Unique identifier for this game instance
            sensitivity: Sensitivity level 0-4 (passed from StartGameConfig)
        """
        self.controller_client = controller_manager_client
        self.event_publisher = event_publisher
        self.audio_client = audio_client

        # Per-instance RNG (#1003 — the load-bearing CRN isolation). Shadow games
        # run concurrently in ONE game_coordinator interpreter, so global
        # random.seed() would corrupt a concurrent game's in-flight sequence.
        # Every random draw in this game (and its subclasses) MUST go through
        # self._rng so two seed-matched games in an (experimental, control) pair
        # produce identical shuffles/samples/choices — the only difference being
        # the flag under test. rng_seed == 0 (real games, unbound shadow games, or
        # any pre-#1003 caller) maps to random.Random(None) == entropy: byte-for-
        # byte today's behavior. A CI grep gate forbids bare ``random.`` under
        # services/game_coordinator/games to keep this invariant from regressing.
        self._rng = random.Random(rng_seed if rng_seed else None)

        # Game ID - subclasses can override with mode-specific prefix
        if not game_id:
            mode_prefix = self.get_game_name().lower().replace(" ", "_")
            game_id = f"{mode_prefix}_{int(time.time())}"
        self.game_id = game_id

        # Multi-session bookkeeping (#775). The servicer overrides these on the
        # game instance after construction based on the owning GameSession. They
        # default to the single-game/primary values so a standalone game (and
        # every existing test) behaves exactly as before:
        #   game_kind: "primary" or "shadow" — labels lifecycle metrics.
        #   experiment_id / arm: experiment attribution within a shadow session
        #     (#975); empty for a non-experiment game. The servicer overrides
        #     these from the owning GameSession (set by #976's spawn binding).
        self.game_kind = "primary"
        self.experiment_id = ""
        self.arm = ""
        # The flag transaction context _set_flag_eval_context set for this game's
        # run; intervention handlers resolve flags in it (flag_eval_context, #1970).
        self._flag_eval_context: EvaluationContext | None = None
        # Callback the owning GameSession sets so the game can publish its
        # gameplay_phase span_id to the trace-correlation gauge (#1195) when that
        # span opens — letting the agent re-parent its in-game decision chain under
        # gameplay_phase. Defaults to a no-op so a standalone game / every existing
        # test runs unchanged.
        self.on_gameplay_phase_open: Callable[[str], None] = lambda _span_id: None
        # Same for the game_cycle span (#1325): the agent's preferred parent for its
        # in-game spans (UC-040 BR-013). No-op by default.
        self.on_game_cycle_open: Callable[[str], None] = lambda _span_id: None

        # Game state
        self.state = GameState.IDLE
        self.players: dict[str, Player] = {}
        self.initial_players = initial_players  # Players from StartGame RPC
        self.start_time = None
        self.running = False
        # Disconnects seen by _warmup_ema(); _game_loop() runs them through the
        # death path before its first frame (UC-020 A2, #1582).
        self._warmup_disconnects: list[str] = []
        # Early stream drain (UC-021 A1, D-G10): a mode that opens its stream before
        # its additional phases reads and discards frames until the start.
        self._early_drain_task: asyncio.Task | None = None
        self._early_drain_stop = asyncio.Event()
        self._early_drain_disconnects: dict[str, None] = {}
        # Set by force_end(): the reason the game was stopped from outside, or
        # None for a game that ended on its own win condition (#1328).
        self.force_end_reason: str | None = None
        # The outcome, frozen once the game loop ends (win condition met, or the
        # loop stopped). A force-end that lands later (e.g. during the winner
        # rainbow) must not turn a real win into force_ended (#1328).
        self._latched_outcome: dict | None = None
        self._outcome_recorded = False
        # Set when run() exits by an exception: the run-end cleanup labels the
        # player spans it ends ``error`` (UC-030 BR-007, #1997).
        self._run_failed = False
        # The game root span (current when run() starts); the outcome lands on it.
        self._game_root_span: trace.Span | None = None
        self.gameplay_stream = None  # Phase 46: Bidirectional stream for feedback commands
        # Shield feedback still running per serial: wall-clock end time (#1301,
        # UC-040 A16/A17). Only a record for the game-end clear; no task.
        self._shield_feedback_until: dict[str, float] = {}
        # Agent shields per serial in this game (#2044, UC-040 BR-022): grants
        # counted and the end of the last agent shield (grant time + seconds; in
        # trigger mode from the charge grant). Per game-mode instance, so a new
        # game starts fresh; keyed by serial, so a revive does not reset them.
        self._agent_shield_grants: dict[str, int] = {}
        self._agent_shield_last_end: dict[str, float] = {}
        # Shield hold (#2096, UC-040 BR-027): latched once, at the start of the game
        # loop or at the first grant, whichever comes first. None = not a hold game.
        self._shield_hold: ShieldHoldParams | None = None
        self._shield_hold_latched = False
        # The mode the latch read, reused by the grant that triggered the latch, so
        # that grant reads shield.activation once (flags.md §4: one read per grant).
        self._shield_mode_at_latch: str | None = None
        # The mode a non-hold game latched (instant or trigger): a mid-game `hold`
        # read maps back to it, so a running game keeps its mode (UC-040 BR-027).
        self._shield_mode_at_start: str = SHIELD_ACTIVATION_INSTANT

        # Settings - sensitivity is now passed via StartGameConfig.
        #
        # Sensitivity arbitration (#816, M9 ownership model — epic #814):
        #   - ``baseline_sensitivity`` is the ADMIN's source of truth (pre-game
        #     lobby intent). The admin may update it mid-game via
        #     ``set_baseline_sensitivity`` (which invalidates any active agent
        #     override — a human baseline change wins).
        #   - ``sensitivity_override`` is the AGENT's bounded in-game override
        #     LAYER (``None`` = no override). The agent never persists into the
        #     baseline; clearing the override returns to the CURRENT baseline,
        #     not a stale game-start snapshot.
        #   - ``sensitivity`` (property below) is the EFFECTIVE level read by
        #     ``_compute_effective_thresholds`` and the metrics/spans: the
        #     override if active, otherwise the baseline.
        # Validate and convert to Sensitivity enum.
        if 0 <= sensitivity <= 4:
            self._baseline_sensitivity = Sensitivity(sensitivity)
        else:
            logger.warning(f"Sensitivity {sensitivity} out of range, using MEDIUM")
            self._baseline_sensitivity = Sensitivity.MEDIUM
        # No agent override at game start; effective == baseline.
        self._sensitivity_override: Sensitivity | None = None

        # #766 F1: death/warning threshold tables promoted to the ``game.thresholds``
        # object flag. Read ONCE here (init-frozen by design — no live re-evaluation);
        # malformed/missing values fall back to the module constants.
        flag_thresholds = read_object_flag("game", "thresholds", {}, game_id=self.game_id)
        tables = resolve_base_thresholds(flag_thresholds)
        self.slow_warning = tables["slow_warning"]
        self.slow_max = tables["slow_max"]
        self.fast_warning = tables["fast_warning"]
        self.fast_max = tables["fast_max"]

        # EMA filter weight (#766 F4, agent-domain `perception.ema_weight`).
        # READ ONCE HERE AT GAME INIT and frozen for the life of the game. It is
        # intentionally never re-evaluated mid-game: changing the smoothing
        # weight while running would invalidate the per-player movement variance
        # baseline that the agent's perception layer relies on (#722 §5 —
        # difficulty/filter changes "invalidate the variance baseline"). Falls
        # back to the hardcoded default (4.0) when flagd is unavailable.
        self._ema_weight = get_config_manager().read_ema_weight()

        # UC-020 BR-002: read here but consumed only by respawn modes; no effect in FFA.
        # #766 F2: post-respawn grace period promoted to ``game.death_grace_period_seconds``.
        # Read ONCE here (init-frozen); malformed/negative values fall back to the
        # DEATH_GRACE_PERIOD module constant (the source of truth).
        #
        # SCOPE (#1090): this is the post-respawn/conversion invincibility window for
        # RESPAWN modes only (zombie conversion + respawn, swapper team-swap). It is
        # CONSUMED by those modes (zombie.py / swapper.py set ``player.grace_until =
        # time.time() + self.death_grace_period``). FFA has no respawn, so this flag
        # has NO effect on FFA — it is NOT an FFA difficulty/pacing lever. The real
        # FFA difficulty/pacing levers are ``game.thresholds`` (death thresholds) and
        # ``game.windows`` (tempo cadence), both consumed per-frame in
        # ``_compute_effective_thresholds``.
        self.death_grace_period = resolve_non_negative_duration(
            read_float_flag("game", "death_grace_period_seconds", DEATH_GRACE_PERIOD, game_id=self.game_id),
            DEATH_GRACE_PERIOD,
        )

        # #766 F3: music tempo-change scheduling windows promoted to the
        # ``game.windows`` object flag (the ``pacing_profile`` presets live as
        # named variants). Read ONCE here (init-frozen calibration); malformed
        # values fall back to the module constants. F6 will add a LIVE
        # ``pacing_profile`` intervention that atomically swaps
        # ``self.music_windows`` mid-game — see MusicWindows for the seam.
        self.music_windows = resolve_music_windows(read_object_flag("game", "windows", {}, game_id=self.game_id))
        # #766 F6: the init-resolved windows are retained so the LIVE
        # ``pacing_profile`` intervention can restore them when it reverts to the
        # neutral profile. Never reassigned after init (the live handler swaps
        # ``self.music_windows`` only).
        self.init_music_windows = self.music_windows

        # #1109: tempo-schedule SOURCE mode (``game.tempo_schedule_mode``). Read
        # ONCE here for the initial value, then kept LIVE by the
        # PROVIDER_CONFIGURATION_CHANGED listener (TempoScheduleManager), which
        # atomically swaps ``self.tempo_schedule_mode`` mid-game exactly like the
        # F6 ``music_windows`` seam. The single pacing pathway
        # (``_decide_next_change_delay``) reads this attribute fresh on every
        # tempo-change decision, so flipping the flag switches the source LIVE
        # without a restart. Default/unknown => "rule" (default rule reproduces
        # today's random-window timing => behavior unchanged).
        self.tempo_schedule_mode = resolve_tempo_schedule_mode(
            read_string_flag("game", "tempo_schedule_mode", TEMPO_SCHEDULE_MODE_DEFAULT, game_id=self.game_id)
        )
        # #1109 agent-mode seam (#1103 follow-up): the next-change delay an agent
        # tempo directive (ramp_tempo) wants the schedule to honor. ``None`` =>
        # no directive present => "agent" mode falls back to the default rule.
        # Set by the agent primitive in a follow-up PR; never set in this PR.
        self.agent_tempo_next_delay: float | None = None
        # #1117 (#1103 MVP action 2): active ramp_tempo CURVE descriptor, or None
        # for no ramp (the default-safe state — behavior unchanged). When set, the
        # single tempo owner (``_check_music_speed``) interpolates the live tempo
        # toward the descriptor's target over its duration and holds there. Set by
        # the ramp_tempo handler; cleared by its revert handler. Lives ONLY on the
        # game object (bounded delta — never persisted to game.json, ownership §1).
        self.tempo_ramp: RampDescriptor | None = None

        # #766 F6: live global difficulty factor (analogue of per-player
        # ``sensitivity_factor``). Combined with the per-player factor in
        # ``_compute_effective_thresholds``; 1.0 is neutral (baseline behavior).
        # Set/cleared by the global_difficulty_factor intervention handler and
        # live-read every frame.
        self.global_difficulty_factor: float = 1.0

        # Phase 70: Music tempo control state
        self.music_track_id = None
        # #1833: the tempo intensity (UC-031 BR-005) is the source; music_speed is
        # derived from it (property below). Starts held at 0 (1.0x, slow table).
        self._intensity = IntensityRamp.hold(0.0)
        # False while the audio service has not accepted the current glide; the
        # music loop resends the remaining glide at _audio_retry_at.
        self._audio_synced = True
        self._audio_retry_at = 0.0
        self._audio_retry_backoff = AUDIO_TEMPO_RETRY_MIN_SECONDS
        # Rate limit for the failing-tick warning of _music_loop (#1841).
        self._tick_error_text: str | None = None
        self._tick_error_logged_at = 0.0
        self._tick_error_suppressed = 0
        # The linear ramp_tempo descriptor already turned into an intensity ramp
        # (identity check, so one descriptor sends exactly one glide).
        self._adopted_tempo_ramp: RampDescriptor | None = None
        # True while the override or ramp_tempo owns the intensity; when control
        # returns to the schedule the running segment is held where it is.
        self._agent_tempo_owned = False
        # True while a ramp ran at the last music tick, so the tick after it ends
        # still emits the final tempo / threshold gauges.
        self._ramp_metrics_open = False
        self.speed_up = True  # True = next change will speed up, False = slow down
        self.change_time = 0.0  # Time of next tempo change
        self.music_loop_task = None
        # Agent tempo override (#730 PR C). When set to a float (1.0-1.3), the
        # music loop adopts this tempo and SUSPENDS its own speed-up/slow-down
        # schedule (resolves the E1 race in 722-intervention-surface §3.1).
        # None = no override; the natural schedule runs. Set/cleared by the
        # music_tempo_override intervention handler; consumed in _check_music_speed.
        self.tempo_override: float | None = None
        # Backwards-compatible alias kept for any external reader (#730 PR C).
        # The reverted global_sensitivity_override now restores the CURRENT admin
        # baseline (#816), so this is no longer the restore target — but the
        # game-start snapshot is still useful telemetry, so expose it as a
        # property mirroring the baseline at construction time.
        self._configured_sensitivity = self._baseline_sensitivity
        self.dead_count = 0  # Track deaths for tempo timing
        # Elimination ordering for game_player_elimination_order metric (#730).
        # Incremented on each kill; the value at kill time is the player's order
        # index (1 = first eliminated). In respawn modes this counts each death.
        self._elimination_count = 0
        # Serials whose _kill_player is mid-flight. _kill_player awaits (death
        # sound) before the mode impl flips player.alive, so a concurrent kill
        # of the same serial (e.g. motion death racing a disconnect) would
        # otherwise pass the alive check and count the death twice.
        self._kills_in_progress: set[str] = set()
        self.gameplay_span: trace.Span | None = None  # Reference for span events
        self.gameplay_span_context = None  # Context for child spans in background tasks
        self._players_span: trace.Span | None = None  # Parent span grouping all player lifecycles
        self._game_cycle_span: trace.Span | None = None  # Span for instrumentation (sounds, music)
        self.game_cycle_context = None  # Context for sound/music spans

        # Tracked async tasks for automatic cleanup on game end
        self._tasks: set[asyncio.Task] = set()

        # Per-channel volumes (F7, #766) — read once at game init from the user
        # domain (init-frozen). Defaults reproduce the original constants, so
        # promotion is behavior-neutral. Malformed values fall back to defaults.
        self.game_volume = _read_volume_flag("audio_volume.game", GAME_VOLUME)
        self.countdown_music_volume = _read_volume_flag("audio_volume.countdown_music", COUNTDOWN_MUSIC_VOLUME)

        logger.info(f"{self.get_game_name()} game initialized: {self.game_id}")

    # ========================================================================
    # Sensitivity arbitration (#816, M9 ownership model — epic #814)
    # ========================================================================
    # ``sensitivity`` is the EFFECTIVE level: the agent override if one is
    # active, otherwise the admin baseline. Every reader (the per-frame
    # ``_compute_effective_thresholds``, the game_sensitivity metric, span
    # attributes) sees the effective value with no change. Writers are split by
    # owner: the admin owns ``baseline_sensitivity`` (assigning ``sensitivity``
    # is the admin path and routes here); the agent owns the override layer via
    # ``apply_sensitivity_override`` / ``clear_sensitivity_override`` and never
    # touches the baseline.

    @property
    def sensitivity(self) -> Sensitivity:
        """Effective sensitivity: agent override if active, else admin baseline."""
        if self._sensitivity_override is not None:
            return self._sensitivity_override
        return self._baseline_sensitivity

    @sensitivity.setter
    def sensitivity(self, value: Sensitivity) -> None:
        """Admin path: set the baseline (clears any active agent override).

        Assigning ``game.sensitivity`` is treated as a HUMAN baseline change
        (the lobby/admin surface and existing test fixtures do this). Per the
        ownership model a human baseline change invalidates the agent's active
        override, so this delegates to :meth:`set_baseline_sensitivity`.
        """
        self.set_baseline_sensitivity(value)

    @property
    def baseline_sensitivity(self) -> Sensitivity:
        """The admin's source-of-truth sensitivity (pre-game lobby intent)."""
        return self._baseline_sensitivity

    @property
    def sensitivity_override(self) -> Sensitivity | None:
        """The agent's active override layer, or ``None`` if no override."""
        return self._sensitivity_override

    @property
    def configured_sensitivity(self) -> Sensitivity:
        """Game-start sensitivity snapshot (telemetry only; not the restore target)."""
        return self._configured_sensitivity

    @property
    def music_speed(self) -> float:
        """Music ratio derived from the tempo intensity now (#1833, UC-031 BR-005)."""
        return tempo_from_intensity(self._intensity.value_at(time.time()))

    @music_speed.setter
    def music_speed(self, tempo: float) -> None:
        """Pin the intensity at the given tempo's value (no ramp); used by fixtures and resets."""
        self._intensity = IntensityRamp.hold(intensity_from_tempo(tempo), time.time())

    @property
    def tempo_intensity(self) -> float:
        """The tempo intensity in [0, 1] now (0 = slow table, 1 = fast table)."""
        return self._intensity.value_at(time.time())

    def set_baseline_sensitivity(self, value: Sensitivity) -> None:
        """Update the admin baseline; invalidate any active agent override (#816).

        A human baseline change wins: the agent's in-game override is dropped so
        a later override clear cannot clobber fresh admin intent, and the
        effective sensitivity reflects the new baseline immediately.
        """
        had_override = self._sensitivity_override is not None
        self._baseline_sensitivity = value
        self._sensitivity_override = None
        if had_override:
            logger.info(f"sensitivity: admin baseline -> {value.name}; active agent override invalidated")
        else:
            logger.info(f"sensitivity: admin baseline -> {value.name}")

    def apply_sensitivity_override(self, value: Sensitivity) -> None:
        """Agent path: set the temporary override layer on top of the baseline."""
        self._sensitivity_override = value
        logger.info(f"sensitivity: agent override -> {value.name} (baseline {self._baseline_sensitivity.name})")

    def clear_sensitivity_override(self) -> None:
        """Agent path: drop the override; effective returns to the CURRENT baseline."""
        self._sensitivity_override = None
        logger.info(f"sensitivity: agent override cleared, restored to baseline {self._baseline_sensitivity.name}")

    # ========================================================================
    # Abstract Methods - Subclasses MUST implement these
    # ========================================================================

    @abstractmethod
    def get_game_name(self) -> str:
        """
        Return game mode identifier for logging and span naming.

        Returns:
            Game mode name (e.g., "FFA", "Teams", "Nonstop Joust")
        """
        pass

    @abstractmethod
    async def _initialize_players_impl(self, controllers: list):
        """
        Initialize players with game-specific logic (team assignment, etc.).

        Args:
            controllers: List of controller protobuf messages from GetReadyControllers

        Subclass responsibilities:
        - Create Player objects and add to self.players
        - Assign teams (FFA uses team=0 for all, Teams uses round-robin, etc.)
        - Set controller colors
        """
        pass

    @abstractmethod
    def _create_player_spans(self, game_context):
        """
        Create player/team lifecycle spans with game-specific hierarchy.

        Args:
            game_context: Parent span context for proper hierarchy

        FFA: Flat hierarchy - player spans directly under gameplay_phase
        Teams/Random Teams: Hierarchical - team spans → player spans
        Nonstop Joust: Flat hierarchy like FFA
        """
        pass

    @abstractmethod
    async def _check_win_condition(self) -> bool:
        """
        Check if game should end.

        Returns:
            True if game should end, False otherwise

        FFA: Last player standing (len(alive_players) <= 1)
        Teams: Last team standing (len(alive_teams) <= 1)
        Nonstop Joust: Time limit reached
        """
        pass

    @abstractmethod
    async def _kill_player_impl(self, serial: str, accel_mag: float):
        """
        Handle player death with game-specific logic.

        Args:
            serial: Controller serial number
            accel_mag: Acceleration magnitude that caused death

        FFA/Teams: Player stays dead, end their lifecycle span
        Nonstop Joust: Player respawns, add death event but keep span open
        """
        pass

    def get_instruction_clip(self) -> Sound | None:
        """Spoken instruction clip played before the countdown (UC-020 A5).

        None means the mode has no clip mapping yet: nothing plays, no wait.
        """
        return None

    def _instructions_enabled(self) -> bool:
        """Read ``user.game_instructions`` at use time; any failure means off."""
        try:
            from openfeature.evaluation_context import EvaluationContext

            from lib.feature_flags import get_flag_client, init_flag_domain

            init_flag_domain("user")
            client = get_flag_client("user")
            value = client.get_boolean_value("game_instructions", False, EvaluationContext())
            return value is True
        except Exception as e:
            logger.warning(f"Failed to read game_instructions, treating as off: {e}")
            return False

    def _is_shadow_game(self) -> bool:
        """True for a shadow game: it sends no audio RPC (UC-031 BR-006, #1835)."""
        return self.game_kind == GAME_KIND_SHADOW

    async def _play_instructions(self, span: trace.Span) -> None:
        """Play the mode's instruction clip and wait for it to finish (UC-020 A5).

        Records ``game.instructions`` on ``span``: ``played``, ``off``,
        ``shadow`` (a shadow game has no audience), ``no_clip`` (the mode has
        no clip), ``no_audio`` (no audio client) or ``failed`` (PlaySound raised).
        Only ``played`` waits.
        """
        if self.game_kind == GAME_KIND_SHADOW:
            span.set_attribute("game.instructions", "shadow")
            return
        if not self._instructions_enabled():
            span.set_attribute("game.instructions", "off")
            return
        clip = self.get_instruction_clip()
        if clip is None:
            span.set_attribute("game.instructions", "no_clip")
            return
        if not self.audio_client:
            span.set_attribute("game.instructions", "no_audio")
            return

        try:
            from proto import audio_pb2

            response = await self.audio_client.PlaySound(
                audio_pb2.PlaySoundRequest(file_path=clip.value, volume=1.0, priority=audio_pb2.HIGH)
            )
            duration = max(0.0, min(float(response.duration_seconds), MAX_INSTRUCTIONS_WAIT_SECONDS))
        except Exception as e:
            logger.warning(f"Failed to play instructions {clip.value}: {e}")
            span.set_attribute("game.instructions", "failed")
            return

        span.set_attribute("game.instructions", "played")
        span.add_event(
            "game.instructions_played",
            {"instructions.clip": clip.value, "instructions.duration_seconds": duration},
        )
        logger.info(f"Playing instructions {clip.value}, waiting {duration:.1f}s before the countdown")

        # Wait for the clip to finish; a force-end stops the wait at once.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration
        for _ in range(math.ceil(duration / INSTRUCTIONS_POLL_SECONDS)):
            remaining = deadline - loop.time()
            if not self.running or remaining <= 0:
                break
            await asyncio.sleep(min(INSTRUCTIONS_POLL_SECONDS, remaining))

        if not self.running:
            await self._stop_instruction_clip()

    async def _stop_instruction_clip(self) -> None:
        """Cut the instruction clip short after a force-end (UC-020 A5, #1586).

        Best effort and bounded: a failure or timeout is logged and never blocks the end.
        """
        try:
            from proto import audio_pb2

            await asyncio.wait_for(
                self.audio_client.StopSound(audio_pb2.StopSoundRequest()), timeout=STOP_SOUND_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.warning(f"StopSound timed out after {STOP_SOUND_TIMEOUT_SECONDS}s; the clip may play on")
        except Exception as e:
            logger.warning(f"Failed to stop the instruction clip after force-end: {e}")

    @abstractmethod
    def _get_additional_phases(self) -> list:
        """
        Return extra phases to execute before countdown (e.g., team_formation).

        Returns:
            List of phase objects with name and execute() method

        FFA/Teams/Nonstop: return []
        Random Teams: return [TeamFormationPhase]
        """
        pass

    @abstractmethod
    async def _end_game_impl(self):
        """
        Handle game ending with game-specific logic.

        Responsibilities:
        - Close any remaining player/team lifecycle spans
        - Determine and declare winner
        - Set controller colors/effects for winner
        - Publish game_ended event
        """
        pass

    # ========================================================================
    # Concrete Methods - Shared implementation used by all subclasses
    # ========================================================================

    def _set_players_alive_aggregate(self, alive_count: int) -> None:
        """Set the unlabeled ``players_alive`` aggregate gauge — primary only.

        ``game_players_alive`` is a single UNLABELED time series feeding the
        live dashboard's "players alive" count. With concurrent shadow games
        (#1018) every game-mode instance would otherwise write this one series,
        so a headless shadow could stomp the real game's count (last-writer
        wins). Only the primary (real, player-facing) session is the "live"
        game the aggregate represents, so shadows must not touch it. The
        per-serial ``player_alive`` gauge is game_id-labeled and stays isolated.
        """
        if self.game_kind != GAME_KIND_SHADOW:
            metrics.players_alive.set(alive_count)

    async def _initialize_players(self):
        """Initialize players from StartGame RPC payload."""
        try:
            # Players must be provided via StartGame RPC (from Menu → Supervisor)
            if not self.initial_players:
                raise RuntimeError("No players provided - StartGame must include player list")

            # Convert protobuf Player messages to controller-like objects for _initialize_players_impl
            # _initialize_players_impl expects controllers with .serial attribute
            class ControllerStub:
                def __init__(self, serial):
                    self.serial = serial

            controllers = [ControllerStub(p.serial) for p in self.initial_players]
            await self._initialize_players_impl(controllers)

            # Set alive metric for all initialized players (Phase 75: filter dead from dashboard)
            for serial in self.players:
                metrics.player_alive.labels(serial=serial, game_id=self.game_id).set(1)
            self._set_players_alive_aggregate(len(self.players))

            logger.info(f"Initialized {len(self.players)} players from StartGame RPC")

            # Publish event
            await self.event_publisher(
                GameEvent.PLAYERS_INITIALIZED,
                {
                    "player_count": len(self.players),
                    "serials": list(self.players.keys()),
                },
            )

        except Exception as e:
            logger.error(f"Error initializing players: {e}", exc_info=True)
            raise

    async def _countdown(self):
        """Run countdown before game starts using unified countdown effect.

        Sends per-controller countdown effects so each effect_countdown span
        appears under the corresponding player's countdown_phase span.
        Sound spans live under gameplay_phase (shared, not player-specific).
        """
        from proto import controller_manager_pb2

        # Get phase duration from runtime config (controlled via flagd game_settings)
        # phase_duration_ms == 0 means skip countdown entirely (Issue #464)
        config = get_config_manager().get_config()
        phase_duration_ms = config.countdown_phase_duration_ms

        logger.info(f"Starting countdown (phase_duration={phase_duration_ms}ms)...")
        await self.event_publisher(GameEvent.COUNTDOWN_START, {"phase_duration_ms": phase_duration_ms})

        if not self.running:
            logger.info(_MSG_COUNTDOWN_INTERRUPTED)
            return

        # Skip countdown entirely if phase duration is 0 (the "skip" variant)
        if phase_duration_ms == 0:
            logger.info("Countdown skipped (phase_duration_ms=0)")
            await self.event_publisher(GameEvent.COUNTDOWN_END, {})
            return

        # Send per-controller countdown effects via gameplay stream.
        # Each effect carries its player's countdown_phase span context so
        # effect_countdown becomes a child of the correct countdown_phase.
        if self.gameplay_stream:
            for player in self.players.values():
                trace_parent, trace_state = inject_trace_context(player._countdown_span)
                effect_cmd = controller_manager_pb2.GameplayStreamControl(
                    game_effect=controller_manager_pb2.GameEffectCommand(
                        serial=player.serial,
                        effect=controller_manager_pb2.GAME_EFFECT_COUNTDOWN,
                        duration_ms=phase_duration_ms,
                        trace_parent=trace_parent,
                        trace_state=trace_state,
                    )
                )
                await self.gameplay_stream.write(effect_cmd)

        # Play countdown beeps in sync with the visual countdown.
        # Sounds are shared (not per-player), so they inherit the active
        # gameplay_phase span as their parent — not any player's countdown_phase.
        beep_count = COUNTDOWN_BEEP_COUNT
        beep_interval_ms = phase_duration_ms  # Match LED phase duration

        for _ in range(beep_count):
            if not self.running:
                logger.info(_MSG_COUNTDOWN_INTERRUPTED)
                return

            # Play countdown beep — inherits gameplay_phase as parent span
            await self._play_sound(Sound.SFX_BEEP_LOUD, priority=2)

            # Wait for beep interval (configurable based on countdown duration)
            wait_iterations = beep_interval_ms // 50  # 50ms per iteration
            for _ in range(wait_iterations):
                if not self.running:
                    logger.info(_MSG_COUNTDOWN_INTERRUPTED)
                    return
                await asyncio.sleep(0.05)

        # Play start sound — inherits gameplay_phase as parent span
        await self._play_sound(Sound.SFX_START3, priority=2)

        await self.event_publisher(GameEvent.COUNTDOWN_END, {})
        logger.info("Countdown complete")

    async def _create_gameplay_stream(self):
        """
        Create gameplay stream, send config, and update alive filter.

        Encapsulates stream creation so it can be called for both initial
        connection and reconnection.
        """
        from proto import controller_manager_pb2

        config = get_config_manager().get_config()
        update_frequency_hz = config.update_frequency_hz

        logger.info(f"Creating gameplay stream at {update_frequency_hz}Hz...")

        # Create bidirectional stream
        self.gameplay_stream = self.controller_client.StreamGameplayData()

        # Build player colors for stream init
        player_colors = []
        for serial, player in self.players.items():
            player_colors.append(
                controller_manager_pb2.ControllerColorConfig(
                    serial=serial,
                    color=controller_manager_pb2.RGB(r=player.color[0], g=player.color[1], b=player.color[2]),
                )
            )

        # Send initial configuration
        initial_config = controller_manager_pb2.GameplayStreamControl(
            config=controller_manager_pb2.GameplayStreamConfig(
                update_frequency_hz=update_frequency_hz,
                colors=player_colors,
            )
        )
        await self.gameplay_stream.write(initial_config)

        # Send current alive filter so reconnected stream only gets alive players
        current_alive = [s for s, p in self.players.items() if p.alive]
        if len(current_alive) < len(self.players):
            filter_msg = controller_manager_pb2.GameplayStreamControl(
                filter_update=controller_manager_pb2.FilterUpdate(serials=current_alive)
            )
            await self.gameplay_stream.write(filter_msg)

        logger.info(f"Gameplay stream created with {len(player_colors)} players ({len(current_alive)} alive)")

        # UC-040 BR-021: after a reconnect, re-send every held charge and running
        # shield with the time left, so the new stream owns the layers again.
        await self._resend_shield_layers()

    async def _start_gameplay_stream(self):
        """
        Create and configure the gameplay stream.

        Called before countdown to allow EMA warmup during countdown phase.
        The stream is stored in self.gameplay_stream for use by _game_loop().
        """
        await self._create_gameplay_stream()

    def _opens_stream_before_additional_phases(self) -> bool:
        """Whether run() opens the gameplay stream before the additional phases.

        A mode whose phases send effects over the stream (Random Teams formation
        pulse, UC-021 A1; Traitor signal, UC-022 BR-003) returns True. The stream
        config carries the player colours as they are before the phases; a mode
        that sets them later (Traitor) writes them as base colours over the
        stream, and a reopen after the phases carries the final ones. Default False: the stream
        opens after the phases and the instructions, as for FFA, Teams and Swapper,
        whose colour_assignment phase sets the colours the config sends.
        """
        return False

    def _start_early_stream_drain(self) -> None:
        """Read and discard frames of the early-opened stream (UC-021 A1).

        Without a reader the server fills the HTTP/2 window with formation motion,
        which the 100 ms EMA warmup would not clear before death detection.
        """
        self._early_drain_stop.clear()
        self._early_drain_disconnects = {}
        self._early_drain_task = self._track_task(asyncio.create_task(self._drain_early_stream()))

    async def _drain_early_stream(self) -> bool:
        """Discard frames until told to stop, keeping disconnects (UC-020 A2).

        A controller whose frames come back after its disconnect is dropped from
        the pending list, as the controller manager does for a drop and reconnect
        before a stream opens (UC-002 BR-004, #1607). It stops with ``break`` after
        the next frame: cancelling a grpc.aio read cancels the whole call, which
        would end the game at the start.

        Returns True only when it stopped on request with the call still usable.
        """
        frames = 0
        stopped = False
        try:
            async for gameplay_update in self.gameplay_stream:
                frames += 1
                for disconnect in gameplay_update.disconnects:
                    self._early_drain_disconnects.pop(disconnect.serial, None)
                    self._early_drain_disconnects[disconnect.serial] = None
                for controller_data in gameplay_update.controllers:
                    self._early_drain_disconnects.pop(controller_data.serial, None)
                if self._early_drain_stop.is_set():
                    stopped = True
                    break
        except Exception as e:
            # grpc.aio keeps one reader per call: after an error it yields nothing
            # more, so _stop_early_stream_drain() opens a new call.
            logger.warning(f"Early stream drain interrupted: {e}")
        logger.info(f"Early stream drain stopped: discarded {frames} frames")
        return stopped

    _EARLY_DRAIN_STOP_TIMEOUT_S = 0.5

    async def _stop_early_stream_drain(self, reopen: bool = True) -> None:
        """Stop the drain where the stream used to open, and keep its disconnects.

        Waits for the drain's next frame; only a drain that gets none within
        ``_EARLY_DRAIN_STOP_TIMEOUT_S`` is cancelled. The drain has finished when this returns, so it
        never reads alongside ``_warmup_ema()``.

        A drain that ended by a stream error, by the stream ending, or by the
        timeout (which cancels the call) leaves a call that yields no more frames.
        When ``reopen`` is set and the game still runs, the stream is then opened
        once more, as it would open here without the early stream; a failure of
        that open raises. The collected disconnects are kept either way.
        """
        task, self._early_drain_task = self._early_drain_task, None
        if task is None:
            return
        self._early_drain_stop.set()
        stopped = False
        try:
            stopped = await asyncio.wait_for(task, timeout=self._EARLY_DRAIN_STOP_TIMEOUT_S)
        except TimeoutError:
            logger.warning(f"Early stream drain got no frame in {self._EARLY_DRAIN_STOP_TIMEOUT_S}s: cancelled")
        pending, self._early_drain_disconnects = self._early_drain_disconnects, {}
        self._warmup_disconnects.extend(pending)
        if reopen and self.running and (not stopped or self._gameplay_call_done()):
            await self._reopen_gameplay_stream("the early stream ended during the formation")

    def _gameplay_call_done(self) -> bool:
        """Whether the gameplay call has finished (test fakes without ``done()`` never do)."""
        done = getattr(self.gameplay_stream, "done", None)
        return bool(done()) if callable(done) else False

    def _cancel_gameplay_call(self) -> None:
        """Cancel the gameplay call, if it has a ``cancel()`` (a no-op on a finished call)."""
        cancel = getattr(self.gameplay_stream, "cancel", None)
        if callable(cancel):
            cancel()

    async def _write_end_effect(self, message) -> bool:
        """Write an end-of-game effect (winner rainbow, loser colour) while the gameplay call is open.

        The controller manager can end the call after the win, before teardown
        (UC-030 BR-006): the effect is skipped with a warning and the game keeps
        its outcome, never ``game_error`` (UC-020 BR-003, #1956). grpc.aio rejects
        a write on a finished call with ``InvalidStateError`` and raises
        ``AioRpcError`` when the call fails under the write. A cancel of this
        task still propagates. Returns whether the effect was written.
        """
        return await self._write_effect_guarded(message, "End-of-game effect")

    async def _write_effect_guarded(self, message, what: str) -> bool:
        """Write a gameplay effect, skipping it with a warning when the call has ended.

        Shared by ``_write_end_effect`` and in-game effects that must not abort a
        state transition (Fight Club round loser, #1345). ``what`` names the
        effect in the warning. Skips on a finished call; catches
        ``InvalidStateError`` / ``AioRpcError`` from the write; a cancel still
        propagates. Returns whether the effect was written.
        """
        stream = self.gameplay_stream
        if stream is None:
            return False
        if self._gameplay_call_done():
            logger.warning(f"{what} skipped: the gameplay stream has already ended")
            return False
        try:
            await stream.write(message)
        except (asyncio.InvalidStateError, grpc.aio.AioRpcError) as e:
            logger.warning(f"{what} skipped: the gameplay stream ended during the write ({e!r})")
            return False
        return True

    async def _gameplay_call_failed(self) -> bool:
        """Whether the gameplay call has finished with a non-OK gRPC status (UC-030 BR-006).

        A call that ended with OK, a live call, and a test fake without
        ``done()`` / ``code()`` are not failed.
        """
        if not self._gameplay_call_done():
            return False
        code = getattr(self.gameplay_stream, "code", None)
        if not callable(code):
            return False
        status = code()
        if inspect.isawaitable(status):
            status = await status
        return isinstance(status, grpc.StatusCode) and status != grpc.StatusCode.OK

    async def _reopen_gameplay_stream(self, reason: str) -> None:
        """Open a new gameplay call, then close the old one (UC-030 BR-006, #1913).

        The new call is in ``self.gameplay_stream`` before the old one is
        cancelled, so a concurrent effect write never sees a missing stream. A
        failed open raises; the old call is closed either way.
        """
        logger.warning(f"Reopening the gameplay stream: {reason}")
        metrics.gameplay_stream_reconnects_total.labels(game_mode=self.get_game_name()).inc()
        # UC-030 BR-008: no drain crosses a reconnect.
        self._force_release_shield_holds()
        old_stream = self.gameplay_stream
        try:
            await self._create_gameplay_stream()
        finally:
            cancel = getattr(old_stream, "cancel", None)
            if old_stream is not self.gameplay_stream and callable(cancel):
                cancel()

    def _release_gameplay_stream(self) -> None:
        """Close the gameplay call and drop it (UC-030 BR-006, #1914). Idempotent.

        Without the cancel the call stays open until the garbage collector finds
        it, and the controller manager keeps the game's roster until then (UC-004).
        """
        self._cancel_gameplay_call()
        self.gameplay_stream = None

    async def _warmup_ema(self):
        """
        Drain buffered stream data and prime EMA filters before game loop.

        The gameplay stream opens ~2.25s before the game loop starts (before
        countdown). Data buffered during that time can contain stale spikes
        that cause instant deaths when the EMA is uninitialized. This method
        reads and discards buffered frames while priming each player's EMA
        with real sensor data — no death checks run.

        Disconnect events are one-shot (controller-manager drains them once),
        so they are kept in ``_warmup_disconnects`` for ``_game_loop()`` to
        handle instead of being dropped with the frame (UC-020 A2, #1582).

        A stream that ends, fails or stalls here is opened once more and the
        warm-up repeats once (UC-030 BR-006, #1913); a second failure, and a
        reopen that fails to open, are left to the game loop's reconnect.
        """
        if await self._read_warmup_frames():
            return
        if self.running and self._gameplay_call_done():
            try:
                await self._reopen_gameplay_stream("the stream ended during the countdown or the EMA warm-up")
            except Exception as e:
                logger.warning(f"EMA warmup could not reopen the stream, left to the game loop: {e}")
                return
            await self._read_warmup_frames()

    _WARMUP_DURATION_S = 0.1  # enough to drain the buffer and get ~6 frames at 60 Hz
    _WARMUP_STALL_TIMEOUT_S = 0.5

    async def _read_warmup_frames(self) -> bool:
        """Prime the EMA from the stream for ``_WARMUP_DURATION_S``.

        Returns True only when it read for the full duration. A read that gets
        no frame for ``_WARMUP_STALL_TIMEOUT_S`` is cancelled, which cancels the
        gRPC call (the call is done afterwards).
        """
        warmup_start = time.monotonic()
        frames_consumed = 0
        completed = False

        async def read() -> bool:
            nonlocal frames_consumed
            async for gameplay_update in self.gameplay_stream:
                self._warmup_disconnects.extend(d.serial for d in gameplay_update.disconnects)
                for controller_data in gameplay_update.controllers:
                    serial = controller_data.serial
                    if serial in self.players and self.players[serial].alive:
                        accel_mag = self._compute_accel_magnitude(controller_data.accel)
                        self._update_ema(self.players[serial], accel_mag, self._ema_weight)
                frames_consumed += 1

                if time.monotonic() - warmup_start >= self._WARMUP_DURATION_S:
                    return True
            return False

        try:
            completed = await asyncio.wait_for(read(), timeout=self._WARMUP_DURATION_S + self._WARMUP_STALL_TIMEOUT_S)
        except TimeoutError:
            logger.warning(f"EMA warmup got no frame in {self._WARMUP_STALL_TIMEOUT_S}s: cancelled the stream read")
        except Exception as e:
            logger.warning(f"EMA warmup interrupted: {e}")

        logger.info(f"EMA warmup complete: drained {frames_consumed} buffered frames")
        return completed

    async def _process_gameplay_update(self, gameplay_update) -> bool:
        """
        Process a single gameplay update: handle disconnects then run death detection.

        Disconnect events are processed first so that a disconnected player is
        killed before any remaining controller data is evaluated. Checks win
        condition after each disconnect and each controller to prevent
        simultaneous deaths, ensuring the last player standing can never die.

        Args:
            gameplay_update: GameplayDataUpdate protobuf with controller data

        Returns:
            True if game is over (win condition met), False otherwise
        """
        # UC-020 BR-001: win check after every disconnect and every controller sample.
        # Handle disconnects first (#580)
        for disconnect in gameplay_update.disconnects:
            await self._handle_disconnect(disconnect.serial)
            if await self._check_win_condition():
                return True

        for gameplay_data in gameplay_update.controllers:
            await self._process_controller_state(gameplay_data)

            if await self._check_win_condition():
                return True
        return False

    async def _handle_warmup_disconnects(self) -> bool:
        """Run disconnects buffered by ``_warmup_ema()`` through the death path.

        Same order and win check as ``_process_gameplay_update`` (UC-020 A2, BR-001).

        Returns:
            True if game is over (win condition met), False otherwise
        """
        pending, self._warmup_disconnects = self._warmup_disconnects, []
        for serial in pending:
            await self._handle_disconnect(serial)
            if await self._check_win_condition():
                return True
        return False

    async def _handle_disconnect(self, serial: str) -> None:
        """Handle a controller disconnect by killing the player.

        Uses the existing _kill_player pipeline so that death events, audio
        feedback, metrics, and spans all fire correctly.

        Args:
            serial: Serial number of the disconnected controller.
        """
        player = self.players.get(serial)
        if not player or not player.alive:
            return

        logger.info(f"Player disconnected during game: {serial}")

        # Record disconnect on the player's span before kill closes it
        if player.span:
            player.span.add_event(
                "player_disconnected",
                attributes={"reason": "controller_disconnect"},
            )

        # Kill the player using the standard pipeline (accel_mag=0.0 — no motion)
        await self._kill_player(serial, accel_mag=0.0)

    async def _send_alive_filter_update(self, last_alive_serials: set[str]) -> set[str]:
        """
        Check if alive players changed and send filter update if so.

        Args:
            last_alive_serials: The previous set of alive player serials

        Returns:
            The current set of alive player serials
        """
        from proto import controller_manager_pb2

        current_alive_serials = {p.serial for p in self.players.values() if p.alive}

        if current_alive_serials != last_alive_serials:
            filter_msg = controller_manager_pb2.GameplayStreamControl(
                filter_update=controller_manager_pb2.FilterUpdate(serials=list(current_alive_serials))
            )
            await self.gameplay_stream.write(filter_msg)

            logger.info(
                f"Updated controller filter: {len(last_alive_serials)} → {len(current_alive_serials)} alive players"
            )

            metrics.filter_updates_total.labels(game_mode=self.get_game_name()).inc()
            metrics.active_controllers.set(len(current_alive_serials))
            metrics.filtered_controllers.set(len(self.players) - len(current_alive_serials))

        return current_alive_serials

    def _update_frame_metrics(
        self,
        iteration_latency_ms: float,
        target_frame_time_ms: float,
        recent_frame_times: list[float],
        frames_on_target: int,
        loop_iterations: int,
        loop_start_time: float,
    ) -> int:
        """
        Update frame timing metrics for monitoring.

        Tracks frame consistency, dropped frames, actual Hz, and jitter.

        Args:
            iteration_latency_ms: Duration of this iteration in ms
            target_frame_time_ms: Expected frame time in ms
            recent_frame_times: Rolling window of recent frame times (mutated in place)
            frames_on_target: Running count of frames within target tolerance
            loop_iterations: Total iterations so far
            loop_start_time: Timestamp when loop started

        Returns:
            Updated frames_on_target count
        """
        recent_frame_times.append(iteration_latency_ms)
        if len(recent_frame_times) > 60:  # Keep last 60 frames (1 second at 60Hz)
            recent_frame_times.pop(0)

        if iteration_latency_ms <= target_frame_time_ms * 1.5:
            frames_on_target += 1

        if iteration_latency_ms > target_frame_time_ms * 2:
            metrics.game_loop_frames_dropped_total.labels(mode=self.get_game_name()).inc()

        if loop_iterations % 10 == 0:
            elapsed = time.time() - loop_start_time
            actual_hz = loop_iterations / elapsed if elapsed > 0 else 0
            metrics.actual_update_frequency_hz.labels(**self._game_state_labels()).set(actual_hz)

            consistency_percent = (frames_on_target / loop_iterations) * 100
            metrics.game_loop_frame_consistency_percent.labels(**self._game_state_labels()).set(consistency_percent)

            if len(recent_frame_times) >= 2:
                jitter_ms = statistics.stdev(recent_frame_times)
                metrics.game_loop_jitter_ms.labels(**self._game_state_labels()).set(jitter_ms)

        return frames_on_target

    async def _handle_stream_reconnection(self, error: Exception, attempt: int, max_attempts: int) -> int:
        """
        Handle gameplay stream reconnection after an error.

        Args:
            error: The exception that caused the stream failure
            attempt: Current reconnection attempt number (pre-incremented)
            max_attempts: Maximum allowed reconnection attempts

        Returns:
            The new attempt number after reconnection

        Raises:
            Exception: If max reconnection attempts exceeded
        """
        if attempt > max_attempts:
            logger.error(f"Gameplay stream failed after {max_attempts} reconnect attempts, ending game")
            raise error

        backoff = attempt * self._RECONNECT_BACKOFF_S  # Linear backoff: 1s, 2s, 3s
        logger.warning(
            f"Gameplay stream error (attempt {attempt}/{max_attempts}): {error}. Reconnecting in {backoff}s..."
        )
        await asyncio.sleep(backoff)
        if not self.running:
            # Force-ended during the backoff: the outer loop ends, no reopen (UC-030 BR-006).
            return attempt

        await self._reopen_gameplay_stream(f"game loop reconnect {attempt}/{max_attempts}")
        return attempt

    _RECONNECT_BACKOFF_S = 1.0
    # Errors of the gameplay call itself, raised by a read or by a write on it
    # (UC-030 BR-006): the game loop reconnects on these. Any other error while a
    # frame is processed ends the game with game_error (UC-020 BR-003, #2001).
    _STREAM_CALL_ERRORS = (grpc.aio.AioRpcError, asyncio.InvalidStateError)
    # A reopened stream counts as healthy, and the reconnect count starts again, after
    # this many frames (about 1 s at the default 60 Hz; UC-030 BR-006, #1931).
    _RECONNECT_HEALTHY_FRAMES = 60

    async def _hold_win_display(self) -> None:
        """Latch the outcome and keep the game active 1 s to show the winner (UC-020 BR-003).

        A mode that ends the loop on an error it raises from ``run()`` latches no
        outcome (Tournament bracket step, #2001): the loop ends at once, no hold.
        """
        self._latch_game_outcome()
        if self._latched_outcome is None:
            return
        logger.info("Win condition met, keeping game active for 1 second to show winner")
        await asyncio.sleep(1.0)

    async def _game_loop(self):
        """Main game loop - processes controller states and checks for deaths."""
        logger.info("Starting game loop...")

        try:
            # Player spans already created in run() before countdown

            # Get runtime config for frame timing calculations
            config = get_config_manager().get_config()
            update_frequency_hz = config.update_frequency_hz

            logger.info("Starting game loop (stream rate controlled by controller-manager)")

            # Stream was already created in _start_gameplay_stream() before countdown.
            # EMA filters were primed and buffered data drained by _warmup_ema().
            # Apply startup grace period so any residual spikes can't cause instant death.
            # It never shortens a longer grace (an agent shield) granted before the loop.
            grace_until = time.time() + 0.3
            for player in self.players.values():
                if player.alive:
                    player.grace_until = max(player.grace_until, grace_until)
            # UC-040 BR-027: hold is latched here at the latest; the recharge starts now.
            self._latch_shield_hold()
            self._start_shield_hold_clock(time.time())

            # Track current alive set for detecting changes
            last_alive_serials = {p.serial for p in self.players.values() if p.alive}
            logger.info(f"Initial alive players: {len(last_alive_serials)}")

            # UC-020 A2: disconnects seen during EMA warmup go through the death path now.
            # A game force-ended during the countdown or warmup stays force_ended (BR-003).
            if self.running and await self._handle_warmup_disconnects():
                await self._hold_win_display()
                return
            last_alive_serials = await self._send_alive_filter_update(last_alive_serials)

            # Track loop timing for actual Hz calculation (Phase 43)
            loop_start_time = time.time()
            loop_iterations = 0
            last_iteration_time = loop_start_time

            # Track frame consistency (Issue #183)
            target_frame_time_ms = 1000.0 / update_frequency_hz
            recent_frame_times: list[float] = []  # Store recent frame times for jitter calculation
            frames_on_target = 0  # Frames within 50% of target time

            # Stream gameplay data and process game logic with reconnection
            max_reconnect_attempts = 3
            reconnect_attempt = 0
            frames_since_reconnect = 0
            frame_failed = False

            logger.info("Starting gameplay data stream loop, waiting for first update...")
            while self.running:
                try:
                    async for gameplay_update in self.gameplay_stream:
                        if loop_iterations == 0:
                            logger.info("Received first gameplay update from stream!")
                            self.start_time = time.time()

                        # The count starts again only after a healthy run, not after
                        # one frame (UC-030 BR-006, #1931).
                        if reconnect_attempt:
                            frames_since_reconnect += 1
                            if frames_since_reconnect >= self._RECONNECT_HEALTHY_FRAMES:
                                logger.info("Gameplay stream healthy again, reconnect count reset")
                                reconnect_attempt = 0

                        if not self.running:
                            logger.info("Game running=False, breaking loop")
                            break

                        # Process controllers and check for game over, then update the
                        # alive filter if players changed. An error here that is not a
                        # gRPC error of the call (a mode's kill handler, its win check)
                        # ends the game, no reconnect (UC-030 BR-006, #2001).
                        try:
                            game_over = await self._process_gameplay_update(gameplay_update)
                            if not game_over:
                                last_alive_serials = await self._send_alive_filter_update(last_alive_serials)
                        except self._STREAM_CALL_ERRORS:
                            raise
                        except Exception:
                            frame_failed = True
                            raise

                        if game_over:
                            await self._hold_win_display()
                            return

                        # Track loop timing for metrics
                        loop_iterations += 1
                        iteration_end = time.time()
                        iteration_latency_ms = (iteration_end - last_iteration_time) * 1000

                        frames_on_target = self._update_frame_metrics(
                            iteration_latency_ms,
                            target_frame_time_ms,
                            recent_frame_times,
                            frames_on_target,
                            loop_iterations,
                            loop_start_time,
                        )

                        last_iteration_time = iteration_end

                    # A call that ended with an error status is dead, not finished: an
                    # exhausted grpc.aio reader ends the loop silently (UC-030 BR-006, #1913).
                    if self.running and await self._gameplay_call_failed():
                        raise RuntimeError("gameplay call ended with an error status")

                    # Stream ended normally (server closed it with OK) — exit loop
                    break

                except asyncio.CancelledError:
                    raise  # Never retry on cancellation

                except Exception as e:
                    if not self.running:
                        if frame_failed:
                            logger.warning(f"Frame processing failed after the game stopped: {e!r}", exc_info=True)
                        break
                    if frame_failed:
                        logger.error("Frame processing failed, ending the game without a reconnect")
                        raise

                    reconnect_attempt += 1
                    frames_since_reconnect = 0
                    reconnect_attempt = await self._handle_stream_reconnection(
                        e, reconnect_attempt, max_reconnect_attempts
                    )

        except Exception as e:
            logger.error(f"Game loop error: {e}", exc_info=True)
            raise
        # Note: Don't set gameplay_stream = None here - it's needed by _end_game_impl
        # for sending winner effects. Stream cleanup happens after teardown phase.

    @staticmethod
    def _compute_accel_magnitude(accel) -> float:
        """
        Compute acceleration magnitude from 3-axis vector (g-force units).

        Standing still: sqrt(0^2 + 0^2 + 1^2) ~ 1.0g.
        Movement adds to this, e.g., 1.8g = significant movement.

        Args:
            accel: Vector3 protobuf with x, y, z components

        Returns:
            Scalar magnitude in g-force units
        """
        return math.sqrt(accel.x**2 + accel.y**2 + accel.z**2)

    @staticmethod
    def _update_ema(player: Player, accel_mag: float, weight: float = 4.0) -> None:
        """
        Apply exponential moving average filter to acceleration.

        Formula: smoothed = (smoothed * weight + raw) / (weight + 1)
        With weight=4 (default): 80% weight to previous value, 20% to current —
        smooths sensor noise. First reading primes the filter to prevent false
        deaths at game start.

        ``weight`` is sourced from the agent-domain ``perception.ema_weight`` flag
        (#766 F4), frozen per game at init (``self._ema_weight``); see __init__
        for why it is never re-read mid-game (#722 §5 variance-baseline caveat).
        The default of 4.0 preserves the historical hardcoded behavior for
        callers that do not pass a weight (e.g. unit tests).

        Args:
            player: Player whose EMA to update
            accel_mag: Raw acceleration magnitude for this frame
            weight: EMA weight on the previous smoothed value (default 4.0)
        """
        if player.smoothed_accel < 1e-9:  # Check for uninitialized (avoids float equality)
            player.smoothed_accel = accel_mag  # Prime filter with first real reading
        else:
            player.smoothed_accel = (player.smoothed_accel * weight + accel_mag) / (weight + 1)
        player.last_accel_mag = player.smoothed_accel

    def _compute_effective_thresholds(self, player: Player) -> tuple[float, float]:
        """
        Compute effective warning and death thresholds for a player.

        Uses LERP between slow/fast threshold arrays by the tempo intensity
        (UC-030 step 5, UC-031 BR-005), then applies per-player sensitivity factor.

        Hot path (every frame, per player): no flag reads, no awaits, no locks.

        Args:
            player: Player to compute thresholds for

        Returns:
            Tuple of (effective_warning_threshold, effective_death_threshold)
        """
        sens_idx = self.sensitivity.value
        now = time.time()

        # Tempo intensity now (0.0 = slow table, 1.0 = fast table), clamped to [0, 1].
        speed_percent = self._intensity.value_at(now)

        # LERP between slow and fast thresholds (instance tables, #766 F1)
        base_warn = self._lerp(self.slow_warning[sens_idx], self.fast_warning[sens_idx], speed_percent)
        base_death = self._lerp(self.slow_max[sens_idx], self.fast_max[sens_idx], speed_percent)

        # Apply per-player sensitivity factor combined with the live global
        # difficulty factor (#766 F6). Both are multiplied, then the COMBINED
        # factor is clamped to [0.5, 2.0] for safety before dividing thresholds.
        # Higher combined factor = lower threshold = easier to die. At the
        # neutral 1.0/1.0 this preserves the prior behavior exactly.
        combined_factor = player.sensitivity_factor * self.global_difficulty_factor
        clamped_factor = max(0.5, min(2.0, combined_factor))
        warn = base_warn / clamped_factor
        death = base_death / clamped_factor

        # Apply the per-player agent handicap (#1107) as a SEPARATE multiplicative
        # knob that composes with — does not replace — the sensitivity/difficulty
        # factor above. It is clamped independently to [0.5, 2.0] and MULTIPLIES
        # the threshold (intent-framed, opposite direction to sensitivity_factor):
        # >1.0 raises the threshold (harder to die / "help"); <1.0 lowers it
        # (easier / "rein in"). At the neutral 1.0 this is a no-op, preserving the
        # prior behavior exactly.
        # A time-boxed toughness boost (#1129) takes the MAX with the standing
        # handicap (it can only ever STRENGTHEN protection for its window, never
        # weaken a standing set_player_handicap delta). Precedence: while the
        # deadline is in the future the effective handicap is
        # max(handicap_factor, toughness_boost_factor); once it expires the boost
        # silently drops out and the standing handicap_factor resumes. Still
        # clamped [0.5, 2.0] below, so even a ~2.0 boost is "much harder to die",
        # never truly immune (the deliberate distinction from grant_shield).
        #
        # Composition precedence (last-applied wins by intent):
        #   1. standing handicap_factor (set_player_handicap, #1107)
        #   2. toughness boost (#1129) — STRENGTHENS via max (protection)
        #   2b. auto_rubberband boost (#1143) — STRENGTHENS via max (gap-compression)
        #   3. soft_penalty tighten (#1134) — WEAKENS via min (pressure)
        # The two STRENGTHENING boosts (toughness_boost, auto_rubberband) both
        # compose by MAX so the strongest active protection wins; the soft_penalty
        # MIN is applied AFTER, so an active tighten can cut through either boost:
        # pressure deliberately overrides protection (the mirror of toughness_boost,
        # which could never weaken a standing handicap). The final clamp [0.5, 2.0]
        # keeps even a fully-tightened threshold finite — never instant-death — and
        # caps even a maxed boost at 2x — "harder to die", never immune.
        effective_handicap = player.handicap_factor
        if now < player.toughness_boost_until:
            effective_handicap = max(effective_handicap, player.toughness_boost_factor)
        if now < player.rubberband_until:
            effective_handicap = max(effective_handicap, player.rubberband_boost)
        if now < player.soft_penalty_until:
            effective_handicap = min(effective_handicap, player.soft_penalty_factor)
        handicap = max(0.5, min(2.0, effective_handicap))
        return warn * handicap, death * handicap

    def _record_player_analytics(
        self,
        player: Player,
        serial: str,
        accel_mag: float,
        smoothed: float,
        effective_death: float,
    ) -> None:
        """
        Record analytics sample and emit periodic Prometheus metrics.

        Args:
            player: Player to record analytics for
            serial: Controller serial number
            accel_mag: Raw acceleration magnitude
            smoothed: Smoothed acceleration value
            effective_death: Current effective death threshold
        """
        config = get_config_manager().get_config()
        if not config.analytics.enabled or player.analytics is None:
            return

        # Record sample (returns current movement zone)
        zone = player.analytics.record_sample(
            raw_accel_mag=accel_mag,
            smoothed_accel=smoothed,
            death_threshold=effective_death,
            config=config.analytics,
            frame_duration_ms=1000.0 / config.update_frequency_hz,
        )

        # Emit Prometheus metrics periodically (every ~1 second)
        if player.analytics.sample_count % config.analytics.metrics_emit_interval_frames == 0:
            metrics.player_accel_magnitude.labels(serial=serial, game_id=self.game_id).set(accel_mag)
            metrics.player_movement_zone.labels(serial=serial, game_id=self.game_id).set(zone.value)
            metrics.player_peak_accel.labels(serial=serial, game_id=self.game_id).set(player.analytics.peak_accel)
            metrics.player_playstyle.labels(serial=serial, game_id=self.game_id).set(
                player.analytics.get_playstyle().value
            )
            # Intervention observability signals (#730 / #722 §7)
            metrics.player_movement_variance.labels(serial=serial, game_id=self.game_id).set(
                player.analytics.windowed_variance
            )
            metrics.player_skill_level.labels(serial=serial, game_id=self.game_id).set(
                player.analytics.get_skill_level()
            )
            # Whole-game RETAINED movement-variance aggregate (#1024). Emitted
            # while alive like skill_level; the agent retains it into the
            # conclusion snapshot so balanced-fitness spike-survival is meaningful
            # post-game (vs. the frozen-last-sample game_player_movement_variance).
            metrics.player_movement_variance_aggregate.labels(serial=serial, game_id=self.game_id).set(
                player.analytics.cumulative_variance
            )

        # Record to histogram for distribution analysis
        metrics.accel_distribution.labels(game_mode=self.get_game_name()).observe(accel_mag)

    def _process_health(self, player: Player, serial: str, controller_state) -> None:
        """Process controller health counters and manage degradation child spans.

        Uses a rolling window to calculate poll drop rate (drops/sec) rather
        than checking per-frame counts. This avoids false positives from single
        frames that accumulate pre-game drops and correctly detects sustained
        degradation at 100Hz polling with 60Hz streaming.

        Args:
            player: The player to process health for
            serial: Controller serial number
            controller_state: GameplayData protobuf (may have .health field)
        """
        health = controller_state.health if controller_state.HasField("health") else None
        config = get_config_manager().get_config()
        threshold = config.poll_drop_threshold
        has_led_issues = health and health.led_failures

        # Always accumulate totals for final summary, even below threshold
        if health and health.poll_drops:
            player.total_poll_drops += health.poll_drops
        if health and health.poll_errors:
            player.total_poll_errors += health.poll_errors

        # --- Rolling window rate detection for poll drops ---
        now = time.monotonic()
        window_seconds = 2.0  # evaluation window

        # Initialize window on first call
        if player._health_window_start is None:
            player._health_window_start = now
            player._health_window_drops = player.total_poll_drops

        elapsed = now - player._health_window_start
        if elapsed >= window_seconds:
            drops_in_window = player.total_poll_drops - player._health_window_drops
            player._health_drop_rate = drops_in_window / elapsed
            # Reset window
            player._health_window_start = now
            player._health_window_drops = player.total_poll_drops

        # Threshold is drops/sec (not drops/frame)
        has_poll_issues = player._health_drop_rate > threshold or (health and health.poll_errors > 0)

        # --- Poll degradation span (only when rolling rate exceeds threshold) ---
        if has_poll_issues:
            # Open child span on transition healthy -> degraded
            if player._poll_degraded_span is None and player.span:
                ctx = trace.set_span_in_context(player.span)
                player._poll_degraded_span = tracer.start_span(
                    "controller_poll_degraded",
                    context=ctx,
                    attributes={"player.serial": serial},
                )
            # Add event for this frame's drops inside the child span
            if player._poll_degraded_span:
                attrs: dict[str, float | int] = {"drop_rate": player._health_drop_rate}
                if health and health.poll_drops:
                    attrs["poll_drops"] = health.poll_drops
                if health and health.poll_errors:
                    attrs["poll_errors"] = health.poll_errors
                player._poll_degraded_span.add_event("poll_issues", attrs)
        else:
            # Close child span on transition degraded -> healthy
            if player._poll_degraded_span is not None:
                from opentelemetry.trace import Status, StatusCode

                player._poll_degraded_span.set_attribute("health.total_poll_drops", player.total_poll_drops)
                player._poll_degraded_span.set_attribute("health.total_poll_errors", player.total_poll_errors)
                msg = f"drops={player.total_poll_drops} errors={player.total_poll_errors} serial={serial}"
                player._poll_degraded_span.record_exception(PollDegradationError(msg))
                player._poll_degraded_span.set_status(Status(StatusCode.ERROR, "poll degradation detected"))
                player._poll_degraded_span.end()
                player._poll_degraded_span = None

        # --- LED degradation span ---
        if has_led_issues:
            player.total_led_failures += health.led_failures
            if player._led_degraded_span is None and player.span:
                ctx = trace.set_span_in_context(player.span)
                player._led_degraded_span = tracer.start_span(
                    "controller_led_degraded",
                    context=ctx,
                    attributes={"player.serial": serial},
                )
        else:
            if player._led_degraded_span is not None:
                player._led_degraded_span.set_attribute("health.total_led_failures", player.total_led_failures)
                player._led_degraded_span.end()
                player._led_degraded_span = None

    def _capture_controller_info(self, player: Player, serial: str, controller_state) -> None:
        """
        Capture the per-frame controller info that is not death detection: the
        name, the health counters and the battery. A mode that skips detection
        for some players (Fight Club waiting players, #1347) still calls this.
        """
        # Capture controller name on first frame and update span title
        if not player.name and controller_state.name:
            player.name = controller_state.name
            if player.span:
                player.span.update_name(f"player: {player.name}")
                player.span.set_attribute("player.name", player.name)

        # Process controller health counters (#571)
        self._process_health(player, serial, controller_state)

        # Capture the latest battery reading (#798) so the intervention battery
        # guard has a live in-process source. GameplayData.battery is the same
        # signal that feeds controller_battery_pct; normalize identically.
        player.battery_pct = battery_to_pct(controller_state.battery)

    async def _process_controller_state(self, controller_state):
        """
        Process a single controller's state and check for death.

        Args:
            controller_state: ControllerState protobuf message
        """
        serial = controller_state.serial

        if serial not in self.players:
            return  # Unknown controller

        player = self.players[serial]

        if not player.alive:
            return  # Dead player, ignore

        self._capture_controller_info(player, serial, controller_state)

        accel = controller_state.accel
        accel_mag = self._compute_accel_magnitude(accel)
        current_time = time.time()

        # UC-030 BR-005: the trigger is evaluated BEFORE the grace and death checks,
        # so an activation in this frame makes this frame a grace frame (A6 step 3).
        # Hot path: a bool read and a few compares; no flag read, no allocation.
        hold = self._shield_hold
        if hold is not None:
            # UC-030 A7 / BR-008: compares only; nothing is awaited, and the work
            # runs only while the trigger is held or an activation is running.
            pressed = controller_state.trigger_pressed
            if not pressed:
                player.trigger_armed = True
            if pressed or player.hold_started_at:
                self._process_shield_hold(player, pressed, current_time, hold)
        elif self.SHIELD_TRIGGER_ALLOWED:
            pressed = controller_state.trigger_pressed
            edge = pressed and player.trigger_armed and not player.trigger_prev
            player.trigger_prev = pressed
            if not pressed:
                player.trigger_armed = True
            # Await only when work is due (an edge, the expiry or the re-send),
            # so a held charge costs a few compares per frame, not a coroutine.
            if edge or (
                player.shield_charge_seconds > 0
                and (
                    (player.shield_charge_expires_at and current_time >= player.shield_charge_expires_at)
                    or (
                        current_time >= player.shield_charge_layer_until - SHIELD_CHARGE_RESEND_MARGIN_SECONDS
                        and (
                            not player.shield_charge_expires_at
                            or player.shield_charge_expires_at > player.shield_charge_layer_until
                        )
                    )
                )
            ):
                await self._process_shield_charge(player, edge, current_time)

        # Check grace period first - no death or warning during grace period
        # Matches original JoustMania: if time.time() > no_rumble. Uses the unified
        # effective-grace rule (#817): max(agent shield grace_until, admin
        # invincible_until) so the agent shield and admin baseline compose here too.
        if current_time < self.effective_grace_until(player):
            # During grace, reset the EMA so it re-primes from the first
            # post-grace frame. Invincibility must *forget* the death spike:
            # otherwise the EMA still holds it when grace expires and the
            # player is instantly re-killed by stale filter memory (#757).
            # Same re-prime pattern as NonStop revive (nonstop_joust.py).
            player.smoothed_accel = 0.0
            player.last_accel_mag = accel_mag
            self._record_player_analytics(
                player,
                serial,
                accel_mag,
                accel_mag,
                self._compute_effective_thresholds(player)[1],
            )
            return  # In grace period, skip death/warning checks

        if not (player.hold_prime_cap and self._prime_ema_after_hold(player, accel_mag)):
            self._update_ema(player, accel_mag, self._ema_weight)

        effective_warn, effective_death = self._compute_effective_thresholds(player)
        smoothed = player.smoothed_accel

        # Record analytics (handles enabled check internally)
        self._record_player_analytics(player, serial, accel_mag, smoothed, effective_death)

        # Death and warning checks (matches original JoustMania logic)
        # Key: warning is just feedback, NOT protection - player can die during warning!
        if smoothed > effective_death:
            # Player exceeded death threshold - kill them
            await self._kill_player(serial, smoothed)
        elif smoothed > effective_warn and current_time >= player.warning_until:
            # Player exceeded warning threshold and not already in warning state
            # Start warning feedback (flash + rumble)
            await self._warn_player(serial, smoothed, effective_warn)

    async def _warn_player(self, serial: str, accel_mag: float, threshold: float):
        """
        Warn a player that they're moving too much.

        This is purely visual/haptic feedback (flash + rumble) - NOT protection!
        Player can still die immediately if they exceed death threshold.
        Matches original JoustMania where warning was just feedback.

        Args:
            serial: Controller serial number
            accel_mag: Acceleration magnitude that triggered warning
            threshold: The effective warning threshold (after lerp)
        """
        from proto import controller_manager_pb2

        player = self.players.get(serial)
        if not player or not player.alive:
            return

        # Set warning feedback duration (prevents repeated warnings during flash)
        # This is NOT protection - player can still die during this time!
        player.warning_until = time.time() + WARNING_DURATION

        # Analytics: Record warning event
        if player.analytics is not None:
            player.analytics.record_warning()
            metrics.player_warnings_total.labels(serial=serial, game_mode=self.get_game_name()).inc()

        # Add warning event to player's lifecycle span
        if player.span:
            player.span.add_event(
                "death_warning",
                attributes={
                    "accel_magnitude": accel_mag,
                    "threshold": threshold,
                    "sensitivity": self.sensitivity.name,
                    "sensitivity_factor": player.sensitivity_factor,
                    "music_speed": self.music_speed,
                },
            )
        logger.info(f"Player {serial} triggered warning (accel: {accel_mag:.2f}, threshold: {threshold:.2f})")

        # Send warning effect via stream (white flash + vibrate, auto-restore)
        # Use player's span as parent so effect appears under player_lifecycle in traces
        if self.gameplay_stream:
            trace_parent, trace_state = inject_trace_context(player.span)
            effect_cmd = controller_manager_pb2.GameplayStreamControl(
                game_effect=controller_manager_pb2.GameEffectCommand(
                    serial=serial,
                    effect=controller_manager_pb2.GAME_EFFECT_PLAYER_WARNING,
                    trace_parent=trace_parent,
                    trace_state=trace_state,
                )
            )
            await self.gameplay_stream.write(effect_cmd)

    @staticmethod
    def effective_grace_until(player: "Player") -> float:
        """
        Single source of truth for a player's effective death-immunity deadline (#817).

        Two independent actors grant temporary death-immunity through different
        fields, and #817 (epic #814) ratifies how they compose:

        - **Admin baseline** — ``invincible_until``: the pre-game
          ``invincibility_seconds`` flag, materialized at match/round start by
          Tournament/Fight Club (mode-specific; base ``Player`` has no such
          field, so we read it defensively). The human owns this baseline.
        - **Agent delta** — ``grace_until``: a temporary additive shield granted
          mid-game via ``grant_shield`` (also reused by respawn/spawn grace). The
          agent owns this bounded in-game delta and NEVER persists it to game.json.

        Composition rule (ratified, do not relitigate):

            effective_grace_until = max(grace_until, invincible_until)

        i.e. a player is death-immune while *either* window is open. The agent can
        only ever EXTEND protection (``grant_shield`` is extend-not-shorten on
        ``grace_until``); it can neither shorten the admin baseline nor be
        shortened by it. Symmetrically the admin baseline never silently cancels
        an active agent shield. ``max`` is the only rule that honors both
        ownership boundaries at once — overwrite or last-write-wins would let one
        actor silently void the other.

        Every death/immunity check (the base per-frame gate and the mode-specific
        ``_kill_player_impl`` / ``_process_controller_state`` checks in Tournament
        and Fight Club) routes through this helper so the rule lives in exactly
        one place.

        Amended by #2096 (UC-030 BR-008): a running shield hold adds its own
        deadline, ``hold_until``. Hold never writes ``grace_until``, so a release
        can never shorten another window; ``hold_until`` is 0 outside hold games.
        """
        return max(BaseGameMode._other_grace_until(player), player.hold_until)

    @staticmethod
    def _other_grace_until(player: "Player") -> float:
        """The effective grace without the shield hold: ``max(grace_until, invincible_until)`` (#817).

        A hold activation waits for this window to close (UC-030 BR-008: holding
        during another grace does not drain).
        """
        # base Player has no invincible_until; only Tournament/Fight Club add it.
        return max(player.grace_until, getattr(player, "invincible_until", 0.0))

    def is_in_grace(self, player: "Player", now: float | None = None) -> bool:
        """True while the player's effective grace (admin ∪ agent) is still open (#817)."""
        if now is None:
            now = time.time()
        return now < self.effective_grace_until(player)

    async def grant_shield(self, serial: str, seconds: float) -> bool:
        """
        Grant a temporary shield (invulnerability) to a player (#730, N3).

        A shield extends the player's ``grace_until`` timestamp: the per-frame
        check in ``_process_controller_state`` already skips all death/warning
        checks while ``time.time() < grace_until``, so this is the natural,
        mode-agnostic shield primitive (Tournament/Fight Club ``invincible_until``
        and Nonstop ``spawn_protected`` are the same idea). FFA-first per
        docs/research/722-intervention-surface.md §3, but the primitive lives on
        ``BaseGameMode`` so every mode inherits it. Mode-level opt-outs (e.g.
        hidden-role LED leaks) are enforced by the intervention manager's §9
        capability matrix BEFORE the handler runs — this primitive does not
        re-check mode.

        Idempotent-extend semantics: a new shield only ever extends an existing
        one. If the player is already shielded past ``now + seconds`` the existing
        (longer) grace is kept; a shorter request never shortens it.

        Shield limits (#2044, UC-040 A34, BR-022): after the alive check (and in
        trigger mode after ``already_charged``) a grant is refused with
        ``InterventionNotApplied`` once the player has had
        ``policy.max_shields_per_player_per_game`` agent shields in this game
        (``shield_cap_reached``), or while ``now`` is before the end of the last
        agent shield plus ``policy.shield_cooldown_seconds`` (``shield_cooldown``).
        So with a cooldown set, a running agent shield is never extended; cooldown
        0 restores extend-not-shorten. A kept (no-op) grant does not count.

        Shield feedback (#1301, UC-040 A16): the ``shield_up`` sound, a
        ``GAME_EFFECT_SHIELD`` for the remaining protection (rendered by the
        controller manager: fade, last-second blink, restore) and a
        ``player_shielded`` event. A kept (no-op) grant sends no feedback.

        Args:
            serial: Controller serial number.
            seconds: Shield duration in seconds. Non-positive durations are a
                no-op (returns False).

        Returns:
            True if a shield was granted/extended, False if the player is
            unknown/dead or the duration was non-positive.
        """
        if seconds <= 0:
            return False

        player = self.players.get(serial)
        if not player or not player.alive:
            return False

        # UC-040 BR-027: a hold game adds the grant to the player's energy (A36).
        latched_here = not self._shield_hold_latched
        hold = self._latch_shield_hold()
        if hold is not None:
            return self._grant_shield_energy(player, seconds, hold)

        # UC-040 BR-019: in trigger mode the grant is a charge the player activates
        # (A30). Read per grant, so a flip applies to the next grant only; the grant
        # that latched the game reuses the latch's read.
        mode, self._shield_mode_at_latch = self._shield_mode_at_latch, None
        if mode is None or not latched_here:
            mode = self._shield_activation_mode()
        trigger = mode == SHIELD_ACTIVATION_TRIGGER
        if trigger and player.shield_charge_seconds > 0:
            # UC-040 A31: a held charge is refused before the shield limits.
            from services.game_coordinator.intervention_errors import InterventionNotApplied

            logger.info(f"Shield charge for {serial} refused: a charge is already held")
            raise InterventionNotApplied("already_charged")

        now = time.time()
        self._check_shield_limits(player, now)

        if trigger:
            granted = await self._grant_shield_charge(player, seconds)
            # BR-022: in trigger mode the cooldown runs from the grant (the charge
            # stored), not from the activation.
            self._record_agent_shield(serial, now, seconds)
            return granted

        new_grace = now + seconds
        # Extend-not-shorten (#817): the agent only ever writes its own
        # grace_until field — never invincible_until (the admin baseline). If the
        # player's effective grace (max of agent shield + admin invincibility) is
        # already longer than the request, this shield is a no-op extension: it
        # adds nothing, and crucially does NOT shorten an active admin baseline.
        if new_grace <= self.effective_grace_until(player):
            logger.info(
                f"Shield for {serial}: existing effective grace "
                f"({self.effective_grace_until(player):.2f}) already exceeds "
                f"requested ({new_grace:.2f}); kept"
            )
            return True

        # Sound only on the unprotected -> protected transition (#1301, UC-040
        # BR-010); an extension restarts the LED fade silently.
        was_protected = self._shield_protected(player, time.time())
        player.grace_until = new_grace
        self._record_agent_shield(serial, now, seconds)
        logger.info(f"Shield granted to {serial} for {seconds:.1f}s (grace_until={new_grace:.2f})")

        if player.span:
            player.span.add_event(
                "shield_granted",
                attributes={"shield_seconds": seconds, **player_identity_attributes(serial, player.name)},
            )

        # Perceptible feedback for the remaining protection (#1301, UC-040 A16).
        await self._send_shield_feedback(player, new_grace - time.time(), kind="full", sound=not was_protected)

        return True

    def _shield_limits(self) -> tuple[int, int]:
        """``(max shields per player per game, cooldown seconds)`` for the next grant (UC-040 BR-022).

        Operator flags in the ``agent`` set, read per grant with the game's context
        (flags.md §3). 0 means unlimited / off. A read error or a negative value
        gives the default (3 / 15 s), so the limits stay on.
        """
        # Literal flag keys, so the contract scan resolves the reads.
        max_shields = read_int_flag(
            "agent",
            "policy.max_shields_per_player_per_game",
            DEFAULT_MAX_SHIELDS_PER_PLAYER_PER_GAME,
            game_id=self.game_id,
        )
        cooldown = read_int_flag(
            "agent", "policy.shield_cooldown_seconds", DEFAULT_SHIELD_COOLDOWN_SECONDS, game_id=self.game_id
        )
        return (
            resolve_shield_limit(max_shields, DEFAULT_MAX_SHIELDS_PER_PLAYER_PER_GAME),
            resolve_shield_limit(cooldown, DEFAULT_SHIELD_COOLDOWN_SECONDS),
        )

    def _check_shield_limits(self, player: "Player", now: float) -> None:
        """Raise ``InterventionNotApplied`` when the cap or the cooldown refuses a grant (UC-040 A34)."""
        max_shields, cooldown = self._shield_limits()
        used = self._agent_shield_grants.get(player.serial, 0)
        if max_shields > 0 and used >= max_shields:
            self._refuse_shield(player, SHIELD_REFUSED_CAP, used, 0.0)
        last_end = self._agent_shield_last_end.get(player.serial)
        if cooldown > 0 and last_end is not None and now < last_end + cooldown:
            self._refuse_shield(player, SHIELD_REFUSED_COOLDOWN, used, last_end + cooldown - now)

    def _refuse_shield(self, player: "Player", reason: str, used: int, cooldown_remaining: float) -> None:
        """Log, record ``shield_refused`` on the player's span and raise (UC-040 A34)."""
        from services.game_coordinator.intervention_errors import InterventionNotApplied

        logger.info(
            f"Shield for {player.serial} refused: {reason} "
            f"(grants used {used}, cooldown remaining {cooldown_remaining:.1f}s)"
        )
        if player.span:
            with contextlib.suppress(Exception):
                player.span.add_event(
                    "shield_refused",
                    attributes={
                        "shield.refusal_reason": reason,
                        "shield.grants_used": used,
                        "shield.cooldown_remaining_s": round(cooldown_remaining, 3),
                        **player_identity_attributes(player.serial, player.name),
                    },
                )
        raise InterventionNotApplied(reason)

    def _record_agent_shield(self, serial: str, now: float, seconds: float) -> None:
        """Count an applied agent shield and remember its own end (UC-040 BR-022; never ``grace_until``)."""
        self._agent_shield_grants[serial] = self._agent_shield_grants.get(serial, 0) + 1
        self._agent_shield_last_end[serial] = now + seconds

    # Toughness boost defaults (#1129, #1103 Phase 2). Boost is clamped to the same
    # upper bound as handicap_factor so a toughness boost is "much harder to die"
    # but never immune; seconds are capped so a single arming can't last forever.
    TOUGHNESS_BOOST_DEFAULT_FACTOR = 2.0
    TOUGHNESS_BOOST_FACTOR_MIN = 1.0
    TOUGHNESS_BOOST_FACTOR_MAX = 2.0
    TOUGHNESS_BOOST_MAX_SECONDS = 30.0

    async def grant_toughness_boost(self, serial: str, seconds: float, boost: float | None = None) -> bool:
        """
        Grant a time-boxed toughness boost to a player (#1129, #1103 Phase 2).

        Unlike :meth:`grant_shield` (total immunity via ``grace_until`` that skips
        the death check entirely), a toughness boost only raises the player's
        effective death/warning threshold for ``seconds`` by applying a temporary
        ``handicap`` boost — making them MUCH harder to eliminate but NOT immune: a
        genuinely huge spike can still exceed the (still-finite) boosted threshold.
        It reuses the #1107 per-player handicap mechanism: while the deadline is in
        the future, ``_compute_effective_thresholds`` takes ``max(handicap_factor,
        toughness_boost_factor)`` (so it never weakens a standing set_player_handicap
        delta, only strengthens it for the window), still clamped [0.5, 2.0].

        Time-box semantics mirror ``grant_shield``: extend-not-shorten on the
        deadline. A new arming only ever lengthens an active window (and never
        lowers an active boost); a shorter/weaker request is a no-op extension. On
        expiry the boost silently drops out — no reset task needed — and the
        standing ``handicap_factor`` resumes.

        Args:
            serial: Controller serial number.
            seconds: Shield duration in seconds. Non-positive is a no-op (False);
                capped at ``TOUGHNESS_BOOST_MAX_SECONDS``.
            boost: Handicap boost while active; clamped
                [``TOUGHNESS_BOOST_FACTOR_MIN``, ``TOUGHNESS_BOOST_FACTOR_MAX``].
                Defaults to ``TOUGHNESS_BOOST_DEFAULT_FACTOR`` (~2.0).

        Returns:
            True if a toughness boost was armed/extended, False if the player is
            unknown/dead or the duration was non-positive.
        """
        if seconds <= 0:
            return False

        player = self.players.get(serial)
        if not player or not player.alive:
            return False

        seconds = min(seconds, self.TOUGHNESS_BOOST_MAX_SECONDS)
        if boost is None:
            boost = self.TOUGHNESS_BOOST_DEFAULT_FACTOR
        boost = max(self.TOUGHNESS_BOOST_FACTOR_MIN, min(self.TOUGHNESS_BOOST_FACTOR_MAX, boost))

        new_until = time.time() + seconds
        # Extend-not-shorten / strengthen-not-weaken: keep whichever active window
        # is longer and whichever active boost is higher, so repeated arming never
        # reduces in-flight protection.
        changed = True
        was_protected = self._shield_protected(player, time.time())
        if time.time() < player.toughness_boost_until:
            changed = new_until > player.toughness_boost_until or boost > player.toughness_boost_factor
            new_until = max(new_until, player.toughness_boost_until)
            boost = max(boost, player.toughness_boost_factor)
        player.toughness_boost_until = new_until
        player.toughness_boost_factor = boost
        logger.info(f"Toughness boost armed for {serial}: boost={boost:.2f} for {seconds:.1f}s (until={new_until:.2f})")

        if player.span:
            player.span.add_event(
                "toughness_boost_armed",
                attributes={
                    "toughness_boost_seconds": seconds,
                    "toughness_boost_factor": boost,
                    **player_identity_attributes(serial, player.name),
                },
            )

        # Weaker perceptible feedback, only when the arming changed the window or
        # the boost (#1301, UC-040 A16, BR-009/BR-010).
        if changed:
            await self._send_shield_feedback(
                player, new_until - time.time(), kind="toughness_boost", sound=not was_protected
            )

        return True

    def _shield_protected(self, player: "Player", now: float) -> bool:
        """Whether any shield protection (grace / invincibility / toughness boost) is active."""
        return now < self.effective_grace_until(player) or now < player.toughness_boost_until

    async def _send_shield_feedback(
        self,
        player: "Player",
        remaining_s: float,
        *,
        kind: str,
        sound: bool = True,
        charge_delay_ms: int | None = None,
    ) -> None:
        """Shield LED effect + shield sound + ``player_shielded`` event (#1301).

        UC-040 A16 / BR-008..BR-010. One command per grant: the controller manager
        renders the fade / last-second blink / restore, so nothing is streamed and
        no task is kept here. ``sound`` is False for an extension of an already
        protected player (the LED restarts silently). Each channel is tried on
        its own, so one failing channel never skips the others; feedback never
        fails the intervention.

        ``charge_delay_ms`` marks the activation of a shield charge (UC-030 A6,
        #1837): the swell rumble follows the shield LED, and the span event and
        ``player_shielded`` carry ``activation=trigger`` and the grant-to-use delay.
        """
        if remaining_s <= 0:
            return
        serial = player.serial
        duration_ms = int(round(remaining_s * 1000))
        until = time.time() + remaining_s
        # Keep the latest end of any running shield: a shorter toughness boost after
        # a longer full one must not hide the full one from _end_shield_feedback.
        self._shield_feedback_until[serial] = max(self._shield_feedback_until.get(serial, 0.0), until)
        activated = charge_delay_ms is not None
        if player.span:
            with contextlib.suppress(Exception):
                attributes = {
                    "shield.kind": kind,
                    "shield.duration_ms": duration_ms,
                    "shield.sound": Sound.SFX_SHIELD_UP.value if sound else "none",
                }
                if activated:
                    attributes["shield.activation"] = SHIELD_ACTIVATION_TRIGGER
                player.span.add_event("shield_feedback", attributes=attributes)
        if self.gameplay_stream:
            try:
                from proto import controller_manager_pb2

                trace_parent, trace_state = inject_trace_context(player.span)
                await self.gameplay_stream.write(
                    controller_manager_pb2.GameplayStreamControl(
                        game_effect=controller_manager_pb2.GameEffectCommand(
                            serial=serial,
                            effect=(
                                controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST
                                if kind == "toughness_boost"
                                else controller_manager_pb2.GAME_EFFECT_SHIELD
                            ),
                            duration_ms=duration_ms,
                            trace_parent=trace_parent,
                            trace_state=trace_state,
                        )
                    )
                )
            except Exception as e:
                logger.warning(f"Shield LED feedback for {serial} failed (protection unaffected): {e}")
        if activated:
            # UC-030 A6 step 2 / BR-021: the activation swell (rumble only) right
            # after the shield LED, before the slower sound RPC.
            await self._write_shield_effect(player, "GAME_EFFECT_SHIELD_SWELL", 0)
        if sound:
            try:
                await self._play_sound(
                    Sound.SFX_SHIELD_UP, priority=2, volume=0.5 if kind == "toughness_boost" else 1.0
                )
            except Exception as e:
                logger.warning(f"Shield sound for {serial} failed (protection unaffected): {e}")
        try:
            await self.event_publisher(
                GameEvent.PLAYER_SHIELDED,
                {
                    "serial": serial,
                    "kind": kind,
                    "seconds": f"{remaining_s:.1f}",
                    "until_ms": str(int(until * 1000)),
                    **(
                        {"activation": SHIELD_ACTIVATION_TRIGGER, "charge_delay_ms": str(charge_delay_ms)}
                        if activated
                        else {}
                    ),
                },
            )
        except Exception as e:
            logger.warning(f"player_shielded event for {serial} failed (protection unaffected): {e}")

    async def _end_shield_feedback(self) -> None:
        """End every shield effect still running before the end-of-game effects (#1301, UC-040 A17).

        A zero-duration SHIELD ends only a running shield on the controller (the
        controller manager never cancels another effect for it) and restores the
        base colour. Best-effort; clears the record either way.

        A held shield charge ends here too, with ``game_end`` (UC-040 A32, #1837).
        """
        for player in self.players.values():
            if player.shield_charge_seconds > 0:
                await self._discard_shield_charge(player, CHARGE_OUTCOME_GAME_END)
        now = time.time()
        # UC-040 A39: a hold game drops every pool; an open activation ends game_end.
        if self._shield_hold is not None:
            for player in self.players.values():
                self._drop_shield_hold(player, HOLD_OUTCOME_GAME_END, now)
        running = [serial for serial, until in self._shield_feedback_until.items() if until > now]
        self._shield_feedback_until.clear()
        if not running or not self.gameplay_stream:
            return
        try:
            from proto import controller_manager_pb2

            for serial in running:
                await self.gameplay_stream.write(
                    controller_manager_pb2.GameplayStreamControl(
                        game_effect=controller_manager_pb2.GameEffectCommand(
                            serial=serial,
                            effect=controller_manager_pb2.GAME_EFFECT_SHIELD,
                            duration_ms=0,
                        )
                    )
                )
        except Exception as e:
            logger.warning(f"Ending shield feedback failed: {e}")

    # ------------------------------------------------------------------
    # Shield charge (#1837, FR-160; UC-040 A30-A32, BR-019-BR-021; UC-030 A6)
    # ------------------------------------------------------------------

    def _shield_activation_mode(self) -> str:
        """``instant``, ``trigger`` or ``hold`` for the next grant_shield (UC-040 BR-019, BR-027).

        Only an allow-listed mode (``SHIELD_TRIGGER_ALLOWED``, Free-for-All) and
        only a real game reads the flag; every other game is instant. A game
        latched to hold stays hold; otherwise the flag is read per grant, never per
        frame, and a ``hold`` read in a game not latched to hold gives the mode the
        game latched at its start (``instant`` or ``trigger``).
        """
        if self._shield_hold is not None:
            return SHIELD_ACTIVATION_HOLD
        if not self.SHIELD_TRIGGER_ALLOWED or self.game_kind == GAME_KIND_SHADOW:
            return SHIELD_ACTIVATION_INSTANT
        # Literal flag key, so the contract scan resolves the read.
        value = read_string_flag("game", "shield.activation", DEFAULT_SHIELD_ACTIVATION, game_id=self.game_id)
        mode = resolve_shield_activation(value)
        if mode == SHIELD_ACTIVATION_HOLD and self._shield_hold_latched:
            # BR-027: a game not latched to hold keeps the mode it latched at its start.
            return self._shield_mode_at_start
        return mode

    def _read_charge_expiry(self) -> float:
        """Seconds a new charge is held before it expires, 0 = never (flags.md §4, read per grant)."""
        value = read_float_flag(
            "game", "shield.charge_expiry_seconds", DEFAULT_CHARGE_EXPIRY_SECONDS, game_id=self.game_id
        )
        return resolve_charge_expiry(value)

    async def _write_shield_effect(self, player: "Player", effect_name: str, duration_ms: int) -> None:
        """Best-effort ``GameEffectCommand`` for a shield-charge effect on the player's controller."""
        if not self.gameplay_stream:
            return
        try:
            from proto import controller_manager_pb2

            trace_parent, trace_state = inject_trace_context(player.span)
            await self.gameplay_stream.write(
                controller_manager_pb2.GameplayStreamControl(
                    game_effect=controller_manager_pb2.GameEffectCommand(
                        serial=player.serial,
                        effect=getattr(controller_manager_pb2, effect_name),
                        duration_ms=duration_ms,
                        trace_parent=trace_parent,
                        trace_state=trace_state,
                    )
                )
            )
        except Exception as e:
            logger.warning(f"{effect_name} for {player.serial} failed (charge state unaffected): {e}")

    async def _grant_shield_charge(self, player: "Player", seconds: float) -> bool:
        """Store one shield charge for the player (UC-040 A30, BR-020).

        ``grace_until`` is left alone: the player is not protected until they
        activate the charge with the trigger (UC-030 A6). A grant while a charge
        is held is refused by ``grant_shield`` before this runs (``already_charged``,
        A31); the intervention manager records it as blocked and refunds the
        backstop reservation.
        """
        now = time.time()
        expiry = self._read_charge_expiry()
        player.shield_charge_seconds = seconds
        player.shield_charge_granted_at = now
        player.shield_charge_expires_at = now + expiry if expiry > 0 else 0.0
        identity = player_identity_attributes(player.serial, player.name)
        logger.info(f"Shield charge granted to {player.serial}: {seconds:.1f}s, expiry {expiry:.0f}s (0 = never)")

        if player.span:
            with contextlib.suppress(Exception):
                player.span.add_event(
                    "shield_charge_granted",
                    attributes={"shield.seconds": seconds, "shield.charge_expiry_s": expiry, **identity},
                )
            with contextlib.suppress(Exception):
                player._shield_charge_span = tracer.start_span(
                    "game.shield_charge",
                    context=trace.set_span_in_context(player.span),
                    attributes={
                        **identity,
                        "game.id": self.game_id,
                        "shield.seconds": seconds,
                        "shield.charge_expiry_s": expiry,
                    },
                )

        await self._send_shield_charge_layer(player, now)
        try:
            await self.event_publisher(
                GameEvent.PLAYER_SHIELD_CHARGED,
                {
                    "serial": player.serial,
                    "seconds": f"{seconds:.1f}",
                    "expires_ms": str(int(player.shield_charge_expires_at * 1000)),
                },
            )
        except Exception as e:
            logger.warning(f"player_shield_charged event for {player.serial} failed (charge unaffected): {e}")
        return True

    async def _send_shield_charge_layer(self, player: "Player", now: float) -> None:
        """Send (or re-send) the charge LED layer for the time left, capped at 120 s (BR-021)."""
        if player.shield_charge_expires_at:
            layer_s = min(player.shield_charge_expires_at - now, SHIELD_CHARGE_LAYER_MAX_SECONDS)
        else:
            layer_s = SHIELD_CHARGE_LAYER_MAX_SECONDS
        if layer_s <= 0:
            return
        # Recorded before the write, so a failing stream never re-sends every frame.
        player.shield_charge_layer_until = now + layer_s
        await self._write_shield_effect(player, "GAME_EFFECT_SHIELD_CHARGE", int(round(layer_s * 1000)))

    async def _process_shield_charge(self, player: "Player", edge: bool, now: float) -> None:
        """Frame-path work for a player who holds a charge or pressed the trigger (UC-030 BR-005).

        Lazy expiry (no timer, BR-020), the re-send before the 120 s layer cap
        (BR-021), then the activation when the press is valid: a charge is held
        and no grace, shield or invincibility window is open. An invalid press does
        nothing and is not remembered.
        """
        if player.shield_charge_seconds > 0:
            expires_at = player.shield_charge_expires_at
            if expires_at and now >= expires_at:
                await self._discard_shield_charge(player, CHARGE_OUTCOME_EXPIRED)
            elif now >= player.shield_charge_layer_until - SHIELD_CHARGE_RESEND_MARGIN_SECONDS and (
                not expires_at or expires_at > player.shield_charge_layer_until
            ):
                await self._send_shield_charge_layer(player, now)
        if not edge or player.shield_charge_seconds <= 0:
            return
        if now < self.effective_grace_until(player):
            return
        await self._activate_shield_charge(player, now)

    def _clear_shield_charge(self, player: "Player") -> None:
        player.shield_charge_seconds = 0.0
        player.shield_charge_granted_at = 0.0
        player.shield_charge_expires_at = 0.0
        player.shield_charge_layer_until = 0.0

    @staticmethod
    def _end_shield_charge_span(player: "Player", outcome: str, delay_ms: int | None = None) -> None:
        span, player._shield_charge_span = player._shield_charge_span, None
        if span is None:
            return
        with contextlib.suppress(Exception):
            span.set_attribute("shield.charge_outcome", outcome)
            if delay_ms is not None:
                span.set_attribute("shield.charge_delay_ms", delay_ms)
            span.end()

    async def _activate_shield_charge(self, player: "Player", now: float) -> None:
        """The player activates the charge (UC-030 A6): today's shield plus the swell."""
        seconds = player.shield_charge_seconds
        delay_ms = int(round((now - player.shield_charge_granted_at) * 1000))
        self._clear_shield_charge(player)
        player.grace_until = max(player.grace_until, now + seconds)
        logger.info(f"Shield charge activated by {player.serial}: {seconds:.1f}s after {delay_ms} ms")

        # A6 step 2: SHIELD_CHARGE 0, then SHIELD and SWELL (in _send_shield_feedback).
        await self._write_shield_effect(player, "GAME_EFFECT_SHIELD_CHARGE", 0)
        if player.span:
            with contextlib.suppress(Exception):
                player.span.add_event(
                    "shield_activated",
                    attributes={
                        "shield.seconds": seconds,
                        "shield.charge_delay_ms": delay_ms,
                        **player_identity_attributes(player.serial, player.name),
                    },
                )
        self._end_shield_charge_span(player, CHARGE_OUTCOME_ACTIVATED, delay_ms)
        await self._publish_charge_ended(player, CHARGE_OUTCOME_ACTIVATED)
        # The player acted, so the sound always plays (UC-040 A16 / UC-030 A6).
        await self._send_shield_feedback(
            player, player.grace_until - now, kind="full", sound=True, charge_delay_ms=delay_ms
        )

    async def _discard_shield_charge(self, player: "Player", reason: str) -> None:
        """Drop a held charge unused: expired, death or game end (UC-040 A32)."""
        if player.shield_charge_seconds <= 0:
            return
        self._clear_shield_charge(player)
        logger.info(f"Shield charge of {player.serial} ended unused: {reason}")
        await self._write_shield_effect(player, "GAME_EFFECT_SHIELD_CHARGE", 0)
        if player.span:
            with contextlib.suppress(Exception):
                player.span.add_event(
                    "shield_charge_ended",
                    attributes={
                        "shield.charge_outcome": reason,
                        **player_identity_attributes(player.serial, player.name),
                    },
                )
        self._end_shield_charge_span(player, reason)
        await self._publish_charge_ended(player, reason)

    async def _publish_charge_ended(self, player: "Player", reason: str) -> None:
        try:
            await self.event_publisher(
                GameEvent.PLAYER_SHIELD_CHARGE_ENDED, {"serial": player.serial, "reason": reason}
            )
        except Exception as e:
            logger.warning(f"player_shield_charge_ended event for {player.serial} failed: {e}")

    async def _resend_shield_layers(self) -> None:
        """Re-send held charges and running shields on a (re)created stream (UC-040 BR-021).

        Nothing is held when the stream is first created, so this only sends
        after a mid-game reconnect, on every attempt. A running full shield or
        toughness boost is re-sent with the time left; a charge with its layer.
        """
        now = time.time()
        for serial, player in self.players.items():
            if not player.alive:
                continue
            if player.shield_charge_seconds > 0:
                await self._send_shield_charge_layer(player, now)
            if self._shield_feedback_until.get(serial, 0.0) <= now:
                continue
            if player.grace_until > now:
                await self._write_shield_effect(
                    player, "GAME_EFFECT_SHIELD", int(round((player.grace_until - now) * 1000))
                )
            if player.toughness_boost_until > now:
                await self._write_shield_effect(
                    player, "GAME_EFFECT_TOUGHNESS_BOOST", int(round((player.toughness_boost_until - now) * 1000))
                )

    def _close_shield_charge_spans(self) -> None:
        """End every open ``game.shield_charge`` and ``game.shield_hold`` span (error teardown path); no I/O."""
        now = time.time()
        for player in self.players.values():
            if player._shield_charge_span is not None:
                self._clear_shield_charge(player)
                self._end_shield_charge_span(player, CHARGE_OUTCOME_GAME_END)
            if player.hold_started_at:
                self._drop_shield_hold(player, HOLD_OUTCOME_GAME_END, now)

    # ------------------------------------------------------------------
    # Shield hold (#2096, FR-161; UC-030 A7, BR-008, BR-009; UC-040 A36-A39,
    # BR-024-BR-027). Everything here is synchronous: no I/O, nothing awaited.
    # ------------------------------------------------------------------

    def _latch_shield_hold(self) -> ShieldHoldParams | None:
        """Latch hold mode and its values once per game (UC-040 BR-027); None = not a hold game.

        Called at the start of the game loop and by the first grant, in the game's
        flag context, so an experiment arm resolves. Only Free-for-All real games
        (the BR-019 allow-list) can be hold games. Every player starts with the
        starting energy, clamped to the cap and the budget.
        """
        if self._shield_hold_latched:
            return self._shield_hold
        if not self.SHIELD_TRIGGER_ALLOWED or self.game_kind == GAME_KIND_SHADOW:
            self._shield_hold_latched = True
            return None
        # Before the latch is set this read keeps `hold` (it is not mapped to instant).
        mode = self._shield_activation_mode()
        self._shield_hold_latched = True
        if mode != SHIELD_ACTIVATION_HOLD:
            self._shield_mode_at_latch = mode
            self._shield_mode_at_start = mode
            return None
        raw = {
            "shield.energy_max_seconds": read_float_flag(
                "game",
                "shield.energy_max_seconds",
                SHIELD_HOLD_FLAGS["shield.energy_max_seconds"][0],
                game_id=self.game_id,
            ),
            "shield.energy_budget_seconds": read_float_flag(
                "game",
                "shield.energy_budget_seconds",
                SHIELD_HOLD_FLAGS["shield.energy_budget_seconds"][0],
                game_id=self.game_id,
            ),
            "shield.energy_initial_seconds": read_float_flag(
                "game",
                "shield.energy_initial_seconds",
                SHIELD_HOLD_FLAGS["shield.energy_initial_seconds"][0],
                game_id=self.game_id,
            ),
            "shield.recharge_per_second": read_float_flag(
                "game",
                "shield.recharge_per_second",
                SHIELD_HOLD_FLAGS["shield.recharge_per_second"][0],
                game_id=self.game_id,
            ),
            "shield.min_burst_seconds": read_float_flag(
                "game",
                "shield.min_burst_seconds",
                SHIELD_HOLD_FLAGS["shield.min_burst_seconds"][0],
                game_id=self.game_id,
            ),
        }
        values = {key: resolve_hold_value(raw[key], *bounds) for key, bounds in SHIELD_HOLD_FLAGS.items()}
        params = ShieldHoldParams(
            cap=values["shield.energy_max_seconds"],
            budget=values["shield.energy_budget_seconds"],
            initial=values["shield.energy_initial_seconds"],
            recharge=values["shield.recharge_per_second"],
            min_burst=values["shield.min_burst_seconds"],
        )
        initial = min(params.initial, params.cap, params.budget)
        for player in self.players.values():
            player.hold_energy = initial
            player.hold_energy_at = 0.0
        self._shield_hold = params
        logger.info(f"Shield hold latched for {self.game_id}: {params}")
        return params

    def _start_shield_hold_clock(self, now: float) -> None:
        """Start the recharge clock of every pool at the start of the game loop (UC-040 A38)."""
        if self._shield_hold is None:
            return
        for player in self.players.values():
            if not player.hold_energy_at and not player.hold_started_at:
                player.hold_energy_at = now

    def _shield_hold_budget_left(self, player: "Player", now: float) -> float:
        """Budget remaining at ``now``, counting the running activation (UC-040 BR-024)."""
        used = player.hold_budget_used
        if player.hold_started_at:
            used += max(0.0, min(now, player.hold_until) - player.hold_started_at)
        return max(0.0, self._shield_hold.budget - used)

    def _shield_hold_energy(self, player: "Player", now: float) -> float:
        """The player's energy at ``now``: drained while active, recharged while idle (UC-040 BR-024, A38)."""
        hold = self._shield_hold
        if player.hold_started_at:
            spent = max(0.0, min(now, player.hold_until) - player.hold_started_at)
            return max(0.0, player.hold_energy_start - spent)
        energy = player.hold_energy
        if hold.recharge > 0 and player.hold_energy_at and now > player.hold_energy_at:
            energy += hold.recharge * (now - player.hold_energy_at)
        return max(0.0, min(energy, hold.cap, self._shield_hold_budget_left(player, now)))

    def _process_shield_hold(self, player: "Player", pressed: bool, now: float, hold: ShieldHoldParams) -> None:
        """Frame-path state machine of the shield hold (UC-030 A7, BR-008); compares and field writes only."""
        if player.hold_started_at:
            if player.hold_pressed:
                if now >= player.hold_until:
                    # Held until the usable energy ran out: the budget or the pool.
                    budget_limited = player.hold_budget_start <= player.hold_energy_start
                    self._end_shield_hold(
                        player, HOLD_OUTCOME_BUDGET_EXHAUSTED if budget_limited else HOLD_OUTCOME_EMPTY, now
                    )
                    # BR-008: a new activation needs a release first.
                    player.trigger_armed = False
                    return
                if pressed:
                    player.hold_last_pressed_at = now
                    return
                self._release_shield_hold(player, now, hold)
            elif pressed and now < player.hold_until:
                # BR-008: a press inside the minimum-burst tail continues the activation.
                player.hold_pressed = True
                player.hold_until = player.hold_started_at + player.hold_usable_start
                player.hold_last_pressed_at = now
                return
            if now < player.hold_until:
                return
            self._end_shield_hold(player, HOLD_OUTCOME_RELEASED, now)
        if pressed and player.trigger_armed:
            self._start_shield_hold(player, now, hold)

    def _start_shield_hold(self, player: "Player", now: float, hold: ShieldHoldParams) -> None:
        """Start an activation when it is valid (UC-030 A7 trigger); otherwise do nothing."""
        if player.serial in self._kills_in_progress:
            return
        if now < self._other_grace_until(player):
            return  # BR-008: holding during another grace does not drain
        energy = self._shield_hold_energy(player, now)
        budget_left = self._shield_hold_budget_left(player, now)
        usable = min(energy, budget_left)
        if usable < hold.min_burst:
            return
        player.hold_started_at = now
        player.hold_pressed = True
        player.hold_energy_start = energy
        player.hold_budget_start = budget_left
        player.hold_usable_start = usable
        player.hold_until = now + usable
        player.hold_last_pressed_at = now
        player.hold_min_burst_applied = False
        if player.span:
            with contextlib.suppress(Exception):
                player._shield_hold_span = tracer.start_span(
                    "game.shield_hold",
                    context=trace.set_span_in_context(player.span),
                    attributes={
                        **player_identity_attributes(player.serial, player.name),
                        "game.id": self.game_id,
                        "shield.energy_start_s": round(energy, 3),
                        "shield.budget_remaining_start_s": round(budget_left, 3),
                        "shield.min_burst_s": hold.min_burst,
                    },
                )

    def _release_shield_hold(self, player: "Player", released_at: float, hold: ShieldHoldParams) -> None:
        """The trigger reads released: the cost is ``max(held, min burst)``, at most the usable energy (BR-024)."""
        held = max(0.0, released_at - player.hold_started_at)
        cost = min(max(held, hold.min_burst), player.hold_usable_start)
        player.hold_min_burst_applied = held < hold.min_burst
        player.hold_until = player.hold_started_at + cost
        player.hold_pressed = False

    def _end_shield_hold(self, player: "Player", outcome: str, now: float) -> None:
        """End the running activation: book its cost, close its span, count it (UC-030 A7 step 4, UC-040 A39)."""
        hold = self._shield_hold
        ended_at = min(now, player.hold_until)
        cost = max(0.0, ended_at - player.hold_started_at)
        energy_end = max(0.0, player.hold_energy_start - cost)
        player.hold_budget_used += cost
        player.hold_energy = energy_end
        player.hold_energy_at = ended_at
        player.hold_started_at = 0.0
        player.hold_until = 0.0
        player.hold_pressed = False
        player.hold_energy_start = 0.0
        player.hold_budget_start = 0.0
        player.hold_usable_start = 0.0
        if outcome in (HOLD_OUTCOME_RELEASED, HOLD_OUTCOME_EMPTY, HOLD_OUTCOME_BUDGET_EXHAUSTED):
            player.hold_prime_cap = True  # UC-030 BR-009
        span, player._shield_hold_span = player._shield_hold_span, None
        if span is not None:
            with contextlib.suppress(Exception):
                span.set_attribute("shield.hold_outcome", outcome)
                span.set_attribute("shield.hold_cost_s", round(cost, 3))
                span.set_attribute("shield.min_burst_applied", player.hold_min_burst_applied)
                span.set_attribute("shield.energy_end_s", round(energy_end, 3))
                span.end()
        player.hold_min_burst_applied = False
        with contextlib.suppress(Exception):
            metrics.shield_hold_activations_total.labels(
                mode=self.get_game_name(), game_kind=self.game_kind, outcome=outcome
            ).inc()
        if (
            hold is not None
            and not player.hold_budget_exhausted_noted
            and hold.budget - player.hold_budget_used < hold.min_burst
        ):
            player.hold_budget_exhausted_noted = True
            if player.span:
                with contextlib.suppress(Exception):
                    player.span.add_event(
                        "shield_budget_exhausted",
                        attributes={
                            "shield.budget_s": hold.budget,
                            **player_identity_attributes(player.serial, player.name),
                        },
                    )

    def _drop_shield_hold(self, player: "Player", outcome: str, now: float) -> None:
        """Drop the pool on death, disconnect or game end (UC-040 A39); a running activation ends with ``outcome``."""
        if player.hold_started_at:
            self._end_shield_hold(player, outcome, now)
        player.hold_energy = 0.0
        player.hold_energy_at = 0.0
        player.hold_prime_cap = False

    def _force_release_shield_holds(self) -> None:
        """Release every held activation at its last held frame (UC-030 BR-008, stream reopen)."""
        hold = self._shield_hold
        if hold is None:
            return
        for player in self.players.values():
            if player.hold_started_at and player.hold_pressed:
                self._release_shield_hold(player, player.hold_last_pressed_at, hold)

    def _prime_ema_after_hold(self, player: "Player", accel_mag: float) -> bool:
        """UC-030 BR-009: prime the EMA with min(raw, warning threshold); True when it primed."""
        player.hold_prime_cap = False
        if player.smoothed_accel >= 1e-9:
            return False
        effective_warn, _ = self._compute_effective_thresholds(player)
        player.smoothed_accel = min(accel_mag, effective_warn)
        player.last_accel_mag = player.smoothed_accel
        return True

    def _grant_shield_energy(self, player: "Player", seconds: float, hold: ShieldHoldParams) -> bool:
        """Add a grant to the player's energy (UC-040 A36); refuse one that could add nothing (A37).

        Order: budget used up, pool full, then the BR-022 cap and cooldown. A
        refusal raises ``InterventionNotApplied``, so the backstop is refunded and
        nothing counts. During a held activation the deadline moves out.
        """
        now = time.time()
        used = self._agent_shield_grants.get(player.serial, 0)
        budget_left = self._shield_hold_budget_left(player, now)
        if budget_left < hold.min_burst:
            self._refuse_shield(player, SHIELD_REFUSED_BUDGET_EXHAUSTED, used, 0.0)
        energy = self._shield_hold_energy(player, now)
        full = min(hold.cap, budget_left)
        room = full - energy
        if room < SHIELD_HOLD_POOL_FULL_ROOM_SECONDS:
            self._refuse_shield(player, SHIELD_REFUSED_POOL_FULL, used, 0.0)
        self._check_shield_limits(player, now)

        added = min(seconds, room)
        if player.hold_started_at:
            player.hold_energy_start += added
            player.hold_usable_start = min(player.hold_energy_start, player.hold_budget_start)
            if player.hold_pressed:
                player.hold_until = player.hold_started_at + player.hold_usable_start
        else:
            player.hold_energy = energy + added
            if player.hold_energy_at:
                player.hold_energy_at = now
        self._record_agent_shield(player.serial, now, seconds)
        energy_after = energy + added
        logger.info(f"Shield energy granted to {player.serial}: +{added:.2f}s of {seconds:.1f}s ({energy_after:.2f}s)")
        if player.span:
            identity = player_identity_attributes(player.serial, player.name)
            with contextlib.suppress(Exception):
                player.span.add_event(
                    "shield_energy_granted",
                    attributes={
                        "shield.seconds_requested": seconds,
                        "shield.seconds_added": round(added, 3),
                        "shield.energy_s": round(energy_after, 3),
                        **identity,
                    },
                )
                if energy_after >= full - 1e-9:
                    player.span.add_event(
                        "shield_energy_full", attributes={"shield.energy_source": "agent", **identity}
                    )
        return True

    # Soft-penalty defaults (#1134, #1103 Phase 3). The tighten factor is the
    # MIRROR of the toughness boost: a factor BELOW 1.0 weakens the player's
    # threshold for a short window (easier to die). It is clamped to the same
    # lower handicap bound (0.5) so even a fully-tightened threshold stays finite
    # — never instant-death. Seconds are capped so a single arming can't last
    # forever.
    SOFT_PENALTY_DEFAULT_FACTOR = 0.6
    SOFT_PENALTY_FACTOR_MIN = 0.5
    SOFT_PENALTY_FACTOR_MAX = 1.0
    SOFT_PENALTY_MAX_SECONDS = 30.0

    async def warn_player(self, serial: str) -> bool:
        """Fire the visible warning feedback for a player as an agent cue (#1134).

        This is the ``soft_penalty`` ``"warn"`` action: a graduated, fully
        RECOVERABLE alternative to ``eliminate_player``. It reuses the existing
        ``_warn_player`` feedback (white flash + rumble) as an agent-driven
        "you're on notice" cue. It changes NO thresholds and grants NO protection
        — purely visual/haptic, the player can still die immediately. We pass the
        player's current effective warning threshold so the span event is
        consistent with a natural warning; ``accel_mag`` is 0.0 (agent-driven, no
        triggering motion).

        Returns True if the warning fired, False if the player is unknown/dead.
        """
        player = self.players.get(serial)
        if not player or not player.alive:
            return False
        effective_warn, _ = self._compute_effective_thresholds(player)
        await self._warn_player(serial, 0.0, effective_warn)
        return True

    async def apply_soft_penalty(self, serial: str, seconds: float, factor: float | None = None) -> bool:
        """Apply a time-boxed soft-penalty TIGHTEN to a player (#1134, Phase 3).

        The deliberate MIRROR of :meth:`grant_toughness_boost`: where a toughness boost
        STRENGTHENS protection (boost >= 1.0, composed by ``max``), a
        tighten WEAKENS it for ``seconds`` (``factor`` < 1.0, composed by ``min``
        in :meth:`_compute_effective_thresholds`) — making the player TEMPORARILY
        EASIER to eliminate. It is graduated PRESSURE, not guaranteed elimination:
        the effective handicap is still clamped [0.5, 2.0], so even a maxed
        tighten only lowers the threshold to half — never instant-death.

        Because the tighten composes by ``min`` AFTER the toughness_boost ``max``,
        it can cut through an active toughness boost (pressure overrides
        protection). On expiry the factor silently stops applying — no reset task
        — and the standing/shielded handicap resumes.

        Time-box semantics mirror :meth:`grant_toughness_boost`: extend-not-shorten
        on the deadline and tighten-not-loosen on the factor, so repeated arming
        never reduces in-flight pressure (keeps the LOWER, stronger factor).

        Args:
            serial: Controller serial number.
            seconds: Penalty duration. Non-positive is a no-op (False); capped at
                ``SOFT_PENALTY_MAX_SECONDS``.
            factor: Handicap multiplier while active; clamped
                [``SOFT_PENALTY_FACTOR_MIN``, ``SOFT_PENALTY_FACTOR_MAX``].
                Defaults to ``SOFT_PENALTY_DEFAULT_FACTOR`` (0.6).

        Returns:
            True if a tighten was armed/extended, False if the player is
            unknown/dead or the duration was non-positive.
        """
        if seconds <= 0:
            return False

        player = self.players.get(serial)
        if not player or not player.alive:
            return False

        seconds = min(seconds, self.SOFT_PENALTY_MAX_SECONDS)
        if factor is None:
            factor = self.SOFT_PENALTY_DEFAULT_FACTOR
        factor = max(self.SOFT_PENALTY_FACTOR_MIN, min(self.SOFT_PENALTY_FACTOR_MAX, factor))

        new_until = time.time() + seconds
        # Extend-not-shorten / tighten-not-loosen: keep whichever active window is
        # longer and whichever active factor is LOWER (stronger pressure), so
        # repeated arming never reduces in-flight pressure.
        if time.time() < player.soft_penalty_until:
            new_until = max(new_until, player.soft_penalty_until)
            factor = min(factor, player.soft_penalty_factor)
        player.soft_penalty_until = new_until
        player.soft_penalty_factor = factor
        logger.info(f"Soft penalty tighten {serial}: factor={factor:.2f} for {seconds:.1f}s (until={new_until:.2f})")

        if player.span:
            player.span.add_event(
                "soft_penalty_tighten",
                attributes={
                    "soft_penalty_seconds": seconds,
                    "soft_penalty_factor": factor,
                    **player_identity_attributes(serial, player.name),
                },
            )

        # Visible warning feedback so the tighten is observable to the player
        # ("you're on notice"), reusing the same warn cue as the "warn" action.
        await self._warn_player(serial, 0.0, self._compute_effective_thresholds(player)[0])

        return True

    # Auto-rubberband defaults (#1143, #1103 Phase 3). A SINGLE agent decision is
    # expanded across players by the coordinator from the live skill gap. The
    # boost is TIME-BOXED (re-applied each agent cycle while the decision holds)
    # and BOUNDED: ``RUBBERBAND_MAX_BOOST_GENTLE``/``_STRONG`` cap the per-player
    # boost added on top of the neutral 1.0, and the boost a laggard gets is
    # ADDITIONALLY capped so its boosted death threshold can never exceed the
    # current leader's (no inversion of standings). Seconds are short so the boost
    # naturally expires shortly after the decision is withdrawn.
    RUBBERBAND_BOOST_MIN = 1.0
    RUBBERBAND_BOOST_MAX = 2.0
    RUBBERBAND_DEFAULT_SECONDS = 5.0
    RUBBERBAND_MAX_SECONDS = 30.0
    # Per-strength cap on the boost ADDED above the neutral 1.0 (so gentle tops out
    # at 1.2x, strong at 1.4x — well inside the [0.5, 2.0] threshold clamp).
    RUBBERBAND_MAX_BOOST_GENTLE = 0.2
    RUBBERBAND_MAX_BOOST_STRONG = 0.4

    async def apply_auto_rubberband(self, strength: str, seconds: float | None = None) -> int:
        """Auto-compress the live skill gap by boosting the trailing player(s) (#1143).

        Unlike :meth:`set_player_handicap` / :meth:`grant_toughness_boost` (the
        agent hand-picks one serial + factor), ``auto_rubberband`` is a SINGLE
        decision (``"gentle"`` | ``"strong"``) that the COORDINATOR expands across
        players from the live skill gap. The standing proxy is each alive player's
        HEADROOM to their effective death threshold (``death - smoothed_accel``):
        a large headroom = comfortably ahead (the leader), a small headroom = on
        the edge (the laggard). Trailing players receive a temporary handicap
        BOOST — the same #1107 mechanism, composed by MAX in
        :meth:`_compute_effective_thresholds` — that raises their death threshold,
        compressing the gap to the leader.

        Safety (never inverts standings, never instant-death):

        - The boost added above 1.0 is capped per strength
          (``RUBBERBAND_MAX_BOOST_GENTLE`` 0.2 / ``RUBBERBAND_MAX_BOOST_STRONG``
          0.4), so a laggard's threshold rises by at most 20% / 40%.
        - It is ADDITIONALLY capped so the laggard's BOOSTED death threshold can
          never exceed the current leader's death threshold — compression only,
          never overtaking (no standings inversion).
        - The combined handicap is still clamped [0.5, 2.0] downstream, so even a
          maxed boost is "harder to die", never immune / never instant-death.

        Needs at least two alive players to have a gap to compress; with fewer it
        is a no-op. Time-box / strengthen semantics mirror
        :meth:`grant_toughness_boost` (extend-not-shorten, strengthen-not-weaken).

        Args:
            strength: ``"gentle"`` or ``"strong"``. Any other value is a no-op.
            seconds: Boost window; defaults to ``RUBBERBAND_DEFAULT_SECONDS``,
                capped at ``RUBBERBAND_MAX_SECONDS``.

        Returns:
            The number of players boosted (0 on a no-op).
        """
        strength = (strength or "").strip().lower()
        if strength == "gentle":
            max_boost = self.RUBBERBAND_MAX_BOOST_GENTLE
        elif strength == "strong":
            max_boost = self.RUBBERBAND_MAX_BOOST_STRONG
        else:
            return 0  # unknown strength: safe no-op

        if seconds is None:
            seconds = self.RUBBERBAND_DEFAULT_SECONDS
        if seconds <= 0:
            return 0
        seconds = min(seconds, self.RUBBERBAND_MAX_SECONDS)

        # Rank alive players by headroom to their effective death threshold. We
        # need at least two to have a gap to compress.
        alive = [p for p in self.players.values() if p.alive]
        if len(alive) < 2:
            return 0

        headrooms: dict[str, tuple[float, float]] = {}  # serial -> (headroom, death)
        for p in alive:
            _, death = self._compute_effective_thresholds(p)
            headrooms[p.serial] = (death - p.smoothed_accel, death)

        # Leader = most headroom (comfortably ahead); laggard ranking from least.
        max_leader_headroom = max(hd[0] for hd in headrooms.values())
        # The gap span across the field; if everyone is equal there is nothing to
        # compress (gap == 0) and we no-op rather than boost identically.
        min_headroom = min(hd[0] for hd in headrooms.values())
        gap = max_leader_headroom - min_headroom
        if gap <= 1e-9:
            return 0

        now = time.time()
        boosted = 0
        for p in alive:
            headroom, death = headrooms[p.serial]
            if death <= 0:
                continue
            # How far behind the leader this player is, in [0, 1]: the laggard
            # (min headroom) is 1.0, the leader is 0.0. The boost scales with this
            # deficit and the strength cap, so the leader gets ~0 and the laggard
            # gets up to max_boost.
            deficit = (max_leader_headroom - headroom) / gap
            target_boost = 1.0 + max_boost * deficit
            # No-invert cap (standings = headroom to death, NOT the raw threshold).
            # The boosted player's headroom (death*boost - accel) must never exceed
            # the leader's headroom — compression toward the leader, never past it.
            # Solve death*boost - accel <= leader_headroom => boost <= cap.
            no_invert_cap = (max_leader_headroom + p.smoothed_accel) / death
            target_boost = min(target_boost, max(1.0, no_invert_cap))
            # Final clamp to the rubberband boost band (still re-clamped [0.5,2.0]
            # downstream when composed).
            target_boost = max(self.RUBBERBAND_BOOST_MIN, min(self.RUBBERBAND_BOOST_MAX, target_boost))
            if target_boost <= 1.0 + 1e-9:
                continue  # nothing to add for this player (e.g. the leader)

            new_until = now + seconds
            # Extend-not-shorten / strengthen-not-weaken (mirror toughness_boost).
            boost = target_boost
            if now < p.rubberband_until:
                new_until = max(new_until, p.rubberband_until)
                boost = max(boost, p.rubberband_boost)
            p.rubberband_until = new_until
            p.rubberband_boost = boost
            boosted += 1
            if p.span:
                p.span.add_event(
                    "auto_rubberband_boost",
                    attributes={
                        "rubberband_strength": strength,
                        "rubberband_boost": boost,
                        "rubberband_seconds": seconds,
                        **player_identity_attributes(p.serial, p.name),
                    },
                )

        if boosted:
            logger.info(f"Auto-rubberband ({strength}): boosted {boosted} trailing player(s) for {seconds:.1f}s")
        return boosted

    # Spawn grace applied to a revived player so they don't instantly re-die
    # before they can stop moving (#730, N5). Mirrors Nonstop/Zombie respawn grace.
    REVIVE_SPAWN_GRACE = 2.0

    async def revive_player(self, serial: str, spawn_grace: float | None = None) -> bool:
        """
        Revive a dead player, re-entering them into the game (#730, N5).

        Whether reviving is allowed at all in a given mode is decided by the
        intervention manager's §9 capability matrix BEFORE this runs (it denies
        ``revive_player`` for permanent-elimination / bracket / hidden-role
        modes). This method only implements the actual re-entry once allowed.

        Re-entry restores the player to the alive state, clears stale
        warning/motion state, restores the controller LED to the player's color,
        and grants a brief spawn grace (via the shield grace mechanism) so the
        revived player can't instantly re-die. Modes with a native respawn path
        (Nonstop ``_respawn_player``) delegate to it so their mode-specific state
        (spawn protection, scoring) stays consistent; other modes use the generic
        re-entry below.

        Args:
            serial: Controller serial number.
            spawn_grace: Spawn-protection grace in seconds; defaults to
                ``REVIVE_SPAWN_GRACE``.

        Returns:
            True if the player was revived, False if unknown / already alive or
            the mode's native respawn refused (returned False).
        """
        player = self.players.get(serial)
        if player is None:
            logger.warning(f"revive_player: unknown serial {serial}, ignoring")
            return False
        if player.alive:
            logger.info(f"revive_player: {serial} already alive, no-op")
            return False

        grace = self.REVIVE_SPAWN_GRACE if spawn_grace is None else spawn_grace

        # Prefer a mode-native respawn path so mode-specific state stays correct.
        native = getattr(self, "_respawn_player", None)
        if callable(native):
            # A native path may refuse (returns False, e.g. Zombies once decided);
            # None (NonStop) keeps the old "always revived" behaviour.
            if await native(serial) is False:
                return False
            # Ensure spawn grace even if the native path uses its own scheme.
            player.grace_until = max(player.grace_until, time.time() + grace)
            logger.info(f"revive_player: {serial} revived via native respawn")
            return True

        # Generic re-entry for modes without a native respawn path.
        player.alive = True
        player.warning_until = 0.0
        player.smoothed_accel = 0.0
        player.grace_until = time.time() + grace

        metrics.player_alive.labels(serial=serial, game_id=self.game_id).set(1)
        alive_count = len([p for p in self.players.values() if p.alive])
        self._set_players_alive_aggregate(alive_count)

        if player.span:
            player.span.add_event(
                "player_revived",
                attributes={"spawn_grace": grace, **player_identity_attributes(serial, player.name)},
            )

        if self.gameplay_stream:
            from proto import controller_manager_pb2

            trace_parent, trace_state = inject_trace_context(player.span)
            # Restore the player's base LED color, then a respawn pulse.
            base_color_cmd = controller_manager_pb2.GameplayStreamControl(
                base_color=controller_manager_pb2.ControllerColorConfig(
                    serial=serial,
                    color=controller_manager_pb2.RGB(r=player.color[0], g=player.color[1], b=player.color[2]),
                )
            )
            await self.gameplay_stream.write(base_color_cmd)
            respawn_cmd = controller_manager_pb2.GameplayStreamControl(
                game_effect=controller_manager_pb2.GameEffectCommand(
                    serial=serial,
                    effect=controller_manager_pb2.GAME_EFFECT_PLAYER_RESPAWN,
                    trace_parent=trace_parent,
                    trace_state=trace_state,
                )
            )
            await self.gameplay_stream.write(respawn_cmd)

        await self.event_publisher("player_revived", {"serial": serial})
        logger.info(f"revive_player: {serial} revived (generic re-entry, grace {grace:.1f}s)")
        return True

    def _player_death_event_attributes(self, serial: str) -> dict:  # noqa: ARG002
        """Mode-specific attributes for the base kill path's ``player_death`` span event.

        Called by ``_kill_player`` BEFORE ``_kill_player_impl`` (which may end the
        player span), so values describe the state *after* this death as if the
        player were already out. The base attributes (``player.serial``,
        ``player.name``, ``death.reason``, ``alive_remaining``, ...) win on a key
        clash. Default: no extra attributes (#1327).
        """
        return {}

    def _sends_death_effect(self, serial: str) -> bool:  # noqa: ARG002
        """Whether the base kill path sends ``GAME_EFFECT_PLAYER_DEATH`` for this kill.

        Called by ``_kill_player`` AFTER ``_kill_player_impl``, synchronously (no
        await between the impl and the write). Default: always (#1342). Zombies
        overrides it so a converted human, still in the game, keeps its zombie
        colour instead of a trailing death effect.
        """
        return True

    def _accepts_kill(self, serial: str) -> bool:  # noqa: ARG002
        """Whether the mode takes this kill at all (UC-030 step 9, #1383).

        Called by ``_kill_player`` synchronously, with its other guards, and again
        by ``_run_kill`` after its awaits (shield charge end, explosion), right
        before the bookkeeping: a declined kill gets no ``dead_count``, ``player_alive``
        gauge, elimination order, ``player_deaths_total``, ``player_death`` span event
        or death effect (nor an explosion, nor the end of a held shield charge, when
        declined at the first check), and
        ``_kill_player`` returns False. True means
        the kill passed the gate, not that the mode's handler applied it. Default:
        until the game's outcome is latched; after that no kill is taken, so a kill
        during the post-win hold or teardown counts nothing and cannot change a
        decided result (UC-030 step 9, UC-020 BR-003, UC-026 BR-003; #2019). Modes
        with their own gate (Swapper, Fight Club, Tournament) decline after their
        own latch.
        """
        return self._latched_outcome is None

    def _kill_gate_open(self, serial: str) -> bool:
        """The kill path's gate: closed for every mode once the game is force-ended, else the mode's gate.

        A force-end (UC-027) runs on the servicer thread and can land while the game
        thread is still in a frame: the frame's later kills, and a kill awaiting its
        explosion, then count nothing, in every mode (UC-030 step 9, UC-020 BR-003;
        #2033). Checked only when a kill is attempted, so it costs nothing per frame.
        """
        return self.force_end_reason is None and self._accepts_kill(serial)

    async def _kill_player(self, serial: str, accel_mag: float, reason: str = "motion") -> bool:
        """
        Kill a player (template method calling subclass implementation).

        Args:
            serial: Controller serial number
            accel_mag: Acceleration magnitude that caused death (0.0 for
                non-motion deaths such as agent interventions)
            reason: What caused the death. ``"motion"`` for natural threshold
                deaths; ``"agent_intervention"`` (#730) when the death is forced
                by an ``eliminate_player`` agent intervention. Recorded on the
                player_death span event so forced deaths are distinguishable from
                natural ones while still emitting identical spans/events/metrics.

        Returns:
            False when the kill was ignored (unknown or dead player, a kill of the
            same player in flight), the game was force-ended (#2033) or the kill was
            declined by ``_accepts_kill``; True otherwise.
        """
        player = self.players.get(serial)
        if not player or not player.alive or serial in self._kills_in_progress:
            return False
        if not self._kill_gate_open(serial):
            return False

        self._kills_in_progress.add(serial)
        try:
            return await self._run_kill(serial, player, accel_mag, reason)
        finally:
            self._kills_in_progress.discard(serial)

    def _count_elimination(self, serial: str) -> None:
        """Count a player out: ``dead_count``, ``game_player_alive`` 0 and the elimination order.

        Called by the kill path for every death, and by a mode that takes a
        player out without a death (Tournament's match time limit, UC-025 A1).
        """
        # Phase 70: Track deaths for music tempo timing
        self.dead_count += 1

        # Mark player as dead in metrics - dashboard template variables filter
        # on game_player_alive==1 so dead players naturally disappear from panels.
        # Metric removal happens at game end via clear_session_player_analytics().
        metrics.player_alive.labels(serial=serial, game_id=self.game_id).set(0)

        # Record elimination order (1 = first out) for #730 / #722 §7.
        # Makes session.elimination_sequence queryable per game.
        self._elimination_count += 1
        metrics.player_elimination_order.labels(serial=serial, game_id=self.game_id).set(self._elimination_count)

    async def _run_kill(self, serial: str, player: Player, accel_mag: float, reason: str) -> bool:
        """Body of _kill_player; runs at most once at a time per serial.

        Returns False when the kill gate closed while the kill path awaited (the
        shield charge end, the explosion): the kill is then declined before any
        bookkeeping (UC-030 step 9, #1988).
        """
        # A held shield charge ends with the death, before the player span closes
        # (UC-040 A32; a disconnect and eliminate_player come through here too).
        if player.shield_charge_seconds > 0:
            await self._discard_shield_charge(player, CHARGE_OUTCOME_DEATH)
        # UC-040 A39: the death drops a hold pool; eliminate_player ignores the hold.
        if self._shield_hold is not None:
            self._drop_shield_hold(player, HOLD_OUTCOME_DEATH, time.time())

        # Play death explosion sound under the player's span
        player_ctx = trace.set_span_in_context(player.span) if player.span else None
        await self._play_sound(Sound.SFX_EXPLOSION, priority=2, parent_context=player_ctx)

        # The gate again, synchronous with the bookkeeping and the mode's handler: a
        # mode's state may have changed during the awaits above (Swapper latched its
        # win, the game was force-ended), and a declined kill counts no death (#1988).
        if not self._kill_gate_open(serial):
            return False
        # Again after the awaits: energy granted while the kill awaited is dropped too.
        if self._shield_hold is not None:
            self._drop_shield_hold(player, HOLD_OUTCOME_DEATH, time.time())

        alive_count_before = len([p for p in self.players.values() if p.alive])
        logger.info(f"Player died: {serial}, {alive_count_before - 1} players remaining")

        self._count_elimination(serial)

        # UC-030 step 9: one increment per death (respawn modes count each death).
        # Aggregate {mode, game_kind} only; per-player order is the gauge above.
        metrics.player_deaths_total.labels(mode=self.get_game_name(), game_kind=self.game_kind).inc()

        # Extract trace context BEFORE _kill_player_impl() which may close the span.
        # This ensures the death effect and sound are parented to the player_lifecycle
        # span even after the subclass impl ends it. (#456)
        trace_parent, trace_state = inject_trace_context(player.span)

        # The ONE player_death span event per death, for every mode (#1327): modes
        # add their own attributes through _player_death_event_attributes() and
        # must not emit a second player_death from _kill_player_impl().
        if player.span:
            player.span.add_event(
                "player_death",
                attributes={
                    "accel_magnitude": accel_mag,
                    "sensitivity": self.sensitivity.name,
                    "sensitivity_factor": player.sensitivity_factor,
                    "music_speed": self.music_speed,
                    "alive_remaining": alive_count_before - 1,
                    "death.reason": reason,
                    **self._player_death_event_attributes(serial),
                    **player_identity_attributes(serial, player.name),
                },
            )

        # Finalize health attributes BEFORE _kill_player_impl() which may close the span.
        # Without this, dead players lose their health.total_poll_drops etc. attributes.
        self._finalize_player_health(player)

        # Call subclass-specific death handling
        await self._kill_player_impl(serial, accel_mag)

        # The aggregate counts the state AFTER the mode's handler: it flips alive to
        # False for a death, keeps it True for a Zombies conversion (#1943).
        # A kill finishing after the game stopped must not overwrite the end-of-game 0.
        if self.running:
            self._set_players_alive_aggregate(len([p for p in self.players.values() if p.alive]))

        # Send death effect via stream (red + vibrate, no restore)
        # Use pre-extracted trace context so effect appears under player_lifecycle
        # in traces, even though _kill_player_impl may have closed the span. (#456)
        from proto import controller_manager_pb2

        if self.gameplay_stream and self._sends_death_effect(serial):
            effect_cmd = controller_manager_pb2.GameplayStreamControl(
                game_effect=controller_manager_pb2.GameEffectCommand(
                    serial=serial,
                    effect=controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH,
                    trace_parent=trace_parent,
                    trace_state=trace_state,
                )
            )
            await self.gameplay_stream.write(effect_cmd)
        return True

    async def _wait_for_rainbow_effect(self) -> bool:
        """
        Wait for the winner rainbow effect to complete.

        Uses runtime config for duration. Interruptible by force_end.

        Returns:
            True if wait completed normally, False if interrupted
        """
        config = get_config_manager().get_config()
        rainbow_duration_s = config.winner_rainbow_duration_ms / 1000.0
        iterations = int(rainbow_duration_s * 10)  # 0.1s increments

        logger.debug(f"Waiting {rainbow_duration_s}s for rainbow effect")
        for i in range(iterations):
            if not self.running:
                logger.info(f"Rainbow wait interrupted at {i * 0.1:.1f}s/{rainbow_duration_s}s")
                return False
            await asyncio.sleep(0.1)

        return True

    def force_end(self, reason: str = ""):
        """Force the game to end (called externally).

        Records the reason so the game's outcome is ``force_ended`` (UC-020 BR-003,
        #1328), never a winner or a set of victories.
        """
        logger.info(f"Force ending game... (reason: {reason or 'unspecified'})")
        self.force_end_reason = reason or "unspecified"
        self.running = False

    @property
    def force_ended(self) -> bool:
        """True once the game was force-ended (#1328)."""
        return self.force_end_reason is not None

    def _natural_game_outcome(self) -> dict:
        """Outcome attributes of a game that ended on its own (not force-ended).

        Modes that classify their end (JoustFFA: ``winner`` / ``tie``) override this.
        Default ``completed``: the mode's own winner events carry the detail.
        """
        return {"game.outcome": "completed"}

    def _latch_game_outcome(self) -> None:
        """Freeze the outcome when the game loop ends (first call wins, #1328).

        Called when the win condition is met and again when the loop exits, so a
        force-end that arrives afterwards (winner rainbow, teardown) cannot
        rewrite a ``winner`` / ``tie`` / ``completed`` outcome.
        """
        if self._latched_outcome is None:
            self._latched_outcome = self._game_outcome_attributes()

    def _game_outcome_attributes(self) -> dict:
        """The game's end-of-game outcome (UC-020 BR-003, #1328).

        The latched outcome wins once the loop ended. Before that, ``force_ended``
        (with ``game.end_reason``) takes precedence over any natural outcome: a
        game force-ended while its loop ran did not reach its win condition.
        """
        if self._latched_outcome is not None:
            return dict(self._latched_outcome)
        if self.force_ended:
            return {"game.outcome": "force_ended", "game.end_reason": self.force_end_reason}
        return self._natural_game_outcome()

    def game_outcome(self) -> str:
        """The ``game.outcome`` value: ``winner``, ``tie``, ``completed`` or ``force_ended``.

        The same value the game root span records; the session labels
        ``games_completed_total{outcome}`` with it (#1427).
        """
        return self._game_outcome_attributes()["game.outcome"]

    def _capture_game_root_span(self) -> None:
        """Remember the current span (the session's game root) for the outcome.

        Called at the start of ``run()`` by the base and by modes that override it.
        """
        self._game_root_span = trace.get_current_span()

    def _record_game_outcome(self, teardown_span: trace.Span | None) -> None:
        """Put the outcome on the game root span and a ``game.outcome`` teardown event.

        Records once per game; later calls are no-ops. ``teardown_span`` is None
        when a mode's run() returns before its teardown (force-ended during the
        intro or countdown).
        """
        if self._outcome_recorded:
            return
        self._outcome_recorded = True
        self._latch_game_outcome()
        attrs = self._game_outcome_attributes()
        if self._game_root_span is not None:
            self._game_root_span.set_attributes(attrs)
        if teardown_span is not None:
            teardown_span.add_event("game.outcome", attributes=attrs)

    def _record_force_ended_outcome_if_unrecorded(self) -> None:
        """For run() overrides: a game force-ended before its teardown still records force_ended."""
        if self.force_ended:
            self._record_game_outcome(None)

    # ========================================================================
    # Task Tracking and Lifecycle Hooks
    # ========================================================================

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        """
        Register an async task for automatic cleanup.

        Tracked tasks are cancelled when cleanup() is called, preventing
        dangling tasks after game end.

        Args:
            task: The asyncio.Task to track

        Returns:
            The same task (for chaining with asyncio.create_task)
        """
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def cleanup(self) -> None:
        """
        Clean up game resources. Called in finally block after game ends.

        Cancels any tracked async tasks registered via _track_task(), then closes
        the gameplay stream (UC-030 BR-006). Subclasses should override to add
        mode-specific cleanup (e.g., cancel timers, close connections) and call
        super().cleanup().
        """
        try:
            if self._tasks:
                # Snapshot tasks before cancelling (done callbacks remove from set)
                tasks_to_cancel = list(self._tasks)
                logger.info(f"Cleaning up {len(tasks_to_cancel)} tracked tasks")
                for task in tasks_to_cancel:
                    if not task.done():
                        task.cancel()
                # Wait for all tasks to complete cancellation
                await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
                self._tasks.clear()
        finally:
            # Every mode's run() reaches here on every end (natural, force-end,
            # error): close the stream once no tracked task can use it.
            self._release_gameplay_stream()
        logger.info(f"{self.get_game_name()} cleanup complete")

    async def on_force_end(self) -> None:
        """
        Hook called when game is force-ended (not natural completion).

        Called before cleanup(). Use for mode-specific force-end logic
        like announcing forced end or saving partial results.
        Subclasses can override to add custom behavior.
        """
        logger.info(f"{self.get_game_name()} force-ended")

    # ========================================================================
    # Template Method - Orchestrates entire game lifecycle with spans
    # ========================================================================

    async def _run_gameplay_phase(self):
        """Start the game and run gameplay_phase: countdown, game loop, outcome latch."""
        # Game starts
        self.state = GameState.RUNNING
        current_span = trace.get_current_span()
        current_span.add_event(
            "game.state_changed",
            {
                "game.state": "RUNNING",
                "player.count": len(self.players),
                "game.id": self.game_id,
            },
        )
        # Note: self.start_time is set in _game_loop when first data is received
        await self.event_publisher(
            GameEvent.GAME_STARTED,
            {"game_id": self.game_id, "player_count": len(self.players)},
        )

        # Phase 2: Gameplay (player spans created before countdown)
        with tracer.start_as_current_span("gameplay_phase") as gameplay_span:
            # Store span reference and context for background tasks
            self.gameplay_span = gameplay_span
            self.gameplay_span_context = otel_context.get_current()

            # Publish the gameplay_phase span_id to the trace-correlation gauge
            # (#1195) so the agent re-parents its in-game decision chain under
            # THIS span instead of the game root (#1187). gameplay_phase is the
            # stable active-play window; a span_ctx with a zero/invalid span_id
            # (unsampled) yields an empty hex id, which the session treats as a
            # no-op so the agent keeps falling back to the game root span.
            gameplay_span_ctx = gameplay_span.get_span_context()
            if gameplay_span_ctx.span_id != 0:
                self.on_gameplay_phase_open(trace.format_span_id(gameplay_span_ctx.span_id))

            # Create "players" parent span (collapsible group in Jaeger)
            self._players_span = tracer.start_span(
                "players",
                context=self.gameplay_span_context,
                attributes={"player_count": len(self.players)},
            )
            players_ctx = trace.set_span_in_context(self._players_span)

            # Create "game_cycle" span for instrumentation (sounds, music tempo)
            self._game_cycle_span = tracer.start_span(
                "game_cycle",
                context=self.gameplay_span_context,
                attributes={"game.id": self.game_id},
            )
            self.game_cycle_context = trace.set_span_in_context(self._game_cycle_span)
            # Publish the game_cycle span_id (#1325, UC-041 BR-004): the agent
            # parents its in-game spans under game_cycle (UC-040 BR-013). An
            # unsampled span has a zero id; publishing nothing keeps the agent on
            # its gameplay_phase / game-root fallback.
            game_cycle_span_ctx = self._game_cycle_span.get_span_context()
            if game_cycle_span_ctx.span_id != 0:
                self.on_game_cycle_open(trace.format_span_id(game_cycle_span_ctx.span_id))

            # Create player lifecycle spans under "players" parent
            self._create_player_spans(players_ctx)

            # Open per-player countdown child spans
            for player in self.players.values():
                if player.span:
                    ctx = trace.set_span_in_context(player.span)
                    player._countdown_span = tracer.start_span(
                        "countdown_phase",
                        context=ctx,
                        attributes={"player.serial": player.serial},
                    )

            # Run countdown (sounds go under game_cycle via _play_sound)
            await self._countdown()

            # Close per-player countdown spans
            for player in self.players.values():
                if player._countdown_span:
                    player._countdown_span.end()
                    player._countdown_span = None

            # Ramp music volume up to game level after countdown (a shadow game is silent, #1835)
            if self.audio_client and not self._is_shadow_game():
                try:
                    from proto import audio_pb2

                    await self.audio_client.SetVolume(audio_pb2.SetVolumeRequest(volume=self.game_volume))
                except Exception as e:
                    logger.warning(f"Failed to set game volume: {e}")

            # Drain buffered stream data and prime EMA filters
            await self._warmup_ema()

            # Start music loop as background task (uses game_cycle_context)
            self.music_loop_task = asyncio.create_task(self._music_loop())

            try:
                await self._game_loop()
            finally:
                self._latch_game_outcome()
                # Stop music loop
                if self.music_loop_task:
                    self.music_loop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self.music_loop_task

                # Close all player spans, then grouping spans
                self._close_all_player_spans()
                self._close_grouping_spans()

    @property
    def flag_eval_context(self) -> EvaluationContext:
        """The flag evaluation context of this game, for reads made outside its task (#1970).

        Intervention handlers run on the game's loop but not in the game's task, so
        the manager sets this context around each one (UC-040 BR-018). After the
        run started it is exactly what :meth:`_set_flag_eval_context` set; before
        that (init phase) it is the session-kind context the game's own init reads
        saw (``game_kind`` plus the experiment identity).
        """
        if self._flag_eval_context is not None:
            return self._flag_eval_context
        return build_session_kind_context(self.game_kind, self.experiment_id or None, self.arm or None)

    def _set_flag_eval_context(self) -> None:
        """Set the per-game flag evaluation context (transaction context) for this game.

        The only setter: it also keeps the context as ``flag_eval_context`` so
        intervention handlers resolve flags in the same context (#1970).
        """
        # Set transaction context for flag evaluations during this game.
        # Map the session kind ("primary"/"shadow", #775) to the eval-context
        # game_kind the agent's experiment writer scopes on ("real"/"shadow",
        # targeting.go #932): only a SHADOW session resolves experiments;
        # every primary (menu-driven, player-facing) game is protected as
        # "real". This runs inside the per-session game loop, so the override
        # is scoped to this session's async context (contextvars).
        # Experiment attribution (#975) rides the same transaction context
        # as game_kind so an experiment-scoped flag override resolves for
        # this session. Pass them only when set (a non-experiment game
        # leaves them absent, so its experiment targeting is false by
        # construction — real-by-default).
        # set_game_transaction_context maps the session kind (eval_game_kind).
        self._flag_eval_context = set_game_transaction_context(
            game_mode=self.get_game_name(),
            controller_count=len(self.players),
            sensitivity=self.sensitivity.value,
            game_kind=self.game_kind,
            experiment_id=self.experiment_id or None,
            arm=self.arm or None,
        )

    async def _cleanup_after_run(self) -> None:
        """Run-end cleanup shared by every mode's run() finally block.

        Closes open shield charge spans, runs cleanup(), ends any player span
        still open and clears this session's analytics, on every exit path
        (natural end, force-end, error).
        """
        # An error teardown skips _end_shield_feedback: end open charge spans (#1837).
        # Before cleanup, so a raising cleanup leaves no span open.
        self._close_shield_charge_spans()
        # Always call cleanup to cancel tracked tasks
        try:
            await self.cleanup()
        finally:
            # A grant arriving while cleanup awaits can open a span after the first close (#1868).
            self._close_shield_charge_spans()
            # An early return (force-end after the intro, countdown or warm-up) or an
            # error skips the end step that closes the player spans (#1997). Guarded
            # so a failure here neither masks the run's own error nor skips the
            # analytics clear below.
            try:
                self._end_open_player_spans_on_exit()
            except Exception:
                logger.exception(f"Ending the open player spans failed: {self.game_id}")
            try:
                self._close_grouping_spans()
            except Exception:
                logger.exception(f"Closing the grouping spans failed: {self.game_id}")
        # Ensure this session's analytics are always cleared, even on error
        # (#775) — per-session targeted cleanup, never a global wipe.
        metrics.clear_session_player_analytics(
            list(self.players.keys()),
            self.game_id,
            game_kind=self.game_kind,
        )

    async def run(self):
        """
        Main entry point to run the game (Template Method).

        Orchestrates all game phases with consistent span hierarchy:
        1. initialization_phase (setup: players, colors, stream, music)
        2. gameplay_phase (player_lifecycle spans with countdown_phase children)
        3. teardown_phase

        Phase spans are automatically children of the current game span.
        """
        try:
            # State transitions
            self.state = GameState.STARTING
            self.running = True  # Set early to allow force_end during countdown
            current_span = trace.get_current_span()
            self._capture_game_root_span()
            current_span.add_event(
                "game.state_changed",
                {
                    "game.state": "STARTING",
                    GAME_MODE_ATTR: self.get_game_name(),
                    "game.id": self.game_id,
                },
            )
            await self.event_publisher(GameEvent.GAME_STARTING, {"game_id": self.game_id})

            # Phase 1: Initialization (setup only — no countdown)
            with tracer.start_as_current_span("initialization_phase") as init_span:
                init_span.set_attribute("game.id", self.game_id)
                init_span.set_attribute(GAME_MODE_ATTR, self.get_game_name())

                self._emit_sensitivity_metric()
                logger.info(f"Sensitivity: {self.sensitivity.name} ({self.sensitivity.value})")

                # Initialize players
                await self._initialize_players()

                # Validate player count
                if len(self.players) < 2:
                    raise ValueError(f"Need at least 2 players, got {len(self.players)}")

                init_span.set_attribute("player_count", len(self.players))

                self._set_flag_eval_context()

                # A mode whose phases send effects over the stream opens it first
                # (UC-021 A1, D-G10); a drain discards frames until the start.
                early_stream = self._opens_stream_before_additional_phases()
                if early_stream:
                    await self._start_gameplay_stream()
                    self._start_early_stream_drain()
                phases_completed = False
                try:
                    # Additional phases (e.g., color_assignment, team_formation)
                    # These are children of initialization_phase
                    for phase in self._get_additional_phases():
                        with tracer.start_as_current_span(phase.name):
                            await phase.execute()

                    # Spoken instructions, when the host switched them on (UC-020 A5):
                    # the clip finishes before the music and the countdown start.
                    await self._play_instructions(init_span)
                    phases_completed = True
                finally:
                    if early_stream:
                        await self._stop_early_stream_drain(reopen=phases_completed)

                # A force-end during the instructions skips the start: no stream,
                # music, game_started or countdown; teardown records force_ended.
                force_ended_before_start = not self.running
                if not force_ended_before_start:
                    # Start gameplay stream before countdown (needed for countdown effects)
                    if not early_stream:
                        await self._start_gameplay_stream()

                    # Start game music BEFORE countdown at low volume.
                    # OGG decode happens during countdown, so music is ready instantly after.
                    await self._start_game_music(volume=self.countdown_music_volume)

            if force_ended_before_start:
                logger.info(f"{self.get_game_name()} force-ended before the start: {self.game_id}")
                self._latch_game_outcome()
            else:
                await self._run_gameplay_phase()

            # Phase 3: Teardown
            with tracer.start_as_current_span("teardown_phase") as teardown_span:
                teardown_span.add_event(
                    "game.state_changed",
                    {
                        "game.state": "ENDING",
                        "game.id": self.game_id,
                    },
                )
                # Stop game music first (inside teardown_phase so StopMusic span is a child)
                await self._stop_game_music()

                # Shield LEDs end with the game, before the winner effects (#1301).
                await self._end_shield_feedback()

                await self._end_game_impl()
                self._record_game_outcome(teardown_span)

                # Winner effects are sent: close the stream (UC-030 BR-006)
                self._release_gameplay_stream()

                # Clear THIS session's analytics so dashboards show no data when
                # the game is over, without clobbering other concurrent sessions
                # (#775). Game-state gauges are per game_id, so each session
                # removes only its own series (FR-041).
                metrics.clear_session_player_analytics(
                    list(self.players.keys()),
                    self.game_id,
                    game_kind=self.game_kind,
                )

        except Exception as e:
            self._run_failed = True
            logger.error(f"{self.get_game_name()} game error: {e}", exc_info=True)
            self.state = GameState.ENDED
            current_span = trace.get_current_span()
            current_span.add_event(
                "game.state_changed",
                {
                    "game.state": "ENDED",
                    "game.id": self.game_id,
                    "game.error": str(e),
                },
            )
            await self.event_publisher(GameEvent.GAME_ERROR, {"game_id": self.game_id, "error": str(e)})
            raise

        finally:
            force_ended = not self.running
            self.running = False
            # Call force-end hook if game was externally stopped
            if force_ended:
                await self.on_force_end()
            await self._cleanup_after_run()
            logger.info(f"{self.get_game_name()} game finished: {self.game_id}")

    # ========================================================================
    # Helper Methods - Span creation utilities
    # ========================================================================

    def _create_player_lifecycle_span(self, serial: str, context=None) -> trace.Span:
        """
        Create a player lifecycle span.

        Args:
            serial: Controller serial number
            context: Parent span context (None to use current active span)

        Returns:
            Started span (caller is responsible for ending it)
        """
        player = self.players[serial]

        # If context is provided, use it; otherwise use current active span context
        if context is None:
            # Get current context from active span
            from opentelemetry import context as otel_context

            context = otel_context.get_current()

        return tracer.start_span(
            "player_lifecycle",  # Consistent name for all players (OpenTelemetry best practice)
            context=context,
            attributes={
                "player.serial": serial,
                "player.team": player.team,
                "player.color": str(player.color),
                GAME_MODE_ATTR: self.get_game_name(),
                # #845: stamp game identity so the agent can attribute a
                # player_lifecycle span to its game (mirrors game.id/game.kind on
                # the parent game-session span in game_session.py).
                "game.id": self.game_id,
                "game.kind": self.game_kind,
            },
        )

    def _finalize_player_health(self, player: Player) -> None:
        """Close health degradation spans and add summary attributes (#571).

        Called per-player before closing the player_lifecycle span.
        Safe to call even if no health spans were opened.
        """
        if not player.span:
            return

        # Close any open health degradation spans
        if player._poll_degraded_span is not None:
            from opentelemetry.trace import Status, StatusCode

            player._poll_degraded_span.set_attribute("health.total_poll_drops", player.total_poll_drops)
            player._poll_degraded_span.set_attribute("health.total_poll_errors", player.total_poll_errors)
            serial = getattr(player, "serial", "unknown")
            msg = f"drops={player.total_poll_drops} errors={player.total_poll_errors} serial={serial}"
            player._poll_degraded_span.record_exception(PollDegradationError(msg))
            player._poll_degraded_span.set_status(Status(StatusCode.ERROR, "poll degradation detected"))
            player._poll_degraded_span.end()
            player._poll_degraded_span = None
        if player._led_degraded_span is not None:
            player._led_degraded_span.set_attribute("health.total_led_failures", player.total_led_failures)
            player._led_degraded_span.end()
            player._led_degraded_span = None

        # Summary health attributes on player_lifecycle span
        player.span.set_attribute("health.total_poll_drops", player.total_poll_drops)
        player.span.set_attribute("health.total_poll_errors", player.total_poll_errors)
        player.span.set_attribute("health.total_led_failures", player.total_led_failures)
        player.span.set_attribute("health.final_drop_rate", player._health_drop_rate)
        player.span.set_attribute(
            "health.had_issues",
            player.total_poll_drops > 0 or player.total_poll_errors > 0 or player.total_led_failures > 0,
        )

    def _close_all_player_spans(self):
        """
        Close all open player lifecycle spans.

        Called at the end of gameplay_phase to ensure all player spans
        end with the gameplay phase, not during teardown.
        Subclasses can override to add custom attributes before closing.
        """
        from opentelemetry.trace import Status, StatusCode

        for serial, player in self.players.items():
            if player.span:
                self._finalize_player_health(player)

                # Add final event based on player state
                if player.alive:
                    player.span.add_event(
                        "game_ended",
                        attributes={
                            "survived": True,
                            "game_duration": time.time() - self.start_time if self.start_time else 0,
                        },
                    )
                player.span.set_status(Status(StatusCode.OK))
                player.span.end()
                player.span = None  # Mark as closed
                logger.debug(f"Closed lifecycle span for player {serial}")

    def _end_open_player_spans_on_exit(self) -> None:
        """End every player span the run left open (#1997, telemetry.md §3.2).

        Idempotent: a span the mode already ended (``is_recording()`` false) or
        cleared (``player.span`` None) is skipped, so a natural end never ends a
        span twice. A span closed here gets ``game_ended`` with ``winner`` false
        and ``game.outcome``: the latched outcome, else ``force_ended`` (+
        ``game.end_reason``), else ``error`` after run() raised, else
        ``completed``. Status ERROR only after run() raised. Open countdown
        spans close too; the caller closes the grouping spans.
        """
        from opentelemetry.trace import Status, StatusCode

        if self._latched_outcome is not None or self.force_ended:
            outcome = self._game_outcome_attributes()
        elif self._run_failed:
            outcome = {"game.outcome": "error"}
        else:
            outcome = {"game.outcome": "completed"}
        for serial, player in self.players.items():
            if player._countdown_span is not None:
                player._countdown_span.end()
                player._countdown_span = None
            span = player.span
            if span is None:
                continue
            if span.is_recording():
                self._finalize_player_health(player)
                span.add_event(
                    "game_ended",
                    attributes={
                        "winner": False,
                        **outcome,
                        "game_duration": time.time() - self.start_time if self.start_time else 0,
                    },
                )
                if self._run_failed:
                    span.set_status(Status(StatusCode.ERROR, "game ended before its end step"))
                else:
                    span.set_status(Status(StatusCode.OK))
                span.end()
                logger.debug(f"Closed lifecycle span for player {serial} on run exit")
            player.span = None

    def _close_grouping_spans(self):
        """Close the players and game_cycle grouping spans.

        Called after all player spans are closed, before gameplay_phase ends.
        """
        if self._players_span:
            self._players_span.end()
            self._players_span = None
        if self._game_cycle_span:
            self._game_cycle_span.end()
            self._game_cycle_span = None
            self.game_cycle_context = None

    async def _play_sound(
        self,
        sound: str | Sound,
        priority: int = 2,
        parent_context: otel_context.Context | None = None,
        volume: float = 1.0,
    ):
        """
        Play sound via Audio service (Phase 29).

        Args:
            sound: Sound enum or string name (e.g., Sound.VOX_CONGRATULATIONS or "congratulations")
            priority: Audio priority (0=LOW, 1=MEDIUM, 2=HIGH, 3=CRITICAL)
            parent_context: Explicit parent context. When set, overrides
                game_cycle_context so the sound span is parented to a specific
                span (e.g., a player lifecycle span for death explosions).
            volume: Playback volume 0.0-1.0 (default 1.0).

        A shadow game plays nothing: it has no audience and the audio service is shared (#1835).
        """
        if not self.audio_client or self._is_shadow_game():
            return

        try:
            from proto import audio_pb2

            # Convert Sound enum to string value if needed
            sound_name = sound.value if isinstance(sound, Sound) else sound
            request = audio_pb2.PlaySoundRequest(file_path=sound_name, volume=volume, priority=priority)

            # Use explicit parent if provided, otherwise fall back to game_cycle
            ctx = parent_context or self.game_cycle_context
            if ctx:
                token = otel_context.attach(ctx)
                try:
                    await self.audio_client.PlaySound(request)
                finally:
                    otel_context.detach(token)
            else:
                await self.audio_client.PlaySound(request)
            logger.debug(f"Playing sound: {sound_name}")
        except Exception as e:
            logger.warning(f"Failed to play sound {sound_name}: {e}")

    # ========================================================================
    # Phase 70: Music Tempo Control
    # ========================================================================

    def _lerp(self, a: float, b: float, t: float) -> float:
        """Linear interpolation between a and b by t (0.0 to 1.0)."""
        return a * (1 - t) + b * t

    def _emit_threshold_metrics(self):
        """Emit effective threshold metrics for the current tempo intensity (Phase 80, #1833)."""
        sens_idx = self.sensitivity.value
        speed_percent = self._intensity.value_at(time.time())

        effective_warn = self._lerp(self.slow_warning[sens_idx], self.fast_warning[sens_idx], speed_percent)
        effective_death = self._lerp(self.slow_max[sens_idx], self.fast_max[sens_idx], speed_percent)

        labels = self._game_state_labels()
        metrics.effective_warning_threshold.labels(**labels).set(effective_warn)
        metrics.effective_death_threshold.labels(**labels).set(effective_death)

    def _game_state_labels(self) -> dict[str, str]:
        """Label set of this game's game-state gauges (FR-041)."""
        return {"game_kind": self.game_kind, "game_id": self.game_id}

    def _emit_sensitivity_metric(self) -> None:
        """Emit this game's sensitivity level for the dashboard and the agent."""
        metrics.game_sensitivity.labels(**self._game_state_labels()).set(self.sensitivity.value)

    def _emit_music_tempo_metric(self) -> None:
        """Emit this game's current music tempo (Phase 80)."""
        metrics.music_tempo.labels(**self._game_state_labels()).set(self.music_speed)

    async def _start_game_music(self, volume: float | None = None):
        """
        Start game music with tempo control (Phase 70).

        Sets volume and starts music at normal speed. When ``volume`` is None,
        uses the per-game configured game volume (F7, #766).
        Note: play_audio setting is checked centrally in audio service.

        #1833 (UC-031 BR-005, D1): the tempo schedule starts here whether or not
        music plays (no audio client, muted, PlayMusic failed): the intensity and
        the thresholds move without a music track.
        """
        self.music_speed = SLOW_MUSIC_SPEED
        self.speed_up = True
        self._audio_synced = True
        self._audio_retry_backoff = AUDIO_TEMPO_RETRY_MIN_SECONDS
        self.change_time = self._get_music_change_time()
        self._emit_music_tempo_metric()
        self._emit_threshold_metrics()

        if self._is_shadow_game():
            # Shadow games are silent (#1835, UC-031 BR-006): PlayMusic on the shared audio
            # service would replace the real game's track. The schedule above still runs (D1).
            trace.get_current_span().set_attribute("game.music", "shadow")
            logger.info(f"Shadow game {self.game_id}: no music, tempo schedule runs silently")
            return

        if not self.audio_client:
            logger.info(
                f"No audio client: tempo schedule runs without music, next change at +{self._next_change_in():.1f}s"
            )
            return

        if volume is None:
            volume = self.game_volume

        try:
            from proto import audio_pb2

            # Set initial volume (may be low during countdown, then ramped up)
            await self.audio_client.SetVolume(audio_pb2.SetVolumeRequest(volume=volume))

            # Start game music
            response = await self.audio_client.PlayMusic(
                audio_pb2.PlayMusicRequest(
                    file_pattern="Joust/music/*.ogg",
                    loop=True,
                    tempo=SLOW_MUSIC_SPEED,
                    priority=audio_pb2.AudioPriority.MEDIUM,
                )
            )

            if response.success:
                self.music_track_id = response.track_id
                logger.info(f"Game music started: {response.track_id}, next change at +{self._next_change_in():.1f}s")
            else:
                logger.warning(f"Failed to start game music (tempo schedule runs without it): {response.error}")

        except Exception as e:
            logger.warning(f"Failed to start game music (tempo schedule runs without it): {e}")

    def _next_change_in(self) -> float:
        """Seconds until the next scheduled tempo change (for logs)."""
        return self.change_time - time.time()

    async def _stop_game_music(self):
        """Stop game music (Phase 70). A shadow game never started any (#1835)."""
        if not self.audio_client or self._is_shadow_game():
            return

        try:
            from proto import audio_pb2

            await self.audio_client.StopMusic(audio_pb2.StopMusicRequest(track_id=""))
            self.music_track_id = None
            logger.info("Game music stopped")

        except Exception as e:
            logger.warning(f"Failed to stop game music: {e}")

    def _get_music_change_time(self) -> float:
        """
        Calculate time of next tempo change based on game progression.

        As more players die, tempo changes become more frequent.
        Returns absolute time (time.time() + delay).

        #1109: this is the SINGLE pacing pathway. The delay is produced by the
        pacing-policy seam (:meth:`_decide_next_change_delay`), which dispatches
        on the live ``self.tempo_schedule_mode``. There is no separate legacy RNG
        branch beside the seam — the historical random-window timing is folded in
        as the rule engine's default rule (:meth:`_default_rule_delay`).
        """
        return time.time() + self._decide_next_change_delay()

    def _decide_next_change_delay(self) -> float:
        """Pacing-policy seam: produce the delay (seconds) until the next change.

        Dispatches on ``self.tempo_schedule_mode`` (read fresh every call, so the
        PROVIDER_CONFIGURATION_CHANGED listener's live swap takes effect on the
        next tempo decision):

        * ``rule`` (default / back-compat) — run the rule engine. The default
          rule is :meth:`_default_rule_delay`, the windowed RNG that reproduces
          today's timing exactly.
        * ``agent`` — yield to an agent tempo directive
          (``self.agent_tempo_next_delay``, set by the #1103 follow-up). STUB for
          Phase 1: with no directive present, fall back to the default rule so
          pacing never freezes when ``agent`` is selected before the primitive
          ships.

        Any unexpected mode falls through to the default rule (fail-safe).
        """
        mode = self.tempo_schedule_mode
        if mode == TEMPO_MODE_AGENT:
            directive = self.agent_tempo_next_delay
            if directive is not None and directive >= 0:
                return float(directive)
            # No directive yet (#1103 not shipped) -> hold the established pacing
            # by falling back to the default rule. (A future variant could hold
            # the current tempo instead; falling back keeps pacing alive today.)
            return self._default_rule_delay()
        # mode == "rule" (and any unknown value): the rule engine's default rule.
        return self._default_rule_delay()

    def _default_rule_delay(self) -> float:
        """Default pacing rule: the historical random-window schedule.

        Folds the former ``_get_music_change_time`` body in verbatim so the
        ``rule`` mode's default policy is byte-for-byte the pre-#1109 timing:
        a ``_rng.uniform`` draw within the (game-progression-lerped, F6-swappable)
        ``self.music_windows``. Randomness survives here as a RULE, not a mode.
        Returns a relative delay in seconds.
        """
        # Calculate game progression (0.0 = start, 1.0 = near end)
        min_moves = len(self.players) - 2
        if min_moves <= 0:
            min_moves = 1
        game_percent = min(1.0, self.dead_count / min_moves)

        # Interpolate between normal and end-game timing using the (init-frozen,
        # F6-swappable) per-instance windows. Read once into a local so a
        # concurrent F6 pacing_profile swap can't tear within a single change.
        windows = self.music_windows
        if self.speed_up:
            # Currently slow, will speed up - use slow timing
            min_t = self._lerp(windows.slow_min, windows.end_slow_min, game_percent)
            max_t = self._lerp(windows.slow_max, windows.end_slow_max, game_percent)
        else:
            # Currently fast, will slow down - use fast timing
            min_t = self._lerp(windows.fast_min, windows.end_fast_min, game_percent)
            max_t = self._lerp(windows.fast_max, windows.end_fast_max, game_percent)

        return self._rng.uniform(min_t, max_t)

    def apply_tempo_schedule_mode(self, value: object) -> None:
        """Atomically swap the live tempo-schedule SOURCE mode (#1109).

        Called by the PROVIDER_CONFIGURATION_CHANGED listener
        (TempoScheduleManager) with the freshly-resolved ``game.tempo_schedule_mode``
        flag value for THIS game's ``gameId``. The value is validated and stored
        in a single attribute store (like the F6 ``music_windows`` swap), so the
        100ms music loop / ``_decide_next_change_delay`` picks up the new source
        on its next decision without tearing — live mid-game mode switching.
        """
        new_mode = resolve_tempo_schedule_mode(value)
        if new_mode != self.tempo_schedule_mode:
            logger.info(f"tempo_schedule_mode: {self.tempo_schedule_mode} -> {new_mode}")
        self.tempo_schedule_mode = new_mode  # atomic single-store swap

    def _read_ramp_seconds(self, rising: bool) -> float:
        """Seconds for a full 0 -> 1 (``rising``) or 1 -> 0 swing (#1833).

        Read at each ramp start from ``game.tempo.ramp_up_seconds`` /
        ``game.tempo.ramp_down_seconds`` ("live per ramp", flags.md §4), never per
        frame. A flagd failure or a malformed value gives the default; the value is
        clamped to [RAMP_SECONDS_MIN, RAMP_SECONDS_MAX] (:func:`resolve_ramp_seconds`).
        """
        # Literal flag keys, so the contract scan resolves both reads.
        if rising:
            value = read_float_flag("game", "tempo.ramp_up_seconds", DEFAULT_RAMP_UP_SECONDS, game_id=self.game_id)
            return resolve_ramp_seconds(value, DEFAULT_RAMP_UP_SECONDS)
        value = read_float_flag("game", "tempo.ramp_down_seconds", DEFAULT_RAMP_DOWN_SECONDS, game_id=self.game_id)
        return resolve_ramp_seconds(value, DEFAULT_RAMP_DOWN_SECONDS)

    async def _start_intensity_ramp(self, target: float, *, scheduled: bool) -> None:
        """Start an intensity segment toward ``target`` from the intensity now (#1833, UC-031 BR-005).

        The segment lasts ``|target - i(now)|`` times the flag for its direction, so
        a half swing takes half the time and a change of target mid-ramp restarts
        from where the intensity is (no jump). The intensity is updated BEFORE the
        audio send, so thresholds never wait for the audio service.

        ``scheduled`` (the natural slow/fast schedule) flips ``speed_up`` and sets
        the next change ``window`` after the ramp END (as the original JoustMania
        did); an override leaves the schedule state alone.
        """
        now = time.time()
        old_intensity = self._intensity.value_at(now)
        target = max(0.0, min(1.0, target))
        delta = target - old_intensity
        duration = abs(delta) * self._read_ramp_seconds(rising=delta > 0) if delta else 0.0
        self._intensity = IntensityRamp(start=old_intensity, target=target, start_time=now, duration=duration)

        if scheduled:
            # Flip first: the default rule's window depends on the NEXT direction.
            self.speed_up = not self.speed_up
            self.change_time = now + duration + self._decide_next_change_delay()

        old_tempo = tempo_from_intensity(old_intensity)
        new_tempo = tempo_from_intensity(target)
        if delta > 0:
            direction = "speed_up"
        elif delta < 0:
            direction = "slow_down"
        else:
            direction = "hold"

        # Child span of game_cycle; the ChangeTempo call inherits it, so the audio
        # service span links back to the game trace.
        with tracer.start_as_current_span("music_tempo_change") as span:
            span.set_attribute("old_tempo", old_tempo)
            span.set_attribute("new_tempo", new_tempo)
            span.set_attribute("dead_count", self.dead_count)
            span.set_attribute("direction", direction)
            span.set_attribute("transition_seconds", duration)
            await self._push_audio(target, duration)

        logger.info(f"Tempo intensity {old_intensity:.2f} -> {target:.2f} over {duration:.2f}s ({direction})")

        # Add timeline marker event on gameplay_span (visible in trace overview)
        if self.gameplay_span:
            self.gameplay_span.add_event(
                "music_tempo_change",
                attributes={
                    "old_tempo": old_tempo,
                    "new_tempo": new_tempo,
                    "direction": direction,
                },
            )

        self._emit_music_tempo_metric()
        self._emit_threshold_metrics()
        if scheduled:
            logger.debug(f"Next tempo change at +{self._next_change_in():.1f}s")

    async def _push_audio(self, target: float, remaining: float) -> None:
        """Send one ChangeTempo glide toward ``target`` over ``remaining`` seconds (#1833).

        Skipped (nothing to keep in sync) without an audio client or a playing track
        (none, failed PlayMusic, or ``muted``): the intensity moves anyway (D1).
        An error or a timeout marks the audio unsynced, so the music loop resends
        the remaining glide after a back-off (0.5 s doubling to 5 s). A refusal
        (``success=False``: the track was replaced or stopped) stops the resends
        for this track: the audio service no longer plays it.
        """
        if not self.audio_client or self.music_track_id in (None, MUTED_TRACK_ID):
            self._audio_synced = True
            return

        from proto import audio_pb2

        request = audio_pb2.ChangeTempoRequest(
            track_id=self.music_track_id,
            new_tempo=tempo_from_intensity(target),
            transition_duration=max(remaining, MIN_GLIDE_SECONDS),
        )
        try:
            response = await asyncio.wait_for(
                self.audio_client.ChangeTempo(request), timeout=AUDIO_TEMPO_RPC_TIMEOUT_SECONDS
            )
        except Exception as e:
            if self._audio_synced or self._audio_retry_backoff >= AUDIO_TEMPO_RETRY_MAX_SECONDS:
                # First failure of a streak, then once per maximum back-off: no log flood.
                logger.warning(f"Failed to change music tempo (intensity continues, resending): {e!r}")
            self._audio_synced = False
            self._audio_retry_at = time.time() + self._audio_retry_backoff
            self._audio_retry_backoff = min(self._audio_retry_backoff * 2, AUDIO_TEMPO_RETRY_MAX_SECONDS)
            return

        self._audio_retry_backoff = AUDIO_TEMPO_RETRY_MIN_SECONDS
        self._audio_synced = True
        if response is not None and not response.success:
            logger.warning(
                f"Audio refused tempo change for track {self.music_track_id} ({response.error}); "
                "no further tempo changes are sent for it"
            )
            self.music_track_id = None

    async def _apply_ramp_tempo(self, target_tempo: float) -> None:
        """Apply one interpolated step of an ``ease`` ramp_tempo curve (#1117).

        The ease curve keeps its per-tick staircase: each 100 ms tick pins the
        intensity at the curve's value and sends a short glide over about one tick
        (RAMP_TEMPO_STEP_TRANSITION), not a full ramp per step. It does NOT flip
        ``speed_up`` or reset ``change_time``, so the natural schedule resumes from
        the held intensity once the ramp is reverted.
        """
        old_tempo = self.music_speed
        self._intensity = IntensityRamp.hold(intensity_from_tempo(target_tempo), time.time())
        with tracer.start_as_current_span("music_tempo_ramp") as span:
            span.set_attribute("old_tempo", old_tempo)
            span.set_attribute("new_tempo", target_tempo)
            span.set_attribute("dead_count", self.dead_count)
            await self._push_audio(self._intensity.target, RAMP_TEMPO_STEP_TRANSITION)

        self._emit_music_tempo_metric()
        self._emit_threshold_metrics()

    async def _adopt_linear_tempo_ramp(self, ramp: RampDescriptor) -> None:
        """Turn a linear ramp_tempo descriptor into ONE intensity segment and one glide (#1833).

        The segment starts at the intensity NOW and ends at the descriptor's end
        time, so a descriptor adopted late (armed under an override that has
        since cleared) never makes the thresholds or the music jump (BR-005). It
        never runs faster than the bounded rate of step 3: when the descriptor's
        end is already near or past, the segment takes |delta i| x the flag for
        its direction instead.
        """
        now = time.time()
        old_intensity = self._intensity.value_at(now)
        target = intensity_from_tempo(ramp.target_tempo)
        delta = target - old_intensity
        bounded = abs(delta) * self._read_ramp_seconds(rising=delta > 0) if delta else 0.0
        remaining = max(ramp.start_time + ramp.duration - now, bounded, 0.0)
        self._intensity = IntensityRamp(start=old_intensity, target=target, start_time=now, duration=remaining)
        with tracer.start_as_current_span("music_tempo_ramp") as span:
            span.set_attribute("old_tempo", tempo_from_intensity(old_intensity))
            span.set_attribute("new_tempo", ramp.target_tempo)
            span.set_attribute("dead_count", self.dead_count)
            await self._push_audio(target, remaining)
        self._emit_music_tempo_metric()
        self._emit_threshold_metrics()

    async def _hold_intensity_now(self) -> None:
        """End an agent-owned segment: hold the intensity reached now and stop the glide there."""
        self._agent_tempo_owned = False
        now = time.time()
        held = self._intensity.value_at(now)
        if not self._intensity.is_complete(now):
            logger.info(f"Agent tempo control ended mid-ramp: holding intensity {held:.2f}")
            self._intensity = IntensityRamp.hold(held, now)
            await self._push_audio(held, MIN_GLIDE_SECONDS)
            self._emit_music_tempo_metric()
            self._emit_threshold_metrics()

    async def _check_music_speed(self):
        """Drive the tempo intensity and keep the music in sync (Phase 70, #1833).

        Called every 100 ms from the music loop; the single tempo owner. Order:
        agent override, then ramp_tempo, then the natural schedule. Runs without a
        music track too (UC-031 BR-005, D1): only the audio send is skipped.
        Every audio failure is handled in :meth:`_push_audio`; this never raises
        for an audio error.
        """
        now = time.time()

        if self.tempo_override is not None:
            # Agent tempo override (#730 PR C): ramp to the override at the bounded
            # rate and hold; the natural schedule is suspended (speed_up and
            # change_time untouched), and resumes from the held intensity when the
            # override clears. A new override value restarts from i(now).
            self._agent_tempo_owned = True
            self._adopted_tempo_ramp = None
            override_target = intensity_from_tempo(self.tempo_override)
            if abs(self._intensity.target - override_target) >= 1e-6:
                await self._start_intensity_ramp(override_target, scheduled=False)
        elif self.tempo_ramp is not None:
            # #1117 ramp_tempo CURVE: a linear curve is one intensity segment (one
            # glide per descriptor, detected by identity); an ease curve steps per
            # tick. change_time is left untouched (mirrors the override).
            self._agent_tempo_owned = True
            ramp = self.tempo_ramp
            if ramp.curve == RAMP_CURVE_LINEAR:
                if ramp is not self._adopted_tempo_ramp:
                    self._adopted_tempo_ramp = ramp
                    await self._adopt_linear_tempo_ramp(ramp)
            else:
                self._adopted_tempo_ramp = None
                desired = ramp.tempo_at(now)
                if abs(self.music_speed - desired) > 1e-3:
                    await self._apply_ramp_tempo(desired)
        else:
            self._adopted_tempo_ramp = None
            if self._agent_tempo_owned:
                # The override cleared or the ramp was reverted (A3): stop the agent's
                # segment where it is now, so a reverted action stops acting at once.
                # The schedule resumes from the held intensity (at once if due).
                await self._hold_intensity_now()
            # Natural schedule: the next change is due once its window has passed
            # AND the previous segment has finished.
            if now >= self.change_time and self._intensity.is_complete(now):
                await self._start_intensity_ramp(1.0 if self.speed_up else 0.0, scheduled=True)

        # Resend the remaining glide after a failed send (back-off in _push_audio).
        now = time.time()
        if not self._audio_synced and now >= self._audio_retry_at:
            await self._push_audio(self._intensity.target, self._intensity.end_time - now)

        # Gauges follow the intensity while a ramp runs, plus once after it ends.
        ramp_running = not self._intensity.is_complete(now)
        if ramp_running or self._ramp_metrics_open:
            self._emit_music_tempo_metric()
            self._emit_threshold_metrics()
        self._ramp_metrics_open = ramp_running

    def _log_tick_failure(self, error: Exception) -> None:
        """Warn about a failed tempo tick: first, on a new error text, then every interval."""
        text = repr(error)
        now = time.time()
        if text != self._tick_error_text:
            self._tick_error_suppressed = 0  # a stale count must not leak into a new error
        if text == self._tick_error_text and now - self._tick_error_logged_at < MUSIC_TICK_LOG_INTERVAL_SECONDS:
            self._tick_error_suppressed += 1
            return
        suffix = f" ({self._tick_error_suppressed} similar suppressed)" if self._tick_error_suppressed else ""
        logger.warning(f"Tempo tick failed, retrying next tick: {text}{suffix}")
        self._tick_error_text = text
        self._tick_error_logged_at = now
        self._tick_error_suppressed = 0

    async def _music_loop(self):
        """
        Background task to manage music tempo changes (Phase 70).

        Runs alongside the main game loop and periodically checks
        if tempo should change based on game progression.

        Sets game_cycle as active context so each music_tempo_change span
        appears directly under game_cycle in traces.
        """
        logger.info("Music loop started")

        try:
            token = otel_context.attach(self.game_cycle_context) if self.game_cycle_context else None
            try:
                while self.running:
                    try:
                        await self._check_music_speed()
                    except Exception as e:
                        # A non-audio error (pacing seam, metrics, tracing) must not end
                        # the tempo loop for the rest of the game: log it, retry next tick.
                        self._log_tick_failure(e)
                    await asyncio.sleep(0.1)  # Check every 100ms
            finally:
                if token is not None:
                    otel_context.detach(token)
        except asyncio.CancelledError:
            logger.info("Music loop cancelled")
            raise  # Re-raise to properly propagate cancellation
        except Exception as e:
            logger.warning(f"Music loop error: {e}")
        finally:
            logger.info("Music loop ended")
