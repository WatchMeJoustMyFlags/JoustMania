"""A failed or timed-out rust-hid Discover is not "no controllers" (UC-002 A1, #2125).

Real DiscoveryLoop + MultiplexerBackend + RustServiceAdapter; only the rust-hid
stub and the loop's other collaborators are mocked.
"""

import queue
import threading
from unittest.mock import MagicMock, create_autospec

import grpc
import pytest

from lib.controller_constants import StateKey
from services.controller_manager.button_detector import ButtonDetector
from services.controller_manager.discovery import PeriodicRescanTimer
from services.controller_manager.discovery_loop import DiscoveryLoop
from services.controller_manager.event_publisher import EventPublisher
from services.controller_manager.feedback_manager import FeedbackManager
from services.controller_manager.monitoring import ControllerMonitoring
from services.controller_manager.multiplexer.multiplexer_backend import (
    MAX_FAILED_DISCOVERY_PASSES,
    MultiplexerBackend,
)
from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter

SERIALS = ["AABBCCDDEE01", "AABBCCDDEE02"]


def _rust_adapter() -> RustServiceAdapter:
    adapter = RustServiceAdapter.__new__(RustServiceAdapter)
    adapter._channel = MagicMock()
    adapter._stub = MagicMock()
    adapter._command_queue = queue.Queue(maxsize=256)
    adapter._latest_data = {}
    adapter._trigger_latch = set()
    adapter._data_lock = threading.Lock()
    adapter._closed = set()
    adapter._stream_thread = None
    adapter._stream_running = True  # no stream thread in this test
    adapter._adapter_names = {}
    # A sample per serial, as the StreamIO stream would have delivered it.
    adapter._last_state = {serial: {StateKey.SERIAL: serial, StateKey.BATTERY: 4} for serial in SERIALS}
    return adapter


def _answer(serials):
    response = MagicMock()
    controllers = []
    for serial in serials:
        ctrl = MagicMock()
        ctrl.serial = serial
        ctrl.adapter_name = "hci0"
        controllers.append(ctrl)
    response.controllers = controllers
    return response


def _timeout():
    err = grpc.RpcError()
    err.code = lambda: grpc.StatusCode.DEADLINE_EXCEEDED
    return err


class _Harness:
    def __init__(self):
        self.rust = _rust_adapter()
        self.backend = MultiplexerBackend(adapters=[self.rust])
        self.button_detector = create_autospec(ButtonDetector, instance=True)
        rescan_timer = create_autospec(PeriodicRescanTimer, instance=True)
        rescan_timer.should_force_rescan.return_value = False
        self.tracked: dict = {}
        self.loop = DiscoveryLoop(
            backend=self.backend,
            tracked_controllers=self.tracked,
            controller_states={},
            button_detector=self.button_detector,
            feedback_manager=create_autospec(FeedbackManager, instance=True),
            monitoring=create_autospec(ControllerMonitoring, instance=True),
            rescan_timer=rescan_timer,
            paired_serials=[],
            base_colors={},
            event_publisher=create_autospec(EventPublisher, instance=True),
        )
        self.loop.backend_initialized = True

    async def pass_with(self, outcome):
        if isinstance(outcome, Exception):
            self.rust._stub.Discover.side_effect = outcome
        else:
            self.rust._stub.Discover.side_effect = None
            self.rust._stub.Discover.return_value = _answer(outcome)
        await self.loop._check_for_new_controllers()

    def disconnects_published(self) -> list[str]:
        return [
            c.args[0]
            for c in self.button_detector.publish_connection_event.call_args_list
            if c.kwargs.get("is_connect") is False
        ]


@pytest.mark.uc("UC-002", flow="A1")
class TestFailedDiscoveryPass:
    @pytest.mark.asyncio
    async def test_one_timeout_mid_game_drops_nobody(self):
        h = _Harness()
        await h.pass_with(SERIALS)
        assert sorted(h.tracked) == SERIALS

        await h.pass_with(_timeout())

        assert sorted(h.tracked) == SERIALS
        assert sorted(h.backend._serial_to_adapter) == SERIALS
        assert h.loop._gameplay_disconnects == []
        assert h.disconnects_published() == []

    @pytest.mark.asyncio
    async def test_consecutive_failures_drop_the_controllers(self):
        h = _Harness()
        await h.pass_with(SERIALS)

        for _ in range(MAX_FAILED_DISCOVERY_PASSES - 1):
            await h.pass_with(_timeout())
        assert sorted(h.tracked) == SERIALS

        await h.pass_with(_timeout())

        assert MAX_FAILED_DISCOVERY_PASSES == 3
        assert h.tracked == {}
        assert sorted(h.loop._gameplay_disconnects) == SERIALS
        assert sorted(h.disconnects_published()) == SERIALS

    @pytest.mark.asyncio
    async def test_a_successful_pass_resets_the_count(self):
        h = _Harness()
        await h.pass_with(SERIALS)

        for _ in range(MAX_FAILED_DISCOVERY_PASSES - 1):
            await h.pass_with(_timeout())
        await h.pass_with(SERIALS)
        for _ in range(MAX_FAILED_DISCOVERY_PASSES - 1):
            await h.pass_with(_timeout())

        assert sorted(h.tracked) == SERIALS
        assert h.loop._gameplay_disconnects == []

    @pytest.mark.asyncio
    async def test_an_empty_answer_still_drops_at_once(self):
        """An answer with no controllers is a real disappearance, not a failure."""
        h = _Harness()
        await h.pass_with(SERIALS)

        await h.pass_with([])

        assert h.tracked == {}
        assert sorted(h.loop._gameplay_disconnects) == SERIALS
