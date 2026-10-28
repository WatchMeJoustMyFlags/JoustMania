"""
Trigger latch: GameplayData.trigger_pressed (#1837, UC-030 A6 / BR-005).

The controller manager reports the digital trigger as "down now OR pressed since the
last frame delivered to a game stream", so a press that starts and ends between two
frames (15/30 Hz) is not lost. Only game streams clear the latch (like the death latch).
"""

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_observer_stream import SERIAL, rig  # noqa: F401  (pytest fixture)

from lib.controller_constants import ButtonKey, normalize_serial
from proto import controller_manager_pb2, psmove_hid_pb2
from services.controller_manager.multiplexer.mock_adapter import MockAdapter
from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend
from services.controller_manager.multiplexer.python_hid_adapter import PythonHidAdapter
from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter
from services.controller_manager.tests.fake_stream_call import FakeStreamCall

HID_SERIAL = normalize_serial("AA:BB:CC:DD:EE:01")


def _frames(servicer, subscriber_id="game", flt=None):
    update = servicer._build_gameplay_update(flt if flt is not None else {SERIAL}, subscriber_id)
    return [c.trigger_pressed for c in update.controllers]


def _wire(servicer, latched: list[bool], level: bool = False):
    """Servicer whose backend latch is a queue of consume results and whose state level is fixed."""
    backend = MagicMock(spec=["consume_trigger_press"])
    backend.consume_trigger_press.side_effect = lambda _serial: latched.pop(0) if latched else False
    servicer.backend = backend
    state = servicer.state_builder.build_state.return_value
    state.trigger_pressed = level
    return backend


class TestUC030TriggerLatchInGameplayData:
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    def test_press_between_frames_is_not_lost(self, rig):  # noqa: F811
        """UC-030 BR-005: pressed and released between two frames -> exactly one frame reports it."""
        servicer, _ = rig
        _wire(servicer, latched=[False, True, False], level=False)
        servicer._open_game_stream("game", {SERIAL})

        assert [_frames(servicer)[0] for _ in range(3)] == [False, True, False]

    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    def test_a_hold_reads_pressed_every_frame_but_latches_once(self, rig):  # noqa: F811
        """UC-030 BR-005: a hold keeps the level true; the latch is drained once, release reads false."""
        servicer, _ = rig
        backend = _wire(servicer, latched=[True], level=True)
        servicer._open_game_stream("game", {SERIAL})
        held = [_frames(servicer)[0] for _ in range(3)]

        servicer.state_builder.build_state.return_value.trigger_pressed = False
        released = _frames(servicer)[0]

        assert held == [True, True, True]
        assert released is False
        assert backend.consume_trigger_press.call_count == 4  # one drain per game frame

    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    def test_observer_stream_does_not_drain_the_latch(self, rig):  # noqa: F811
        """UC-030 BR-005: a dashboard frame leaves the latch for the game stream."""
        servicer, _ = rig
        backend = _wire(servicer, latched=[True], level=False)
        servicer._open_game_stream("game", {SERIAL})

        observer = servicer._build_gameplay_update(None, "dashboard")
        assert observer.controllers[0].trigger_pressed is False
        assert backend.consume_trigger_press.call_count == 0

        assert _frames(servicer) == [True]


