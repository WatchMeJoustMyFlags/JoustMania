"""TC-006 step 5 on the service tier (T2): the permission the host set from the controller blocks the agent (#1600).

AGENT composition on ``fresh_stack`` (the ladder writes the GLOBAL
``interventions_allowed``): real menu, game coordinator and controller manager
from source at HEAD, the Go agent built at HEAD, a fresh flag plane, FakeAudio,
the OtlpSink and the fake OpenAI backend scripted to act at high confidence.

The host restricts the agent from the controller in the lobby (standard ->
ambient -> none, the TC-006 steps 1 to 3 seam: mock button -> menu admin handler
-> flag service admin value -> flagd), then a JoustFFA game starts. Step 5
(UC-040 A14): the agent's cycles for that game send no request to the fake and
record no ``agent.decide`` span, while its call gate proves it is evaluating the
game (``agent_llm_gated_total{reason=no_interventions_allowed}`` grows).

It also carries UC-013 main step 5: the agent's next decision uses the host's write (no call), and the
coordinator's next check does too (an eliminate_player event appended to the game is blocked not_allowed and
the victim stays alive). The clamp of step 3 is not exercised here.

Steps 1 to 4 in full (both ends of the ladder, voice feedback) are the LOBBY
journey ``test_tc006_agent_ladder.py`` (#1598); steps 6 and 7 are the
coordinator half of the test here (#1422). Deviation from TC-006: the host
restricts before the game starts and then plays in it (the menu's admin mode
runs in the lobby only), so the host's controller is one of the four players.
"""

import pytest
from openfeature.evaluation_context import EvaluationContext

from lib.colors import Colors
from proto import controller_manager_mock_pb2, game_coordinator_pb2
from tests.integration.flag_service import append_event, variant_value
from tests.integration.flag_service import default as flag_service_default
from tests.integration.helpers import (
    GameEventCollector,
    async_poll_until,
    enter_admin_mode,
    exit_admin_mode,
    flagd_probe_client,
    get_controller_color,
    get_game_client,
    get_mock_client,
    press_admin_button,
    select_admin_option,
)
from tests.service.agent_flow import (
    AGENT,
    AGENT_SERVICE,
    FIRST_CALL_TIMEOUT,
    HIGH_CONFIDENCE,
    SCRIPTED_INTERVENTION,
    configure_agent,
    decide_spans,
    wait_agent_flags_served,
)
from tests.service.harness import AGENT as AGENT_COMPOSITION
from tests.service.lobby_flow import ready_up_until_started, start_lobby, wait_running

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = AGENT_COMPOSITION

FLAG = "interventions_allowed"
START_LEVEL = "standard"  # TC-006 precondition
PERSIST_TIMEOUT_SECONDS = 5.0
# TC-006 step 5 test data: the agent's next cycles over a 10 s window.
NO_CALL_WINDOW_SECONDS = 10.0
GATED_METRIC = "agent_llm_gated_total"
GATED_REASON = {"reason": "no_interventions_allowed"}

_Button = controller_manager_mock_pb2.ButtonRequest.Button


def _blocked_not_allowed(collector, victim: str) -> bool:
    return any(
        e.data.get("type") == "eliminate_player"
        and e.data.get("target") == victim
        and e.data.get("blocked") == "true"
        and e.data.get("block_reason") == "not_allowed"
        for e in collector.get_events("agent_intervention")
    )


async def _victim_alive(game_client, game_id: str, serial: str) -> bool:
    resp = await game_client.GetGameState(game_coordinator_pb2.GetGameStateRequest(game_id=game_id))
    return any(p.alive for p in resp.game_info.players if p.serial == serial)


def _persisted_admin_variant() -> str | None:
    admin = flag_service_default().get(AGENT, FLAG).get("admin_value")
    return admin.get("variant") if admin else None


def _gated_count(stack) -> float:
    points = stack.otlp_sink.metric_points(GATED_METRIC, GATED_REASON, service=AGENT_SERVICE)
    return points[-1].value if points else 0.0


