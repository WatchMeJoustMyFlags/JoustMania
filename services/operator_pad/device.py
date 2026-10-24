"""Pad input devices: sysfs match, exclusive grab, hotplug rescan (UC-015 Pre-1, A4, FR-195).

Nodes are matched by the USB vendor:product of `operator_pad.device` through sysfs
(``/sys/class/input/eventN/device/id/{vendor,product}``), so no other input device is opened (the
PS Move controllers have nodes there too). Every matching node is grabbed (EVIOCGRAB); an
``EBUSY`` grab is retried, because an old container may still hold it. A pad that is missing or
unplugged is rescanned for and never fails the service.

python-evdev is imported lazily in `evdev_backend()` only, so the tests (and a host without the
binding) run against a fake backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from opentelemetry import trace

from lib.telemetry import get_tracer
from services.operator_pad import metrics
from services.operator_pad.config import PadConfig
from services.operator_pad.controller import PadController

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)

EV_KEY = 1  # linux/input-event-codes.h
LED_SCROLLL = 2
SYSFS_INPUT = Path("/sys/class/input")
RESCAN_SECONDS = 2.0
GRAB_RETRIES = 5
GRAB_RETRY_SECONDS = 0.5


class InputDevice(Protocol):
    path: str

    def grab(self) -> None: ...
    def ungrab(self) -> None: ...
    def close(self) -> None: ...
    def set_led(self, led: int, value: int) -> None: ...
    def async_read_loop(self) -> Any: ...


class Backend(Protocol):
    def list_devices(self) -> list[str]: ...
    def open(self, path: str) -> InputDevice: ...


class _EvdevBackend:
    def __init__(self, evdev_module) -> None:
        self._evdev = evdev_module

    def list_devices(self) -> list[str]:
        return list(self._evdev.list_devices())

    def open(self, path: str) -> InputDevice:
        return self._evdev.InputDevice(path)


def evdev_backend() -> Backend:
    """The real backend; raises ImportError when python-evdev is not installed (`--extra device`)."""
    import evdev  # noqa: PLC0415 - isolated so tests and hosts without the binding never import it

    return _EvdevBackend(evdev)


def _read_id(sysfs_root: Path, node: str, field: str) -> str | None:
    try:
        return (sysfs_root / node / "device" / "id" / field).read_text().strip().lower()
    except OSError:
        return None


def find_pad_nodes(vendor: str, product: str, list_devices: Callable[[], list[str]], sysfs_root: Path) -> list[str]:
    """The /dev/input/eventN paths whose sysfs USB ids are vendor:product, in a stable order."""
    matches = []
    for path in list_devices():
        node = Path(path).name
        if _read_id(sysfs_root, node, "vendor") == vendor and _read_id(sysfs_root, node, "product") == product:
            matches.append(path)
    return sorted(matches)


async def grab_node(
    backend: Backend,
    path: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    retries: int = GRAB_RETRIES,
    delay: float = GRAB_RETRY_SECONDS,
) -> InputDevice | None:
    """Open and grab one node; retry EBUSY up to ``retries`` times. None when it cannot be grabbed."""
    try:
        device = backend.open(path)
    except OSError as e:
        logger.warning(f"Opening pad node {path} failed: {e}")
        metrics.grab_failures_total.labels(reason="error").inc()
        return None
    for attempt in range(retries + 1):
        try:
            device.grab()
            return device
        except OSError as e:
            if e.errno == errno.EBUSY and attempt < retries:
                logger.info(f"Pad node {path} busy (another process holds the grab), retrying")
                await sleep(delay)
                continue
            reason = "ebusy" if e.errno == errno.EBUSY else "error"
            logger.warning(f"Grabbing pad node {path} failed ({reason}): {e}")
            metrics.grab_failures_total.labels(reason=reason).inc()
            with contextlib.suppress(Exception):
                device.close()
            return None
    return None


class DeviceManager:
    """Attach, read, detach, rescan: forever, until ``stop`` is set."""

    def __init__(
        self,
        config: PadConfig,
        controller: PadController,
        backend_factory: Callable[[], Backend] = evdev_backend,
        sysfs_root: Path = SYSFS_INPUT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rescan_seconds: float = RESCAN_SECONDS,
    ) -> None:
        self._config = config
        self._controller = controller
        self._backend_factory = backend_factory
        self._backend: Backend | None = None
        self._sysfs_root = sysfs_root
        self._sleep = sleep
        self._rescan_seconds = rescan_seconds
        self._devices: list[InputDevice] = []

    def set_lock_led(self, on: bool) -> None:
        """Scroll Lock LED of the pad while locked (UC-015 A6); best effort on every grabbed node."""
        for device in self._devices:
            try:
                device.set_led(LED_SCROLLL, 1 if on else 0)
            except Exception as e:
                logger.debug(f"Scroll Lock LED on {device.path}: {e}")

    def _ensure_backend(self) -> Backend | None:
        if self._backend is None:
            try:
                self._backend = self._backend_factory()
            except ImportError as e:
                logger.error(f"python-evdev is not installed, the pad cannot be read: {e}")
                return None
        return self._backend

    async def attach(self) -> bool:
        """One scan: grab every matching node. True when at least one node is grabbed."""
        backend = self._ensure_backend()
        ids = self._config.device()
        if backend is None or ids is None:
            return False
        try:
            paths = find_pad_nodes(ids[0], ids[1], backend.list_devices, self._sysfs_root)
        except OSError as e:
            logger.warning(f"Listing input devices failed: {e}")
            return False
        if not paths:
            return False
        with tracer.start_as_current_span("operator_pad.device_attach") as span:
            span.set_attribute("pad.device", f"{ids[0]}:{ids[1]}")
            devices = []
            for path in paths:
                device = await grab_node(backend, path, self._sleep)
                if device is not None:
                    devices.append(device)
            span.set_attribute("pad.node_count", len(devices))
            if not devices:
                span.set_status(trace.Status(trace.StatusCode.ERROR, "no pad node could be grabbed"))
                return False
        self._devices = devices
        self._controller.attached(self._config.keymap())
        metrics.device_attached.set(1)
        logger.info(f"Pad attached: {[d.path for d in devices]}")
        return True

    def detach(self) -> None:
        for device in self._devices:
            with contextlib.suppress(Exception):
                device.ungrab()
            with contextlib.suppress(Exception):
                device.close()
        if self._devices:
            logger.warning("Pad detached; rescanning")
        self._devices = []
        self._controller.detached()
        metrics.device_attached.set(0)

    async def read_until_gone(self) -> None:
        """Feed key events of every grabbed node to the controller in arrival order until a node fails."""
        queue: asyncio.Queue = asyncio.Queue()
        gone = object()

        async def pump(device: InputDevice) -> None:
            try:
                async for event in device.async_read_loop():
                    if event.type == EV_KEY:
                        await queue.put(event)
            except OSError as e:  # ENODEV on unplug
                logger.info(f"Pad node {device.path} gone: {e}")
            await queue.put(gone)

        pumps = [asyncio.create_task(pump(device)) for device in self._devices]
        try:
            while True:
                event = await queue.get()
                if event is gone:
                    return
                await self._controller.on_code(event.code, event.value, int(event.timestamp() * 1000))
        finally:
            for task in pumps:
                task.cancel()
            for task in pumps:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def run(self, stop: asyncio.Event) -> None:
        metrics.device_attached.set(0)
        while not stop.is_set():
            if await self.attach():
                reader = asyncio.create_task(self.read_until_gone())
                stopper = asyncio.create_task(stop.wait())
                await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
                for task in (reader, stopper):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                self.detach()
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._rescan_seconds)
