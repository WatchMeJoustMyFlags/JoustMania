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

from .intervention_errors import InterventionNotApplied
from .interventions import (
    BLOCK_ELIMINATE_REFUSED,
    BLOCK_REVIVE_REFUSED,
    InterventionContext,
    InterventionManager,
)

logger = logging.getLogger(__name__)

# Reason recorded on the player_death span event for forced eliminations (§3 N4).
ELIMINATE_REASON = "agent_intervention"
# Eliminations are not motion deaths — record zero accel magnitude.
ELIMINATE_ACCEL = 0.0
# Neutral shield value (no shield). Matches shield_seconds none variant.
SHIELD_NONE = 0.0

# Toughness boost value parsing. Value is a STRING
# "<seconds>" or "<seconds>:<boost>"; bounds mirror BaseGameMode's clamps so a
# malformed value degrades to a safe no-op rather than dispatching garbage.
TOUGHNESS_BOOST_NONE_VALUES = frozenset({"", "0", "0.0", "none", "off"})
TOUGHNESS_BOOST_DEFAULT_FACTOR = 2.0
TOUGHNESS_BOOST_FACTOR_MIN = 1.0
TOUGHNESS_BOOST_FACTOR_MAX = 2.0
TOUGHNESS_BOOST_MAX_SECONDS = 30.0


def _grant_values(ctx: InterventionContext, manager: InterventionManager, default: object, value_kind: str) -> dict:
    """The per-serial values a grant handler applies (UC-040 BR-012, #1313).

    The manager dispatches a grant flag once per serial whose grant changed,
    with ``ctx.target_serial`` / ``ctx.value`` set: apply exactly that grant and
    nobody else's. Only a direct call without a target (tests, tools) falls
    back to resolving every active serial.
    """
    if ctx.target_serial:
        return {ctx.target_serial: ctx.value}
    return manager.resolve_player_targets(
        flag_key=ctx.spec.flag_key,
        default=default,
        game=ctx.game,
        value_kind=value_kind,
        battery_gate=True,
        game_id=ctx.game_id,
    )


def parse_toughness_boost_value(value: object) -> tuple[float, float] | None:
    """Parse a toughness_boost value into ``(seconds, boost)`` or ``None``.

    Accepts ``"<seconds>"`` or ``"<seconds>:<boost>"``. Returns ``None`` for the
    neutral / empty / malformed forms (caller treats ``None`` as a safe no-op):
    non-numeric fields, seconds <= 0, or boost outside a sane band. Bounds are
    clamped (seconds capped at ``TOUGHNESS_BOOST_MAX_SECONDS``, boost to
    [``TOUGHNESS_BOOST_FACTOR_MIN``, ``TOUGHNESS_BOOST_FACTOR_MAX``]) so the resolved
    pair is always safe to pass to ``grant_toughness_boost``.
    """
    text = str(value).strip().lower()
    if text in TOUGHNESS_BOOST_NONE_VALUES:
        return None
    parts = text.split(":")
    if len(parts) > 2:
        return None
    try:
        seconds = float(parts[0])
        boost = float(parts[1]) if len(parts) == 2 else TOUGHNESS_BOOST_DEFAULT_FACTOR
    except ValueError:
        return None
    if seconds <= 0:
        return None
    seconds = min(seconds, TOUGHNESS_BOOST_MAX_SECONDS)
    boost = max(TOUGHNESS_BOOST_FACTOR_MIN, min(TOUGHNESS_BOOST_FACTOR_MAX, boost))
    return seconds, boost


async def handle_toughness_boost(ctx: InterventionContext, manager: InterventionManager) -> None:
    """Arm per-player TIME-BOXED toughness boosts via per-serial targeting (#1129).

    Mirrors :func:`handle_shield_seconds`: resolves the STRING flag once per active
    serial via the manager's reusable ``resolve_player_targets`` helper, parses
    each value with :func:`parse_toughness_boost_value` (malformed / neutral -> safe
    skip), and arms a time-boxed handicap boost via ``game.grant_toughness_boost``.
    Unlike ``grant_shield`` (total grace_until immunity), this only raises the
    player's death threshold for the window — much harder to die, NOT immune.
    Reverting to none requires no action: the boost simply stops applying when its
    deadline passes (``_compute_effective_thresholds`` reads it live each frame).
    """
    game = ctx.game
    if game is None:
        logger.debug("toughness_boost: no live game, ignoring")
        return

    values = _grant_values(ctx, manager, "0", "string")

    for serial, raw in values.items():
        parsed = parse_toughness_boost_value(raw)
        if parsed is None:
            continue  # neutral / malformed: safe no-op for this serial
        seconds, boost = parsed
        await game.grant_toughness_boost(serial, seconds, boost)


