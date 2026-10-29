"""Flag-value parsers and defaults of the game modes (#2359).

One place for what ``games/base.py`` used to hold inline: every game flag's
default, its bounds and the pure validator that turns a raw flag value into a
usable one. A validator never reads a flag: ``BaseGameMode`` (and
``games/shields.py``) read through ``lib.feature_flags`` at the time each key's
contract in docs/specs/flags.md names (game init, per ramp, per grant) and pass
the raw value here. A malformed value gives the default, so a safety gate stays
closed. ``_read_volume_flag`` is the one reader that lives here (``user`` domain).

``games/base.py`` re-exports every name, so ``games.base.<name>`` imports and the
``games.base.read_*_flag`` patch points keep working.
"""

import logging
import math
from dataclasses import dataclass

from lib.types import Sensitivity

logger = logging.getLogger(__name__)

DEFAULT_RAMP_UP_SECONDS = 3.0  # The maintainer's "slow increase"
DEFAULT_RAMP_DOWN_SECONDS = 1.5  # The original JoustMania glide, until measured on a Pi
RAMP_SECONDS_MIN = 0.1  # Flag clamp, matches the registry bounds (flags.md §13.3)
RAMP_SECONDS_MAX = 10.0


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


def resolve_hold_value(value: object, default: float, low: float, high: float) -> float:
    """Validate a hold-mode flag value (flags.md §4): malformed gives ``default``, else clamped to [low, high]."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    number = float(value)
    if not math.isfinite(number):
        return default
    return max(low, min(high, number))


# Hold feedback cue flags (flags.md §4, UC-040 BR-026): key -> (default, min, max), the
# registry bounds. Latched with the hold values and shipped to the controller manager.
SHIELD_HOLD_CUE_FLAGS: dict[str, tuple[float, float, float]] = {
    "shield.charge_rumble_intensity": (255.0, 0.0, 255.0),
    "shield.charge_rumble_pulse_ms": (150.0, 50.0, 500.0),
    "shield.ending_rumble_seconds": (1.0, 0.0, 3.0),
    "shield.hold_rumble_intensity": (70.0, 0.0, 150.0),
    "shield.empty_pulse_ms": (400.0, 100.0, 800.0),
}


# Agent shield limits per player per game (#2044, UC-040 A34, BR-022): the
# coordinator's backstop. Operator flags in the `agent` set, read per grant.
DEFAULT_MAX_SHIELDS_PER_PLAYER_PER_GAME = 3
DEFAULT_SHIELD_COOLDOWN_SECONDS = 15


def resolve_shield_limit(value: object, default: int) -> int:
    """Validate a shield-limit flag value (flags.md §3): a negative or non-int value gives the default."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


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


# UC-030 BR-001: threshold tables (defaults of the `thresholds` flag, read at game start).
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
