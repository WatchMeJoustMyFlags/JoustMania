"""
Parallel intervention/F6 coverage via gameId-scoped headless shadow games (#893).

Serves #770. Depends on #838 (gameId evaluation-context routing). The #730 / F6
intervention e2e tests used to run strictly serially: every test needed THE
primary menu game, because an un-targeted intervention only applies to the
primary session, and the menu's single shared lobby cannot host two real games.

Now that #838 routes interventions per session (``gameId`` in the
EvaluationContext, ``get_sessions`` seam), each intervention test can instead:

1. reserve its OWN tagged mock controllers + start its OWN headless shadow game
   (helpers: ``setup_mock_controllers(reserved=, tag=)``, ``start_game_headless``);
2. write its intervention flags **scoped to its own game_id** via a flagd
   targeting rule on the ``gameId`` context var (default variants stay no-op, so
   a write for game A is a guaranteed no-op for game B — zero cross-talk);
3. assert effects on its OWN game's id-stamped event stream + ``GetGameState``.

The independent ``full``-permission tests then run CONCURRENTLY via
``asyncio.gather`` batches on the single shared compose stack — exactly the
structure of ``test_parallel_lifecycle.py`` (#826): per-item tags, per-coroutine
``finally`` cleanup, a fixture sweep backstop, and mode/name-tagged failure
aggregation so one failing test does not mask the rest.

WHAT STAYS SEQUENTIAL (and lives in ``test_intervention_flow.py`` /
``test_f6_flow.py``), with justification:

- ``interventions_allowed`` is a GLOBAL agent-domain flag, read with a bare
  EvaluationContext (no ``gameId``) by ``InterventionManager._allowed_types`` —
  it is NOT gameId-scopeable without a product change. So every test in a batch
  must share ONE permission level. These batches all run under ``full``; the
  permission-BLOCK negatives (``ambient`` / ``standard``) and the ``standard``
  pacing test stay sequential because they need a different global permission
  level and would clobber a concurrent ``full`` batch.
- The live-flagd RPC resolver tests assert file-level / global flag state
  (un-targeted writes, committed game.json) — order-dependent, kept sequential.
- The writer-gate test (``AGENT_INTERVENTIONS_ENABLED`` default) is a compose
  file assertion with no game.
- ``test_gameid_intervention_isolation.py`` keeps the core #838 semantics
  coverage (un-targeted intervention reaches the primary; gameId-scoped reaches
  only the shadow) sequentially.

RATE LIMITER: the weighted sliding-window limiter on the coordinator's
InterventionManager is process-GLOBAL (shared across every live session) and is
NOT reset between tests; its budget comes from the global
``policy.max_interventions_per_minute`` flag. Concurrent intervention tests
consume the SAME 60s window, so we pin a generous suite-wide budget
(``SUITE_RATE_LIMIT_BUDGET``) via the ``flag_files`` fixture before any batch
runs — total worst-case weight per batch (~13) sits far under it. See the PR
body for the per-session-budget product follow-up this surfaces.

All waits POLL (no fixed-sleep one-shots) per the assert-too-soon CI lessons
(LED race, RUNNING race, team-assignment race).
"""

import asyncio
import json
import os
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import game_coordinator_pb2  # noqa: E402

from tests.integration.helpers import (  # noqa: E402
    async_poll_until,
    flagd_probe_client,
    GameEventCollector,
    build_start_config,
    force_end_game_by_id,
    get_game_client,
    get_mock_client,
    list_games,
    remove_reserved_controllers,
    setup_mock_controllers,
    start_game_headless,
    verify_controllers_have_color,
)
from tests.integration.test_intervention_flow import (  # noqa: E402
    AGENT_PATH,
    APPLY_TIMEOUT_SECONDS,
    INTERVENTIONS_PATH,
    RELOAD_SETTLE_SECONDS,
    flag_files,  # re-exported: snapshot/restore + suite-wide budget pin
    set_interventions_allowed,
)

