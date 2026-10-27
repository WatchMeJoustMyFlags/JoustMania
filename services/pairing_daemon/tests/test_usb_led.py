"""LED cue on a PS Move on USB while it is paired (UC-001 BR-010, #2091), against a fake device."""

import asyncio

import pytest

from psmove_pairing import usb_led
from psmove_pairing.usb_led import GREEN, OFF, PULSE_MAX, PULSE_MIN, RED, UsbLedCue, pulse_level

PATH = b"/dev/hidraw7"
OTHER = b"/dev/hidraw8"


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
        cue.pairing(PATH)
        frames = await _frames(device, PATH, 6)
        assert all(r == g == b and PULSE_MIN <= r <= PULSE_MAX for r, g, b in frames)
        assert len({r for r, _g, _b in frames}) > 1
        assert cue.states[PATH] == "pairing"

    @pytest.mark.uc("UC-001", flow="main", steps=[2], br=["BR-010"])
    async def test_pairing_again_keeps_the_running_pulse(self, cue, device):
        cue.pairing(PATH)
        await _frames(device, PATH, 3)
        task = cue._tasks[PATH]
        cue.pairing(PATH)
        assert cue._tasks[PATH] is task


class TestPaired:
    @pytest.mark.uc("UC-001", flow="main", steps=[6], br=["BR-010"])
    async def test_paired_flashes_green_three_times_then_off(self, cue, device):
        cue.pairing(PATH)
        await _frames(device, PATH, 2)
        cue.paired(PATH)
        await cue._tasks[PATH]
        frames = device.frames[PATH]
        tail = frames[frames.index(GREEN) :]
        assert tail == [GREEN, OFF] * 3
        assert cue.states[PATH] == "paired"


class TestFailed:
    @pytest.mark.uc("UC-001", flow="A8", br=["BR-010"], cond=["Post-F-1"])
    async def test_failed_holds_red_until_the_next_cue(self, cue, device):
        cue.failed(PATH)
        frames = await _frames(device, PATH, 3)
        assert frames[:3] == [RED, RED, RED]
        assert cue.states[PATH] == "failed"
        cue.pairing(PATH)  # the next poll retries
        count = len(device.frames[PATH])
        frames = await _frames(device, PATH, count + 2)
        assert RED not in frames[count:]


class TestCueLifecycle:
    @pytest.mark.uc("UC-001", flow="main", steps=[7], br=["BR-010"])
    async def test_cue_ends_when_the_controller_left_usb(self, cue, device):
        cue.failed(PATH)
        cue.pairing(OTHER)
        await _frames(device, PATH, 1)
        cue.keep_only([OTHER])
        await asyncio.sleep(0)
        assert PATH not in cue.states and PATH not in cue._tasks
        assert cue.states[OTHER] == "pairing"

    @pytest.mark.uc("UC-001", flow="main", steps=[7], br=["BR-010"])
    async def test_a_failing_write_ends_the_cue_quietly(self, cue, device):
        device.gone.add(PATH)
        cue.pairing(PATH)
        task = cue._tasks[PATH]
        await asyncio.wait_for(task, 1.0)  # ended by itself, no exception
        assert task.exception() is None

    @pytest.mark.uc("UC-001", flow="A1", br=["BR-010"])
    async def test_stop_pulse_leaves_a_red_cue(self, cue, device):
        cue.failed(PATH)
        cue.stop_pulse(PATH)
        assert cue.states[PATH] == "failed"
        cue.pairing(OTHER)
        cue.stop_pulse(OTHER)
        assert OTHER not in cue.states

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
