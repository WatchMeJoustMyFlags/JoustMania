"""
Unit tests for reserved mock controllers (#777).

Reserved controllers are hidden from button-stream consumers (the menu) at
both leak paths:
  1. connect/disconnect ButtonEvents (initial snapshot AND live discovery path)
  2. the connected_serials roster inside every ButtonEvent

They stay fully usable via gameplay-data streams (per-serial filter) and
explicit-serial LED/feedback commands — suppression is ONLY about
button-stream announcements.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from lib.controller_constants import ControllerInfoKey
from proto import controller_manager_mock_pb2, controller_manager_pb2
from services.controller_manager.discovery_loop import visible_serials
from services.controller_manager.mock_control_service import MockControllerService
from services.controller_manager.multiplexer.mock_adapter import MockAdapter
from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend


def _make_service() -> tuple[MockControllerService, MockAdapter]:
    adapter = MockAdapter(num_controllers=0)
    adapter.discover()
    return MockControllerService(adapter), adapter


# --------------------------------------------------------------------------
# Mock adapter + control service: reserved/tag storage and plumbing
# --------------------------------------------------------------------------


class TestMockAdapterReservation:
    def test_add_controller_defaults_unreserved(self):
        """Backward compat: add_controller without new args is unreserved."""
        adapter = MockAdapter(num_controllers=0)
        serial = adapter.add_controller("C1")
        meta = adapter.get_metadata(serial)
        assert meta == {"reserved": False, "tag": ""}

    def test_add_controller_reserved_with_tag(self):
        adapter = MockAdapter(num_controllers=0)
        serial = adapter.add_controller("C1", reserved=True, tag="agent-7")
        meta = adapter.get_metadata(serial)
        assert meta == {"reserved": True, "tag": "agent-7"}

    def test_get_metadata_unknown_returns_none(self):
        adapter = MockAdapter(num_controllers=0)
        assert adapter.get_metadata("NOPE") is None


class TestAddControllerRpcReservation:
    def test_add_controller_reserved_flag_plumbed(self):
        service, adapter = _make_service()
        req = controller_manager_mock_pb2.AddControllerRequest(serial="R1", reserved=True, tag="shadow")
        resp = service.AddController(req, None)
        assert resp.success
        assert adapter.get_metadata("R1") == {"reserved": True, "tag": "shadow"}

    def test_add_controller_backward_compat(self):
        """AddController without reserved/tag behaves exactly as before."""
        service, adapter = _make_service()
        req = controller_manager_mock_pb2.AddControllerRequest(serial="U1")
        resp = service.AddController(req, None)
        assert resp.success
        assert resp.serial == "U1"
        assert adapter.get_metadata("U1") == {"reserved": False, "tag": ""}

    def test_add_controllers_reserved_applied_to_all(self):
        service, adapter = _make_service()
        req = controller_manager_mock_pb2.AddControllersRequest(count=3, reserved=True, tag="batch")
        resp = service.AddControllers(req, None)
        assert resp.success
        assert len(resp.serials) == 3
        for serial in resp.serials:
            assert adapter.get_metadata(serial) == {"reserved": True, "tag": "batch"}

    def test_add_controllers_backward_compat(self):
        service, adapter = _make_service()
        req = controller_manager_mock_pb2.AddControllersRequest(count=2)
        resp = service.AddControllers(req, None)
        assert resp.success
        for serial in resp.serials:
            assert adapter.get_metadata(serial) == {"reserved": False, "tag": ""}


class TestListMockControllers:
    def test_reports_reserved_and_tag(self):
        service, _ = _make_service()
        service.AddController(controller_manager_mock_pb2.AddControllerRequest(serial="U1"), None)
        service.AddController(
            controller_manager_mock_pb2.AddControllerRequest(serial="R1", reserved=True, tag="agent-1"), None
        )

        resp = service.ListMockControllers(controller_manager_mock_pb2.ListRequest(), None)

        # Legacy fields preserved
        assert set(resp.serials) == {"U1", "R1"}
        assert resp.count == 2

        by_serial = {info.serial: info for info in resp.controllers}
        assert by_serial["U1"].reserved is False
        assert by_serial["U1"].tag == ""
        assert by_serial["R1"].reserved is True
        assert by_serial["R1"].tag == "agent-1"


# --------------------------------------------------------------------------
# visible_serials helper
# --------------------------------------------------------------------------


class TestVisibleSerials:
    def test_excludes_reserved(self):
        tracked = {
            "U1": {ControllerInfoKey.RESERVED: False},
            "R1": {ControllerInfoKey.RESERVED: True},
            "U2": {},  # missing key defaults to not-reserved
        }
        assert set(visible_serials(tracked)) == {"U1", "U2"}

    def test_empty(self):
        assert visible_serials({}) == []


# --------------------------------------------------------------------------
# Reserved controllers stay usable: gameplay stream filter + LED by serial
# --------------------------------------------------------------------------


class TestReservedRemainsUsable:
    @pytest.mark.asyncio
    async def test_reserved_motion_streams_with_filter(self):
        """A reserved controller still polls motion via the backend (used by
        StreamGameplayData when included in the per-serial filter)."""
        adapter = MockAdapter(num_controllers=0)
        backend = MultiplexerBackend([adapter])
        await backend.initialize()
        adapter.add_controller("R1", reserved=True, tag="shadow")
        # Make the new controller routable
        backend.get_connected_controllers(force_rescan=True)

        state = await backend.get_controller_state("R1")
        assert state is not None
        assert state["serial"] == "R1"
        # Metadata still flags it reserved
        assert backend.get_controller_metadata("R1") == {"reserved": True, "tag": "shadow"}

    @pytest.mark.asyncio
    async def test_reserved_led_set_by_serial(self):
        """Explicit-serial LED write works on a reserved controller and
        GetColor reflects it."""
        service, adapter = _make_service()
        adapter.add_controller("R1", reserved=True, tag="shadow")

        backend = MultiplexerBackend([adapter])
        await backend.initialize()
        backend.get_connected_controllers(force_rescan=True)

        ok = await backend.set_led_color("R1", 10, 20, 30)
        assert ok

        # Mock GetColor reflects the write
        resp = service.GetColor(controller_manager_mock_pb2.GetColorRequest(serial="R1"), None)
        assert resp.success
        assert (resp.r, resp.g, resp.b) == (10, 20, 30)


# --------------------------------------------------------------------------
# Servicer suppression: initial connection events + roster
# --------------------------------------------------------------------------


@pytest.fixture
def servicer():
    """Create a servicer with a mocked backend and discovery loop."""
    from services.controller_manager.tests.test_servicer import MockBackend

    with (
        patch("services.controller_manager.servicer.create_backend") as mock_create_backend,
        patch("services.controller_manager.servicer.DiscoveryLoop") as mock_discovery_loop,
    ):
        mock_create_backend.return_value = MockBackend()
        mock_loop = MagicMock()
        mock_loop.start = MagicMock()
        mock_loop.stop = MagicMock()
        mock_loop.wait_stopped = AsyncMock()
        mock_discovery_loop.return_value = mock_loop

        from services.controller_manager.servicer import ControllerManagerServicer

        yield ControllerManagerServicer()


class TestInitialConnectionSuppression:
    @pytest.mark.asyncio
    async def test_only_unreserved_announced_and_in_roster(self, servicer):
        """2 reserved + 2 unreserved: subscriber gets connect events for only
        the 2 unreserved, and every roster contains only unreserved serials."""
        servicer.tracked_controllers["U1"] = {
            ControllerInfoKey.BATTERY: 50,
            ControllerInfoKey.NAME: "P1",
            ControllerInfoKey.RESERVED: False,
        }
        servicer.tracked_controllers["U2"] = {
            ControllerInfoKey.BATTERY: 60,
            ControllerInfoKey.NAME: "P2",
            ControllerInfoKey.RESERVED: False,
        }
        servicer.tracked_controllers["R1"] = {
            ControllerInfoKey.BATTERY: 70,
            ControllerInfoKey.NAME: "S1",
            ControllerInfoKey.RESERVED: True,
        }
        servicer.tracked_controllers["R2"] = {
            ControllerInfoKey.BATTERY: 80,
            ControllerInfoKey.NAME: "S2",
            ControllerInfoKey.RESERVED: True,
        }

        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        await servicer._send_initial_connection_events("sub_1", queue)

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())

        announced = {e.serial for e in events}
        assert announced == {"U1", "U2"}

        for event in events:
            assert event.event_type == controller_manager_pb2.EVENT_CONNECT
            assert set(event.connected_serials) == {"U1", "U2"}

    @pytest.mark.asyncio
    async def test_all_reserved_announces_nothing(self, servicer):
        servicer.tracked_controllers["R1"] = {ControllerInfoKey.RESERVED: True}
        servicer.tracked_controllers["R2"] = {ControllerInfoKey.RESERVED: True}

        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        await servicer._send_initial_connection_events("sub_1", queue)

        assert queue.qsize() == 0


# --------------------------------------------------------------------------
# Discovery loop live connect/disconnect suppression
# --------------------------------------------------------------------------


def _make_discovery_loop(tracked, backend, button_detector):
    """Build a DiscoveryLoop with minimal collaborators for connect/disconnect
    publish-path testing against the real _check_for_new_controllers."""
    from services.controller_manager.discovery_loop import DiscoveryLoop

    loop = DiscoveryLoop(
        backend=backend,
        tracked_controllers=tracked,
        controller_states={},
        button_detector=button_detector,
        state_cache_manager=MagicMock(),
        feedback_manager=MagicMock(),
        monitoring=MagicMock(),
        rescan_timer=MagicMock(should_force_rescan=MagicMock(return_value=False)),
        paired_serials=[],
        base_colors={},
        event_publisher=MagicMock(),
    )
    loop.backend_initialized = True
    return loop


class TestLiveDisconnectSuppression:
    @pytest.mark.asyncio
    async def test_reserved_disconnect_not_published(self):
        """When a reserved controller disconnects, no disconnect ButtonEvent is
        published; the unreserved one is, with a reserved-free roster."""
        button_detector = MagicMock()
        tracked = {
            "U1": {ControllerInfoKey.NAME: "P1", ControllerInfoKey.RESERVED: False},
            "U2": {ControllerInfoKey.NAME: "P2", ControllerInfoKey.RESERVED: False},
            "R1": {ControllerInfoKey.NAME: "S1", ControllerInfoKey.RESERVED: True},
        }
        # Backend reports U2 still connected; U1 and R1 dropped.
        backend = MagicMock()
        backend.get_connected_controllers = MagicMock(return_value=["U2"])
        loop = _make_discovery_loop(tracked, backend, button_detector)

        await loop._check_for_new_controllers()

        # Only the unreserved U1 disconnect was published (R1 suppressed).
        assert button_detector.publish_connection_event.call_count == 1
        call = button_detector.publish_connection_event.call_args
        assert call.args[0] == "U1"
        assert call.kwargs["is_connect"] is False
        assert "R1" not in call.kwargs["connected_serials"]
        assert set(call.kwargs["connected_serials"]) == {"U2"}

    @pytest.mark.asyncio
    async def test_reserved_connect_not_published(self):
        """When a reserved controller appears, no connect ButtonEvent is
        published; an unreserved newcomer is."""
        button_detector = MagicMock()
        tracked: dict = {}
        backend = MagicMock()
        backend.get_connected_controllers = MagicMock(return_value=["U1", "R1"])
        # Adapter type + metadata used by _spawn_controller_process
        backend.get_adapter_type = MagicMock(return_value="mock")
        backend.get_controller_metadata = MagicMock(
            side_effect=lambda s: {
                "reserved": s == "R1",
                "tag": "shadow" if s == "R1" else "",
            }
        )
        backend.get_controller_state = AsyncMock(return_value={"battery": 100})

        loop = _make_discovery_loop(tracked, backend, button_detector)
        loop.feedback_manager.set_controller_color = AsyncMock()

        await loop._check_for_new_controllers()

        # Both tracked, but only U1 announced.
        assert set(loop.tracked_controllers.keys()) == {"U1", "R1"}
        assert loop.tracked_controllers["R1"][ControllerInfoKey.RESERVED] is True
        announced = {
            c.args[0]
            for c in button_detector.publish_connection_event.call_args_list
            if c.kwargs.get("is_connect") is True
        }
        assert announced == {"U1"}
