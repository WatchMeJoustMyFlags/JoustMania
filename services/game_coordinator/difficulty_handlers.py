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

from lib.feature_flags import read_object_flag_variant
from lib.types import Sensitivity

from .games.base import resolve_music_windows
from .interventions import InterventionContext, InterventionManager

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

# Profile names that mean "neutral / restore the game's init-resolved windows".
_NEUTRAL_PACING_PROFILES = frozenset({"none", "default", ""})


async def handle_music_tempo_override(ctx: InterventionContext) -> None:
    """Apply / clear the agent music-tempo override on the live game.

    A value > 0 sets ``game.tempo_override`` so the music loop adopts the
    override tempo and suspends its own schedule. A value of 0 (none) clears the
    override so the natural speed-up/slow-down schedule resumes from the current
    music state. The override is applied by the game's own music loop via
    ``_apply_tempo_change`` (single tempo owner — no race with the schedule).
    """
    game = ctx.game
    if game is None:
        logger.debug("music_tempo_override: no live game, ignoring")
        return

    tempo = float(ctx.value)
    if tempo > 0.0:
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

    factors = manager.resolve_player_targets(
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
        player.sensitivity_factor = clamped
        logger.info(f"player_sensitivity_factor: {serial} -> {clamped:.2f}")


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
    variant = read_object_flag_variant("game", "windows", profile, {})
    new_windows = resolve_music_windows(variant)
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
    # #766 F6: two new state-shaped levers. Both restore their baseline on revert.
    manager.register_handler("global_difficulty_factor", handle_global_difficulty_factor)
    manager._revert_handlers["global_difficulty_factor"] = handle_global_difficulty_factor_revert
    manager.register_handler("pacing_profile", handle_pacing_profile)
    manager._revert_handlers["pacing_profile"] = handle_pacing_profile_revert
