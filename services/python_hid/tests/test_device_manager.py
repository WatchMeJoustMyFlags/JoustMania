"""
Unit tests for DeviceManager — command handling and device operations.

The `hid` module requires native hardware access, so we mock it entirely.
"""

import itertools
import queue
import statistics
import struct
import sys
import threading
import time
from collections import Counter, deque
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

# Ensure mock hidraw module exists
_mock_hid = ModuleType("hidraw")
_mock_hid.enumerate = MagicMock(return_value=[])
_mock_hid.device = MagicMock
sys.modules.setdefault("hidraw", _mock_hid)

from lib.psmove_hid import PRODUCT_ID_NAVIGATION, PRODUCT_ID_ZCM1, PRODUCT_ID_ZCM2, Button  # noqa: E402
from services.python_hid.device_manager import (  # noqa: E402
    ACCEL_SCALE,
    Command,
    CommandType,
    DeviceManager,
    InputSlot,
    SensorData,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_PATH_1 = b"/dev/hidraw0"
FAKE_SERIAL_RAW = "aa:bb:cc:dd:ee:ff"
FAKE_SERIAL_NORMALIZED = "AABBCCDDEEFF"


def _make_dev_info(
    path: bytes = FAKE_PATH_1,
    serial: str = FAKE_SERIAL_RAW,
    product_id: int = PRODUCT_ID_ZCM1,
) -> dict:
    return {"path": path, "serial_number": serial, "product_id": product_id}


def _make_parsed_report(
    buttons: int = 0,
    trigger: int = 0,
    accel: tuple[int, int, int] = (0, 0, 4096),
    gyro: tuple[int, int, int] = (0, 0, 0),
    battery: int = 0x05,
    temperature: int = 300,
) -> dict:
    return {
        "buttons": buttons,
        "trigger": (trigger, trigger),
        "accel": (accel, accel),
        "gyro": (gyro, gyro),
        "battery": battery,
        "temperature": temperature,
        "sequence": 0,
    }


# ---------------------------------------------------------------------------
# Tests: Command handling
# ---------------------------------------------------------------------------


class TestCommandDispatch:
    """Test that commands are dispatched correctly."""

    @patch("services.python_hid.device_manager.hid")
    def test_discover_returns_device_list(self, mock_hid):
        """Discover command returns list of device info dicts."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        dm = DeviceManager()
        result = dm._handle_command(Command(type=CommandType.DISCOVER, args={"force": False, "verify_only": False}))

        assert len(result) == 1
        assert result[0]["serial"] == FAKE_SERIAL_NORMALIZED

    @patch("services.python_hid.device_manager.hid")
    @pytest.mark.uc("UC-002", flow="main", steps=[2])
    def test_open_returns_true_on_success(self, mock_hid):
        """Open command returns True when device is found."""
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = MagicMock()

        dm = DeviceManager()
        result = dm._handle_command(Command(type=CommandType.OPEN, args={"serial": FAKE_SERIAL_NORMALIZED}))
        assert result is True

    @patch("services.python_hid.device_manager.hid")
    def test_open_returns_false_when_not_found(self, mock_hid):
        """Open command returns False when device is not found."""
        mock_hid.enumerate.return_value = []

        dm = DeviceManager()
        result = dm._handle_command(Command(type=CommandType.OPEN, args={"serial": "NONEXISTENT"}))
        assert result is False

    @patch("services.python_hid.device_manager.hid")
    def test_close_returns_true(self, mock_hid):
        """Close command always returns True."""
        dm = DeviceManager()
        result = dm._handle_command(Command(type=CommandType.CLOSE, args={"serial": "NONEXISTENT"}))
        assert result is True

    @patch("services.python_hid.device_manager.hid")
    def test_close_all_returns_true(self, mock_hid):
        """CloseAll command returns True."""
        dm = DeviceManager()
        result = dm._handle_command(Command(type=CommandType.CLOSE_ALL))
        assert result is True

    @patch("services.python_hid.device_manager.build_output_report")
    @patch("services.python_hid.device_manager.hid")
    def test_set_output_returns_true(self, mock_hid, mock_build):
        """SetOutput returns True when device exists."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device
        mock_build.return_value = b"\x00" * 9

        dm = DeviceManager()
        dm._handle_command(Command(type=CommandType.DISCOVER, args={"force": False, "verify_only": False}))

        result = dm._handle_command(
            Command(
                type=CommandType.SET_OUTPUT,
                args={"serial": FAKE_SERIAL_NORMALIZED, "r": 255, "g": 0, "b": 0, "rumble": 0},
            )
        )
        assert result is True

    @patch("services.python_hid.device_manager.hid")
    def test_set_output_returns_false_unknown_serial(self, mock_hid):
        """SetOutput returns False for unknown serial."""
        dm = DeviceManager()
        result = dm._handle_command(
            Command(
                type=CommandType.SET_OUTPUT,
                args={"serial": "UNKNOWN", "r": 0, "g": 0, "b": 0, "rumble": 0},
            )
        )
        assert result is False


# ---------------------------------------------------------------------------
# Tests: Device reading / SensorData
# ---------------------------------------------------------------------------


class TestReadDevice:
    @patch("services.python_hid.device_manager.battery_to_percent", return_value=100)
    @patch("services.python_hid.device_manager.parse_input_report")
    @patch("services.python_hid.device_manager.hid")
    @pytest.mark.uc("UC-002", flow="main", steps=[2])
    def test_read_device_produces_sensor_data(self, mock_hid, mock_parse, mock_battery):
        """_read_device returns SensorData with correct fields."""
        mock_device = MagicMock()
        mock_device.read.side_effect = [b"\x00" * 49, b""]

        mock_parse.return_value = _make_parsed_report(
            buttons=int(Button.MOVE | Button.CROSS),
            trigger=128,
            accel=(100, 200, 4096),
            gyro=(10, 20, 30),
            battery=0x05,
            temperature=300,
        )

        dm = DeviceManager()
        dm._product_ids[FAKE_SERIAL_NORMALIZED] = PRODUCT_ID_ZCM1
        result = dm._read_device(FAKE_SERIAL_NORMALIZED, mock_device)

        assert isinstance(result, SensorData)
        assert result.serial == FAKE_SERIAL_NORMALIZED
        assert result.battery == 100
        assert result.trigger == 128
        assert result.temperature == 300
        assert result.button_move is True
        assert result.button_cross is True
        assert result.button_triangle is False
        assert result.accel_x == 100 / ACCEL_SCALE
        assert result.accel_z == 4096 / ACCEL_SCALE
        assert result.gyro_x == 10.0

    @patch("services.python_hid.device_manager.hid")
    def test_read_device_returns_none_when_no_data(self, mock_hid):
        """_read_device returns None when device has no new data."""
        mock_device = MagicMock()
        mock_device.read.return_value = b""

        dm = DeviceManager()
        result = dm._read_device(FAKE_SERIAL_NORMALIZED, mock_device)
        assert result is None

    @patch("services.python_hid.device_manager.hid")
    def test_read_device_returns_none_on_oserror(self, mock_hid):
        """_read_device returns None and cleans up on OSError."""
        mock_device = MagicMock()
        mock_device.read.side_effect = OSError("disconnected")

        dm = DeviceManager()
        dm._devices[FAKE_SERIAL_NORMALIZED] = mock_device
        dm._paths[FAKE_SERIAL_NORMALIZED] = "/dev/hidraw0"

        result = dm._read_device(FAKE_SERIAL_NORMALIZED, mock_device)
        assert result is None
        assert FAKE_SERIAL_NORMALIZED not in dm._devices


# ---------------------------------------------------------------------------
# Tests: Stream registration
# ---------------------------------------------------------------------------


class TestStreamRegistration:
    def test_register_and_unregister_stream(self):
        """Streams can be registered and unregistered."""
        dm = DeviceManager()
        q = dm.register_stream("test-stream")
        assert q is not None
        assert "test-stream" in dm._stream_slots

        dm.unregister_stream("test-stream")
        assert "test-stream" not in dm._stream_slots

    def test_unregister_nonexistent_is_safe(self):
        """Unregistering a non-existent stream does not raise."""
        dm = DeviceManager()
        dm.unregister_stream("nonexistent")


# ---------------------------------------------------------------------------
# Tests: Discover edge cases
# ---------------------------------------------------------------------------


class TestDiscoverEdgeCases:
    @patch("services.python_hid.device_manager.hid")
    def test_discover_skips_empty_serial(self, mock_hid):
        """Devices with empty serial are skipped."""
        mock_hid.enumerate.return_value = [
            {"path": FAKE_PATH_1, "serial_number": ""},
        ]

        dm = DeviceManager()
        result = dm._discover()
        assert result == []

    @patch("services.python_hid.device_manager.hid")
    def test_discover_excludes_serials(self, mock_hid):
        """Excluded serials are not opened."""
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = MagicMock()

        dm = DeviceManager()
        result = dm._discover(exclude_serials=[FAKE_SERIAL_NORMALIZED])
        assert result == []

    @patch("services.python_hid.device_manager.hid")
    def test_discover_verify_cleans_stale(self, mock_hid):
        """Stale devices are cleaned during verify."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        dm = DeviceManager()
        dm._discover()

        mock_device.read.side_effect = OSError("gone")
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.reset_mock()
        mock_hid.device.return_value = MagicMock()

        result = dm._discover(force=False)
        assert len(result) == 1

    @patch("services.python_hid.device_manager.hid")
    def test_discover_force_removes_disappeared(self, mock_hid):
        """force=True removes devices whose paths are gone."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        dm = DeviceManager()
        dm._discover()

        mock_hid.enumerate.return_value = []
        result = dm._discover(force=True)
        assert result == []
        mock_device.close.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: supported controllers (C-012, UC-001 BR-006, #1442)
# ---------------------------------------------------------------------------


class TestSupportedControllers:
    @patch("services.python_hid.device_manager.hid")
    @pytest.mark.uc("UC-001", br=["BR-006"])
    def test_navigation_controller_is_never_opened(self, mock_hid):
        """0x042F is the PS Move Navigation controller: not enumerated, not opened as a player."""
        by_product = {
            PRODUCT_ID_ZCM1: [_make_dev_info(path=b"/dev/hidraw0", serial="aa:bb:cc:dd:ee:01")],
            PRODUCT_ID_ZCM2: [_make_dev_info(path=b"/dev/hidraw1", serial="aa:bb:cc:dd:ee:02", product_id=0x0C5E)],
            PRODUCT_ID_NAVIGATION: [
                _make_dev_info(path=b"/dev/hidraw2", serial="aa:bb:cc:dd:ee:03", product_id=0x042F)
            ],
        }
        mock_hid.enumerate.side_effect = lambda _vendor, product: by_product.get(product, [])
        mock_hid.device.return_value = MagicMock()

        dm = DeviceManager()
        dm._discover()

        assert sorted(dm._devices) == ["AABBCCDDEE01", "AABBCCDDEE02"]
        assert PRODUCT_ID_NAVIGATION not in {c.args[1] for c in mock_hid.enumerate.call_args_list}

    @pytest.mark.parametrize(("product_id", "zcm2"), [(PRODUCT_ID_ZCM1, False), (PRODUCT_ID_ZCM2, True)])
    @patch("services.python_hid.device_manager.parse_input_report")
    def test_zcm2_product_id_selects_signed_decoding(self, mock_parse, product_id, zcm2):
        """0x0C5E (ZCM2, PS4 era) is decoded as signed; 0x03D5 (ZCM1) with the 0x8000 offset."""
        mock_device = MagicMock()
        mock_device.read.side_effect = [b"\x00" * 49, b""]
        mock_parse.return_value = _make_parsed_report()

        dm = DeviceManager()
        dm._product_ids[FAKE_SERIAL_NORMALIZED] = product_id
        dm._read_device(FAKE_SERIAL_NORMALIZED, mock_device)

        assert mock_parse.call_args.kwargs["zcm2"] is zcm2


class TestPollRate:
    """UC-030 step 2: the HID thread samples the controllers at ~1 kHz."""

    @pytest.mark.uc("UC-030", flow="main", steps=[2])
    @pytest.mark.nfr("NFR-001")
    def test_run_loop_polls_every_millisecond(self):
        dm = DeviceManager()
        dm._running = True
        poll_count = 0

        def stop_after_three_polls():
            nonlocal poll_count
            poll_count += 1
            if poll_count == 3:
                dm._running = False

        with (
            patch.object(dm, "_poll_all_devices", side_effect=stop_after_three_polls),
            patch("services.python_hid.device_manager.time.sleep") as sleep,
        ):
            dm._run()

        assert poll_count == 3
        assert [c.args for c in sleep.call_args_list] == [(0.001,)] * 3


class _TimedDevice:
    """A HID device with one report per poll; records when each poll starts reading it."""

    def __init__(self, polls: int):
        self.poll_times: list[float] = []
        self.done = threading.Event()
        self._polls = polls
        self._pending = False

    def read(self, _size):
        if self._pending:
            self._pending = False
            return b""
        self.poll_times.append(time.perf_counter())
        if len(self.poll_times) >= self._polls:
            self.done.set()
        self._pending = True
        return b"\x00" * 49

    def close(self):
        pass


class TestPollIntervalMeasured:
    """NFR-001: the python-hid thread reads every open controller every 1 ms plus read time."""

    POLLS = 300
    # NFR-001: 1 ms plus read time. Measured median 1.1-1.2 ms, also with 2x CPU overload
    # (review of #1619); a 2 ms budget still fails a loop that sleeps 2 ms or more.
    MEDIAN_BUDGET_S = 0.002

    @pytest.mark.nfr("NFR-001")
    def test_open_device_is_read_about_every_millisecond(self):
        dm = DeviceManager()
        device = _TimedDevice(self.POLLS)
        dm._devices[FAKE_SERIAL_NORMALIZED] = device
        dm._product_ids[FAKE_SERIAL_NORMALIZED] = PRODUCT_ID_ZCM1
        dm._stream_slots["nfr-001"] = InputSlot()

        dm.start()
        try:
            assert device.done.wait(timeout=10), f"only {len(device.poll_times)} polls in 10 s"
        finally:
            dm.stop()

        intervals = sorted(b - a for a, b in itertools.pairwise(device.poll_times))
        median = statistics.median(intervals)
        p90 = intervals[int(len(intervals) * 0.9)]
        print(
            f"NFR-001 python-hid poll interval over {len(intervals)} polls: "
            f"min {intervals[0] * 1e3:.3f} ms, median {median * 1e3:.3f} ms, p90 {p90 * 1e3:.3f} ms"
        )
        assert intervals[0] >= 0.001, "the loop must sleep 1 ms between polls"
        assert median <= self.MEDIAN_BUDGET_S


# ---------------------------------------------------------------------------
# Tests: latest-sample slots (#2503, rust-hid input_slot parity #2372)
# ---------------------------------------------------------------------------


def _zcm2_report(buttons: int = 0, counter: int = 0) -> bytes:
    """A ZCM2 input report with these face buttons; gyro x (frame 2) carries `counter`."""
    data = bytearray(49)
    data[0] = 0x01
    data[2] = buttons & 0xFF
    struct.pack_into("<h", data, 31, counter)
    return bytes(data)


class _StampedDevice:
    """One report per poll; gyro x numbers it and `read_at` keeps when each was read."""

    def __init__(self):
        self.read_at: dict[int, float] = {}
        self._count = 0
        self._pending = False

    def read(self, _size):
        if self._pending:
            self._pending = False
            return b""
        self._pending = True
        self._count += 1
        self.read_at[self._count] = time.perf_counter()
        return _zcm2_report(counter=self._count)

    def close(self):
        pass


class _ScriptedDevice:
    """Returns the pushed reports in order, then b"" (nothing pending)."""

    def __init__(self):
        self._reports: deque[bytes] = deque()

    def push(self, *reports: bytes) -> None:
        self._reports.extend(reports)

    def read(self, _size):
        return self._reports.popleft() if self._reports else b""

    def close(self):
        pass


PRESS = _zcm2_report(buttons=int(Button.CROSS))
RELEASE = _zcm2_report()


def _scripted_manager() -> tuple[DeviceManager, _ScriptedDevice]:
    dm = DeviceManager()
    device = _ScriptedDevice()
    dm._devices[FAKE_SERIAL_NORMALIZED] = device
    dm._product_ids[FAKE_SERIAL_NORMALIZED] = PRODUCT_ID_ZCM2
    return dm, device


def _drain(slot) -> list[SensorData]:
    samples = []
    while True:
        try:
            samples.append(slot.get_nowait())
        except queue.Empty:
            return samples


class TestLatestSampleSlots:
    """UC-002 step 2 / NFR-056: python-hid keeps one waiting sample per controller (#2503)."""

    LAG_S = 0.002
    RUN_S = 1.5
    WARMUP_S = 0.5

    @pytest.mark.uc("UC-002", flow="main", steps=[2])
    @pytest.mark.nfr("NFR-056")
    def test_a_lagging_reader_gets_fresh_samples_from_every_controller(self):
        """4 controllers polled at ~1 kHz each, a reader taking 2 ms per sample: the age stays bounded."""
        dm = DeviceManager()
        devices = {f"SERIAL{i}": _StampedDevice() for i in range(4)}
        for serial, device in devices.items():
            dm._devices[serial] = device
            dm._product_ids[serial] = PRODUCT_ID_ZCM2
        slot = dm.register_stream("lagging-reader")
        ages: list[float] = []
        served: Counter[str] = Counter()

        dm.start()
        try:
            start = time.perf_counter()
            while time.perf_counter() - start < self.RUN_S:
                sample = slot.get(timeout=1.0)
                taken = time.perf_counter()
                if taken - start >= self.WARMUP_S:
                    ages.append(taken - devices[sample.serial].read_at[int(sample.gyro_x)])
                    served[sample.serial] += 1
                time.sleep(self.LAG_S)
        finally:
            dm.stop()

        median = statistics.median(ages)
        print(
            f"NFR-056 python-hid sample age over {len(ages)} samples: "
            f"median {median * 1e3:.1f} ms, max {max(ages) * 1e3:.1f} ms, served {dict(served)}"
        )
        assert median < 0.1, f"median sample age {median * 1e3:.0f} ms: the reader gets a backlog"
        # Round robin: no controller starves behind the others.
        assert set(served) == set(devices)
        assert min(served.values()) >= max(served.values()) // 2

    @pytest.mark.uc("UC-010", br=["BR-012"])
    def test_a_press_released_while_the_reader_lags_is_still_delivered(self):
        """A press, then 1100 release samples before the reader takes any: the press is OR'ed in."""
        dm, device = _scripted_manager()
        slot = dm.register_stream("slow")
        dm._poll_all_devices()  # the registration's flush poll

        device.push(PRESS)
        dm._poll_all_devices()
        for _ in range(1100):
            device.push(RELEASE)
            dm._poll_all_devices()

        samples = _drain(slot)
        assert any(s.button_cross for s in samples), "the press was lost"
        assert len(samples) == 1, "only the newest sample per controller waits"

        # Once the reader catches up, the release is observed too.
        device.push(RELEASE)
        dm._poll_all_devices()
        assert [s.button_cross for s in _drain(slot)] == [False]

    @pytest.mark.uc("UC-010", br=["BR-012"])
    def test_a_press_released_within_one_drain_is_kept(self):
        dm, device = _scripted_manager()
        slot = dm.register_stream("drain")
        dm._poll_all_devices()

        device.push(PRESS, RELEASE)
        dm._poll_all_devices()

        assert [s.button_cross for s in _drain(slot)] == [True]

    @pytest.mark.uc("UC-002", flow="main", steps=[2])
    def test_a_new_stream_starts_after_a_flush_of_queued_reports(self):
        """A press queued before the stream registered is stale: not delivered (rust-hid #2372)."""
        dm, device = _scripted_manager()
        device.push(PRESS)
        slot = dm.register_stream("new")

        dm._poll_all_devices()
        assert _drain(slot) == []

        device.push(RELEASE)
        dm._poll_all_devices()
        assert [s.button_cross for s in _drain(slot)] == [False]

    @pytest.mark.uc("UC-002", flow="main", steps=[2])
    def test_a_new_stream_flush_does_not_starve_a_running_stream(self):
        """Stream A is running; B registers: B flushes, A keeps receiving (flush is per slot)."""
        dm, device = _scripted_manager()
        slot_a = dm.register_stream("a")
        dm._poll_all_devices()  # A's flush poll
        slot_b = dm.register_stream("b")

        device.push(PRESS)
        dm._poll_all_devices()
        assert [s.button_cross for s in _drain(slot_a)] == [True]
        assert _drain(slot_b) == []

        device.push(RELEASE)
        dm._poll_all_devices()
        assert [s.button_cross for s in _drain(slot_a)] == [False]
        assert [s.button_cross for s in _drain(slot_b)] == [False]

    @pytest.mark.uc("UC-010", br=["BR-012"])
    def test_a_malformed_last_report_keeps_the_earlier_valid_one_and_its_buttons(self):
        """rust-hid `read_latest_input`: a bad tail report is skipped, the press is not lost."""
        dm, device = _scripted_manager()
        slot = dm.register_stream("tail")
        dm._poll_all_devices()

        device.push(PRESS, RELEASE, b"\x01\x02")
        dm._poll_all_devices()

        assert [s.button_cross for s in _drain(slot)] == [True]

    def test_a_replaced_sample_keeps_its_queue_position(self):
        slot = InputSlot()
        slot.offer(SensorData(**{**_sample_fields("A"), "button_cross": True}))
        slot.offer(SensorData(**_sample_fields("B")))
        slot.offer(SensorData(**{**_sample_fields("A"), "trigger": 9}))

        first, second = _drain(slot)
        assert (first.serial, first.trigger, first.button_cross) == ("A", 9, True)
        assert second.serial == "B"


def _sample_fields(serial: str) -> dict:
    fields = dict.fromkeys(SensorData.__dataclass_fields__, 0)
    fields.update({name: False for name in fields if name.startswith("button_")})
    fields["serial"] = serial
    return fields
