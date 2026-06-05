"""
Difficulty intervention handlers for JoustMania (#730, PR C).

Registers the real effect handlers for the three *difficulty* state-shaped
intervention flags against the InterventionManager registry built in PR A:

- ``music_tempo_override``        — N6: the music loop adopts the override tempo
  and suspends its own speed-up/slow-down schedule (resolves the E1 tempo race
  in docs/research/722-intervention-surface.md §3.1); reverting to none (0)
  resumes the natural schedule from the current state.
- ``global_sensitivity_override`` — N2: live update of ``game.sensitivity``,
  consumed next frame by ``_compute_effective_thresholds``; reverting to none
  (-1) restores the sensitivity the game started with.
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

from lib.types import Sensitivity

from .interventions import InterventionContext, InterventionManager

logger = logging.getLogger(__name__)

# Per-player sensitivity factor clamp (mirrors base.py _compute_effective_thresholds).
SENSITIVITY_FACTOR_MIN = 0.5
SENSITIVITY_FACTOR_MAX = 2.0
SENSITIVITY_FACTOR_DEFAULT = 1.0


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


async def handle_global_sensitivity_override(ctx: InterventionContext) -> None:
    """Apply / clear the agent global-sensitivity override on the live game.

    A value >= 0 sets ``game.sensitivity`` to the matching ``Sensitivity`` index;
    the change is picked up next frame by ``_compute_effective_thresholds``. A
    value of -1 (none) restores the sensitivity the game was configured with at
    start (``game.configured_sensitivity``).
    """
    game = ctx.game
    if game is None:
        logger.debug("global_sensitivity_override: no live game, ignoring")
        return

    idx = int(ctx.value)
    if idx < 0:
        restored = getattr(game, "configured_sensitivity", game.sensitivity)
        game.sensitivity = restored
        logger.info(f"global_sensitivity_override: cleared, restored to {restored.name}")
        return

    try:
        game.sensitivity = Sensitivity(idx)
    except ValueError:
        logger.warning(f"global_sensitivity_override: invalid sensitivity index {idx}, ignoring")
        return
    logger.info(f"global_sensitivity_override: live sensitivity -> {game.sensitivity.name}")


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
    )

    players = getattr(game, "players", {})
    for serial, factor in factors.items():
        player = players.get(serial)
        if player is None:
            continue
        clamped = max(SENSITIVITY_FACTOR_MIN, min(SENSITIVITY_FACTOR_MAX, factor))
        player.sensitivity_factor = clamped
        logger.info(f"player_sensitivity_factor: {serial} -> {clamped:.2f}")


def register_difficulty_handlers(manager: InterventionManager) -> None:
    """Wire the difficulty handlers onto the manager (one line per flag).

    Called from the servicer after the manager is constructed. Kept one-line-
    per-flag so the parallel ambience PR (E) appends rather than conflicts.
    """
    manager.register_handler("music_tempo_override", handle_music_tempo_override)
    manager.register_handler("global_sensitivity_override", handle_global_sensitivity_override)
    # player_sensitivity_factor needs the manager (for the reusable targeting
    # helper), so bind it via a small closure.
    manager.register_handler(
        "player_sensitivity_factor",
        lambda ctx: handle_player_sensitivity_factor(ctx, manager),
    )
