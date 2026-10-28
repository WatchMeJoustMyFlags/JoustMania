"""
Difficulty intervention handlers for JoustMania (#730, PR C).

Registers the real effect handlers for the three *difficulty* state-shaped
intervention flags against the InterventionManager registry built in PR A:

- ``music_tempo_override``        — N6: the music loop adopts the override tempo
  and suspends its own speed-up/slow-down schedule (resolves the E1 tempo race
  in docs/research/722-intervention-surface.md §3.1); reverting to none (0)
  resumes the natural schedule from the current state via an explicit revert
  handler (#922 — the manager routes reverts to ``_revert_handlers``, not back to
  the apply-handler, so the override must be cleared there).
- ``global_sensitivity_override`` — N2: a bounded, temporary override LAYER on
  top of the admin baseline (never persisted into the baseline — #816 / M9
  ownership model), consumed next frame by ``_compute_effective_thresholds``;
  reverting to none (-1) clears the layer so the effective sensitivity returns
  to the admin's CURRENT baseline (a mid-game admin change wins by invalidating
  the override).
- ``player_sensitivity_factor``   — N1: per-serial ``targeting_key`` evaluation
  sets each active player's ``sensitivity_factor`` (clamped 0.5-2.0); serials
  with no targeting match get the neutral default (1.0).
- ``player_handicap_factor``      — #1107 (#1103 MVP action 1): per-serial
  ``targeting_key`` evaluation sets each active player's ``handicap_factor``
  (clamped 0.5-2.0), a SEPARATE multiplicative knob that COMPOSES with
  ``sensitivity_factor`` (>1 harder to die / "help", <1 easier / "rein in").
  SHADOW-game-only initially via the ``shadow_experimental`` interventions_allowed
  variant; serials with no targeting match get the neutral default (1.0).

Handler contract (PR A): handlers are ``async def handler(ctx) -> None``, called
only after the enforcement chain passes (they do NOT re-check policy), and the
manager records the metric + publishes the EventBus event around the call. The
handlers below mutate live game state; they are defensive (no live game / no
audio client → no-op) because exceptions are swallowed by the manager as a
``handler_error`` blocked outcome.

PR C does not edit ``INTERVENTION_SPECS`` — it only attaches handlers via
``register_handler`` (one line per flag), so the parallel ambience PR (E) does
not conflict structurally.
"""

import logging
from dataclasses import replace

from lib.feature_flags import GAME_KIND_REAL, GAME_KIND_VAR, read_object_flag_variant
from lib.types import Sensitivity
from services.game_coordinator import game_clock

from .games.base import (
    FAST_MUSIC_SPEED,
    SLOW_MUSIC_SPEED,
    parse_ramp_tempo_value,
    player_identity_attributes,
    resolve_music_windows,
)
from .intervention_errors import InterventionNotApplied
from .interventions import InterventionContext, InterventionManager

# UC-043 BR-005 (#1970): the game's experiment sets other windows than the preset.
BLOCK_EXPERIMENT_OVERRIDE = "experiment_override"

logger = logging.getLogger(__name__)

# Per-player sensitivity factor clamp (mirrors base.py _compute_effective_thresholds).
SENSITIVITY_FACTOR_MIN = 0.5
SENSITIVITY_FACTOR_MAX = 2.0
SENSITIVITY_FACTOR_DEFAULT = 1.0

# Global difficulty factor clamp + neutral (#766 F6). Mirrors the combined-factor
# clamp in base.py _compute_effective_thresholds.
GLOBAL_DIFFICULTY_FACTOR_MIN = 0.5
GLOBAL_DIFFICULTY_FACTOR_MAX = 2.0
GLOBAL_DIFFICULTY_FACTOR_DEFAULT = 1.0

# Per-player handicap factor clamp + neutral (#1107). Mirrors the independent
# handicap clamp in base.py _compute_effective_thresholds. A SEPARATE knob that
# composes multiplicatively with sensitivity_factor (>1 harder to die, <1 easier).
HANDICAP_FACTOR_MIN = 0.5
HANDICAP_FACTOR_MAX = 2.0
HANDICAP_FACTOR_DEFAULT = 1.0

