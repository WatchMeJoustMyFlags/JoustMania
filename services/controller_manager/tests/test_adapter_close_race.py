"""Polls no longer revive state of a serial closed by discovery (#1447).

Since #1447 the 100 Hz poll runs on the event loop while discovery closes
loser / switched / lost serials in a worker thread. Adapters must not
re-create per-serial state for a closed serial until it is reopened or
rediscovered.
"""

import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

test_dir = Path(__file__).parent
project_root = test_dir.parent.parent.parent
sys.path.insert(0, str(project_root))

from services.controller_manager.multiplexer.python_hid_adapter import PythonHidAdapter  # noqa: E402
from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter  # noqa: E402
from services.controller_manager.tests.backend_doubles import autospec_channel  # noqa: E402
from services.controller_manager.tests.fake_stream_call import FakeStreamCall  # noqa: E402

SERIAL = "AABBCCDDEE01"  # normalized form, as discover() returns it
FRAME = {"serial": SERIAL, "battery": 5}


def _bare(adapter_cls):
    """Adapter without __init__ (no gRPC channel)."""
    adapter = adapter_cls.__new__(adapter_cls)
    adapter._init_stream_state()
    adapter._channel = autospec_channel()
    adapter._stub = MagicMock()
    adapter._command_queue = queue.Queue(maxsize=256)
    adapter._latest_data = {}
    adapter._last_state = {}
    adapter._trigger_latch = set()
    adapter._data_lock = threading.Lock()
    adapter._closed = set()
    adapter._stream_thread = None
    adapter._stream_running = False
    adapter._adapter_names = {}
    adapter._ensure_stream = lambda: None
    return adapter


@pytest.fixture(params=[RustServiceAdapter, PythonHidAdapter], ids=["rust", "python_hid"])
def hid_adapter(request):
    return _bare(request.param)


@pytest.mark.nfr("NFR-002")
class TestHidAdapterCloseVersusPoll:
    def test_poll_after_close_does_not_revive_state(self, hid_adapter):
        hid_adapter._latest_data[SERIAL] = dict(FRAME)
        assert hid_adapter.poll(SERIAL) is not None

        hid_adapter.close(SERIAL)
        # A frame that slipped in after the close (stream thread race).
        hid_adapter._latest_data[SERIAL] = dict(FRAME)

        assert hid_adapter.poll(SERIAL) is None
        assert SERIAL not in hid_adapter._last_state

    def test_stream_frames_for_a_closed_serial_are_dropped(self, hid_adapter):
        hid_adapter.close(SERIAL)
        sensor = MagicMock()
        sensor.serial = SERIAL
        hid_adapter._stream_running = True
        hid_adapter._stub.StreamIO.return_value = FakeStreamCall([sensor])
        with patch("services.controller_manager.multiplexer.hid_stream.sensor_data_to_dict", return_value=dict(FRAME)):
            hid_adapter._stream_loop()
        assert SERIAL not in hid_adapter._latest_data

    def test_reopen_resumes_polling(self, hid_adapter):
        hid_adapter.close(SERIAL)
        hid_adapter._stub.Open.return_value = SimpleNamespace(success=True)
        assert hid_adapter.open(SERIAL)
        hid_adapter._latest_data[SERIAL] = dict(FRAME)
        assert hid_adapter.poll(SERIAL) is not None

    def test_rediscovery_resumes_polling(self, hid_adapter):
        hid_adapter.close(SERIAL)
        hid_adapter._stub.Discover.return_value = SimpleNamespace(
            controllers=[SimpleNamespace(serial=SERIAL, adapter_name="hci0")]
        )
        assert hid_adapter.discover() == [SERIAL]
        hid_adapter._latest_data[SERIAL] = dict(FRAME)
        assert hid_adapter.poll(SERIAL) is not None
