"""Pad devices: sysfs match, exclusive grab with EBUSY retry, hotplug (UC-015 Pre-1, A4, FR-195)."""

import asyncio
import errno
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

from services.operator_pad import device as device_module
from services.operator_pad.config import DEFAULT_KEYMAP, PadConfig, parse_keymap
from services.operator_pad.controller import PadController
from services.operator_pad.device import DeviceManager, find_pad_nodes, grab_node


def make_sysfs(root: Path, nodes: dict[str, tuple[str, str]]) -> None:
    for node, (vendor, product) in nodes.items():
        ids = root / node / "device" / "id"
        ids.mkdir(parents=True)
        (ids / "vendor").write_text(vendor + "\n")
        (ids / "product").write_text(product + "\n")


class FakeEvent:
    def __init__(self, code: int, value: int, ts: float, type_: int = 1) -> None:
        self.type, self.code, self.value, self._ts = type_, code, value, ts

    def timestamp(self) -> float:
        return self._ts


PAD_KEYS = list(range(183, 195))  # F13 .. F24


class FakeDevice:
    def __init__(
        self,
        path: str,
        grab_errors: list[int] | None = None,
        events: list[FakeEvent] | None = None,
        keys: list[int] | None = None,
        read_error: BaseException | None = None,
    ):
        self.path = path
        self.grab_errors = list(grab_errors or [])
        self.events = list(events or [])
        self.keys = PAD_KEYS if keys is None else keys
        self.read_error = read_error or OSError(errno.ENODEV, "No such device")  # unplugged
        self.grabbed = self.closed = False
        self.grab_calls = 0
        self.leds: list[tuple[int, int]] = []

    def grab(self) -> None:
        self.grab_calls += 1
        if self.grab_errors:
            raise OSError(self.grab_errors.pop(0), "grab")
        self.grabbed = True

    def ungrab(self) -> None:
        self.grabbed = False

    def close(self) -> None:
        self.closed = True

    def set_led(self, led: int, value: int) -> None:
        self.leds.append((led, value))

    def capabilities(self, verbose: bool = False, absinfo: bool = True) -> dict[int, list]:
        return {0: [0, 1, 4], 1: list(self.keys)}

    async def async_read_loop(self):
        for event in self.events:
            yield event
        raise self.read_error


class FakeBackend:
    def __init__(self, devices: dict[str, FakeDevice]) -> None:
        self.devices = devices
        self.opened: list[str] = []

    def list_devices(self) -> list[str]:
        return list(self.devices)

    def open(self, path: str) -> FakeDevice:
        self.opened.append(path)
        return self.devices[path]


async def no_sleep(_seconds: float) -> None:
    return None


PAD = ("cb00", "1337")
PSMOVE = ("054c", "03d5")


@pytest.mark.uc("UC-015", cond=["Pre-1"])
def test_only_nodes_with_the_pad_usb_ids_match(tmp_path):
    make_sysfs(tmp_path, {"event3": PAD, "event4": PAD, "event5": PSMOVE})
    paths = ["/dev/input/event5", "/dev/input/event4", "/dev/input/event3", "/dev/input/event9"]
    assert find_pad_nodes("cb00", "1337", lambda: paths, tmp_path) == ["/dev/input/event3", "/dev/input/event4"]


@pytest.mark.uc("UC-015", cond=["Pre-1"])
async def test_ebusy_grab_is_retried_until_free():
    dev = FakeDevice("/dev/input/event3", grab_errors=[errno.EBUSY, errno.EBUSY])
    got = await grab_node(FakeBackend({dev.path: dev}), dev.path, no_sleep, retries=5)
    assert got is dev and dev.grabbed and dev.grab_calls == 3


@pytest.mark.uc("UC-015", flow="A4")
async def test_ebusy_after_the_retries_gives_up_and_counts():
    dev = FakeDevice("/dev/input/event3", grab_errors=[errno.EBUSY] * 10)
    with patch.object(device_module.metrics, "grab_failures_total") as counter:
        got = await grab_node(FakeBackend({dev.path: dev}), dev.path, no_sleep, retries=2)
    assert got is None and dev.closed and dev.grab_calls == 3
    counter.labels.assert_called_once_with(reason="ebusy")


class FakeClock:
    """The injected monotonic clock (ms): each read returns the next value."""

    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def __call__(self) -> int:
        return self.values.pop(0)


def _manager(tmp_path, backend, clock=None):
    config = create_autospec(PadConfig, instance=True)
    config.device.return_value = PAD
    config.keymap.return_value = parse_keymap(DEFAULT_KEYMAP)
    controller = create_autospec(PadController, instance=True)
    extra = {"clock_ms": clock} if clock is not None else {}
    manager = DeviceManager(
        config, controller, backend_factory=lambda: backend, sysfs_root=tmp_path, sleep=no_sleep, **extra
    )
    return manager, config, controller