# Profile names that mean "neutral / restore the game's init-resolved windows".
_NEUTRAL_PACING_PROFILES = frozenset({"none", "default", ""})


async def handle_music_tempo_override(ctx: InterventionContext) -> None:
    """Apply / clear the agent music-tempo override on the live game.

    A value > 0 sets ``game.tempo_override`` so the music loop adopts the
    override tempo and suspends its own schedule. A value of 0 (none) clears the
    override so the natural speed-up/slow-down schedule resumes from the current
    music state. The value is clamped to [1.0, 1.3]. The override is applied by the
    game's own music loop, which ramps the intensity to it at the bounded rate
    (``_start_intensity_ramp``, single tempo owner, #1833).
    """
    game = ctx.game
    if game is None:
        logger.debug("music_tempo_override: no live game, ignoring")
        return

    tempo = float(ctx.value)
    if tempo > 0.0:
        # Clamp to the music range the intensity maps onto (UC-031 BR-005, #1833).
        tempo = max(SLOW_MUSIC_SPEED, min(FAST_MUSIC_SPEED, tempo))
        game.tempo_override = tempo
        logger.info(f"music_tempo_override: override active at {tempo:.2f}x (schedule suspended)")
    else:
        game.tempo_override = None
        logger.info("music_tempo_override: override cleared, natural schedule resumes")


async def handle_music_tempo_override_revert(ctx: InterventionContext) -> None:
    """Clear the music-tempo override when the flag reverts to neutral (0).

    The apply-handler already clears ``game.tempo_override`` for a 0 value, but
    the manager routes none-value transitions to ``_revert_handlers`` rather than
    re-invoking the apply-handler, so without this entry a revert would leave
    ``game.tempo_override`` pinned at the last override and the music loop stuck
    at that tempo until the game restarts. This restores ``None`` so
    ``_check_music_speed`` resumes the natural speed-up/slow-down schedule on its
    next tick.
    """
    game = ctx.game
    if game is None:
        logger.debug("music_tempo_override: no live game on revert, ignoring")
        return
    game.tempo_override = None
    logger.info("music_tempo_override: reverted, natural schedule resumes")


async def handle_global_sensitivity_override(ctx: InterventionContext) -> None:
    """Apply / clear the agent global-sensitivity override on the live game.

    The override is a bounded, temporary LAYER on top of the admin baseline; it
    is never persisted into the baseline (the M9 ownership model — #816). A value
    >= 0 sets the override to the matching ``Sensitivity`` index, picked up next
    frame by ``_compute_effective_thresholds``. A value of -1 (none) CLEARS the
    override so the effective sensitivity returns to the admin's CURRENT baseline
    (``game.baseline_sensitivity``) — not a stale game-start snapshot. If the
    admin changed the baseline while the override was active, the override was
    already invalidated, so this clear is a safe no-op against the new baseline.
    """
    game = ctx.game
    if game is None:
        logger.debug("global_sensitivity_override: no live game, ignoring")
        return

    idx = int(ctx.value)
    if idx < 0:
        game.clear_sensitivity_override()
        return

    try:
        sensitivity = Sensitivity(idx)
    except ValueError:
        logger.warning(f"global_sensitivity_override: invalid sensitivity index {idx}, ignoring")
        return
    game.apply_sensitivity_override(sensitivity)


def _record_factor_event(player: object, serial: str, event: str, key: str, value: float) -> None:
    """Record a changed per-player factor on the player's span (#1295).

    Only a CHANGED value is recorded (the handler re-applies the whole per-serial
    map on every flag change), with ``player.serial`` / ``player.name`` so "who got
    it" is visible on the span where it happened. No span (headless / closed) = no
    event.
    """
    span = getattr(player, "span", None)
    if span is None:
        return
    span.add_event(event, attributes={key: value, **player_identity_attributes(serial, getattr(player, "name", ""))})


def _changed_levels(ctx: InterventionContext) -> dict | None:
    """The ``{serial: level}`` map the manager dispatches for a per-player level
    (only the players whose level changed in this game, UC-040 BR-017), or None
    when the handler is called with a scalar value and must resolve every
    player itself."""
    return ctx.value if isinstance(ctx.value, dict) else None