# Keep the re-exported fixture referenced so linters don't flag it as unused.
__all__ = ["flag_files"]


# =============================================================================
# gameId-scoped flag-file mutation helpers (mirror services/agent/actions
# writer.go + the #838 gameId targeting shape from test_concurrent_games).
#
# Every write here is SCOPED to one game_id via a flagd targeting rule on the
# ``gameId`` context var, with the default variant left on its neutral value.
# So a write for game A resolves to a no-op for every other live session —
# which is exactly what lets these tests run concurrently without cross-talk.
#
# The writers are fully synchronous (no ``await`` between the in-place read and
# write), so on the single event-loop thread two gathered coroutines cannot
# interleave mid-write. A module lock is held anyway as defense-in-depth (and so
# the helpers remain correct if ever called from a thread pool).
# =============================================================================

_FILE_LOCK = threading.Lock()


def _load(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _dump_in_place(path: str, doc: dict) -> None:
    """In-place truncate+write (rename over a watched bind mount triggers EBUSY)."""
    text = json.dumps(doc, indent=2) + "\n"
    with open(path, "w") as fh:
        fh.write(text)


def _game_rule(game_id: str, match_variant: str, neutral_variant: str) -> dict:
    """flagd targeting rule: serve ``match_variant`` only for ``game_id``."""
    return {
        "if": [
            {"===": [{"var": "gameId"}, game_id]},
            match_variant,
            neutral_variant,
        ]
    }


def _branch_var_name(game_id: str, serial: str | None) -> str:
    """A flagd variant name unique per (game_id[, serial]).

    game_id contains an underscore (``game_<hex>``), so a parse-free stable name
    is required: hash the (game_id, serial) pair. The actual (game_id, serial)
    routing lives in the ``_branches`` side registry the writers maintain, never
    re-parsed from this name.
    """
    key = f"{game_id}\x00{serial or ''}"
    return "g_" + uuid.uuid5(uuid.NAMESPACE_OID, key).hex


def _rebuild_ladder(flag: dict, neutral: str) -> None:
    """Rebuild the flag's targeting if-ladder from its ``_branches`` registry.

    ``_branches`` maps ``variant_name -> {gameId, serial?}``. Each entry becomes
    one if-branch matching ``gameId == <id>`` (and ``targetingKey == serial`` for
    player-targeted flags), falling through to the neutral default. Order is by
    variant name (stable) so the rebuild is deterministic and a new sibling
    branch never clobbers an existing one.
    """
    branches = flag.get("_branches", {})
    expr: object = neutral
    for name in sorted(branches, reverse=True):
        meta = branches[name]
        serial = meta.get("serial")
        if serial:
            cond = {
                "and": [
                    {"===": [{"var": "gameId"}, meta["gameId"]]},
                    {"===": [{"var": "targetingKey"}, serial]},
                ]
            }
        else:
            cond = {"===": [{"var": "gameId"}, meta["gameId"]]}
        expr = {"if": [cond, name, expr]}
    flag["targeting"] = expr
    flag["defaultVariant"] = neutral


def write_targeted_for_game(
    flag_key: str, game_id: str, serial: str, value, *, neutral: str = "default"
) -> None:
    """Per-player state write scoped to one gameId AND one serial (#838 compose).

    Writes a per-(game,serial) variant holding ``value`` and a targeting rule
    that serves it ONLY when both ``gameId == game_id`` and
    ``targetingKey == serial`` match, falling through to the neutral default for
    every other game/player. Merges with existing per-(game,serial) branches via
    the ``_branches`` registry so concurrent tests targeting distinct
    games/players compose into one ladder.

    The handler (``resolve_player_targets``) evaluates this flag per active
    serial with ``{gameId, targetingKey=serial}``, so the rule resolves the
    targeted value for the owning game's targeted player and the neutral default
    everywhere else.
    """
    with _FILE_LOCK:
        doc = _load(INTERVENTIONS_PATH)
        flag = doc["flags"][flag_key]
        var_name = _branch_var_name(game_id, serial)
        flag["variants"][var_name] = value
        flag.setdefault("_branches", {})[var_name] = {"gameId": game_id, "serial": serial}
        _rebuild_ladder(flag, neutral)
        _dump_in_place(INTERVENTIONS_PATH, doc)


def write_state_for_game(flag_key: str, game_id: str, value, *, neutral: str) -> None:
    """State-shaped write scoped to one gameId.

    Writes a per-game variant holding ``value`` and a targeting rule serving it
    only for ``gameId == game_id`` (neutral default otherwise). Merges with
    sibling per-game branches via the ``_branches`` registry so concurrent state
    tests don't clobber each other.
    """
    with _FILE_LOCK:
        doc = _load(INTERVENTIONS_PATH)
        flag = doc["flags"][flag_key]
        var_name = _branch_var_name(game_id, None)
        flag["variants"][var_name] = value
        flag.setdefault("_branches", {})[var_name] = {"gameId": game_id, "serial": None}
        _rebuild_ladder(flag, neutral)
        _dump_in_place(INTERVENTIONS_PATH, doc)


def write_edge_for_game(flag_key: str, game_id: str, payload: str = "") -> str:
    """Edge write scoped to one gameId via a flagd targeting rule (#838).

    The ``active`` variant holds ``<nonce>:<payload>`` (or ``<nonce>`` when
    payload is empty); the targeting rule serves it only when
    ``gameId == game_id`` and the neutral ``none`` variant otherwise. Returns the
    fresh nonce so callers can re-assert exactly-once (same nonce = no re-apply).

    NOTE: edge flags carry a single ``active`` variant (one in-flight edge per
    flag key), so concurrent tests must use DISTINCT edge flag keys
    (``eliminate_player`` vs ``revive_player``) — which the batch composition
    guarantees.
    """
    nonce = uuid.uuid4().hex
    value = nonce if payload == "" else f"{nonce}:{payload}"
    with _FILE_LOCK:
        doc = _load(INTERVENTIONS_PATH)
        flag = doc["flags"][flag_key]
        flag["variants"]["active"] = value
        flag["targeting"] = _game_rule(game_id, "active", "none")
        flag["defaultVariant"] = "none"
        _dump_in_place(INTERVENTIONS_PATH, doc)
    return nonce


def rewrite_edge_value_for_game(flag_key: str, game_id: str, raw_value: str) -> None:
    """Force flagd to re-serve a SPECIFIC ``active`` value for one gameId.

    Re-reads the same nonce (to assert the exactly-once guard): keeps the gameId
    targeting rule and the ``active`` value unchanged, toggling a harmless marker
    so the file changes byte-wise and flagd reloads.
    """
    with _FILE_LOCK:
        doc = _load(INTERVENTIONS_PATH)
        flag = doc["flags"][flag_key]
        flag["variants"]["active"] = raw_value
        flag["targeting"] = _game_rule(game_id, "active", "none")
        flag["defaultVariant"] = "none"
        flag.setdefault("_reload_marker", 0)
        flag["_reload_marker"] = int(flag["_reload_marker"]) + 1
        _dump_in_place(INTERVENTIONS_PATH, doc)


# =============================================================================
# Observable-signal helpers (game_id-aware)
# =============================================================================


def _intervention_events(collector: GameEventCollector, type_id: str, *, blocked=None) -> list:
    out = []
    for e in collector.get_events("agent_intervention"):
        if e.data.get("type") != type_id:
            continue
        if blocked is not None and e.data.get("blocked") != blocked:
            continue
        out.append(e)
    return out


async def _wait_for_intervention(
    collector: GameEventCollector, type_id: str, *, blocked=None, timeout: float = APPLY_TIMEOUT_SECONDS
):
    deadline = time.time() + timeout
    while time.time() < deadline:
        hits = _intervention_events(collector, type_id, blocked=blocked)
        if hits:
            return hits[-1]
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"no agent_intervention(type={type_id}, blocked={blocked}) within {timeout}s; "
        f"saw: {[dict(e.data) for e in collector.get_events('agent_intervention')]}"
    )


