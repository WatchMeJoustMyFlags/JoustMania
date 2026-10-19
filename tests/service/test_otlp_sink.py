"""OtlpSink on the service tier (#1564): a game's telemetry reaches the sink.

The composition turns the OTel SDK on and points every service's OTLP exporter
at the stack's in-process ``OtlpSink``. A three-player JoustFFA game runs
headless; once its game loop runs one controller is removed (UC-030 A5), then
the game is force-ended. The sink must receive the game's root span (the
game-session span, named after the mode, own trace) and the removed player's
span carrying the ``player_disconnected`` event, plus the coordinator's
trace-correlation gauge. A unit-level check covers the tee to a second receiver.
"""

import dataclasses

import grpc
import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

from proto import controller_manager_mock_pb2, controller_manager_pb2, game_coordinator_pb2
from tests.integration.helpers import (
    async_poll_until,
    build_start_config,
    get_controller_client,
    get_game_client,
    get_mock_client,
    setup_mock_controllers,
    start_game_headless,
)
from tests.service.harness import GAME, ServiceStack
from tests.service.harness.fakes import OtlpSink

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = dataclasses.replace(GAME, otlp=True)

TAG = "otlp-sink"
GAME_MODE = "JoustFFA"
COORDINATOR = "game-coordinator-service"
EVENT_TIMEOUT_SECONDS = 15.0
# The BatchSpanProcessor flushes every otel_bsp_schedule_delay_millis (5 s by default).
SPAN_TIMEOUT_SECONDS = 30.0


@pytest.mark.uc("UC-030", flow="A5")
async def test_game_root_span_and_player_disconnected_reach_the_sink(stack, headless_cleanup):
    sink = stack.otlp_sink
    assert sink is not None

    # Unreserved, so GetConnectedControllers shows when discovery tracks them: only a
    # tracked controller's removal is reported as a disconnect.
    serials = await setup_mock_controllers(stack, count=3, tag=headless_cleanup.add_tag(TAG))
    game_client, game_channel = await get_game_client(stack)
    mock_client, mock_channel = await get_mock_client(stack)
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
        # The countdown's EMA warmup drops gameplay frames, disconnects included: remove
        # the controller once the game loop runs. It sets this gauge every 10 frames.
        await sink.wait_metric_async(
            "game_actual_update_frequency_hz", {"game_id": game_id}, service=COORDINATOR, timeout=SPAN_TIMEOUT_SECONDS
        )

        removed = serials[0]
        response = await mock_client.RemoveController(
            controller_manager_mock_pb2.RemoveControllerRequest(serial=removed)
        )
        assert response.success, response.error
        await collector.wait_for_event("player_death", timeout=EVENT_TIMEOUT_SECONDS)

        player_span = await sink.wait_span_async(
            None,
            {"game.id": game_id, "player.serial": removed},
            service=COORDINATOR,
            event="player_disconnected",
            timeout=SPAN_TIMEOUT_SECONDS,
        )
        disconnected = next(e for e in player_span.events if e.name == "player_disconnected")
        assert disconnected.attributes == {"reason": "controller_disconnect"}

        await game_client.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(reason="otlp_sink_test", game_id=game_id)
        )
        await collector.wait_for_event("game_force_ended", timeout=EVENT_TIMEOUT_SECONDS)
    finally:
        if collector is not None:
            await collector.stop()
        await controller_channel.close()
        await mock_channel.close()
        await game_channel.close()

    # The game-session span ends with the game, so it is exported only now.
    root = await sink.wait_span_async(
        # Pinned literally (docs/telemetry.md): a display-name rename must fail here.
        "Joust Free-for-All",
        {"game.id": game_id},
        service=COORDINATOR,
        root=True,
        timeout=SPAN_TIMEOUT_SECONDS,
    )
    assert root.attributes["game.name"] == GAME_MODE
    assert root.attributes["game.outcome"] == "force_ended"
    assert player_span.trace_id == root.trace_id, "the player span belongs to the game's trace"

    # Metrics flow too, and the trace-correlation gauge carries the live root span's
    # trace id (the is_valid branch that a disabled SDK never runs).
    correlation = await sink.wait_metric_async(
        "game_trace_correlation", {"game_id": game_id}, service=COORDINATOR, timeout=SPAN_TIMEOUT_SECONDS
    )
    assert correlation.attributes["game_trace_id"] == root.trace_id