async def handle_player_sensitivity_factor(ctx: InterventionContext, manager: InterventionManager) -> None:
    """Apply per-player sensitivity factors via per-serial targeting resolution.

    Uses the manager's reusable ``resolve_player_targets`` helper to evaluate the
    flag once per active serial with ``EvaluationContext(targeting_key=serial)``.
    Each resolved factor is clamped to [0.5, 2.0] and written to the player's
    ``sensitivity_factor`` (consumed next frame by ``_compute_effective_thresholds``).
    Players without a targeting match get the neutral default (1.0); battery-gated
    serials are skipped by the helper.
    """
    game = ctx.game
    if game is None:
        logger.debug("player_sensitivity_factor: no live game, ignoring")
        return

    factors = _changed_levels(ctx) or manager.resolve_player_targets(
        flag_key=ctx.spec.flag_key,
        default=SENSITIVITY_FACTOR_DEFAULT,
        game=game,
        value_kind="float",
        battery_gate=True,
        game_id=ctx.game_id,
    )

    players = getattr(game, "players", {})
    for serial, factor in factors.items():
        player = players.get(serial)
        if player is None:
            continue
        clamped = max(SENSITIVITY_FACTOR_MIN, min(SENSITIVITY_FACTOR_MAX, factor))
        previous = player.sensitivity_factor
        player.sensitivity_factor = clamped
        logger.info(f"player_sensitivity_factor: {serial} -> {clamped:.2f}")
        if clamped != previous:
            _record_factor_event(player, serial, "sensitivity_factor_applied", "sensitivity_factor", clamped)


async def handle_player_handicap_factor(ctx: InterventionContext, manager: InterventionManager) -> None:
    """Apply per-player MULTIPLICATIVE handicap factors (#1107, #1103 MVP action 1).

    Mirrors :func:`handle_player_sensitivity_factor`: resolves the flag once per
    active serial via the manager's reusable ``resolve_player_targets`` helper
    (per-serial ``targeting_key`` + per-game ``gameId`` composition), clamps each
    resolved value to [0.5, 2.0], and writes it to the player's ``handicap_factor``
    (consumed next frame by ``_compute_effective_thresholds`` where it MULTIPLIES
    the threshold, composing with — not replacing — ``sensitivity_factor``).

    Intent-framed and OPPOSITE in direction to sensitivity: >1.0 raises the
    threshold (harder to die / "help"); <1.0 lowers it (easier / "rein in").
    Serials with no targeting match resolve to the neutral default (1.0), so a
    partial revert restores the reverted players to neutral on the next change.
    Battery-gated serials are skipped by the helper. SHADOW-game-only initially:
    rejected for real games by the interventions_allowed allow-list (the
    load-bearing chain gate), so this handler only ever runs in shadow games.
    """
    game = ctx.game
    if game is None:
        logger.debug("player_handicap_factor: no live game, ignoring")
        return

    factors = _changed_levels(ctx) or manager.resolve_player_targets(
        flag_key=ctx.spec.flag_key,
        default=HANDICAP_FACTOR_DEFAULT,
        game=game,
        value_kind="float",
        battery_gate=True,
        game_id=ctx.game_id,
    )

    players = getattr(game, "players", {})
    for serial, factor in factors.items():
        player = players.get(serial)
        if player is None:
            continue
        clamped = max(HANDICAP_FACTOR_MIN, min(HANDICAP_FACTOR_MAX, factor))
        previous = player.handicap_factor
        player.handicap_factor = clamped
        logger.info(f"player_handicap_factor: {serial} -> {clamped:.2f}")
        if clamped != previous:
            _record_factor_event(player, serial, "handicap_factor_applied", "handicap_factor", clamped)


def _reset_player_factor(ctx: InterventionContext, attr: str, neutral: float, event: str) -> None:
    """Set ``attr`` back to ``neutral`` for the players in ``ctx.value`` (the
    ``{serial: level}`` map of changed players), or for every player of the game
    when no map is given; a changed player gets the same span event as an
    applied factor (#1295)."""
    game = ctx.game
    if game is None:
        return
    players = getattr(game, "players", {})
    serials = list(ctx.value) if isinstance(ctx.value, dict) else list(players)
    for serial in serials:
        player = players.get(serial)
        if player is None:
            continue
        previous = getattr(player, attr, neutral)
        setattr(player, attr, neutral)
        if previous != neutral:
            logger.info(f"{attr}: {serial} reset to {neutral:.2f}")
            _record_factor_event(player, serial, event, attr, neutral)


