"""TC-013 step 6-7 on the service tier (T2): ``end_game`` ends only the shadow game it is addressed to (#1404).

LOBBY composition: the real menu, game coordinator and controller manager
(mock backend) from source at HEAD, a fresh flag plane, FakeAudio.

Three games run at once: a real menu game R1 on four lobby controllers and two
headless shadow games S1 and S2, each on its own four reserved controllers. An
``end_game`` event appended to the event list of S1 (the agent's writer request,
services/agent/actions/writer.go) force-ends S1 with reason
``agent_intervention``. S2 and R1 keep running: no ``game_force_ended``, no
``end_game`` intervention, every player still alive (UC-040 A18, UC-027 A3,
UC-040 BR-011).

``test_tc013_shadow_alongside_real.py`` (full stack) proves the same with one
shadow game; this case adds the second shadow game, so a handler that ends
"some other session" (the primary, or every shadow) is caught.
The agent does not run: its own side (writing ``end_game`` from its decide
loop for the deciding game only) is unit-tested in
services/agent/actions/writer_scoping_test.go.
"""

import asyncio

import pytest
from openfeature.evaluation_context import EvaluationContext

from proto import game_coordinator_pb2
from tests.integration.flag_service import append_event, variant_value
from tests.integration.flag_service import default as flag_service_default
from tests.integration.helpers import (
    GameEventCollector,
    async_poll_until,
    build_start_config,
    flagd_probe_client,
    get_game_client,
    setup_mock_controllers,
    start_game_headless,
    wait_until_running,
)
from tests.service.harness import LOBBY
from tests.service.lobby_flow import ready_up_until_started, start_lobby, wait_running

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = LOBBY

S1_TAG = "tc013-end-game-s1"
S2_TAG = "tc013-end-game-s2"
APPLY_TIMEOUT_SECONDS = 20.0
RUNNING_TIMEOUT_SECONDS = 20.0
# Absence window: after S1 has ended, a stray force-end of S2 or R1 would follow
# within one flagd push and one coordinator evaluation; the bounded wait IS the test.
ABSENCE_WINDOW_SECONDS = 3.0


def _events(collector: GameEventCollector, game_id: str, event_type: str) -> list:
    return [e for e in collector.get_events(event_type) if e.game_id == game_id]


def _end_game_interventions(collector: GameEventCollector, game_id: str, *, blocked: str | None = None) -> list:
    return [
        e
        for e in _events(collector, game_id, "agent_intervention")
        if e.data.get("type") == "end_game" and (blocked is None or e.data.get("blocked") == blocked)
    ]


def _summary(collector: GameEventCollector, event_type: str | None = None) -> list:
    return [(e.event_type, dict(e.data)) for e in collector.get_events(event_type)]


async def _serve_full_permission(stack) -> None:
    """end_game is only in the ``full`` allow-list; a stack overlay the flag_service fixture deletes."""
    flag_service_default().put("agent", "interventions_allowed", variant="full")
    expected = variant_value("agent", "interventions_allowed", "full")
    with flagd_probe_client(stack, "agent") as client:
        assert await async_poll_until(
            lambda: client.get_string_value("interventions_allowed", "", EvaluationContext()) == expected
        ), "flagd did not serve interventions_allowed=full"


async def _alive(game_client, game_id: str) -> dict[str, bool]:
    resp = await game_client.GetGameState(game_coordinator_pb2.GetGameStateRequest(game_id=game_id))
    assert resp.success, f"GetGameState({game_id}) failed: {resp.error}"
    return {p.serial: p.alive for p in resp.game_info.players}