async def _player(game_client, game_id: str, serial: str):
    resp = await game_client.GetGameState(
        game_coordinator_pb2.GetGameStateRequest(game_id=game_id)
    )
    if not resp.success:
        return None
    for p in resp.game_info.players:
        if p.serial == serial:
            return p
    return None


async def _wait_for_sensitivity_factor(
    game_client, game_id: str, serial: str, expected: float, *, tol: float = 0.01, timeout: float = APPLY_TIMEOUT_SECONDS
) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        p = await _player(game_client, game_id, serial)
        if p is not None:
            last = p.sensitivity_factor
            if abs(p.sensitivity_factor - expected) <= tol:
                return
        await asyncio.sleep(0.3)
    raise AssertionError(
        f"{serial} (game {game_id}) sensitivity_factor did not reach {expected} "
        f"(last={last}) within {timeout}s"
    )


async def _wait_until_dead(game_client, game_id: str, serial: str, timeout: float = APPLY_TIMEOUT_SECONDS) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = await _player(game_client, game_id, serial)
        if p is not None and not p.alive:
            return True
        await asyncio.sleep(0.3)
    return False


async def _wait_for_game_colors(mock_client, serials: list[str], timeout: float = 10.0) -> None:
    """Poll until every controller shows a non-zero LED (parallel load lags it)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            await verify_controllers_have_color(mock_client, serials)
            return
        except AssertionError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.5)


async def _start_headless_with_retry(game_client, start_config, attempts: int = 4, backoff: float = 2.0):
    """Start headless, retrying brief "Game already in progress" cap windows.

    A sibling batch coroutine's force-ended game may still be in its ENDING
    window when this start hits the concurrency-cap check; the lazy sweep frees
    the slot moments later, so a short retry is correct (mirrors
    test_parallel_lifecycle).
    """
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            return await start_game_headless(game_client, start_config, timeout=25.0)
        except Exception as exc:  # noqa: BLE001 - only the cap window is retryable
            if "Game already in progress" not in str(exc):
                raise
            last_exc = exc
            await asyncio.sleep(backoff)
    raise last_exc


async def _await_running(game_client, game_id: str, timeout: float = 12.0) -> None:
    """Poll ListGames until this game is RUNNING (start returns on STARTING)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        running = {
            g.game_id
            for g in await list_games(game_client)
            if g.state == game_coordinator_pb2.RUNNING
        }
        if game_id in running:
            return
        await asyncio.sleep(0.3)
    raise AssertionError(f"game {game_id} never reached RUNNING within {timeout}s")