# Soft-penalty value parsing. Value is a STRING:
#   "warn" (default)               -> visible warning feedback, no threshold change
#   "tighten"                      -> default expiring handicap reduction
#   "tighten:<seconds>:<factor>"   -> explicit tighten window/strength
# Bounds mirror BaseGameMode's clamps so a malformed value degrades to a safe
# default ("warn") rather than dispatching garbage.
SOFT_PENALTY_NONE_VALUES = frozenset({"", "none", "off", "0"})
SOFT_PENALTY_DEFAULT_SECONDS = 5.0
SOFT_PENALTY_DEFAULT_FACTOR = 0.6
SOFT_PENALTY_FACTOR_MIN = 0.5
SOFT_PENALTY_FACTOR_MAX = 1.0
SOFT_PENALTY_MAX_SECONDS = 30.0


def parse_soft_penalty_value(value: object) -> tuple[str, float, float] | None:
    """Parse a soft_penalty value into ``(action, seconds, factor)`` or ``None``.

    Accepts ``"warn"``, ``"tighten"`` or ``"tighten:<seconds>:<factor>"``.
    Returns ``None`` only for the neutral / empty forms (caller treats ``None`` as
    a safe no-op). ANY malformed/unknown value degrades to the safe default
    ``("warn", 0.0, 1.0)`` — never garbage:

    - ``"warn"`` -> ``("warn", 0.0, 1.0)`` (seconds/factor unused by warn).
    - ``"tighten"`` -> default seconds/factor.
    - ``"tighten:<seconds>:<factor>"`` -> parsed + clamped (seconds capped at
      ``SOFT_PENALTY_MAX_SECONDS``, factor to [``SOFT_PENALTY_FACTOR_MIN``,
      ``SOFT_PENALTY_FACTOR_MAX``]); non-numeric / non-positive seconds -> the
      default tighten window rather than a garbage dispatch.
    """
    text = str(value).strip().lower()
    if text in SOFT_PENALTY_NONE_VALUES:
        return None

    parts = text.split(":")
    head = parts[0]

    if head == "tighten":
        seconds = SOFT_PENALTY_DEFAULT_SECONDS
        factor = SOFT_PENALTY_DEFAULT_FACTOR
        if len(parts) == 3:
            try:
                parsed_seconds = float(parts[1])
                parsed_factor = float(parts[2])
            except ValueError:
                parsed_seconds = parsed_factor = None
            if parsed_seconds is not None and parsed_seconds > 0:
                seconds = min(parsed_seconds, SOFT_PENALTY_MAX_SECONDS)
            if parsed_factor is not None:
                factor = max(SOFT_PENALTY_FACTOR_MIN, min(SOFT_PENALTY_FACTOR_MAX, parsed_factor))
        # Any other shape ("tighten:5", "tighten:a:b:c") keeps safe defaults.
        return "tighten", seconds, factor

    # "warn" and every unknown/malformed value degrade to the safe default warn.
    return "warn", 0.0, 1.0


async def handle_soft_penalty(ctx: InterventionContext, manager: InterventionManager) -> None:
    """Apply per-player graduated soft penalties via per-serial targeting (#1134).

    A RECOVERABLE alternative to the permanent ``eliminate_player``. Resolves the
    STRING flag once per active serial via the manager's reusable
    ``resolve_player_targets`` helper, parses each value with
    :func:`parse_soft_penalty_value` (neutral -> skip; malformed -> safe default
    ``warn``), and dispatches:

    - ``warn`` -> ``game.warn_player`` (visible flash + rumble cue, NO threshold
      change — fully recoverable).
    - ``tighten`` -> ``game.apply_soft_penalty`` (short EXPIRING handicap
      reduction; the MIRROR of toughness_boost — temporarily easier to die, still
      clamped, never instant-death).

    Reverting requires no action: warn leaves no state, and a tighten simply stops
    applying when its deadline passes (``_compute_effective_thresholds`` reads it
    live each frame).
    """
    game = ctx.game
    if game is None:
        logger.debug("soft_penalty: no live game, ignoring")
        return

    values = _grant_values(ctx, manager, "none", "string")

    for serial, raw in values.items():
        parsed = parse_soft_penalty_value(raw)
        if parsed is None:
            continue  # neutral: no penalty for this serial
        action, seconds, factor = parsed
        if action == "tighten":
            await game.apply_soft_penalty(serial, seconds, factor)
        else:
            await game.warn_player(serial)