async def _start_shadow(stack, game_client, tag: str, headless_cleanup) -> tuple[str, list[str], GameEventCollector]:
    headless_cleanup.add_tag(tag)
    serials = await setup_mock_controllers(stack, count=4, reserved=True, tag=tag)
    game_id, collector = await start_game_headless(
        game_client, build_start_config("JoustFFA", serials), timeout=RUNNING_TIMEOUT_SECONDS
    )
    headless_cleanup.add_game(game_id)
    return game_id, serials, collector


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A18", br=["BR-011"], tc="TC-013")
@pytest.mark.uc("UC-027", flow="A3", tc="TC-013")
async def test_tc013_end_game_ends_only_its_shadow_game(service_stack, flag_service, headless_cleanup):
    """end_game in S1's event list force-ends S1 only; S2 and the real game R1 run on."""
    stack = service_stack
    game_client, channel = await get_game_client(stack)
    primary = GameEventCollector(game_client)  # the menu game's (primary) stream
    shadow1 = shadow2 = None
    try:
        await primary.start()

        # Step 1: the real game R1 from the menu, on four lobby controllers.
        lobby = await start_lobby(stack, count=4)
        r1 = await ready_up_until_started(stack, lobby, primary, known_ids=set())
        await wait_running(game_client, r1)

        # Step 2: two shadow games on reserved controllers, added after the
        # ready-up so the menu only ever saw the lobby controllers.
        s1, s1_serials, shadow1 = await _start_shadow(stack, game_client, S1_TAG, headless_cleanup)
        s2, s2_serials, shadow2 = await _start_shadow(stack, game_client, S2_TAG, headless_cleanup)
        assert len({r1, s1, s2}) == 3, f"game ids collide: {r1}, {s1}, {s2}"
        running = await wait_until_running(game_client, {r1, s1, s2}, timeout=RUNNING_TIMEOUT_SECONDS)
        assert {r1, s1, s2} <= running, f"not all three games RUNNING: {running}"
        assert set(s1_serials).isdisjoint(s2_serials) and set(lobby).isdisjoint(s1_serials + s2_serials)

        await _serve_full_permission(stack)
        for game_id in (r1, s1, s2):
            flag_service.wait_game_registered(game_id)

        # Step 6: end_game appended to S1's event list (UC-040 A18 step 1).
        append_event("end_game", s1)

        # UC-027 A3: S1 force-ends once, reason agent_intervention, through the
        # applied (unblocked) end_game intervention, and declares no winner.
        assert await async_poll_until(lambda: _events(shadow1, s1, "game_force_ended"), APPLY_TIMEOUT_SECONDS), (
            f"S1 was not force-ended by its end_game event; S1 events: {_summary(shadow1)}"
        )
        assert [e.data.get("reason") for e in _events(shadow1, s1, "game_force_ended")] == ["agent_intervention"]
        assert await async_poll_until(
            lambda: _end_game_interventions(shadow1, s1, blocked="false"), APPLY_TIMEOUT_SECONDS
        ), f"no unblocked end_game agent_intervention on S1: {_summary(shadow1, 'agent_intervention')}"

        # Step 7 (UC-040 A18 step 2): S2 and R1 see no new event. Absence window.
        await asyncio.sleep(ABSENCE_WINDOW_SECONDS)
        still_running = await wait_until_running(game_client, set(), timeout=0)
        assert {r1, s2} <= still_running, f"S1's end_game stopped another game: running {still_running}"
        assert s1 not in still_running, f"S1 still RUNNING: {still_running}"
        for collector, game_id, name in ((shadow2, s2, "S2"), (primary, r1, "R1")):
            assert not _events(collector, game_id, "game_force_ended"), f"{name} was force-ended by S1's end_game"
            assert not _end_game_interventions(collector, game_id), f"{name} published an end_game addressed to S1"
            alive = await _alive(game_client, game_id)
            assert alive and all(alive.values()), f"{name} lost a player to S1's end_game: {alive}"
        assert len(_events(shadow1, s1, "game_force_ended")) == 1, "S1 force-ended more than once"
        assert not _events(shadow1, s1, "game_winner"), "a force-ended S1 declared a winner"
    finally:
        for collector in (primary, shadow1, shadow2):
            if collector is not None:
                await collector.stop()
        await channel.close()