# =============================================================================
# Per-test shadow-game scenarios (gameId-scoped; safe to gather concurrently)
#
# Each scenario is a coroutine ``async def(docker_compose, tag) -> None`` that
# owns its reserved controllers + headless game + gameId-scoped flag writes and
# cleans up in its own finally. Raising tags the failure with the scenario name
# so a gathered batch reports exactly which one broke.
# =============================================================================


async def _scenario_player_sensitivity(docker_compose, tag: str) -> None:
    """Per-player sensitivity factor scoped to one shadow game + player.

    A gameId+serial targeting rule applies the factor to the targeted player of
    THIS game only; observable via this game's GetGameState.sensitivity_factor
    and an unblocked adjust_player_sensitivity event on this game's stream.
    """
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(docker_compose, count=4, reserved=True, tag=tag)
        target, other = serials[0], serials[1]
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        # Baseline: this game's players at the neutral 1.0 factor.
        await _wait_for_sensitivity_factor(game_client, game_id, target, 1.0)

        # gameId+serial-scoped factor for the target only.
        write_targeted_for_game("player_sensitivity_factor", game_id, target, 1.5)

        evt = await _wait_for_intervention(collector, "adjust_player_sensitivity", blocked="false")
        assert evt.data.get("blocked") == "false"

        await _wait_for_sensitivity_factor(game_client, game_id, target, 1.5)
        other_player = await _player(game_client, game_id, other)
        assert other_player is not None
        assert abs(other_player.sensitivity_factor - 1.0) <= 0.01, (
            f"non-targeted {other} changed: {other_player.sensitivity_factor}"
        )
    finally:
        if collector is not None:
            await collector.stop()
        if game_id:
            await force_end_game_by_id(game_client, game_id, reason="parallel_intervention_cleanup")
        await remove_reserved_controllers(docker_compose, tag)
        await mock_channel.close()
        await game_channel.close()


