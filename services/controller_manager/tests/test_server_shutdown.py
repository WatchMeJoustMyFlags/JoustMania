"""Server shutdown releases the HID backend after the gRPC servers stopped (#1726)."""

from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import grpc.aio
import pytest

from services.controller_manager.backend import ControllerBackend
from services.controller_manager.server import shutdown_services


def _servicer_with_call_log(calls):
    """Real servicer class with autospecced collaborators, logging call order."""
    with (
        patch("services.controller_manager.servicer.create_backend") as create_backend,
        patch("services.controller_manager.servicer.DiscoveryLoop") as discovery_cls,
    ):
        backend = create_autospec(ControllerBackend, instance=True, spec_set=True)
        backend.shutdown.side_effect = lambda: calls.append("backend.shutdown")
        create_backend.return_value = backend
        loop = MagicMock()
        loop.stop.side_effect = lambda: calls.append("discovery.stop")
        loop.wait_stopped = AsyncMock()
        discovery_cls.return_value = loop

        from services.controller_manager.servicer import ControllerManagerServicer

        return ControllerManagerServicer()


def _server(calls, name):
    server = create_autospec(grpc.aio.Server, instance=True, spec_set=True)
    server.stop.side_effect = lambda **_kwargs: calls.append(f"{name}.stop")
    return server


@pytest.mark.asyncio
async def test_backend_shutdown_runs_once_after_servers_stop():
    calls = []
    servicer = _servicer_with_call_log(calls)

    await shutdown_services(servicer, _server(calls, "server"), _server(calls, "mock"))

    assert calls == ["discovery.stop", "server.stop", "mock.stop", "backend.shutdown"]


@pytest.mark.asyncio
async def test_second_shutdown_does_not_close_backend_again():
    calls = []
    servicer = _servicer_with_call_log(calls)
    server = _server(calls, "server")

    await shutdown_services(servicer, server)
    await shutdown_services(servicer, server)

    assert calls.count("backend.shutdown") == 1


@pytest.mark.asyncio
async def test_backend_closes_even_when_server_stop_raises():
    calls = []
    servicer = _servicer_with_call_log(calls)
    server = _server(calls, "server")
    server.stop.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await shutdown_services(servicer, server)

    assert calls[-1] == "backend.shutdown"