@pytest.mark.uc("UC-015", flow="A4", cond=["Pre-1"])
async def test_every_matching_interface_is_grabbed_and_the_keymap_read_at_attach(tmp_path):
    make_sysfs(tmp_path, {"event3": PAD, "event4": PAD, "event5": PSMOVE})
    devices = {f"/dev/input/event{n}": FakeDevice(f"/dev/input/event{n}") for n in (3, 4, 5)}
    backend = FakeBackend(devices)
    manager, config, controller = _manager(tmp_path, backend)
    assert await manager.attach()
    assert sorted(backend.opened) == ["/dev/input/event3", "/dev/input/event4"]  # the PS Move node is never opened
    assert devices["/dev/input/event3"].grabbed and devices["/dev/input/event4"].grabbed
    controller.attached.assert_called_once_with(config.keymap.return_value)
    manager.set_lock_led(True)
    assert devices["/dev/input/event3"].leds == [(device_module.LED_SCROLLL, 1)]


@pytest.mark.uc("UC-015", flow="A4")
async def test_hotplug_missing_pad_then_plugged_in(tmp_path):
    backend = FakeBackend({})
    manager, _, controller = _manager(tmp_path, backend)
    assert not await manager.attach()
    controller.attached.assert_not_called()
    make_sysfs(tmp_path, {"event7": PAD})
    backend.devices["/dev/input/event7"] = FakeDevice("/dev/input/event7")
    assert await manager.attach()


@pytest.mark.uc("UC-015", flow="A4", br=["BR-010"])
async def test_events_are_fed_in_order_and_unplug_detaches(tmp_path):
    make_sysfs(tmp_path, {"event3": PAD})
    events = [FakeEvent(183, 1, 10.0), FakeEvent(0, 0, 10.0, type_=0), FakeEvent(183, 0, 10.2)]
    dev = FakeDevice("/dev/input/event3", events=events)
    manager, _, controller = _manager(tmp_path, FakeBackend({dev.path: dev}), clock=FakeClock([5000, 5200]))
    assert await manager.attach()
    await asyncio.wait_for(manager.read_until_gone(), timeout=2)
    assert [c.args for c in controller.on_code.await_args_list] == [(183, 1, 5000), (183, 0, 5200)]
    manager.detach()
    controller.detached.assert_called_once()
    assert dev.closed and not dev.grabbed


@pytest.mark.uc("UC-015", flow="A4")
async def test_run_rescans_after_an_unplug_and_stops_cleanly(tmp_path):
    make_sysfs(tmp_path, {"event3": PAD})
    backend = FakeBackend({"/dev/input/event3": FakeDevice("/dev/input/event3")})
    manager, _, controller = _manager(tmp_path, backend)
    manager._rescan_seconds = 0.01
    stop = asyncio.Event()
    task = asyncio.create_task(manager.run(stop))
    for _ in range(200):
        if len(backend.opened) >= 2:
            break
        await asyncio.sleep(0.005)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert len(backend.opened) >= 2  # attached, unplugged, attached again
    assert controller.detached.call_count >= 1


@pytest.mark.uc("UC-015", flow="A4")
async def test_missing_evdev_binding_never_crashes(tmp_path):
    def missing():
        raise ImportError("No module named 'evdev'")

    config = create_autospec(PadConfig, instance=True)
    config.device.return_value = PAD
    manager = DeviceManager(
        config, create_autospec(PadController, instance=True), backend_factory=missing, sysfs_root=tmp_path
    )
    assert not await manager.attach()


@pytest.mark.uc("UC-015", flow="main", br=["BR-007"], steps=[7])
@pytest.mark.parametrize("step_s", [3600.0, -3600.0], ids=["clock-forward", "clock-backward"])
async def test_events_are_timed_by_the_monotonic_clock_not_the_realtime_evdev_time(tmp_path, step_s):
    # NTP steps the wall clock between the press and the release of a K4 tap: the evdev time jumps.
    make_sysfs(tmp_path, {"event3": PAD})
    events = [FakeEvent(189, 1, 100.0), FakeEvent(189, 0, 100.1 + step_s)]
    dev = FakeDevice("/dev/input/event3", events=events)
    manager, _, controller = _manager(tmp_path, FakeBackend({dev.path: dev}), clock=FakeClock([7000, 7100]))
    assert await manager.attach()
    await asyncio.wait_for(manager.read_until_gone(), timeout=2)
    assert [c.args for c in controller.on_code.await_args_list] == [(189, 1, 7000), (189, 0, 7100)]