async def _scenario_eliminate_exactly_once(docker_compose, tag: str) -> None:
    """Edge-triggered elimination + exactly-once nonce, scoped to one shadow game.

    A gameId-scoped eliminate_player nonce kills the targeted player of THIS game
    through the real kill path; re-reading the SAME nonce does NOT re-apply; a
    FRESH nonce applies again to a different player.
    """
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(docker_compose, count=4, reserved=True, tag=tag)
        victim, second_victim = serials[0], serials[1]
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        assert (await _player(game_client, game_id, victim)).alive, "victim should start alive"

        # Edge 1: eliminate the victim (scoped to this game).
        nonce1 = write_edge_for_game("eliminate_player", game_id, victim)
        await _wait_for_intervention(collector, "eliminate_player", blocked="false")
        assert await _wait_until_dead(game_client, game_id, victim), "victim was not eliminated"

        applied_after_first = len(_intervention_events(collector, "eliminate_player", blocked="false"))

        # Re-read SAME nonce (force reload, value unchanged): no re-apply.
        # Absence assertion — bounded wait by design (#894): there is no event
        # to wait for when the correct behavior is "no re-apply".
        rewrite_edge_value_for_game("eliminate_player", game_id, f"{nonce1}:{victim}")
        await asyncio.sleep(RELOAD_SETTLE_SECONDS + 1.0)
        applied_after_reread = len(_intervention_events(collector, "eliminate_player", blocked="false"))
        assert applied_after_reread == applied_after_first, (
            "same nonce re-applied the elimination (exactly-once broken)"
        )

        # FRESH nonce -> applies again, to a different player.
        assert (await _player(game_client, game_id, second_victim)).alive
        write_edge_for_game("eliminate_player", game_id, second_victim)
        assert await _wait_until_dead(game_client, game_id, second_victim), (
            "fresh nonce did not re-apply elimination"
        )
    finally:
        if collector is not None:
            await collector.stop()
        if game_id:
            await force_end_game_by_id(game_client, game_id, reason="parallel_intervention_cleanup")
        await remove_reserved_controllers(docker_compose, tag)
        await mock_channel.close()
        await game_channel.close()


async def _scenario_revive_allowed_respawn(docker_compose, tag: str) -> None:
    """Mode-matrix positive: revive_player is NOT mode-blocked in NonStop (respawn).

    Scoped to a NonStop shadow game; the revive intervention's event must not
    carry block_reason=mode_unsupported.
    """
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(docker_compose, count=4, reserved=True, tag=tag)
        victim = serials[0]
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("NonStop", serials)
        )
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        write_edge_for_game("revive_player", game_id, victim)
        evt = await _wait_for_intervention(collector, "revive_player")
        assert evt.data.get("block_reason") != "mode_unsupported", (
            "revive wrongly blocked as mode_unsupported in a respawn mode"
        )
    finally:
        if collector is not None:
            await collector.stop()
        if game_id:
            await force_end_game_by_id(game_client, game_id, reason="parallel_intervention_cleanup")
        await remove_reserved_controllers(docker_compose, tag)
        await mock_channel.close()
        await game_channel.close()