async def handle_player_sensitivity_factor_revert(ctx: InterventionContext) -> None:
    """Every player's sensitivity factor is neutral again: reset them all to 1.0
    (UC-040 A24 / BR-017, #1317). Before this, the targeted path returned on an
    empty change key and the last non-neutral factor stayed for the whole game."""
    _reset_player_factor(ctx, "sensitivity_factor", SENSITIVITY_FACTOR_DEFAULT, "sensitivity_factor_applied")


async def handle_player_handicap_factor_revert(ctx: InterventionContext) -> None:
    """Every player's handicap factor is neutral again: reset them all to 1.0
    (UC-040 A24 / BR-017, #1317)."""
    _reset_player_factor(ctx, "handicap_factor", HANDICAP_FACTOR_DEFAULT, "handicap_factor_applied")


async def handle_global_difficulty_factor(ctx: InterventionContext) -> None:
    """Apply / clear the live global difficulty factor on the running game (#766 F6).

    State-shaped: ``ctx.value`` is the float factor (range 0.5-2.0). It is stored
    on ``game.global_difficulty_factor`` and live-read every frame by
    ``_compute_effective_thresholds``, where it is multiplied with each player's
    own ``sensitivity_factor`` before the combined factor is clamped and divides
    the thresholds. A value of 1.0 (the none-value) never reaches here (the
    manager skips reverts-to-neutral) — the neutral revert restores 1.0 via
    ``handle_global_difficulty_factor_revert``. Values are clamped to [0.5, 2.0].
    """
    game = ctx.game
    if game is None:
        logger.debug("global_difficulty_factor: no live game, ignoring")
        return

    factor = float(ctx.value)
    clamped = max(GLOBAL_DIFFICULTY_FACTOR_MIN, min(GLOBAL_DIFFICULTY_FACTOR_MAX, factor))
    game.global_difficulty_factor = clamped
    logger.info(f"global_difficulty_factor: live factor -> {clamped:.2f}")


async def handle_global_difficulty_factor_revert(ctx: InterventionContext) -> None:
    """Restore the neutral global difficulty factor (1.0) on revert (#766 F6)."""
    game = ctx.game
    if game is None:
        logger.debug("global_difficulty_factor: no live game on revert, ignoring")
        return
    game.global_difficulty_factor = GLOBAL_DIFFICULTY_FACTOR_DEFAULT
    logger.info("global_difficulty_factor: reverted to neutral 1.0")


async def handle_pacing_profile(ctx: InterventionContext) -> None:
    """Swap the live music schedule windows to a named preset (#766 F6).

    State-shaped: ``ctx.value`` is the profile name (``calm`` / ``frantic`` /
    ``default``). The named preset is resolved from the ``game.windows`` flag's
    variants (reusing F3's ``resolve_music_windows`` over the targeting-selected
    variant) and assigned ATOMICALLY to ``game.music_windows`` (single store,
    never mutated in place) so the music loop picks it up on its next tempo
    change without tearing. The neutral profile (``none``/``default``) never
    reaches here; reverting restores the init-resolved windows via
    ``handle_pacing_profile_revert``. An unknown/malformed preset falls back to
    the game's init windows (``resolve_music_windows`` validates; the flag's
    targeting ``else`` resolves unknown names to ``default``).

    The read runs in the game's flag context (UC-040 BR-018, #1970), so in an
    experimental-arm game the experiment's ``windows`` rung wins over the preset.
    When that value differs from the preset's (a second read with
    ``game_kind=real``, which no experiment rung matches), the experiment
    overrides the lever: the windows stay as they are and the handler raises
    ``InterventionNotApplied("experiment_override")`` (UC-043 BR-005). Equal
    values are applied.
    """
    game = ctx.game
    if game is None:
        logger.debug("pacing_profile: no live game, ignoring")
        return

    profile = str(ctx.value).strip().lower()
    if profile in _NEUTRAL_PACING_PROFILES:
        _restore_init_windows(game)
        return

    init_windows = getattr(game, "init_music_windows", game.music_windows)
    # A read that fails returns the supplied default object itself, so identity tells a
    # flag error from a real (possibly empty) value.
    read_failed: dict = {}
    variant = read_object_flag_variant("game", "windows", profile, read_failed)
    new_windows = resolve_music_windows(variant)
    if variant is not read_failed:
        _refuse_experiment_override(game, profile, new_windows)
    # resolve_music_windows falls back to module defaults (not the game's init
    # windows) on malformed values; an unknown profile resolves to the flag's
    # "default" variant. Either way the windows are well-formed; assign atomically.
    game.music_windows = new_windows
    logger.info(f"pacing_profile: swapped music windows to preset '{profile}'")
    if new_windows == resolve_music_windows({}) and init_windows is not None:
        # Unknown/malformed preset resolved to bare module defaults; if the game
        # was started with custom windows, prefer its init windows over defaults.
        game.music_windows = init_windows
        logger.info("pacing_profile: unknown preset, restored init windows")