def test_default_clock_is_monotonic():
    with patch.object(device_module.time, "monotonic_ns", return_value=12_345_678_901):
        assert device_module.monotonic_ms() == 12_345


@pytest.mark.uc("UC-015", flow="A4", br=["BR-007"])
async def test_syn_dropped_resets_the_gestures_in_order(tmp_path):
    make_sysfs(tmp_path, {"event3": PAD})
    events = [FakeEvent(189, 1, 1.0), FakeEvent(device_module.SYN_DROPPED, 0, 1.1, type_=0), FakeEvent(183, 1, 1.2)]
    dev = FakeDevice("/dev/input/event3", events=events)
    manager, _, controller = _manager(tmp_path, FakeBackend({dev.path: dev}), clock=FakeClock([1, 2]))
    assert await manager.attach()
    await asyncio.wait_for(manager.read_until_gone(), timeout=2)
    names = [c[0] for c in controller.mock_calls if c[0] in ("on_code", "events_dropped")]
    assert names == ["on_code", "events_dropped", "on_code"]


@pytest.mark.uc("UC-015", flow="A4")
async def test_any_read_error_counts_as_the_device_gone(tmp_path):
    make_sysfs(tmp_path, {"event3": PAD})
    dev = FakeDevice("/dev/input/event3", read_error=ValueError("binding bug"))
    manager, _, _ = _manager(tmp_path, FakeBackend({dev.path: dev}))
    assert await manager.attach()
    await asyncio.wait_for(manager.read_until_gone(), timeout=2)  # returns instead of waiting forever


@pytest.mark.uc("UC-015", flow="A4", br=["BR-014"], cond=["Pre-1"])
async def test_a_partial_grab_fails_the_attach_and_releases_the_grabbed_nodes(tmp_path):
    make_sysfs(tmp_path, {"event3": PAD, "event4": PAD})
    ok = FakeDevice("/dev/input/event3")
    busy = FakeDevice("/dev/input/event4", grab_errors=[errno.EBUSY] * 10)
    manager, _, controller = _manager(tmp_path, FakeBackend({ok.path: ok, busy.path: busy}))
    assert not await manager.attach()
    assert not ok.grabbed and ok.closed
    controller.attached.assert_not_called()
    busy.grab_errors.clear()  # the other process let go: the next rescan attaches both
    ok.closed = False
    assert await manager.attach()
    assert ok.grabbed and busy.grabbed


@pytest.mark.uc("UC-015", br=["BR-014"], cond=["Pre-1"])
@pytest.mark.parametrize("ids", [("054c", "03d5"), ("054C", "0C5E")], ids=["zcm1", "zcm2"])
async def test_ps_move_ids_are_never_opened_whatever_the_flag_says(tmp_path, ids):
    make_sysfs(tmp_path, {"event5": ids})
    backend = FakeBackend({"/dev/input/event5": FakeDevice("/dev/input/event5")})
    manager, config, controller = _manager(tmp_path, backend)
    config.device.return_value = (ids[0].lower(), ids[1].lower())
    assert not await manager.attach()
    assert backend.opened == []
    assert find_pad_nodes(ids[0], ids[1], backend.list_devices, tmp_path) == []
    controller.attached.assert_not_called()


@pytest.mark.uc("UC-015", br=["BR-014"], cond=["Pre-1"])
async def test_a_matching_node_without_a_keymap_key_is_not_grabbed(tmp_path):
    make_sysfs(tmp_path, {"event3": PAD, "event4": PAD})
    keys = FakeDevice("/dev/input/event3")
    mouse = FakeDevice("/dev/input/event4", keys=[272, 273])  # BTN_LEFT, BTN_RIGHT only
    manager, _, _ = _manager(tmp_path, FakeBackend({keys.path: keys, mouse.path: mouse}))
    assert await manager.attach()
    assert keys.grabbed and not mouse.grabbed and mouse.closed
    lone = FakeDevice("/dev/input/event4", keys=[272])
    manager2, _, controller2 = _manager(tmp_path, FakeBackend({lone.path: lone}))
    assert not await manager2.attach()
    controller2.attached.assert_not_called()


@pytest.mark.uc("UC-015", flow="A4")
async def test_a_node_that_fails_right_after_the_attach_waits_the_rescan_interval(tmp_path):
    make_sysfs(tmp_path, {"event3": PAD})
    backend = FakeBackend({"/dev/input/event3": FakeDevice("/dev/input/event3")})  # read fails at once
    manager, _, _ = _manager(tmp_path, backend)
    manager._rescan_seconds = 0.1
    stop = asyncio.Event()
    task = asyncio.create_task(manager.run(stop))
    await asyncio.sleep(0.25)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert 1 <= len(backend.opened) <= 4  # without the wait: hundreds of attach / detach cycles