async def _scenario_revive_blocked_permanent(docker_compose, tag: str) -> None:
    """Mode-matrix negative: revive_player is mode-blocked in FFA (permanent elim).

    Scoped to an FFA shadow game; the revive event must carry
    block_reason=mode_unsupported.
    """
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(docker_compose, count=4, reserved=True, tag=tag)
        victim = serials[0]
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        write_edge_for_game("revive_player", game_id, victim)
        evt = await _wait_for_intervention(collector, "revive_player", blocked="true")
        assert evt.data.get("block_reason") == "mode_unsupported", (
            f"expected mode_unsupported, got {evt.data.get('block_reason')}"
        )
    finally:
        if collector is not None:
            await collector.stop()
        if game_id:
            await force_end_game_by_id(game_client, game_id, reason="parallel_intervention_cleanup")
        await remove_reserved_controllers(docker_compose, tag)
        await mock_channel.close()
        await game_channel.close()


async def _scenario_global_difficulty(docker_compose, tag: str) -> None:
    """F6 global_difficulty_factor apply + revert, scoped to one shadow game.

    Writing a non-neutral factor for THIS game fires an unblocked
    adjust_global_difficulty event on this game's stream; reverting to neutral
    1.0 runs the revert path WITHOUT a new applied event.
    """
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(docker_compose, count=4, reserved=True, tag=tag)
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        # Harder global difficulty (1.5) scoped to this game.
        write_state_for_game("global_difficulty_factor", game_id, 1.5, neutral="default")
        await _wait_for_intervention(collector, "adjust_global_difficulty", blocked="false")
        applied_after_apply = len(_intervention_events(collector, "adjust_global_difficulty", blocked="false"))

        # Revert to neutral 1.0: revert handler runs, no new applied event.
        # Absence assertion — bounded wait by design (#894): there is no event
        # to wait for when the correct behavior is "no new event".
        write_state_for_game("global_difficulty_factor", game_id, 1.0, neutral="default")
        await asyncio.sleep(RELOAD_SETTLE_SECONDS + 1.0)
        applied_after_revert = len(_intervention_events(collector, "adjust_global_difficulty", blocked="false"))
        assert applied_after_revert == applied_after_apply, (
            "revert to neutral 1.0 spuriously fired a new applied event"
        )
    finally:
        if collector is not None:
            await collector.stop()
        if game_id:
            await force_end_game_by_id(game_client, game_id, reason="parallel_intervention_cleanup")
        await remove_reserved_controllers(docker_compose, tag)
        await mock_channel.close()
        await game_channel.close()


async def _scenario_repeated_writes(docker_compose, tag: str) -> None:
    """Repeated in-place gameId-scoped writes all propagate (file watch healthy).

    Drives this game's player_sensitivity_factor through a sequence of distinct
    values and asserts the game converges on the FINAL value (and an intermediate
    value was observed at least once, proving multiple reloads landed).
    """
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(docker_compose, count=4, reserved=True, tag=tag)
        target = serials[0]
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        sequence = [1.5, 0.7, 1.5, 0.5, 2.0]
        observed_intermediate = False
        for value in sequence:
            write_targeted_for_game("player_sensitivity_factor", game_id, target, value)

            # Poll (bounded by the old fixed settle, #894) for THIS value to be
            # applied; on success move on immediately instead of paying the full
            # constant. A miss is tolerated by design — under load a write may
            # be superseded before the coordinator observes it; the asserts
            # below only require the FINAL value plus >=1 observed intermediate.
            async def _applied(v=value) -> bool:
                p = await _player(game_client, game_id, target)
                return p is not None and abs(p.sensitivity_factor - v) <= 0.01

            if await async_poll_until(_applied, RELOAD_SETTLE_SECONDS):
                observed_intermediate = True

        await _wait_for_sensitivity_factor(game_client, game_id, target, sequence[-1])
        assert observed_intermediate, (
            "no intermediate write was ever observed — file watch may be stuck"
        )
        applied = _intervention_events(collector, "adjust_player_sensitivity", blocked="false")
        assert len(applied) >= 2, (
            f"expected multiple applied events across writes, got {len(applied)}"
        )
    finally:
        if collector is not None:
            await collector.stop()
        if game_id:
            await force_end_game_by_id(game_client, game_id, reason="parallel_intervention_cleanup")
        await remove_reserved_controllers(docker_compose, tag)
        await mock_channel.close()
        await game_channel.close()


