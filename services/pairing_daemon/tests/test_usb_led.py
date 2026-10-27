"""LED cue on a PS Move on USB while it is paired (UC-001 BR-010, #2091), against a fake device."""

import asyncio
import contextlib
import threading

import pytest

from psmove_pairing import usb_led
from psmove_pairing.usb_led import GREEN, OFF, PULSE_MAX, PULSE_MIN, RED, UsbLedCue, pulse_level

PATH = b"/dev/hidraw7"
OTHER = b"/dev/hidraw8"
SERIAL = "00:06:F7:00:00:07"
OTHER_SERIAL = "00:06:F7:00:00:08"
KEY = (PATH, SERIAL)
OTHER_KEY = (OTHER, OTHER_SERIAL)


class FakeDevice:
    """Records every colour written per device path; can fail like an unplugged controller."""

    def __init__(self):
        self.frames: dict[bytes, list[tuple[int, int, int]]] = {}
        self.gone: set[bytes] = set()

    def write(self, path: bytes, rgb: tuple[int, int, int]) -> None:
        if path in self.gone:
            raise OSError(19, "No such device")
        self.frames.setdefault(path, []).append(rgb)


async def _yield(_seconds: float) -> None:
    await asyncio.sleep(0)


@pytest.fixture
async def device():
    fake = FakeDevice()
    yield fake


@pytest.fixture
async def cue(device):
    led = UsbLedCue(write=device.write, sleep=_yield)
    yield led
    await led.close()


async def _frames(device: FakeDevice, path: bytes, count: int) -> list[tuple[int, int, int]]:
    for _ in range(1000):
        if len(device.frames.get(path, [])) >= count:
            return device.frames[path]
        await asyncio.sleep(0.001)
    raise AssertionError(f"only {device.frames.get(path, [])} written to {path!r}")


class TestPulse:
    @pytest.mark.uc("UC-001", flow="main", steps=[2], br=["BR-010"])
    def test_pulse_is_a_slow_triangle(self):
        assert pulse_level(0.0) == PULSE_MIN
        assert pulse_level(usb_led.PULSE_PERIOD_SECONDS / 2) == PULSE_MAX
        assert pulse_level(usb_led.PULSE_PERIOD_SECONDS) == PULSE_MIN
        assert usb_led.PULSE_PERIOD_SECONDS >= 1.5  # slow: a host sees it breathe, not blink

    @pytest.mark.uc("UC-001", flow="main", steps=[2], br=["BR-010"])
    async def test_pairing_pulses_white_with_changing_brightness(self, cue, device):
        cue.pairing(PATH, SERIAL)
        frames = await _frames(device, PATH, 6)
        assert all(r == g == b and PULSE_MIN <= r <= PULSE_MAX for r, g, b in frames)
        assert len({r for r, _g, _b in frames}) > 1
        assert cue.states[KEY] == "pairing"

    @pytest.mark.uc("UC-001", flow="main", steps=[2], br=["BR-010"])
    async def test_pairing_again_keeps_the_running_pulse(self, cue, device):
        cue.pairing(PATH, SERIAL)
        await _frames(device, PATH, 3)
        task = cue._tasks[KEY]
        cue.pairing(PATH, SERIAL)
        assert cue._tasks[KEY] is task


class TestPaired:
    @pytest.mark.uc("UC-001", flow="main", steps=[6], br=["BR-010"])
    async def test_paired_flashes_green_three_times_then_off(self, cue, device):
        cue.pairing(PATH, SERIAL)
        await _frames(device, PATH, 2)
        cue.paired(PATH, SERIAL)
        await cue._tasks[KEY]
        frames = device.frames[PATH]
        tail = frames[frames.index(GREEN) :]
        assert tail == [GREEN, OFF] * 3
        assert cue.states[KEY] == "paired"


class TestFailed:
    @pytest.mark.uc("UC-001", flow="A8", br=["BR-010"], cond=["Post-F-1"])
    async def test_failed_holds_red_until_the_next_cue(self, cue, device):
        cue.failed(PATH, SERIAL)
        frames = await _frames(device, PATH, 3)
        assert frames[:3] == [RED, RED, RED]
        assert cue.states[KEY] == "failed"
        red = cue._tasks[KEY]
        cue.pairing(PATH, SERIAL)  # the next poll retries
        with contextlib.suppress(asyncio.CancelledError):
            await red  # returns once a red frame already handed to a thread has landed
        count = len(device.frames[PATH])
        frames = await _frames(device, PATH, count + 2)
        assert RED not in frames[count:]


