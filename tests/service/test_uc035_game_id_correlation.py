"""UC-035 Post-S-1: one game id correlates a game's traces, metrics and logs (#1650).

A three-player JoustFFA game runs headless with the OTel SDK on, exporting to the
stack's ``OtlpSink`` (the collector's role). The game id is on the game's spans
(``game.id``) and on its metrics (``game_id``); log lines carry no game id of
their own, so they correlate through the trace id the log handler stamps on every
line logged inside the game's spans. The sink must receive coordinator log lines
under the game-session span's trace.

Scope: the collector and Grafana are not in the T2 tier. This checks what
the services emit; that the backends keep the ids is the contract tests' and
the full stack's part.
"""

import dataclasses

import pytest

from proto import controller_manager_pb2, game_coordinator_pb2
from tests.integration.helpers import (
    async_poll_until,
    build_start_config,
    get_controller_client,
    get_game_client,
    setup_mock_controllers,
    start_game_headless,
)
from tests.service.harness import GAME

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = dataclasses.replace(GAME, otlp=True)

TAG = "uc035-correlation"
GAME_MODE = "JoustFFA"
COORDINATOR = "game-coordinator-service"
EVENT_TIMEOUT_SECONDS = 15.0
SPAN_TIMEOUT_SECONDS = 30.0


@pytest.mark.uc("UC-035", flow="main", steps=[7], cond=["Post-S-1"])
async def test_game_id_correlates_spans_metrics_and_logs(stack, headless_cleanup):
    sink = stack.otlp_sink
    assert sink is not None

    serials = await setup_mock_controllers(stack, count=3, tag=headless_cleanup.add_tag(TAG))
    game_client, game_channel = await get_game_client(stack)
    controller_client, controller_channel = await get_controller_client(stack)
    collector = None
    try:

        async def tracked() -> bool:
            roster = await controller_client.GetConnectedControllers(
                controller_manager_pb2.GetConnectedControllersRequest()
            )
            return set(serials) <= {c.serial for c in roster.controllers}

        assert await async_poll_until(tracked, timeout=EVENT_TIMEOUT_SECONDS), "discovery did not track the controllers"

        game_id, collector = await start_game_headless(game_client, build_start_config(GAME_MODE, serials))
        headless_cleanup.add_game(game_id)
        await sink.wait_metric_async(
            "game_actual_update_frequency_hz", {"game_id": game_id}, service=COORDINATOR, timeout=SPAN_TIMEOUT_SECONDS
        )
        await game_client.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="uc035_test", game_id=game_id))
        await collector.wait_for_event("game_force_ended", timeout=EVENT_TIMEOUT_SECONDS)
    finally:
        if collector is not None:
            await collector.stop()
        await controller_channel.close()
        await game_channel.close()

    root = await sink.wait_span_async(
        "Joust Free-for-All", {"game.id": game_id}, service=COORDINATOR, root=True, timeout=SPAN_TIMEOUT_SECONDS
    )
    # Traces: the game's spans carry the id and include the root's trace.
    game_spans = sink.spans(None, {"game.id": game_id}, service=COORDINATOR)
    in_game_trace = [s for s in game_spans if s.trace_id == root.trace_id]
    assert len(in_game_trace) > 1, "the game id is on more than the root span of the game's trace"

    # Metrics: the coordinator's series for this game carry the same id.
    point = await sink.wait_metric_async(
        "game_trace_correlation", {"game_id": game_id}, service=COORDINATOR, timeout=SPAN_TIMEOUT_SECONDS
    )
    assert point.attributes["game_trace_id"] == root.trace_id

    # Logs: lines logged during the game carry the root span's trace id.
    line = await sink.wait_log_async(service=COORDINATOR, trace_id=root.trace_id, timeout=SPAN_TIMEOUT_SECONDS)
    assert line.body
