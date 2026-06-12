"""
Integration regression test for gameId-scoped interventions (#838).

The direct regression the issue requires: with ``shadow_policy=allow`` (the CI
default — see services/flagd/game.ci.json), run a REAL menu game AND a shadow
(headless) game concurrently, then write a gameId-scoped intervention flag that
targets the SHADOW game's id. Assert:

- the SHADOW game received the effect (an unblocked agent_intervention event on
  its OWN game_id-stamped stream, and the targeted player actually eliminated);
- the REAL (primary) game is provably UNTOUCHED — no agent_intervention event on
  the primary stream and its targeted-by-name player stays alive;
- an UN-TARGETED intervention still hits the PRIMARY (today's semantics).

This builds on the #779 isolation harness (test_concurrent_games.py) for the
shadow-vs-menu setup and the #780 e2e flag-writer helpers
(test_intervention_flow.py) for the in-place flagd mutations the Go ActionSink
would produce. The agent's Go writer does NOT run in the integration compose;
these in-place writes reproduce its file mutation exactly, and flagd's file
watch reloads them.

All waits poll (no fixed-sleep one-shots) per the assert-too-soon CI lesson.
"""

import asyncio
import json
import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import game_coordinator_pb2  # noqa: E402

from tests.integration.helpers import (  # noqa: E402
    GameEventCollector,
    build_start_config,
    flagd_probe_client,
    force_end_game_by_id,
    get_game_client,
    get_mock_client,
    kill_players_until_one_remains,
    list_games,
    poll_until,
    setup_mock_controllers,
    start_game_via_menu,
    start_game_headless,
    wait_flag_file_restored,
    wait_for_lobby_colors,
)

_FLAGD_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../services/flagd")
)
INTERVENTIONS_PATH = os.path.join(_FLAGD_DIR, "interventions.json")
AGENT_PATH = os.path.join(_FLAGD_DIR, "agent.json")

APPLY_TIMEOUT_SECONDS = 15.0
SUITE_RATE_LIMIT_BUDGET = 50


# =============================================================================
# Flag-file mutation helpers (mirror services/agent/actions/writer.go + #780)
# =============================================================================


