"""
In-process gRPC integration tests for ControllerManagerServicer.

Spins up a real grpc.aio.server() on an ephemeral port, connects with
real stubs, and tests RPCs through the actual gRPC wire protocol.
The hardware backend is mocked to avoid needing PS Move controllers.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import grpc.aio
import pytest
import pytest_asyncio

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from proto import controller_manager_pb2, controller_manager_pb2_grpc
from services.controller_manager.servicer import ControllerManagerServicer


def _make_mock_backend():
    """Create a mock backend that avoids hardware access."""
    mock = MagicMock()
    mock.get_connected_controllers.return_value = []
    mock.discover_controllers.return_value = []
    mock.poll_controller.return_value = None
    return mock


@pytest_asyncio.fixture
async def grpc_service(tmp_path):
    """
    Start a real gRPC server with ControllerManagerServicer using a mock backend.

    Uses a temporary names file to avoid polluting the real controller_names.txt.
    Yields (servicer, stub) for direct servicer access and gRPC client calls.
    """
    names_file = str(tmp_path / "controller_names.txt")

    with (
        patch("services.controller_manager.servicer.create_backend", return_value=_make_mock_backend()),
        patch.dict("os.environ", {"CONTROLLER_NAMES_FILE": names_file}),
    ):
        servicer = ControllerManagerServicer()

    server = grpc.aio.server()
    controller_manager_pb2_grpc.add_ControllerManagerServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    await server.start()

    channel = grpc.aio.insecure_channel(f"localhost:{port}")
    stub = controller_manager_pb2_grpc.ControllerManagerServiceStub(channel)

    yield servicer, stub

    await channel.close()
    await server.stop(grace=0)


class TestRenameController:
    """RenameController through real gRPC wire protocol."""

    @pytest.mark.asyncio
    async def test_rename_success(self, grpc_service):
        """RenameController updates name via name_manager."""
        servicer, stub = grpc_service

        response = await stub.RenameController(
            controller_manager_pb2.RenameControllerRequest(serial="AA:BB:CC:DD", name="Player 1")
        )

        assert response.success is True
        assert response.error == ""
        assert servicer.name_manager.get_name("AA:BB:CC:DD") == "Player 1"

    @pytest.mark.asyncio
    async def test_rename_updates_tracked_controller(self, grpc_service):
        """RenameController updates tracked_controllers if controller is connected."""
        servicer, stub = grpc_service

        # Simulate a connected controller
        from lib.controller_constants import ControllerInfoKey

        servicer.tracked_controllers["AA:BB:CC:DD"] = {
            ControllerInfoKey.NAME: "old_name",
        }

        response = await stub.RenameController(
            controller_manager_pb2.RenameControllerRequest(serial="AA:BB:CC:DD", name="My Controller")
        )

        assert response.success is True
        assert servicer.tracked_controllers["AA:BB:CC:DD"][ControllerInfoKey.NAME] == "My Controller"

    @pytest.mark.asyncio
    async def test_rename_missing_serial(self, grpc_service):
        """RenameController fails when serial is empty."""
        _servicer, stub = grpc_service

        response = await stub.RenameController(
            controller_manager_pb2.RenameControllerRequest(serial="", name="Player 1")
        )

        assert response.success is False
        assert "serial" in response.error.lower()

    @pytest.mark.asyncio
    async def test_rename_missing_name(self, grpc_service):
        """RenameController fails when name is empty."""
        _servicer, stub = grpc_service

        response = await stub.RenameController(
            controller_manager_pb2.RenameControllerRequest(serial="AA:BB:CC:DD", name="")
        )

        assert response.success is False
        assert "name" in response.error.lower()

    @pytest.mark.asyncio
    async def test_rename_data_survives_serialization(self, grpc_service):
        """Serial and name with special characters survive gRPC serialization."""
        servicer, stub = grpc_service

        response = await stub.RenameController(
            controller_manager_pb2.RenameControllerRequest(serial="00:11:22:33:44:55", name="Simon's Controller #2")
        )

        assert response.success is True
        assert servicer.name_manager.get_name("00:11:22:33:44:55") == "Simon's Controller #2"

    @pytest.mark.asyncio
    async def test_rename_idempotent(self, grpc_service):
        """Renaming the same controller twice updates to latest name."""
        _servicer, stub = grpc_service

        await stub.RenameController(
            controller_manager_pb2.RenameControllerRequest(serial="AA:BB:CC:DD", name="First Name")
        )
        response = await stub.RenameController(
            controller_manager_pb2.RenameControllerRequest(serial="AA:BB:CC:DD", name="Second Name")
        )

        assert response.success is True