# Auto-rubberband value parsing. The value is a single
# STRING strength selector: "off" (default / neutral, no-op) | "gentle" | "strong".
# Unlike the per-player flags this is a GLOBAL decision the coordinator expands.
# Any malformed/unknown value degrades to the safe default "off" (no-op).
AUTO_RUBBERBAND_NONE_VALUES = frozenset({"", "off", "none", "0"})
AUTO_RUBBERBAND_STRENGTHS = frozenset({"gentle", "strong"})


def parse_auto_rubberband_value(value: object) -> str | None:
    """Parse an auto_rubberband value into a strength (``"gentle"``/``"strong"``).

    Returns the strength string for a recognized value, or ``None`` for the
    neutral / empty / unknown forms (caller treats ``None`` as a safe no-op). A
    malformed/unknown value is intentionally a no-op rather than a garbage
    dispatch — the coordinator only ever expands a recognized strength.
    """
    text = str(value).strip().lower()
    if text in AUTO_RUBBERBAND_NONE_VALUES:
        return None
    if text in AUTO_RUBBERBAND_STRENGTHS:
        return text
    return None  # unknown strength: safe no-op


async def handle_auto_rubberband(ctx: InterventionContext) -> None:
    """Expand a single auto_rubberband decision across players (#1143).

    Unlike the per-player handlers (which resolve a per-serial targeting ladder),
    this is a GLOBAL state decision: ``ctx.value`` is the strength selector
    (``"gentle"`` / ``"strong"`` / neutral). The coordinator reads the LIVE skill
    gap and applies a bounded, time-boxed handicap BOOST to the trailing
    player(s) via ``game.apply_auto_rubberband`` — compressing the gap without
    ever inverting standings or causing instant death (both bounded inside the
    method + the downstream [0.5, 2.0] clamp).

    Reverting requires no action: the per-player boosts simply stop applying when
    their deadlines pass (``_compute_effective_thresholds`` reads them live).
    No battery guard is needed: rubberband only ever RAISES a player's threshold
    (it is benefit-only). A low-battery laggard simply receives a smaller benefit
    and is never penalized, so there is nothing to gate on battery here.
    """
    game = ctx.game
    if game is None:
        logger.debug("auto_rubberband: no live game, ignoring")
        return

    strength = parse_auto_rubberband_value(ctx.value)
    if strength is None:
        return  # neutral / malformed: safe no-op

    await game.apply_auto_rubberband(strength)


async def handle_shield_seconds(ctx: InterventionContext, manager: InterventionManager) -> None:
    """Grant per-player shields via per-serial targeting resolution (N3).

    The manager dispatches once per serial whose grant changed (UC-040 BR-012,
    #1313), so only ``ctx.target_serial`` is shielded with ``ctx.value``
    seconds; a direct call without a target resolves every active serial.
    Serials with a value > 0 get a shield of that many seconds via
    ``game.grant_shield`` (extend-not-shorten, with a visible pulse). Serials at
    the neutral default (0) — or battery-gated by the helper — are skipped.
    Reverting to none requires no action: existing shields simply expire.

    In trigger mode a grant becomes a shield charge (#1837, UC-040 A30). A grant
    to a player who holds one raises ``InterventionNotApplied`` (A31); in a
    fan-out it is re-raised only when no serial was applied.
    """
    game = ctx.game
    if game is None:
        logger.debug("shield_seconds: no live game, ignoring")
        return

    shields = _grant_values(ctx, manager, SHIELD_NONE, "float")

    applied = False
    not_applied: InterventionNotApplied | None = None
    for serial, raw_seconds in shields.items():
        try:
            seconds = float(raw_seconds)
        except (TypeError, ValueError):
            continue  # malformed: safe no-op for this serial
        if seconds <= SHIELD_NONE:
            continue  # neutral default: no shield for this serial
        try:
            # grant_shield() returns False for a dead or unknown serial: not applied.
            applied = bool(await game.grant_shield(serial, seconds)) or applied
        except InterventionNotApplied as e:
            not_applied = not_applied or e
    if not_applied is not None and not applied:
        raise not_applied