async def _restrict_from_controller(stack, mock_client, host: str) -> None:
    """TC-006 steps 1 to 4: admin combo, agent_control, Start twice; flagd serves none.

    The second press follows as soon as the flag service holds ``ambient``; the menu steps from the
    flag service's view (UC-013 BR-003, #2095). On this tier flagd usually catches up before the
    second press, so this test does NOT reproduce that race: it is pinned by the unit test
    ``services/menu/tests/test_admin_handler.py::TestAgentLadderAgainstTheFlagService::
    test_second_press_steps_from_a_timed_out_write_that_applied``. flagd is awaited after the last press only.
    """
    await enter_admin_mode(mock_client, host)
    # Move x3 reaches agent_control (index 0 -> 3); the LED colour confirms it (#1845).
    await select_admin_option(
        lambda: press_admin_button(mock_client, host, _Button.MOVE),
        lambda: get_controller_color(mock_client, host),
        Colors.Purple.value,
    )
    for level in ("ambient", "none"):
        await press_admin_button(mock_client, host, _Button.START)
        assert await async_poll_until(
            lambda level=level: _persisted_admin_variant() == level, timeout=PERSIST_TIMEOUT_SECONDS
        ), f"after Start: persisted {FLAG} is {_persisted_admin_variant()!r}, expected {level!r}"
    await exit_admin_mode(mock_client, host)

    expected = variant_value(AGENT, FLAG, "none")
    with flagd_probe_client(stack, AGENT) as probe:
        assert await async_poll_until(
            lambda: probe.get_string_value(FLAG, "unset", EvaluationContext()) == expected,
            timeout=PERSIST_TIMEOUT_SECONDS,
        ), f"flagd did not serve {FLAG}=none"


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A14", tc="TC-006")
@pytest.mark.uc("UC-013", flow="main", steps=[5], tc="TC-006")
@pytest.mark.uc("UC-045", flow="A2", tc="TC-006")
@pytest.mark.uc("UC-040", flow="A22", tc="TC-006")
@pytest.mark.usefixtures("flag_service")
async def test_agent_makes_no_remote_call_after_host_restricts_it(fresh_stack):
    """With interventions_allowed=none set by the host from the controller, the agent
    sends no request to the remote backend and records no agent.decide span for the game."""
    stack = fresh_stack
    fake = stack.fake_openai
    fake.script("valid", intervention=SCRIPTED_INTERVENTION, confidence=HIGH_CONFIDENCE)
    configure_agent(backend="openai", allowed=START_LEVEL)
    wait_agent_flags_served(
        stack, backend="openai", enabled=True, rewrite=lambda: configure_agent(backend="openai", allowed=START_LEVEL)
    )

    game_client, game_channel = await get_game_client(stack)
    mock_client, mock_channel = await get_mock_client(stack)
    try:
        # Menu restart + mode select BEFORE the controllers are added (#1575), lobby colours shown.
        serials = await start_lobby(stack, count=4)
        await _restrict_from_controller(stack, mock_client, host=serials[0])

        collector = GameEventCollector(game_client)
        async with collector:
            game_id = await ready_up_until_started(stack, serials, collector, known_ids=set())
            await wait_running(game_client, game_id)

            # The agent evaluates the game and its call gate skips the remote call.
            await stack.otlp_sink.wait_metric_async(
                GATED_METRIC, GATED_REASON, service=AGENT_SERVICE, timeout=FIRST_CALL_TIMEOUT
            )
            gated_at_start = _gated_count(stack)

            # Step 5: over the window, no request and no agent.decide span for the game.
            def violation() -> bool:
                return bool(fake.requests(game_id)) or bool(decide_spans(stack, game_id))

            assert not await async_poll_until(violation, NO_CALL_WINDOW_SECONDS), (
                f"the agent called the backend or decided for {game_id} under {FLAG}=none: "
                f"requests {fake.requests(game_id)}, "
                f"agent.decide {[s.attributes for s in decide_spans(stack, game_id)]}"
            )
            # The window covered live cycles: the gate kept skipping, it was not idle.
            assert _gated_count(stack) > gated_at_start, (
                f"{GATED_METRIC}{GATED_REASON} did not grow over the window: the agent did not evaluate {game_id}"
            )
            # Validation 1: nothing reached the game.
            applied = [e.data for e in collector.get_events("agent_intervention") if e.data.get("blocked") == "false"]
            assert not applied, f"unblocked agent_intervention under {FLAG}=none: {applied}"
            assert _persisted_admin_variant() == "none"

            # Step 6: an intervention that does reach the coordinator (appended to its
            # source like the agent would) ...
            victim = serials[1]
            assert await _victim_alive(game_client, game_id, victim), "the victim was not alive before the event"
            append_event("eliminate_player", game_id, {"serial": victim})
            # Step 7: ... is rejected by the coordinator's own check and the victim stays alive.
            assert await async_poll_until(lambda: _blocked_not_allowed(collector, victim), timeout=15.0), (
                "no blocked/not_allowed agent_intervention for eliminate_player under none: "
                f"{[e.data for e in collector.get_events('agent_intervention')]}"
            )
            assert await _victim_alive(game_client, game_id, victim), "the blocked intervention still killed the player"
    finally:
        await mock_channel.close()
        await game_channel.close()