def _refuse_experiment_override(game: object, profile: str, resolved: object) -> None:
    """Raise ``experiment_override`` when the game's experiment changed the preset's windows (#1970).

    Only a game bound to an experiment can resolve an experiment rung, so any
    other game never pays the second read. A failed baseline read is a flag error, not
    an override: it is logged and the first read's windows apply, as before #1970.
    """
    experiment_id = getattr(game, "experiment_id", "")
    if not (isinstance(experiment_id, str) and experiment_id):
        return
    read_failed: dict = {}
    baseline_variant = read_object_flag_variant(
        "game", "windows", profile, read_failed, attributes={GAME_KIND_VAR: GAME_KIND_REAL}
    )
    if baseline_variant is read_failed:
        logger.warning(f"pacing_profile: baseline windows read failed for '{profile}'; not treated as an override")
        return
    baseline = resolve_music_windows(baseline_variant)
    if resolved == baseline:
        return
    arm = getattr(game, "arm", "")
    logger.info(
        f"pacing_profile: experiment {experiment_id} (arm {arm}) sets other windows than preset "
        f"'{profile}'; the experiment's windows stay (experiment_override)"
    )
    raise InterventionNotApplied(
        BLOCK_EXPERIMENT_OVERRIDE, f"experiment {experiment_id} overrides pacing_profile '{profile}'"
    )


async def handle_pacing_profile_revert(ctx: InterventionContext) -> None:
    """Restore the game's init-resolved music windows on revert (#766 F6)."""
    game = ctx.game
    if game is None:
        logger.debug("pacing_profile: no live game on revert, ignoring")
        return
    _restore_init_windows(game)


def _restore_init_windows(game: object) -> None:
    """Atomically restore the game's init-resolved music windows."""
    init_windows = getattr(game, "init_music_windows", None)
    if init_windows is None:
        logger.debug("pacing_profile: no init windows recorded, leaving current windows")
        return
    game.music_windows = init_windows
    logger.info("pacing_profile: restored init-resolved music windows")


async def handle_ramp_tempo(ctx: InterventionContext) -> None:
    """Arm a scheduled tempo CURVE on the live game (#1117, #1103 MVP action 2).

    State-shaped STRING: ``ctx.value`` is ``"<target>:<seconds>:<curve>"`` (e.g.
    ``"1.3:8:linear"``). The value is parsed + bounds-validated by
    :func:`parse_ramp_tempo_value` (target clamped to the valid tempo range,
    seconds > 0 and capped, curve in {linear, ease}); a MALFORMED value yields
    ``None`` and is a safe no-op (the descriptor is NOT changed, so no garbage is
    dispatched). On a valid value the descriptor's interpolation origin is anchored
    to the live music_speed + now and stored on ``game.tempo_ramp``; the game's
    100ms ``_check_music_speed`` loop then interpolates toward the target via the
    SINGLE tempo-owner seam (no second writer). When ``tempo_schedule_mode==agent``
    this curve drives the schedule. The neutral value ("none") never reaches here;
    reverting drops the descriptor via :func:`handle_ramp_tempo_revert`.
    """
    game = ctx.game
    if game is None:
        logger.debug("ramp_tempo: no live game, ignoring")
        return

    descriptor = parse_ramp_tempo_value(ctx.value)
    if descriptor is None:
        logger.warning(f"ramp_tempo: malformed value {ctx.value!r}, ignoring (no-op)")
        return

    # Anchor the curve's origin to the live tempo + clock so it ramps from where
    # the music actually is right now.
    armed = replace(descriptor, start_tempo=game.music_speed, start_time=game_clock.now())
    game.tempo_ramp = armed
    logger.info(
        f"ramp_tempo: armed {armed.start_tempo:.2f}x -> {armed.target_tempo:.2f}x "
        f"over {armed.duration:.1f}s ({armed.curve})"
    )


