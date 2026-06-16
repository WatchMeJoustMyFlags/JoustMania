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

from opentelemetry import context as otel_context
from opentelemetry import trace

from lib.controller_constants import battery_to_pct
from lib.feature_flags import (
    GAME_KIND_REAL,
    GAME_KIND_SHADOW,
    read_float_flag,
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
MUSIC_TRANSITION_DURATION = 1.5  # Seconds to smoothly transition

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
# Upper bound on a single ramp's duration (seconds). Generous enough for a slow
# pacing curve, small enough that a malformed huge value can't suspend the natural
# schedule for an entire game.
RAMP_TEMPO_MAX_SECONDS = 60.0

# Per-step audio transition for a ramp (#1122 review): the ramp applies a small
# tempo step every ~100ms loop tick, so each step's ChangeTempo must glide over
# roughly one tick rather than MUSIC_TRANSITION_DURATION (1.5s) — otherwise every
# tick stacks an overlapping 1.5s transition chasing a stale target. The loop's
# interpolation owns the overall smoothness; the audio just tracks each step.
RAMP_TEMPO_STEP_TRANSITION = 0.12


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
        if self.curve == RAMP_CURVE_EASE:
            # Smoothstep: ease in and out, monotone, hits 0/1 at the endpoints.
            progress = progress * progress * (3.0 - 2.0 * progress)
        return self.start_tempo + (self.target_tempo - self.start_tempo) * progress

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
# Formula: threshold = lerp(SLOW[sens], FAST[sens], music_speed_percent)
# Where music_speed_percent = (current_speed - SLOW_SPEED) / (FAST_SPEED - SLOW_SPEED)
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
    # Time-boxed partial shield (#1129, #1103 Phase 2). When ``partial_shield_until``
    # is in the future, ``partial_shield_boost`` acts as a TEMPORARY handicap
    # override that COMPOSES with ``handicap_factor`` by taking the MAX of the two
    # (so a standing set_player_handicap is never weakened, only strengthened for
    # the window) — see ``_compute_effective_thresholds``. A large boost (~2.0)
    # makes the player MUCH harder to eliminate but NOT immune (vs grant_shield's
    # total grace_until immunity): the combined factor is still clamped [0.5, 2.0].
    # On expiry the boost simply stops applying (no reset task needed) and the
    # effective handicap falls back to the standing ``handicap_factor``. 0.0 / past
    # deadline = inactive (no effect). Shadow-game-only via the allow-list gate.
    partial_shield_until: float = 0.0
    partial_shield_boost: float = 1.0
    # Time-boxed SOFT-PENALTY tighten (#1134, #1103 Phase 3). The deliberate
    # MIRROR of partial_shield: where partial_shield STRENGTHENS protection (max,
    # factor >= 1.0), a tighten WEAKENS it for a short window (factor < 1.0),
    # making the player TEMPORARILY EASIER to eliminate — graduated pressure, not
    # guaranteed elimination. While ``soft_penalty_until`` is in the future,
    # ``soft_penalty_factor`` composes into the effective handicap by taking the
    # MIN (so it can cut through even a partial_shield boost — pressure overrides
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
    # COMPRESSES the gap to the leader. Like partial_shield it STRENGTHENS (boost
    # >= 1.0, composed by MAX), but unlike it the boost is computed per-player
    # from the gap and is CAPPED so the boosted laggard can never overtake the
    # current leader (never inverts standings). While ``rubberband_until`` is in
    # the future, ``rubberband_boost`` composes into the effective handicap by the
    # MAX (alongside partial_shield), still clamped [0.5, 2.0] so even a maxed
    # boost is "harder to die", never immune / never instant-death. On expiry the
    # boost silently stops applying (no reset task needed). 1.0 / past deadline =
    # inactive. Shadow-game-only via the allow-list gate.
    rubberband_until: float = 0.0
    rubberband_boost: float = 1.0
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
        #   _reset_global_gauges_on_end: only the primary session resets the
        #     single-game state gauges (music_tempo, sensitivity, thresholds).
        #   experiment_id / arm: experiment attribution within a shadow session
        #     (#975); empty for a non-experiment game. The servicer overrides
        #     these from the owning GameSession (set by #976's spawn binding).
        self.game_kind = "primary"
        self._reset_global_gauges_on_end = True
        self.experiment_id = ""
        self.arm = ""

        # Game state
        self.state = GameState.IDLE
        self.players: dict[str, Player] = {}
        self.initial_players = initial_players  # Players from StartGame RPC
        self.start_time = None
        self.running = False
        self.gameplay_stream = None  # Phase 46: Bidirectional stream for feedback commands

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
        self.music_speed = SLOW_MUSIC_SPEED
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

    async def _start_gameplay_stream(self):
        """
        Create and configure the gameplay stream.

        Called before countdown to allow EMA warmup during countdown phase.
        The stream is stored in self.gameplay_stream for use by _game_loop().
        """
        await self._create_gameplay_stream()

    async def _warmup_ema(self):
        """
        Drain buffered stream data and prime EMA filters before game loop.

        The gameplay stream opens ~2.25s before the game loop starts (before
        countdown). Data buffered during that time can contain stale spikes
        that cause instant deaths when the EMA is uninitialized. This method
        reads and discards buffered frames while priming each player's EMA
        with real sensor data — no death checks run.
        """
        warmup_duration = 0.1  # 100ms — enough to drain buffer and get ~6 frames at 60Hz
        warmup_start = time.time()
        frames_consumed = 0

        try:
            async for gameplay_update in self.gameplay_stream:
                for controller_data in gameplay_update.controllers:
                    serial = controller_data.serial
                    if serial in self.players and self.players[serial].alive:
                        accel_mag = self._compute_accel_magnitude(controller_data.accel)
                        self._update_ema(self.players[serial], accel_mag, self._ema_weight)
                frames_consumed += 1

                if time.time() - warmup_start >= warmup_duration:
                    break
        except Exception as e:
            logger.warning(f"EMA warmup interrupted: {e}")

        logger.info(f"EMA warmup complete: drained {frames_consumed} buffered frames")

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
            metrics.actual_update_frequency_hz.set(actual_hz)

            consistency_percent = (frames_on_target / loop_iterations) * 100
            metrics.game_loop_frame_consistency_percent.set(consistency_percent)

            if len(recent_frame_times) >= 2:
                jitter_ms = statistics.stdev(recent_frame_times)
                metrics.game_loop_jitter_ms.set(jitter_ms)

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

        backoff = attempt  # Linear backoff: 1s, 2s, 3s
        logger.warning(
            f"Gameplay stream error (attempt {attempt}/{max_attempts}): {error}. Reconnecting in {backoff}s..."
        )
        metrics.gameplay_stream_reconnects_total.labels(game_mode=self.get_game_name()).inc()
        await asyncio.sleep(backoff)

        await self._create_gameplay_stream()
        return attempt

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
            grace_until = time.time() + 0.3
            for player in self.players.values():
                if player.alive:
                    player.grace_until = grace_until

            # Track current alive set for detecting changes
            last_alive_serials = {p.serial for p in self.players.values() if p.alive}
            logger.info(f"Initial alive players: {len(last_alive_serials)}")

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

            logger.info("Starting gameplay data stream loop, waiting for first update...")
            while self.running:
                try:
                    async for gameplay_update in self.gameplay_stream:
                        if loop_iterations == 0:
                            logger.info("Received first gameplay update from stream!")
                            self.start_time = time.time()

                        # Reset reconnect counter on successful data
                        reconnect_attempt = 0

                        if not self.running:
                            logger.info("Game running=False, breaking loop")
                            break

                        # Process controllers and check for game over
                        if await self._process_gameplay_update(gameplay_update):
                            logger.info("Win condition met, keeping game active for 1 second to show winner")
                            await asyncio.sleep(1.0)
                            return

                        # Update alive filter if players changed
                        last_alive_serials = await self._send_alive_filter_update(last_alive_serials)

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

                    # Stream ended normally (server closed it) — exit loop
                    break

                except asyncio.CancelledError:
                    raise  # Never retry on cancellation

                except Exception as e:
                    if not self.running:
                        break

                    reconnect_attempt += 1
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

        Uses LERP between slow/fast threshold arrays based on music speed,
        then applies per-player sensitivity factor.

        Args:
            player: Player to compute thresholds for

        Returns:
            Tuple of (effective_warning_threshold, effective_death_threshold)
        """
        sens_idx = self.sensitivity.value

        # Calculate music speed as percentage (0.0 = slow, 1.0 = fast)
        speed_range = FAST_MUSIC_SPEED - SLOW_MUSIC_SPEED
        speed_percent = (self.music_speed - SLOW_MUSIC_SPEED) / speed_range if speed_range > 0 else 0.0
        speed_percent = max(0.0, min(1.0, speed_percent))  # Clamp to [0, 1]

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
        # A time-boxed partial shield (#1129) takes the MAX with the standing
        # handicap (it can only ever STRENGTHEN protection for its window, never
        # weaken a standing set_player_handicap delta). Precedence: while the
        # deadline is in the future the effective handicap is
        # max(handicap_factor, partial_shield_boost); once it expires the boost
        # silently drops out and the standing handicap_factor resumes. Still
        # clamped [0.5, 2.0] below, so even a ~2.0 boost is "much harder to die",
        # never truly immune (the deliberate distinction from grant_shield).
        #
        # Composition precedence (last-applied wins by intent):
        #   1. standing handicap_factor (set_player_handicap, #1107)
        #   2. partial_shield boost (#1129) — STRENGTHENS via max (protection)
        #   2b. auto_rubberband boost (#1143) — STRENGTHENS via max (gap-compression)
        #   3. soft_penalty tighten (#1134) — WEAKENS via min (pressure)
        # The two STRENGTHENING boosts (partial_shield, auto_rubberband) both
        # compose by MAX so the strongest active protection wins; the soft_penalty
        # MIN is applied AFTER, so an active tighten can cut through either boost:
        # pressure deliberately overrides protection (the mirror of partial_shield,
        # which could never weaken a standing handicap). The final clamp [0.5, 2.0]
        # keeps even a fully-tightened threshold finite — never instant-death — and
        # caps even a maxed boost at 2x — "harder to die", never immune.
        now = time.time()
        effective_handicap = player.handicap_factor
        if now < player.partial_shield_until:
            effective_handicap = max(effective_handicap, player.partial_shield_boost)
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
        accel,
        accel_mag: float,
        smoothed: float,
        effective_death: float,
        controller_state,
    ) -> None:
        """
        Record analytics sample and emit periodic Prometheus metrics.

        Args:
            player: Player to record analytics for
            serial: Controller serial number
            accel: Raw acceleration vector
            accel_mag: Raw acceleration magnitude
            smoothed: Smoothed acceleration value
            effective_death: Current effective death threshold
            controller_state: Full controller state protobuf (for gyro data)
        """
        config = get_config_manager().get_config()
        if not config.analytics.enabled or player.analytics is None:
            return

        # Get gyro data if available
        gyro = controller_state.gyro if hasattr(controller_state, "gyro") else None
        gyro_x = gyro.x if gyro else 0.0
        gyro_y = gyro.y if gyro else 0.0
        gyro_z = gyro.z if gyro else 0.0

        # Record sample (returns current movement zone)
        zone = player.analytics.record_sample(
            accel_x=accel.x,
            accel_y=accel.y,
            accel_z=accel.z,
            raw_accel_mag=accel_mag,
            smoothed_accel=smoothed,
            death_threshold=effective_death,
            config=config.analytics,
            gyro_x=gyro_x,
            gyro_y=gyro_y,
            gyro_z=gyro_z,
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

        accel = controller_state.accel
        accel_mag = self._compute_accel_magnitude(accel)

        # Check grace period first - no death or warning during grace period
        # Matches original JoustMania: if time.time() > no_rumble. Uses the unified
        # effective-grace rule (#817): max(agent shield grace_until, admin
        # invincible_until) so the agent shield and admin baseline compose here too.
        current_time = time.time()
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
                accel,
                accel_mag,
                accel_mag,
                self._compute_effective_thresholds(player)[1],
                controller_state,
            )
            return  # In grace period, skip death/warning checks

        self._update_ema(player, accel_mag, self._ema_weight)

        effective_warn, effective_death = self._compute_effective_thresholds(player)
        smoothed = player.smoothed_accel

        # Record analytics (handles enabled check internally)
        self._record_player_analytics(player, serial, accel, accel_mag, smoothed, effective_death, controller_state)

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

        A visible pulse effect (``GAME_EFFECT_PULSE``) is sent to the controller
        so the shield is observable, reusing the same gameplay-stream effect
        mechanism as warnings/deaths.

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

        new_grace = time.time() + seconds
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

        player.grace_until = new_grace
        logger.info(f"Shield granted to {serial} for {seconds:.1f}s (grace_until={new_grace:.2f})")

        if player.span:
            player.span.add_event("shield_granted", attributes={"shield_seconds": seconds})

        # Visible pulse effect so the shield is observable on the controller.
        if self.gameplay_stream:
            from proto import controller_manager_pb2

            trace_parent, trace_state = inject_trace_context(player.span)
            effect_cmd = controller_manager_pb2.GameplayStreamControl(
                game_effect=controller_manager_pb2.GameEffectCommand(
                    serial=serial,
                    effect=controller_manager_pb2.GAME_EFFECT_PULSE,
                    trace_parent=trace_parent,
                    trace_state=trace_state,
                )
            )
            await self.gameplay_stream.write(effect_cmd)

        return True

    # Partial-shield defaults (#1129, #1103 Phase 2). Boost is clamped to the same
    # upper bound as handicap_factor so a partial shield is "much harder to die"
    # but never immune; seconds are capped so a single arming can't last forever.
    PARTIAL_SHIELD_DEFAULT_BOOST = 2.0
    PARTIAL_SHIELD_BOOST_MIN = 1.0
    PARTIAL_SHIELD_BOOST_MAX = 2.0
    PARTIAL_SHIELD_MAX_SECONDS = 30.0

    async def grant_partial_shield(self, serial: str, seconds: float, boost: float | None = None) -> bool:
        """
        Grant a time-boxed PARTIAL shield to a player (#1129, #1103 Phase 2).

        Unlike :meth:`grant_shield` (total immunity via ``grace_until`` that skips
        the death check entirely), a partial shield only raises the player's
        effective death/warning threshold for ``seconds`` by applying a temporary
        ``handicap`` boost — making them MUCH harder to eliminate but NOT immune: a
        genuinely huge spike can still exceed the (still-finite) boosted threshold.
        It reuses the #1107 per-player handicap mechanism: while the deadline is in
        the future, ``_compute_effective_thresholds`` takes ``max(handicap_factor,
        partial_shield_boost)`` (so it never weakens a standing set_player_handicap
        delta, only strengthens it for the window), still clamped [0.5, 2.0].

        Time-box semantics mirror ``grant_shield``: extend-not-shorten on the
        deadline. A new arming only ever lengthens an active window (and never
        lowers an active boost); a shorter/weaker request is a no-op extension. On
        expiry the boost silently drops out — no reset task needed — and the
        standing ``handicap_factor`` resumes.

        Args:
            serial: Controller serial number.
            seconds: Shield duration in seconds. Non-positive is a no-op (False);
                capped at ``PARTIAL_SHIELD_MAX_SECONDS``.
            boost: Handicap boost while active; clamped
                [``PARTIAL_SHIELD_BOOST_MIN``, ``PARTIAL_SHIELD_BOOST_MAX``].
                Defaults to ``PARTIAL_SHIELD_DEFAULT_BOOST`` (~2.0).

        Returns:
            True if a partial shield was armed/extended, False if the player is
            unknown/dead or the duration was non-positive.
        """
        if seconds <= 0:
            return False

        player = self.players.get(serial)
        if not player or not player.alive:
            return False

        seconds = min(seconds, self.PARTIAL_SHIELD_MAX_SECONDS)
        if boost is None:
            boost = self.PARTIAL_SHIELD_DEFAULT_BOOST
        boost = max(self.PARTIAL_SHIELD_BOOST_MIN, min(self.PARTIAL_SHIELD_BOOST_MAX, boost))

        new_until = time.time() + seconds
        # Extend-not-shorten / strengthen-not-weaken: keep whichever active window
        # is longer and whichever active boost is higher, so repeated arming never
        # reduces in-flight protection.
        if time.time() < player.partial_shield_until:
            new_until = max(new_until, player.partial_shield_until)
            boost = max(boost, player.partial_shield_boost)
        player.partial_shield_until = new_until
        player.partial_shield_boost = boost
        logger.info(f"Partial shield armed for {serial}: boost={boost:.2f} for {seconds:.1f}s (until={new_until:.2f})")

        if player.span:
            player.span.add_event(
                "partial_shield_armed",
                attributes={"partial_shield_seconds": seconds, "partial_shield_boost": boost},
            )

        # Visible pulse effect so the partial shield is observable (same mechanism
        # as grant_shield's pulse).
        if self.gameplay_stream:
            from proto import controller_manager_pb2

            trace_parent, trace_state = inject_trace_context(player.span)
            effect_cmd = controller_manager_pb2.GameplayStreamControl(
                game_effect=controller_manager_pb2.GameEffectCommand(
                    serial=serial,
                    effect=controller_manager_pb2.GAME_EFFECT_PULSE,
                    trace_parent=trace_parent,
                    trace_state=trace_state,
                )
            )
            await self.gameplay_stream.write(effect_cmd)

        return True

    # Soft-penalty defaults (#1134, #1103 Phase 3). The tighten factor is the
    # MIRROR of the partial_shield boost: a factor BELOW 1.0 weakens the player's
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

        The deliberate MIRROR of :meth:`grant_partial_shield`: where a partial
        shield STRENGTHENS protection (boost >= 1.0, composed by ``max``), a
        tighten WEAKENS it for ``seconds`` (``factor`` < 1.0, composed by ``min``
        in :meth:`_compute_effective_thresholds`) — making the player TEMPORARILY
        EASIER to eliminate. It is graduated PRESSURE, not guaranteed elimination:
        the effective handicap is still clamped [0.5, 2.0], so even a maxed
        tighten only lowers the threshold to half — never instant-death.

        Because the tighten composes by ``min`` AFTER the partial_shield ``max``,
        it can cut through an active partial_shield boost (pressure overrides
        protection). On expiry the factor silently stops applying — no reset task
        — and the standing/shielded handicap resumes.

        Time-box semantics mirror :meth:`grant_partial_shield`: extend-not-shorten
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
                attributes={"soft_penalty_seconds": seconds, "soft_penalty_factor": factor},
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

        Unlike :meth:`set_player_handicap` / :meth:`grant_partial_shield` (the
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
        :meth:`grant_partial_shield` (extend-not-shorten, strengthen-not-weaken).

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
            # Extend-not-shorten / strengthen-not-weaken (mirror partial_shield).
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
            True if the player was revived, False if unknown / already alive.
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
            await native(serial)
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
            player.span.add_event("player_revived", attributes={"spawn_grace": grace})

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

    async def _kill_player(self, serial: str, accel_mag: float, reason: str = "motion"):
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
        """
        player = self.players.get(serial)
        if not player or not player.alive:
            return

        alive_count_before = len([p for p in self.players.values() if p.alive])
        logger.info(f"Player died: {serial}, {alive_count_before - 1} players remaining")

        # Phase 70: Track deaths for music tempo timing
        self.dead_count += 1

        # Mark player as dead in metrics - dashboard template variables filter
        # on game_player_alive==1 so dead players naturally disappear from panels.
        # Metric removal happens at game end via clear_all_player_analytics().
        metrics.player_alive.labels(serial=serial, game_id=self.game_id).set(0)
        alive_count = len([p for p in self.players.values() if p.alive])
        self._set_players_alive_aggregate(alive_count)

        # Record elimination order (1 = first out) for #730 / #722 §7.
        # Makes session.elimination_sequence queryable per game.
        self._elimination_count += 1
        metrics.player_elimination_order.labels(serial=serial, game_id=self.game_id).set(self._elimination_count)

        # Extract trace context BEFORE _kill_player_impl() which may close the span.
        # This ensures the death effect and sound are parented to the player_lifecycle
        # span even after the subclass impl ends it. (#456)
        trace_parent, trace_state = inject_trace_context(player.span)

        # Play death explosion sound under the player's span
        player_ctx = trace.set_span_in_context(player.span) if player.span else None
        await self._play_sound(Sound.SFX_EXPLOSION, priority=2, parent_context=player_ctx)

        # Add death event to player's lifecycle span (Phase 3: Per-Player Sensitivity)
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
                },
            )

        # Finalize health attributes BEFORE _kill_player_impl() which may close the span.
        # Without this, dead players lose their health.total_poll_drops etc. attributes.
        self._finalize_player_health(player)

        # Call subclass-specific death handling
        await self._kill_player_impl(serial, accel_mag)

        # Send death effect via stream (red + vibrate, no restore)
        # Use pre-extracted trace context so effect appears under player_lifecycle
        # in traces, even though _kill_player_impl may have closed the span. (#456)
        from proto import controller_manager_pb2

        if self.gameplay_stream:
            effect_cmd = controller_manager_pb2.GameplayStreamControl(
                game_effect=controller_manager_pb2.GameEffectCommand(
                    serial=serial,
                    effect=controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH,
                    trace_parent=trace_parent,
                    trace_state=trace_state,
                )
            )
            await self.gameplay_stream.write(effect_cmd)

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

    def force_end(self):
        """Force the game to end (called externally)."""
        logger.info("Force ending game...")
        self.running = False

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

        Cancels any tracked async tasks registered via _track_task().
        Subclasses should override to add mode-specific cleanup
        (e.g., cancel timers, close connections) and call super().cleanup().
        """
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

                # Emit sensitivity metric for dashboard
                metrics.game_sensitivity.set(self.sensitivity.value)
                logger.info(f"Sensitivity: {self.sensitivity.name} ({self.sensitivity.value})")

                # Initialize players
                await self._initialize_players()

                # Validate player count
                if len(self.players) < 2:
                    raise ValueError(f"Need at least 2 players, got {len(self.players)}")

                init_span.set_attribute("player_count", len(self.players))

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
                eval_game_kind = GAME_KIND_SHADOW if self.game_kind == GAME_KIND_SHADOW else GAME_KIND_REAL
                set_game_transaction_context(
                    game_mode=self.get_game_name(),
                    controller_count=len(self.players),
                    sensitivity=self.sensitivity.value,
                    game_kind=eval_game_kind,
                    experiment_id=self.experiment_id or None,
                    arm=self.arm or None,
                )

                # Additional phases (e.g., color_assignment, team_formation)
                # These are children of initialization_phase
                for phase in self._get_additional_phases():
                    with tracer.start_as_current_span(phase.name):
                        await phase.execute()

                # Start gameplay stream before countdown (needed for countdown effects)
                await self._start_gameplay_stream()

                # Start game music BEFORE countdown at low volume.
                # OGG decode happens during countdown, so music is ready instantly after.
                await self._start_game_music(volume=self.countdown_music_volume)

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

                # Ramp music volume up to game level after countdown
                if self.audio_client:
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
                    # Stop music loop
                    if self.music_loop_task:
                        self.music_loop_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await self.music_loop_task

                    # Close all player spans, then grouping spans
                    self._close_all_player_spans()
                    self._close_grouping_spans()

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

                await self._end_game_impl()

                # Cleanup stream reference after winner effects are sent
                self.gameplay_stream = None

                # Clear THIS session's analytics so dashboards show no data when
                # the game is over, without clobbering other concurrent sessions
                # (#775). Only the primary session resets the single-game gauges.
                metrics.clear_session_player_analytics(
                    list(self.players.keys()),
                    self.game_id,
                    reset_global_gauges=self._reset_global_gauges_on_end,
                )

        except Exception as e:
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
            # Always call cleanup to cancel tracked tasks
            await self.cleanup()
            # Ensure this session's analytics are always cleared, even on error
            # (#775) — per-session targeted cleanup, never a global wipe.
            metrics.clear_session_player_analytics(
                list(self.players.keys()),
                self.game_id,
                reset_global_gauges=self._reset_global_gauges_on_end,
            )
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
    ):
        """
        Play sound via Audio service (Phase 29).

        Args:
            sound: Sound enum or string name (e.g., Sound.VOX_CONGRATULATIONS or "congratulations")
            priority: Audio priority (0=LOW, 1=MEDIUM, 2=HIGH, 3=CRITICAL)
            parent_context: Explicit parent context. When set, overrides
                game_cycle_context so the sound span is parented to a specific
                span (e.g., a player lifecycle span for death explosions).
        """
        if not self.audio_client:
            return

        try:
            from proto import audio_pb2

            # Convert Sound enum to string value if needed
            sound_name = sound.value if isinstance(sound, Sound) else sound
            request = audio_pb2.PlaySoundRequest(file_path=sound_name, volume=1.0, priority=priority)

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
        """Emit effective threshold metrics for the current music speed (Phase 80)."""
        sens_idx = self.sensitivity.value
        speed_range = FAST_MUSIC_SPEED - SLOW_MUSIC_SPEED
        speed_percent = (self.music_speed - SLOW_MUSIC_SPEED) / speed_range if speed_range > 0 else 0.0
        speed_percent = max(0.0, min(1.0, speed_percent))

        effective_warn = self._lerp(self.slow_warning[sens_idx], self.fast_warning[sens_idx], speed_percent)
        effective_death = self._lerp(self.slow_max[sens_idx], self.fast_max[sens_idx], speed_percent)

        metrics.effective_warning_threshold.set(effective_warn)
        metrics.effective_death_threshold.set(effective_death)

    async def _start_game_music(self, volume: float | None = None):
        """
        Start game music with tempo control (Phase 70).

        Sets volume and starts music at normal speed. When ``volume`` is None,
        uses the per-game configured game volume (F7, #766).
        Note: play_audio setting is checked centrally in audio service.
        """
        if not self.audio_client:
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
                self.music_speed = SLOW_MUSIC_SPEED
                self.speed_up = True
                self.change_time = self._get_music_change_time()
                next_change = self.change_time - time.time()
                # Update metrics for dashboard (Phase 80)
                metrics.music_tempo.set(self.music_speed)
                self._emit_threshold_metrics()
                logger.info(f"Game music started: {response.track_id}, next change at +{next_change:.1f}s")
            else:
                logger.warning(f"Failed to start game music: {response.error}")

        except Exception as e:
            logger.warning(f"Failed to start game music: {e}")

    async def _stop_game_music(self):
        """Stop game music (Phase 70)."""
        if not self.audio_client:
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

    async def _apply_tempo_change(self, target_tempo: float) -> None:
        """
        Apply a tempo change to the music track.

        Creates a child span of music_tempo_control, sends gRPC request,
        logs the change, and updates internal state and metrics.

        Args:
            target_tempo: The target tempo to transition to
        """
        from proto import audio_pb2

        old_tempo = self.music_speed
        direction = "speed_up" if self.speed_up else "slow_down"

        # Create a child span — the gRPC ChangeTempo call inherits this context
        # naturally, so the audio service span links back to the game trace.
        with tracer.start_as_current_span("music_tempo_change") as span:
            span.set_attribute("old_tempo", old_tempo)
            span.set_attribute("new_tempo", target_tempo)
            span.set_attribute("dead_count", self.dead_count)
            span.set_attribute("direction", direction)

            await self.audio_client.ChangeTempo(
                audio_pb2.ChangeTempoRequest(
                    track_id=self.music_track_id,
                    new_tempo=target_tempo,
                    transition_duration=MUSIC_TRANSITION_DURATION,
                )
            )

        logger.info(f"Music tempo changing: {old_tempo:.2f} -> {target_tempo:.2f}")

        # Add timeline marker event on gameplay_span (visible in trace overview)
        if self.gameplay_span:
            self.gameplay_span.add_event(
                "music_tempo_change",
                attributes={
                    "old_tempo": old_tempo,
                    "new_tempo": target_tempo,
                    "direction": direction,
                },
            )

        # Update state
        self.music_speed = target_tempo
        self.speed_up = not self.speed_up
        self.change_time = self._get_music_change_time()
        # Update metrics for dashboard (Phase 80)
        metrics.music_tempo.set(self.music_speed)
        self._emit_threshold_metrics()

        logger.debug(f"Next tempo change at +{self.change_time - time.time():.1f}s")

    async def _apply_ramp_tempo(self, target_tempo: float) -> None:
        """Apply one interpolated step of a ramp_tempo curve (#1117).

        Like :meth:`_apply_tempo_change` this sends the audio ``ChangeTempo`` RPC
        and updates ``music_speed`` + metrics, so the ramp goes through the SAME
        single tempo owner (no second writer / no race). It deliberately does NOT
        flip ``speed_up`` or reset ``change_time``: a ramp applies many small steps
        toward a target, and leaving the schedule bookkeeping untouched lets the
        natural slow/fast schedule resume from the held tempo once the ramp is
        reverted — exactly like the override path.
        """
        from proto import audio_pb2

        old_tempo = self.music_speed
        with tracer.start_as_current_span("music_tempo_ramp") as span:
            span.set_attribute("old_tempo", old_tempo)
            span.set_attribute("new_tempo", target_tempo)
            span.set_attribute("dead_count", self.dead_count)
            await self.audio_client.ChangeTempo(
                audio_pb2.ChangeTempoRequest(
                    track_id=self.music_track_id,
                    new_tempo=target_tempo,
                    # Short per-step transition (#1122 review): the ramp applies
                    # a small step every ~100ms tick, so each step must glide over
                    # roughly one tick — NOT the 1.5s MUSIC_TRANSITION_DURATION,
                    # which would stack ~N overlapping 1.5s transitions all chasing
                    # a stale target. The loop's own interpolation provides the
                    # overall smoothness; the audio just needs to track each step.
                    transition_duration=RAMP_TEMPO_STEP_TRANSITION,
                )
            )

        self.music_speed = target_tempo
        metrics.music_tempo.set(self.music_speed)
        self._emit_threshold_metrics()

    async def _check_music_speed(self):
        """
        Check and update music tempo (Phase 70).

        Called periodically from music loop. Handles smooth transitions
        between slow and fast tempos.
        """
        if not self.audio_client or not self.music_track_id:
            return

        # Agent tempo override (#730 PR C): while an override is active the music
        # loop adopts that tempo and suspends its own schedule. Apply once when
        # the override differs from the current speed, then hold — no scheduled
        # transitions fire (self.change_time is left untouched so the natural
        # schedule resumes from the current state when the override clears).
        if self.tempo_override is not None:
            if abs(self.music_speed - self.tempo_override) > 1e-9:
                try:
                    await self._apply_tempo_change(self.tempo_override)
                except Exception as e:
                    logger.warning(f"Failed to apply tempo override: {e}")
            return

        # #1117 ramp_tempo CURVE (#1103 MVP action 2): while a ramp descriptor is
        # active the music loop interpolates the live tempo toward the target each
        # 100ms tick and holds at target when complete — the SAME single tempo
        # owner the override uses, so there is no second tempo writer / no race.
        # This drives the schedule when ``tempo_schedule_mode == agent``: the agent
        # primitive replaces the schedule's discrete slow/fast hops with this
        # curve. ``change_time`` is left untouched so the natural slow/fast schedule
        # resumes from the held tempo once the ramp is reverted (mirrors override).
        if self.tempo_ramp is not None:
            desired = self.tempo_ramp.tempo_at(time.time())
            if abs(self.music_speed - desired) > 1e-3:
                try:
                    await self._apply_ramp_tempo(desired)
                except Exception as e:
                    logger.warning(f"Failed to apply ramp tempo: {e}")
            return

        if time.time() >= self.change_time:
            try:
                target_tempo = FAST_MUSIC_SPEED if self.speed_up else SLOW_MUSIC_SPEED
                await self._apply_tempo_change(target_tempo)
            except Exception as e:
                logger.warning(f"Failed to change music tempo: {e}")

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
                    await self._check_music_speed()
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