def _load(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _dump_in_place(path: str, doc: dict) -> None:
    """In-place truncate+write (rename over a watched bind mount triggers EBUSY)."""
    text = json.dumps(doc, indent=2) + "\n"
    with open(path, "w") as fh:
        fh.write(text)


def write_edge_for_game(flag_key: str, game_id: str, payload: str) -> str:
    """Edge write scoped to a single gameId via a flagd targeting rule (#838).

    Produces the mutation a gameId-aware agent writer makes: an ``active``
    variant holding ``<nonce>:<payload>`` and a targeting rule that serves it
    ONLY when ``gameId == game_id``, falling through to the neutral ``none``
    variant otherwise:

        {"if": [{"===": [{"var": "gameId"}, game_id]}, "active", "none"]}

    So the matching (shadow) session reads the live nonce while every other
    session (the primary) reads the neutral empty value -> no-op. Returns the
    fresh nonce.
    """
    nonce = uuid.uuid4().hex
    value = f"{nonce}:{payload}"
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"][flag_key]
    flag["variants"]["active"] = value
    flag["targeting"] = {
        "if": [{"===": [{"var": "gameId"}, game_id]}, "active", "none"]
    }
    # Default stays neutral so an un-targeted read (no gameId / different id) is a no-op.
    flag["defaultVariant"] = "none"
    _dump_in_place(INTERVENTIONS_PATH, doc)
    return nonce


def write_edge_untargeted(flag_key: str, payload: str) -> str:
    """Un-targeted edge write: ``active`` variant + defaultVariant flipped, no
    gameId rule. Mirrors #780's write_edge — applies to the primary only because
    the InterventionManager evaluates the primary session's read and shadow reads
    resolve to the same global default (but the primary is the one that gets the
    enforced effect under today's semantics)."""
    nonce = uuid.uuid4().hex
    value = f"{nonce}:{payload}"
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"][flag_key]
    flag["variants"]["active"] = value
    flag.pop("targeting", None)
    flag["defaultVariant"] = "active"
    _dump_in_place(INTERVENTIONS_PATH, doc)
    return nonce


def set_interventions_allowed(variant: str) -> None:
    doc = _load(AGENT_PATH)
    doc["flags"]["interventions_allowed"]["defaultVariant"] = variant
    _dump_in_place(AGENT_PATH, doc)


def set_policy_budget(per_minute: int) -> None:
    """Pin BOTH rate-limit budgets generously (#919): the agent's per-game
    ``policy.max_interventions_per_minute`` and the coordinator's process-global
    backstop ``policy.coordinator_backstop_per_minute``. This suite asserts
    per-game-id isolation, so neither limiter must spuriously throttle the
    gameId-scoped writes."""
    doc = _load(AGENT_PATH)
    for flag_key in ("policy.max_interventions_per_minute", "policy.coordinator_backstop_per_minute"):
        flag = doc["flags"][flag_key]
        flag["variants"]["active"] = int(per_minute)
        flag["defaultVariant"] = "active"
    _dump_in_place(AGENT_PATH, doc)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def flag_files(docker_compose):
    """Snapshot interventions.json + agent.json; restore byte-for-byte on exit.

    Pins a generous suite-wide rate-limit budget up front (the limiter is
    process-global on the coordinator and not reset between tests) so the
    gameId-scoped writes are never spuriously rate-limited.
    """
    with open(INTERVENTIONS_PATH) as fh:
        interventions_backup = fh.read()
    with open(AGENT_PATH) as fh:
        agent_backup = fh.read()

    # Pin the budget and wait until flagd actually SERVES it (#894: condition
    # poll, not a fixed settle).
    set_policy_budget(SUITE_RATE_LIMIT_BUDGET)
    from openfeature.evaluation_context import EvaluationContext

    with flagd_probe_client(docker_compose, "agent") as client:
        assert poll_until(
            lambda: client.get_integer_value(
                "policy.max_interventions_per_minute", -1, EvaluationContext()
            )
            == SUITE_RATE_LIMIT_BUDGET,
            rewrite=lambda: set_policy_budget(SUITE_RATE_LIMIT_BUDGET),
        ), "flagd did not serve the pinned suite rate-limit budget"

    yield

    def _restore():
        with open(INTERVENTIONS_PATH, "w") as fh:
            fh.write(interventions_backup)
        with open(AGENT_PATH, "w") as fh:
            fh.write(agent_backup)

    with open(INTERVENTIONS_PATH) as fh:
        interventions_mutated = fh.read()
    with open(AGENT_PATH) as fh:
        agent_mutated = fh.read()
    _restore()
    # Wait until flagd serves the restored baselines before the next test (#894).
    wait_flag_file_restored(
        docker_compose, "interventions", interventions_backup, interventions_mutated, _restore
    )
    wait_flag_file_restored(docker_compose, "agent", agent_backup, agent_mutated, _restore)


# =============================================================================
# Signal helpers
# =============================================================================


def _intervention_events(
    collector: GameEventCollector, type_id: str, *, blocked=None
) -> list:
    out = []
    for e in collector.get_events("agent_intervention"):
        if e.data.get("type") != type_id:
            continue
        if blocked is not None and e.data.get("blocked") != blocked:
            continue
        out.append(e)
    return out


async def _wait_for_intervention(
    collector, type_id, *, blocked=None, timeout=APPLY_TIMEOUT_SECONDS
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


async def _player(game_client, game_id, serial):
    resp = await game_client.GetGameState(
        game_coordinator_pb2.GetGameStateRequest(game_id=game_id)
    )
    if not resp.success:
        return None
    for p in resp.game_info.players:
        if p.serial == serial:
            return p
    return None


async def _wait_until_dead(game_client, game_id, serial, timeout=APPLY_TIMEOUT_SECONDS):
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = await _player(game_client, game_id, serial)
        if p is not None and not p.alive:
            return True
        await asyncio.sleep(0.3)
    return False


# =============================================================================
# The regression test
# =============================================================================


@pytest.mark.asyncio
async def test_gameid_scoped_intervention_isolates_shadow_from_real(
    flag_files, docker_compose, ensure_game_stopped, headless_cleanup
):
    """A gameId-scoped intervention reaches ONLY the targeted shadow game; the
    concurrently running real (menu) game is provably untouched. An un-targeted
    intervention still hits the primary."""
    set_interventions_allowed("full")  # eliminate_player must be allowed
    shadow_tag = "gameid-shadow"
    headless_cleanup.add_tag(shadow_tag)

    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)

    # 4 unreserved mocks for the real menu game.
    menu_serials = await setup_mock_controllers(docker_compose, count=4)

    primary_collector = GameEventCollector(game_client)  # zero-arg => primary stream
    shadow_collector = None
    try:
        await primary_collector.start()

        # 1. REAL menu game (primary session) on the unreserved mocks.
        await start_game_via_menu(
            docker_compose,
            game_mode="JoustFFA",
            timeout=25.0,
            event_collector=primary_collector,
        )
        primary_started = primary_collector.get_events("game_started")
        assert primary_started, "menu game should emit game_started"
        primary_game_id = primary_started[0].game_id

        # 2. SHADOW headless game on reserved mocks, concurrently.
        shadow_serials = await setup_mock_controllers(
            docker_compose, count=3, reserved=True, tag=shadow_tag
        )
        assert set(menu_serials).isdisjoint(shadow_serials)
        shadow_game_id, shadow_collector = await start_game_headless(
            game_client, build_start_config("JoustFFA", shadow_serials), timeout=25.0
        )
        headless_cleanup.add_game(shadow_game_id)
        assert shadow_game_id != primary_game_id

        # Both RUNNING (poll, never one-shot).
        deadline = time.time() + 10.0
        while time.time() < deadline:
            running = {
                g.game_id
                for g in await list_games(game_client)
                if g.state == game_coordinator_pb2.RUNNING
            }
            if {primary_game_id, shadow_game_id} <= running:
                break
            await asyncio.sleep(0.3)
        assert primary_game_id in running and shadow_game_id in running, (
            f"not both RUNNING: {running}"
        )

        shadow_victim = shadow_serials[0]
        # A like-positioned menu player we will prove is NOT eliminated.
        menu_bystander = menu_serials[0]
        assert (await _player(game_client, shadow_game_id, shadow_victim)).alive
        assert (await _player(game_client, primary_game_id, menu_bystander)).alive

        # 3. gameId-scoped eliminate targeting the SHADOW game only.
        write_edge_for_game("eliminate_player", shadow_game_id, shadow_victim)

        # The shadow stream sees the unblocked elimination event...
        evt = await _wait_for_intervention(
            shadow_collector, "eliminate_player", blocked="false"
        )
        assert evt.game_id in ("", shadow_game_id), (
            f"shadow event carried foreign game_id {evt.game_id}"
        )
        # ...and the shadow victim actually died through the real kill path.
        assert await _wait_until_dead(game_client, shadow_game_id, shadow_victim), (
            "gameId-targeted shadow victim was not eliminated"
        )

        # 4. The REAL game is provably untouched: no eliminate intervention on the
        # primary stream, and the menu bystander stays alive.
        primary_elim = _intervention_events(primary_collector, "eliminate_player")
        assert not primary_elim, (
            f"primary stream received a gameId-scoped shadow intervention: {primary_elim}"
        )
        assert (await _player(game_client, primary_game_id, menu_bystander)).alive, (
            "the real game's player was eliminated by a shadow-scoped intervention"
        )

        # 5. An UN-TARGETED intervention still hits the PRIMARY (today's semantics).
        # Re-allow + write an un-targeted eliminate; the primary's bystander dies.
        write_edge_untargeted("eliminate_player", menu_bystander)
        await _wait_for_intervention(
            primary_collector, "eliminate_player", blocked="false"
        )
        assert await _wait_until_dead(game_client, primary_game_id, menu_bystander), (
            "un-targeted intervention did not reach the primary game"
        )

        # 6. Tear down: end the shadow, then the menu game; menu lobby restores.
        await force_end_game_by_id(game_client, shadow_game_id)
        await shadow_collector.wait_for_any_event(
            ["game_ended", "game_force_ended", "game_error"], timeout=20.0
        )
        # The menu game already lost one player (bystander); finish it naturally.
        await kill_players_until_one_remains(
            mock_client, menu_serials, delay=0.2, game_client=game_client
        )
        await primary_collector.wait_for_any_event(
            ["game_ended", "game_force_ended", "game_error"], timeout=20.0
        )
        await wait_for_lobby_colors(mock_client, menu_serials, timeout=12.0)
    finally:
        await primary_collector.stop()
        if shadow_collector:
            await shadow_collector.stop()
        await mock_channel.close()
        await game_channel.close()
