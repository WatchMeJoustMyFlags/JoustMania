"""
Shield + lifecycle intervention handlers for JoustMania (#730, PR D).

Registers the real effect handlers for the three *shield / lifecycle*
intervention flags against the InterventionManager registry built in PR A:

- ``shield_seconds``   — N3: per-serial ``targeting_key`` evaluation grants a
  temporary shield (``grant_shield`` on ``BaseGameMode``) to each targeted
  player with a value > 0; serials with the neutral default (0) are skipped.
  Reverting to none expires naturally (shields simply run out — no early
  revocation).
- ``eliminate_player`` — N4: edge-triggered. The payload is the target serial;
  the player is killed through the game's existing ``_kill_player`` path with
  reason ``agent_intervention`` and accel 0.0, so spans/events/metrics fire
  identically to natural deaths.
- ``revive_player``    — N5: edge-triggered. The payload is the target serial;
  the player is re-entered through ``BaseGameMode.revive_player`` (which prefers
  a mode-native respawn path and applies brief spawn grace). Whether reviving is
  allowed at all per mode is enforced by the manager's §9 capability matrix
  BEFORE this handler runs.

Handler contract (PR A): handlers are ``async def handler(ctx) -> None``, called
only after the enforcement chain passes (they do NOT re-check policy), and the
manager records the metric + publishes the EventBus event around the call. The
handlers below mutate live game state; they are defensive (no live game / unknown
serial / already-dead-or-alive → log + no-op) because exceptions are swallowed by
the manager as a ``handler_error`` blocked outcome.

PR D does not edit ``INTERVENTION_SPECS`` — it only attaches handlers via
``register_handler`` (one line per flag), so the parallel ambience PR (E) does
not conflict structurally.
"""

import logging

from .interventions import InterventionContext, InterventionManager

logger = logging.getLogger(__name__)

# Reason recorded on the player_death span event for forced eliminations (§3 N4).
ELIMINATE_REASON = "agent_intervention"
# Eliminations are not motion deaths — record zero accel magnitude.
ELIMINATE_ACCEL = 0.0
# Neutral shield value (no shield). Matches shield_seconds none variant.
SHIELD_NONE = 0.0

# Partial-shield (#1129, #1103 Phase 2) value parsing. Value is a STRING
# "<seconds>" or "<seconds>:<boost>"; bounds mirror BaseGameMode's clamps so a
# malformed value degrades to a safe no-op rather than dispatching garbage.
PARTIAL_SHIELD_NONE_VALUES = frozenset({"", "0", "0.0", "none", "off"})
PARTIAL_SHIELD_DEFAULT_BOOST = 2.0
PARTIAL_SHIELD_BOOST_MIN = 1.0
PARTIAL_SHIELD_BOOST_MAX = 2.0
PARTIAL_SHIELD_MAX_SECONDS = 30.0


def parse_partial_shield_value(value: object) -> tuple[float, float] | None:
    """Parse a partial_shield value into ``(seconds, boost)`` or ``None``.

    Accepts ``"<seconds>"`` or ``"<seconds>:<boost>"``. Returns ``None`` for the
    neutral / empty / malformed forms (caller treats ``None`` as a safe no-op):
    non-numeric fields, seconds <= 0, or boost outside a sane band. Bounds are
    clamped (seconds capped at ``PARTIAL_SHIELD_MAX_SECONDS``, boost to
    [``PARTIAL_SHIELD_BOOST_MIN``, ``PARTIAL_SHIELD_BOOST_MAX``]) so the resolved
    pair is always safe to pass to ``grant_partial_shield``.
    """
    text = str(value).strip().lower()
    if text in PARTIAL_SHIELD_NONE_VALUES:
        return None
    parts = text.split(":")
    if len(parts) > 2:
        return None
    try:
        seconds = float(parts[0])
        boost = float(parts[1]) if len(parts) == 2 else PARTIAL_SHIELD_DEFAULT_BOOST
    except ValueError:
        return None
    if seconds <= 0:
        return None
    seconds = min(seconds, PARTIAL_SHIELD_MAX_SECONDS)
    boost = max(PARTIAL_SHIELD_BOOST_MIN, min(PARTIAL_SHIELD_BOOST_MAX, boost))
    return seconds, boost


async def handle_partial_shield(ctx: InterventionContext, manager: InterventionManager) -> None:
    """Arm per-player TIME-BOXED partial shields via per-serial targeting (#1129).

    Mirrors :func:`handle_shield_seconds`: resolves the STRING flag once per active
    serial via the manager's reusable ``resolve_player_targets`` helper, parses
    each value with :func:`parse_partial_shield_value` (malformed / neutral -> safe
    skip), and arms a time-boxed handicap boost via ``game.grant_partial_shield``.
    Unlike ``grant_shield`` (total grace_until immunity), this only raises the
    player's death threshold for the window — much harder to die, NOT immune.
    Reverting to none requires no action: the boost simply stops applying when its
    deadline passes (``_compute_effective_thresholds`` reads it live each frame).
    """
    game = ctx.game
    if game is None:
        logger.debug("partial_shield: no live game, ignoring")
        return

    values = manager.resolve_player_targets(
        flag_key=ctx.spec.flag_key,
        default="0",
        game=game,
        value_kind="string",
        battery_gate=True,
        game_id=ctx.game_id,
    )

    grant = getattr(game, "grant_partial_shield", None)
    if not callable(grant):
        logger.debug("partial_shield: game has no grant_partial_shield, ignoring")
        return

    for serial, raw in values.items():
        parsed = parse_partial_shield_value(raw)
        if parsed is None:
            continue  # neutral / malformed: safe no-op for this serial
        seconds, boost = parsed
        await grant(serial, seconds, boost)