CHILD_SPAN_ID = bytes(range(9, 17))


def _export_request(span_name: str) -> trace_service_pb2.ExportTraceServiceRequest:
    """One trace: a root span ``span_name`` and its child ``<span_name>.child``."""
    resource = resource_pb2.Resource(
        attributes=[common_pb2.KeyValue(key="service.name", value=common_pb2.AnyValue(string_value="probe"))]
    )
    span = trace_pb2.Span(
        name=span_name,
        trace_id=bytes(range(1, 17)),
        span_id=bytes(range(1, 9)),
        attributes=[common_pb2.KeyValue(key="k", value=common_pb2.AnyValue(int_value=7))],
        events=[trace_pb2.Span.Event(name="ev")],
    )
    child = trace_pb2.Span(
        name=f"{span_name}.child",
        trace_id=span.trace_id,
        span_id=CHILD_SPAN_ID,
        parent_span_id=span.span_id,
        attributes=[common_pb2.KeyValue(key="k", value=common_pb2.AnyValue(int_value=7))],
    )
    return trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(resource=resource, scope_spans=[trace_pb2.ScopeSpans(spans=[span, child])])
        ]
    )


def test_sink_tees_trace_exports_to_another_receiver():
    """No stack: the sink records and forwards; a dead forward target is counted, never raised."""
    downstream = OtlpSink()
    downstream.start()
    tee = OtlpSink(forward_to=f"127.0.0.1:{downstream.port}")
    tee.start()
    dead_tee = OtlpSink(forward_to="127.0.0.1:1")
    dead_tee.start()
    try:
        for sink in (tee, dead_tee):
            with grpc.insecure_channel(f"127.0.0.1:{sink.port}") as channel:
                trace_service_pb2_grpc.TraceServiceStub(channel).Export(_export_request("probe.span"), timeout=5)

        recorded = tee.wait_span("probe.span", {"k": 7}, service="probe", event="ev", root=True, timeout=5)
        assert recorded.trace_id == bytes(range(1, 17)).hex()
        # root= tells a root span from its child (parent_span_id set).
        assert [s.name for s in tee.spans(attrs={"k": 7}, root=True)] == ["probe.span"]
        child = tee.wait_span(None, {"k": 7}, root=False, timeout=5)
        assert (child.name, child.span_id, child.parent_span_id) == (
            "probe.span.child",
            CHILD_SPAN_ID.hex(),
            recorded.span_id,
        )
        forwarded = downstream.wait_span("probe.span", {"k": 7}, timeout=5)
        assert forwarded.span_id == recorded.span_id
        assert len(downstream.spans()) == 2, "the whole request is forwarded, child included"
        assert (tee.forward_ok, tee.forward_errors) == (1, 0)

        assert dead_tee.wait_span("probe.span", timeout=5)
        assert (dead_tee.forward_ok, dead_tee.forward_errors) == (0, 1)
    finally:
        for sink in (dead_tee, tee, downstream):
            sink.stop()


def test_otlp_composition_points_every_service_at_the_sink(tmp_path):
    stack = ServiceStack(SERVICE_COMPOSITION, tmp_path)
    assert isinstance(stack.otlp_sink, OtlpSink)
    assert ServiceStack(GAME, tmp_path).otlp_sink is None, "telemetry stays off by default"

    envs = stack.service_envs(
        {"controller-manager": 40001, "game-coordinator": 40002}, mock_port=40004, audio_port=40005, otlp_port=40006
    )

    for name, env in envs.items():
        assert "OTEL_SDK_DISABLED" not in env, name
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:40006", name