async def handle_eliminate_player(ctx: InterventionContext) -> None:
    """Force-eliminate the targeted player through the natural kill path (N4).

    The payload is the target serial. Routes through ``game._kill_player`` with
    reason ``agent_intervention`` and accel 0.0 so the same spans, EventBus
    events, and metrics fire as for a natural death. Defensive: no live game /
    empty serial → log + no-op. When nobody is killed (unknown serial, already-dead
    player, or ``_kill_player`` returns False: a kill of the same player in flight
    or the mode declined it) nothing was applied, so it raises
    ``InterventionNotApplied`` and the intervention is recorded as
    ``blocked=true`` / ``eliminate_refused`` (UC-040 A22, #1383).
    """
    game = ctx.game
    if game is None:
        logger.debug("eliminate_player: no live game, ignoring")
        return

    serial = (ctx.payload or ctx.target_serial or "").strip()
    if not serial:
        logger.warning("eliminate_player: empty target serial, ignoring")
        return

    player = game.players.get(serial)
    if player is None:
        logger.warning(f"eliminate_player: unknown serial {serial}, refused")
        raise InterventionNotApplied(BLOCK_ELIMINATE_REFUSED, f"eliminate_player: unknown serial {serial}")
    if not player.alive:
        logger.info(f"eliminate_player: {serial} already dead, refused")
        raise InterventionNotApplied(BLOCK_ELIMINATE_REFUSED, f"eliminate_player: {serial} already dead")

    logger.info(f"eliminate_player: forcing elimination of {serial} ({ELIMINATE_REASON})")
    # ``is False``: a legacy override returning None still counts as applied.
    if await game._kill_player(serial, ELIMINATE_ACCEL, reason=ELIMINATE_REASON) is False:
        raise InterventionNotApplied(BLOCK_ELIMINATE_REFUSED, f"eliminate_player: mode declined the kill of {serial}")


async def handle_revive_player(ctx: InterventionContext) -> None:
    """Revive the targeted player through the mode's re-entry path (N5).

    The payload is the target serial. Routes through ``game.revive_player`` which
    prefers a mode-native respawn path and grants brief spawn grace. Mode
    eligibility is already enforced by the §9 capability matrix before this
    handler runs. Defensive: no live game / empty serial → log + no-op. When the
    mode refuses (decided game, unknown or already-alive player) nothing was
    applied, so it raises ``InterventionNotApplied`` and the intervention is
    recorded as ``blocked=true`` / ``revive_refused`` (UC-040 A22, #1948).
    """
    game = ctx.game
    if game is None:
        logger.debug("revive_player: no live game, ignoring")
        return

    serial = (ctx.payload or ctx.target_serial or "").strip()
    if not serial:
        logger.warning("revive_player: empty target serial, ignoring")
        return

    logger.info(f"revive_player: reviving {serial}")
    if not await game.revive_player(serial):
        raise InterventionNotApplied(BLOCK_REVIVE_REFUSED, f"revive_player: mode refused revive of {serial}")


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
    # toughness_boost also needs the manager for the targeting helper.
    manager.register_handler(
        "toughness_boost_seconds",
        lambda ctx: handle_toughness_boost(ctx, manager),
    )
    # soft_penalty also needs the manager for the targeting helper.
    manager.register_handler(
        "soft_penalty_action",
        lambda ctx: handle_soft_penalty(ctx, manager),
    )
    # auto_rubberband is a GLOBAL state decision (not per-serial), so it
    # needs no targeting helper — register the plain handler directly.
    manager.register_handler("auto_rubberband", handle_auto_rubberband)
    manager.register_handler("eliminate_player", handle_eliminate_player)
    manager.register_handler("revive_player", handle_revive_player)