class TestUC030MockTriggerLatch:
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    def test_mock_latch_is_consumed_once(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        serial = next(iter(adapter.controllers))

        assert adapter.consume_trigger_press(serial) is False
        assert adapter.latch_trigger_press(serial) is True
        assert adapter.consume_trigger_press(serial) is True
        assert adapter.consume_trigger_press(serial) is False

    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    def test_simulate_button_trigger_sets_level_and_latch(self):
        from proto import controller_manager_mock_pb2
        from services.controller_manager.mock_control_service import MockControllerService

        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        serial = next(iter(adapter.controllers))
        service = MockControllerService(adapter)

        request = controller_manager_mock_pb2.ButtonRequest(
            serial=serial, button=controller_manager_mock_pb2.ButtonRequest.TRIGGER, pressed=True
        )
        assert service.SimulateButton(request, None).success
        assert adapter.poll(serial)[ButtonKey.TRIGGER] is True  # the level
        request.pressed = False
        assert service.SimulateButton(request, None).success
        assert adapter.poll(serial)[ButtonKey.TRIGGER] is False
        assert adapter.consume_trigger_press(serial) is True  # the press survived the release

    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    def test_multiplexer_routes_to_the_owning_adapter(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        serial = next(iter(adapter.controllers))
        adapter.latch_trigger_press(serial)
        mux = MultiplexerBackend.__new__(MultiplexerBackend)
        mux._serial_to_adapter = {serial: adapter}

        assert mux.consume_trigger_press(serial) is True
        assert mux.consume_trigger_press(serial) is False
        assert mux.consume_trigger_press("unknown") is False


def _bare_hid(adapter_cls):
    adapter = adapter_cls.__new__(adapter_cls)
    adapter._init_stream_state()
    adapter._channel = MagicMock()
    adapter._stub = MagicMock()
    adapter._latest_data = {}
    adapter._last_state = {}
    adapter._trigger_latch = set()
    adapter._data_lock = threading.Lock()
    adapter._closed = set()
    adapter._stream_running = True
    adapter._adapter_names = {}
    return adapter


@pytest.mark.parametrize("adapter_cls", [RustServiceAdapter, PythonHidAdapter], ids=["rust", "python_hid"])
class TestUC030HidTriggerLatch:
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    def test_press_and_release_between_polls_is_latched(self, adapter_cls):
        """UC-030 BR-005: the game-frame latch remembers the press after the release, independent of poll()."""
        adapter = _bare_hid(adapter_cls)
        adapter._stub.StreamIO.return_value = FakeStreamCall(
            [
                psmove_hid_pb2.SensorData(serial=HID_SERIAL, button_trigger=False),
                psmove_hid_pb2.SensorData(serial=HID_SERIAL, button_trigger=True),
                psmove_hid_pb2.SensorData(serial=HID_SERIAL, button_trigger=True),
                psmove_hid_pb2.SensorData(serial=HID_SERIAL, button_trigger=False),
            ]
        )

        adapter._stream_loop()

        # The poll path has its own press latch since #2312 (UC-010 BR-012): one poll pressed, then the real level.
        assert adapter.poll(HID_SERIAL)[ButtonKey.TRIGGER] is True
        assert adapter.poll(HID_SERIAL)[ButtonKey.TRIGGER] is False
        # A poll never drains the game-frame latch.
        assert adapter.consume_trigger_press(HID_SERIAL) is True
        assert adapter.consume_trigger_press(HID_SERIAL) is False  # a hold is one press

    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    def test_no_press_no_latch_and_close_clears_it(self, adapter_cls):
        adapter = _bare_hid(adapter_cls)
        adapter._stub.StreamIO.return_value = FakeStreamCall([psmove_hid_pb2.SensorData(serial=HID_SERIAL)])
        adapter._stream_loop()
        assert adapter.consume_trigger_press(HID_SERIAL) is False

        adapter._trigger_latch.add(HID_SERIAL)
        adapter._stub.Close = MagicMock()
        adapter.close(HID_SERIAL)
        assert adapter.consume_trigger_press(HID_SERIAL) is False


def test_gameplay_data_has_the_trigger_field():
    """Proto contract: GameplayData.trigger_pressed = 13, GameEffect SHIELD_CHARGE = 17, SHIELD_SWELL = 18."""
    assert controller_manager_pb2.GameplayData.DESCRIPTOR.fields_by_name["trigger_pressed"].number == 13
    assert controller_manager_pb2.GameEffect.Value("GAME_EFFECT_SHIELD_CHARGE") == 17
    assert controller_manager_pb2.GameEffect.Value("GAME_EFFECT_SHIELD_SWELL") == 18