async def handle_ramp_tempo_revert(ctx: InterventionContext) -> None:
    """Drop the ramp descriptor on revert so the default rule resumes (#1117).

    The manager routes a state flag's revert-to-neutral ("none") to
    ``_revert_handlers`` rather than re-invoking the apply handler, so without this
    entry ``game.tempo_ramp`` would stay pinned at the last curve and the music
    loop would hold at its target forever. Clearing it lets ``_check_music_speed``
    resume the natural slow/fast schedule from the held tempo on its next tick.
    """
    game = ctx.game
    if game is None:
        logger.debug("ramp_tempo: no live game on revert, ignoring")
        return
    game.tempo_ramp = None
    logger.info("ramp_tempo: reverted, natural schedule resumes from held tempo")


def register_difficulty_handlers(manager: InterventionManager) -> None:
    """Wire the difficulty handlers onto the manager (one line per flag).

    Called from the servicer after the manager is constructed. Kept one-line-
    per-flag so the parallel ambience PR (E) appends rather than conflicts.
    """
    manager.register_handler("music_tempo_override", handle_music_tempo_override)
    # music_tempo_override needs an explicit revert handler (#922): the manager
    # routes a flag's revert-to-neutral (value 0) to _revert_handlers, NOT back
    # to the apply-handler, so the apply-handler's "value 0 -> clear" branch never
    # runs on revert. Without this entry game.tempo_override stays pinned at the
    # last override and the music loop (_check_music_speed re-reads it every tick)
    # is stuck at that tempo until the game restarts.
    manager._revert_handlers["music_tempo_override"] = handle_music_tempo_override_revert
    manager.register_handler("global_sensitivity_override", handle_global_sensitivity_override)
    # player_sensitivity_factor needs the manager (for the reusable targeting
    # helper), so bind it via a small closure.
    manager.register_handler(
        "player_sensitivity_factor",
        lambda ctx: handle_player_sensitivity_factor(ctx, manager),
    )
    # player_handicap_factor (#1107) mirrors player_sensitivity_factor exactly.
    # A PARTIAL revert of either level is handled inside the apply handler
    # (unmatched serials resolve to the neutral 1.0 default). The transition to
    # every player neutral dispatches nothing through the apply handler, so the
    # targeted-state path runs these revert handlers instead (UC-040 A24, #1317).
    manager.register_handler(
        "player_handicap_factor",
        lambda ctx: handle_player_handicap_factor(ctx, manager),
    )
    manager._revert_handlers["player_sensitivity_factor"] = handle_player_sensitivity_factor_revert
    manager._revert_handlers["player_handicap_factor"] = handle_player_handicap_factor_revert
    # #766 F6: two new state-shaped levers. Both restore their baseline on revert.
    manager.register_handler("global_difficulty_factor", handle_global_difficulty_factor)
    manager._revert_handlers["global_difficulty_factor"] = handle_global_difficulty_factor_revert
    manager.register_handler("pacing_profile", handle_pacing_profile)
    manager._revert_handlers["pacing_profile"] = handle_pacing_profile_revert
    # #1117 ramp_tempo: session-scoped string state flag (scheduled tempo curve).
    # Like music_tempo_override it needs an explicit revert handler (#922): the
    # manager routes the revert-to-neutral ("none") to _revert_handlers, NOT the
    # apply handler, so the descriptor must be dropped there or it stays pinned.
    manager.register_handler("tempo_ramp", handle_ramp_tempo)
    manager._revert_handlers["tempo_ramp"] = handle_ramp_tempo_revert