# =============================================================================
# Batch definitions + the gathered runner
#
# All scenarios in a batch run under the SAME global ``interventions_allowed``
# level (``full``) — the only level they share — and write DISTINCT flag keys
# (so the per-flag ``active`` edge slot is never contended; see
# ``write_edge_for_game``). Batch size <= GAME_MAX_CONCURRENT_GAMES (4 in CI).
# =============================================================================

_BATCHES = [
    pytest.param(
        {
            "sensitivity": _scenario_player_sensitivity,
            "eliminate": _scenario_eliminate_exactly_once,
            "revive-allowed": _scenario_revive_allowed_respawn,
            "global-difficulty": _scenario_global_difficulty,
        },
        id="full-batch-a",
    ),
    pytest.param(
        {
            "revive-blocked": _scenario_revive_blocked_permanent,
            "repeated-writes": _scenario_repeated_writes,
        },
        id="full-batch-b",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenarios", _BATCHES)
async def test_parallel_interventions(flag_files, docker_compose, headless_cleanup, scenarios):
    """Run a batch of independent ``full``-permission intervention scenarios
    concurrently, each as a gameId-scoped headless shadow game.

    Each scenario reserves its own tagged controllers, starts its own headless
    game, writes its intervention flags scoped to its own game_id (default
    variants no-op for every other game), and asserts effects on its own stream.
    The batch runs via ``asyncio.gather(..., return_exceptions=True)`` so one
    scenario's failure does not mask the others; failures are aggregated, each
    tagged with its scenario name.
    """
    # All batch scenarios share the single global ``interventions_allowed`` flag
    # (not gameId-scopeable); they are all selected to run under ``full``.
    set_interventions_allowed("full")
    # Wait until flagd actually SERVES the permission flip before any scenario
    # writes (#894: condition poll, not a fixed settle).
    with open(AGENT_PATH) as fh:
        expected_full = json.load(fh)["flags"]["interventions_allowed"]["variants"]["full"]
    from openfeature.evaluation_context import EvaluationContext

    with flagd_probe_client(docker_compose, "agent") as flag_client:
        assert await async_poll_until(
            lambda: flag_client.get_object_value(
                "interventions_allowed", None, EvaluationContext()
            )
            == expected_full,
            rewrite=lambda: set_interventions_allowed("full"),
        ), "flagd did not serve interventions_allowed=full"

    # Register tags up front so the fixture sweeps reserved controllers even if a
    # coroutine dies before its own finally-block cleanup runs.
    tags = {}
    for name in scenarios:
        tag = f"parallel-intv-{name}"
        headless_cleanup.add_tag(tag)
        tags[name] = tag

    async def _run(name, fn):
        try:
            await fn(docker_compose, tags[name])
        except Exception as exc:  # noqa: BLE001 - re-raise tagged with scenario
            raise AssertionError(f"[{name}] intervention scenario failed: {exc}") from exc

    results = await asyncio.gather(
        *(_run(name, fn) for name, fn in scenarios.items()),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        messages = "; ".join(str(f) for f in failures)
        raise AssertionError(f"{len(failures)}/{len(scenarios)} scenarios failed: {messages}")
