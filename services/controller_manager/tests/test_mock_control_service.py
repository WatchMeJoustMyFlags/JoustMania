"""
Unit tests for MockControllerService RPC handlers.
"""

import sys
from pathlib import Path

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from proto import controller_manager_mock_pb2
from services.controller_manager.mock_control_service import MockControllerService
from services.controller_manager.multiplexer.mock_adapter import MockAdapter


def _make_service(num_controllers: int = 0) -> tuple[MockControllerService, MockAdapter]:
    """Create a MockControllerService backed by a MockAdapter."""
    adapter = MockAdapter(num_controllers=num_controllers)
    adapter.discover()
    return MockControllerService(adapter), adapter


class TestAddController:
    def test_add_controller_auto_serial(self):
        service, adapter = _make_service()
        request = controller_manager_mock_pb2.AddControllerRequest()
        response = service.AddController(request, None)
        assert response.success
        assert response.serial == "MOCK0000"
        assert "MOCK0000" in adapter.controllers

    def test_add_controller_with_serial(self):
        service, adapter = _make_service()
        request = controller_manager_mock_pb2.AddControllerRequest(serial="MY_CTRL")
        response = service.AddController(request, None)
        assert response.success
        assert response.serial == "MY_CTRL"
        assert "MY_CTRL" in adapter.controllers

    def test_add_controller_duplicate_returns_existing(self):
        service, _adapter = _make_service()
        request = controller_manager_mock_pb2.AddControllerRequest(serial="DUP")
        service.AddController(request, None)
        response = service.AddController(request, None)
        assert response.success
        assert response.serial == "DUP"


class TestRemoveController:
    def test_remove_existing_controller(self):
        service, adapter = _make_service()
        adapter.add_controller("TO_REMOVE")
        request = controller_manager_mock_pb2.RemoveControllerRequest(serial="TO_REMOVE")
        response = service.RemoveController(request, None)
        assert response.success
        assert "TO_REMOVE" not in adapter.controllers

    def test_remove_nonexistent_controller(self):
        service, _adapter = _make_service()
        request = controller_manager_mock_pb2.RemoveControllerRequest(serial="NOPE")
        response = service.RemoveController(request, None)
        assert not response.success
        assert "not found" in response.error


class TestAddControllers:
    def test_add_multiple_controllers(self):
        service, adapter = _make_service()
        request = controller_manager_mock_pb2.AddControllersRequest(count=3)
        response = service.AddControllers(request, None)
        assert response.success
        assert len(response.serials) == 3
        assert len(adapter.controllers) == 3

    def test_add_zero_controllers(self):
        service, adapter = _make_service()
        request = controller_manager_mock_pb2.AddControllersRequest(count=0)
        response = service.AddControllers(request, None)
        assert response.success
        assert len(response.serials) == 0
        assert len(adapter.controllers) == 0

    def test_add_controllers_incremental(self):
        service, adapter = _make_service()
        # Add 2, then 3 more
        service.AddControllers(controller_manager_mock_pb2.AddControllersRequest(count=2), None)
        response = service.AddControllers(controller_manager_mock_pb2.AddControllersRequest(count=3), None)
        assert response.success
        assert len(response.serials) == 3
        assert len(adapter.controllers) == 5
