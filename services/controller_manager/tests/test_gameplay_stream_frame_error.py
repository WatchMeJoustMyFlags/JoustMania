"""A per-frame error in StreamGameplayData ends the call non-OK; a normal end stays OK (#1930, UC-030 BR-006)."""

import asyncio
import sys
from pathlib import Path

import grpc
import grpc.aio
import pytest
import pytest_asyncio

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_identify_controller import SERIAL, _game_config, servicer  # noqa: F401  (pytest fixture)

from proto import controller_manager_pb2
from services.controller_manager.tests.backend_doubles import stub_frame_inputs, stub_method


class _AbortedError(Exception):
    pass


class _Context:
    def __init__(self):
        self.cancelled_flag = False
        self.abort_args = None

    def cancelled(self):
        return self.cancelled_flag

    async def abort(self, code, details):
        self.abort_args = (code, details)
        raise _AbortedError(details)


async def _requests():
    yield controller_manager_pb2.GameplayStreamControl(config=_game_config(SERIAL))
    await asyncio.Event().wait()


@pytest.fixture
def stream_servicer(servicer):  # noqa: F811
    stub_method(
        servicer.state_builder,
        "frame_inputs",
        side_effect=lambda serial, _info: stub_frame_inputs(serial=serial),
    )
    servicer.discovery_loop.drain_health_counters.return_value = (0, 0, 0)
    return servicer


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", br=["BR-006"])
async def test_frame_error_ends_the_call_with_internal(stream_servicer):
    """UC-030 BR-006: an unexpected per-frame exception aborts INTERNAL, never ends OK."""
    stub_method(stream_servicer, "_build_gameplay_update", side_effect=RuntimeError("boom"))
    context = _Context()

    with pytest.raises(_AbortedError):
        async for _ in stream_servicer.StreamGameplayData(_requests(), context):
            pass

    code, details = context.abort_args
    assert code == grpc.StatusCode.INTERNAL
    assert "boom" in details


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", br=["BR-006"])
async def test_client_cancel_ends_ok(stream_servicer):
    """UC-030 BR-006: a client cancel ends the stream normally, without abort."""
    context = _Context()
    frames = 0
    async for _ in stream_servicer.StreamGameplayData(_requests(), context):
        frames += 1
        context.cancelled_flag = True

    assert frames >= 1
    assert context.abort_args is None


@pytest.fixture
def release_calls(stream_servicer):
    """Count the stream cleanups (one per call, whatever way it ends)."""
    calls = []
    real_release = stream_servicer._release_game_stream

    def counting_release(subscriber_id):
        calls.append(subscriber_id)
        return real_release(subscriber_id)

    stream_servicer._release_game_stream = counting_release
    return calls


@pytest_asyncio.fixture
async def wire(stream_servicer):
    """A real grpc.aio server with the production interceptors, and a client stub."""
    from lib.grpc_tracing import get_server_interceptors
    from proto import controller_manager_pb2_grpc

    server = grpc.aio.server(interceptors=get_server_interceptors())
    controller_manager_pb2_grpc.add_ControllerManagerServiceServicer_to_server(stream_servicer, server)
    port = server.add_insecure_port("[::]:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"localhost:{port}")
    yield controller_manager_pb2_grpc.ControllerManagerServiceStub(channel)
    await channel.close()
    await server.stop(grace=0)


async def _wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():  # noqa: ASYNC110  (the predicate is a plain list length)
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", br=["BR-006"])
async def test_wire_client_cancel_is_not_a_stream_error(wire, release_calls, caplog):
    """UC-030 BR-006: a real client cancel logs no stream error and cleans up once."""
    caplog.set_level("ERROR")
    call = wire.StreamGameplayData(_requests())
    await call.read()
    call.cancel()
    await _wait_for(lambda: len(release_calls) == 1)
    await asyncio.sleep(0.2)

    assert len(release_calls) == 1
    assert "Gameplay stream error" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", br=["BR-006"])
async def test_wire_frame_error_reaches_the_client_as_internal(stream_servicer, wire, release_calls):
    """UC-030 BR-006: after some frames, a failing frame ends the client's call INTERNAL; cleanup runs once."""
    real_build = stream_servicer._build_gameplay_update
    built = []

    def failing_third_frame(*args, **kwargs):
        if len(built) >= 2:
            raise RuntimeError("boom")
        built.append(1)
        return real_build(*args, **kwargs)

    stream_servicer._build_gameplay_update = failing_third_frame
    call = wire.StreamGameplayData(_requests())
    received = 0
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        async for _ in call:
            received += 1

    assert received == 2
    assert excinfo.value.code() == grpc.StatusCode.INTERNAL
    assert "boom" in excinfo.value.details()
    await _wait_for(lambda: len(release_calls) >= 1)
    await asyncio.sleep(0.2)
    assert len(release_calls) == 1
