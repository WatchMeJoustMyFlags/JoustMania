"""
Press latch for the button path (#2312, UC-010 BR-012).

The HID stream thread records every sample, but the 100 Hz state loop only polls the
latest one, and ButtonDetector finds edges between two polls. A tap that starts and
ends between two polls (both samples arrive in one 10 ms window) used to leave no
trace: poll() saw "released" twice and no event reached the menu. Every button seen
down since the previous poll now reads as pressed in that poll, and the next poll
shows the real (released) state, so the tap yields a press and a release event.
"""

import queue
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))

from lib.controller_constants import ButtonKey, normalize_serial
from proto import controller_manager_pb2, psmove_hid_pb2
from services.controller_manager.button_detector import ButtonDetector
from services.controller_manager.multiplexer.python_hid_adapter import PythonHidAdapter
from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter
from services.controller_manager.tests.backend_doubles import autospec_channel, autospec_publisher
from services.controller_manager.tests.fake_stream_call import FakeStreamCall

HID_SERIAL = normalize_serial("AA:BB:CC:DD:EE:01")

PRESS = controller_manager_pb2.ACTION_PRESS
RELEASE = controller_manager_pb2.ACTION_RELEASE
TRIGGER = controller_manager_pb2.BUTTON_TRIGGER
MOVE = controller_manager_pb2.BUTTON_MOVE


def _bare_hid(adapter_cls):
    adapter = adapter_cls.__new__(adapter_cls)
    adapter._init_stream_state()
    adapter._channel = autospec_channel()
    adapter._stub = MagicMock()
    adapter._latest_data = {}
    adapter._last_state = {}
    adapter._trigger_latch = set()
    adapter._data_lock = threading.Lock()
    adapter._closed = set()
    adapter._command_queue = queue.Queue(maxsize=256)
    adapter._adapter_names = {}
    return adapter


def _stream(adapter, *samples):
    """Deliver ``samples`` through the adapter's real stream reader, as rust-hid / python-hid would."""
    adapter._stub.StreamIO.return_value = FakeStreamCall(samples)
    adapter._stream_running = True
    adapter._stream_loop()


def _sample(trigger=False, move=False, cross=False):
    return psmove_hid_pb2.SensorData(serial=HID_SERIAL, button_trigger=trigger, button_move=move, button_cross=cross)


def _poll_events(adapter, detector, publisher):
    """One 100 Hz poll pass: poll the adapter, detect edges, return the (button, action) events it published."""
    publisher.publish_to_subscribers.reset_mock()
    detector.detect_transitions_from_state(HID_SERIAL, adapter.poll(HID_SERIAL), {})
    return sorted((c.args[1].button, c.args[1].action) for c in publisher.publish_to_subscribers.call_args_list)


@pytest.fixture
def detector():
    publisher = autospec_publisher()
    button_detector = ButtonDetector(publisher)
    button_detector._subscribers = {"menu": MagicMock()}
    with patch("services.controller_manager.button_detector.metrics"):
        yield button_detector, publisher


@pytest.mark.parametrize("adapter_cls", [RustServiceAdapter, PythonHidAdapter], ids=["rust", "python_hid"])
class TestUC010ShortTapBetweenPolls:
    @pytest.mark.uc("UC-010", flow="A1", br=["BR-012"])
    def test_tap_of_trigger_and_move_between_two_polls_gives_press_and_release(self, adapter_cls, detector):
        """#2312: a 100 ms tap whose press and release both land between two polls is not lost."""
        button_detector, publisher = detector
        adapter = _bare_hid(adapter_cls)
        _stream(adapter, _sample())
        assert _poll_events(adapter, button_detector, publisher) == []  # tracking starts, all up

        # The tap: down, then up again, both before the next poll picks up the latest sample.
        _stream(adapter, _sample(trigger=True, move=True), _sample(trigger=True, move=True), _sample())

        assert _poll_events(adapter, button_detector, publisher) == sorted([(TRIGGER, PRESS), (MOVE, PRESS)])
        assert _poll_events(adapter, button_detector, publisher) == sorted([(TRIGGER, RELEASE), (MOVE, RELEASE)])
        assert _poll_events(adapter, button_detector, publisher) == []

    @pytest.mark.uc("UC-010", br=["BR-012"])
    def test_a_held_button_is_one_press_and_a_released_one_stays_up(self, adapter_cls, detector):
        button_detector, publisher = detector
        adapter = _bare_hid(adapter_cls)
        _stream(adapter, _sample())
        _poll_events(adapter, button_detector, publisher)

        _stream(adapter, _sample(cross=True))
        assert _poll_events(adapter, button_detector, publisher) == [(controller_manager_pb2.BUTTON_CROSS, PRESS)]
        _stream(adapter, _sample(cross=True), _sample(cross=True))
        assert _poll_events(adapter, button_detector, publisher) == []  # still held: no second press
        _stream(adapter, _sample())
        assert _poll_events(adapter, button_detector, publisher) == [(controller_manager_pb2.BUTTON_CROSS, RELEASE)]
        assert _poll_events(adapter, button_detector, publisher) == []

    @pytest.mark.uc("UC-010", br=["BR-012"])
    def test_the_latched_press_never_leaks_into_the_cached_state(self, adapter_cls):
        """The merged poll result is a copy: the cached last state keeps the real (released) sample."""
        adapter = _bare_hid(adapter_cls)
        _stream(adapter, _sample(trigger=True), _sample())

        merged = adapter.poll(HID_SERIAL)

        assert merged[ButtonKey.TRIGGER] is True
        assert adapter._last_state[HID_SERIAL][ButtonKey.TRIGGER] is False
        assert adapter.poll(HID_SERIAL)[ButtonKey.TRIGGER] is False

    @pytest.mark.uc("UC-010", br=["BR-012"])
    def test_close_and_close_all_drop_the_latch(self, adapter_cls):
        adapter = _bare_hid(adapter_cls)
        adapter._stub.Close = MagicMock()
        _stream(adapter, _sample(move=True), _sample())
        adapter.close(HID_SERIAL)
        assert HID_SERIAL not in adapter._press_latch

        _stream(adapter, _sample(move=True), _sample())  # closed serial: nothing is recorded
        assert HID_SERIAL not in adapter._press_latch

        adapter._closed.clear()
        _stream(adapter, _sample(move=True), _sample())
        adapter._stub.CloseAll = MagicMock()
        adapter.close_all()
        assert adapter._press_latch == {}