class TestWritesPerNode:
    @pytest.mark.uc("UC-001", flow="A8", br=["BR-010"])
    async def test_a_cancelled_cues_write_in_flight_lands_before_the_next_cue(self, device):
        """A red frame stuck in its thread when the retry starts must not land on top of the pulse."""
        release = threading.Event()
        first = threading.Event()

        def write(path, rgb):
            if rgb == RED and not first.is_set():
                first.set()
                release.wait(5)  # the first red write hangs in its thread
            device.write(path, rgb)

        cue = UsbLedCue(write=write, sleep=_yield)
        try:
            cue.failed(PATH, SERIAL)
            for _ in range(1000):
                if first.is_set():
                    break
                await asyncio.sleep(0.001)
            cue.pairing(PATH, SERIAL)  # the retry starts while the red frame is in flight
            for _ in range(20):
                await asyncio.sleep(0.001)  # give the pulse every chance to write first
            release.set()
            frames = await _frames(device, PATH, 4)
            assert frames[0] == RED
            assert RED not in frames[1:]
        finally:
            release.set()
            await cue.close()

    @pytest.mark.uc("UC-001", flow="main", steps=[2], br=["BR-010"])
    async def test_another_controller_on_a_reused_node_ends_the_old_cue(self, cue, device):
        """Unplug A, plug B on the same hidraw node within one poll: A's red is not carried onto B."""
        cue.failed(PATH, SERIAL)
        await _frames(device, PATH, 1)
        red = cue._tasks[KEY]
        cue.keep_only([(PATH, OTHER_SERIAL)])  # B is skipped (verified): no cue of its own
        assert cue.states == {}
        with contextlib.suppress(asyncio.CancelledError):
            await red
        count = len(device.frames[PATH])
        for _ in range(10):
            await asyncio.sleep(0)
        assert device.frames[PATH][count:] == []


class TestCueLifecycle:
    @pytest.mark.uc("UC-001", flow="main", steps=[7], br=["BR-010"])
    async def test_cue_ends_when_the_controller_left_usb(self, cue, device):
        cue.failed(PATH, SERIAL)
        cue.pairing(OTHER, OTHER_SERIAL)
        await _frames(device, PATH, 1)
        cue.keep_only([OTHER_KEY])
        await asyncio.sleep(0)
        assert KEY not in cue.states and KEY not in cue._tasks
        assert cue.states[OTHER_KEY] == "pairing"

    @pytest.mark.uc("UC-001", flow="main", steps=[7], br=["BR-010"])
    async def test_a_failing_write_ends_the_cue_quietly(self, cue, device):
        device.gone.add(PATH)
        cue.pairing(PATH, SERIAL)
        task = cue._tasks[KEY]
        await asyncio.wait_for(task, 1.0)  # ended by itself, no exception
        assert task.exception() is None

    @pytest.mark.uc("UC-001", flow="A1", br=["BR-010"])
    async def test_stop_pulse_leaves_a_red_cue(self, cue, device):
        cue.failed(PATH, SERIAL)
        cue.stop_pulse(PATH, SERIAL)
        assert cue.states[KEY] == "failed"
        cue.pairing(OTHER, OTHER_SERIAL)
        cue.stop_pulse(OTHER, OTHER_SERIAL)
        assert OTHER_KEY not in cue.states

    @pytest.mark.uc("UC-001", flow="main", steps=[2], br=["BR-010"])
    def test_write_led_sends_output_report_0x06(self, monkeypatch):
        written = []

        class Device:
            def open_path(self, path):
                written.append(("open", path))

            def write(self, report):
                written.append(("write", bytes(report)))

            def close(self):
                written.append(("close",))

        monkeypatch.setattr(usb_led.hid, "device", Device)
        usb_led.write_led(PATH, (1, 2, 3))
        assert written == [("open", PATH), ("write", bytes([0x06, 0, 1, 2, 3, 0, 0, 0, 0])), ("close",)]
