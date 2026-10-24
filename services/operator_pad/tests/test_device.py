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


class FakeDevice:
    def __init__(self, path: str, grab_errors: list[int] | None = None, events: list[FakeEvent] | None = None):
        self.path = path
        self.grab_errors = list(grab_errors or [])
        self.events = list(events or [])
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

    async def async_read_loop(self):
        for event in self.events:
            yield event
        raise OSError(errno.ENODEV, "No such device")  # unplugged


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


def _manager(tmp_path, backend):
    config = create_autospec(PadConfig, instance=True)
    config.device.return_value = PAD
    config.keymap.return_value = parse_keymap(DEFAULT_KEYMAP)
    controller = create_autospec(PadController, instance=True)
    manager = DeviceManager(config, controller, backend_factory=lambda: backend, sysfs_root=tmp_path, sleep=no_sleep)
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
    manager, _, controller = _manager(tmp_path, FakeBackend({dev.path: dev}))
    assert await manager.attach()
    await asyncio.wait_for(manager.read_until_gone(), timeout=2)
    assert [c.args for c in controller.on_code.await_args_list] == [(183, 1, 10000), (183, 0, 10200)]
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