async def handle_shield_seconds(ctx: InterventionContext, manager: InterventionManager) -> None:
    """Grant per-player shields via per-serial targeting resolution (N3).

    Uses the manager's reusable ``resolve_player_targets`` helper to evaluate the
    flag once per active serial with ``EvaluationContext(targeting_key=serial)``.
    Serials resolving to a value > 0 get a shield of that many seconds via
    ``game.grant_shield`` (extend-not-shorten, with a visible pulse). Serials at
    the neutral default (0) — or battery-gated by the helper — are skipped.
    Reverting to none requires no action: existing shields simply expire.
    """
    game = ctx.game
    if game is None:
        logger.debug("shield_seconds: no live game, ignoring")
        return

    shields = manager.resolve_player_targets(
        flag_key=ctx.spec.flag_key,
        default=SHIELD_NONE,
        game=game,
        value_kind="float",
        battery_gate=True,
        game_id=ctx.game_id,
    )

    grant = getattr(game, "grant_shield", None)
    if not callable(grant):
        logger.debug("shield_seconds: game has no grant_shield, ignoring")
        return

    for serial, seconds in shields.items():
        if seconds <= SHIELD_NONE:
            continue  # neutral default: no shield for this serial
        await grant(serial, seconds)


async def handle_eliminate_player(ctx: InterventionContext) -> None:
    """Force-eliminate the targeted player through the natural kill path (N4).

    The payload is the target serial. Routes through ``game._kill_player`` with
    reason ``agent_intervention`` and accel 0.0 so the same spans, EventBus
    events, and metrics fire as for a natural death. Defensive: no live game /
    unknown serial / already-dead → log + no-op (``_kill_player`` itself ignores
    unknown/dead serials, but we check first to log a clear reason).
    """
    game = ctx.game
    if game is None:
        logger.debug("eliminate_player: no live game, ignoring")
        return

    serial = (ctx.payload or ctx.target_serial or "").strip()
    if not serial:
        logger.warning("eliminate_player: empty target serial, ignoring")
        return

    players = getattr(game, "players", {})
    player = players.get(serial)
    if player is None:
        logger.warning(f"eliminate_player: unknown serial {serial}, ignoring")
        return
    if not getattr(player, "alive", False):
        logger.info(f"eliminate_player: {serial} already dead, no-op")
        return

    logger.info(f"eliminate_player: forcing elimination of {serial} ({ELIMINATE_REASON})")
    await game._kill_player(serial, ELIMINATE_ACCEL, reason=ELIMINATE_REASON)


async def handle_revive_player(ctx: InterventionContext) -> None:
    """Revive the targeted player through the mode's re-entry path (N5).

    The payload is the target serial. Routes through ``game.revive_player`` which
    prefers a mode-native respawn path and grants brief spawn grace. Mode
    eligibility is already enforced by the §9 capability matrix before this
    handler runs. Defensive: no live game / unknown serial / already-alive → the
    primitive logs + no-ops.
    """
    game = ctx.game
    if game is None:
        logger.debug("revive_player: no live game, ignoring")
        return

    serial = (ctx.payload or ctx.target_serial or "").strip()
    if not serial:
        logger.warning("revive_player: empty target serial, ignoring")
        return

    revive = getattr(game, "revive_player", None)
    if not callable(revive):
        logger.debug("revive_player: game has no revive_player, ignoring")
        return

    logger.info(f"revive_player: reviving {serial}")
    await revive(serial)


def register_lifecycle_handlers(manager: InterventionManager) -> None:
    """Wire the shield + lifecycle handlers onto the manager (one line per flag).

    Called from the servicer after the manager is constructed. Kept one-line-
    per-flag so the parallel ambience PR (E) appends rather than conflicts.
    """
    # shield_seconds needs the manager (for the reusable targeting helper), so
    # bind it via a small closure.
    manager.register_handler(
        "shield_seconds",
        lambda ctx: handle_shield_seconds(ctx, manager),
    )
    # partial_shield (#1129) also needs the manager for the targeting helper.
    manager.register_handler(
        "partial_shield_seconds",
        lambda ctx: handle_partial_shield(ctx, manager),
    )
    manager.register_handler("eliminate_player", handle_eliminate_player)
    manager.register_handler("revive_player", handle_revive_player)
