"""Pad input devices: sysfs match, exclusive grab, hotplug rescan (UC-015 Pre-1, A4, FR-195).

Nodes are matched by the USB vendor:product of `operator_pad.device` through sysfs
(``/sys/class/input/eventN/device/id/{vendor,product}``), so no other input device is opened (the
PS Move controllers have nodes there too, and their ids are denied whatever the flag says). A
matching node is the pad only when it reports a key code of the keymap (UC-015 BR-014). Every pad
node is grabbed (EVIOCGRAB); an ``EBUSY`` grab is retried, because an old container may still hold
it, and a node that still cannot be grabbed fails the whole attach (the next rescan tries again).
A pad that is missing or unplugged is rescanned for and never fails the service.

Each event is stamped with the monotonic clock when it is read (BR-007): the kernel's evdev time
is CLOCK_REALTIME, which jumps when a Pi without an RTC syncs its clock.

python-evdev is imported lazily in `evdev_backend()` only, so the tests (and a host without the
binding) run against a fake backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import time
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

EV_SYN, EV_KEY = 0, 1  # linux/input-event-codes.h
SYN_DROPPED = 3
LED_SCROLLL = 2
SYSFS_INPUT = Path("/sys/class/input")
RESCAN_SECONDS = 2.0
GRAB_RETRIES = 5
GRAB_RETRY_SECONDS = 0.5
# PS Move (ZCM1, ZCM2): never opened or grabbed, whatever operator_pad.device says (UC-015 BR-014).
DENIED_USB_IDS = frozenset({("054c", "03d5"), ("054c", "0c5e")})


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


class InputDevice(Protocol):
    path: str

    def grab(self) -> None: ...
    def ungrab(self) -> None: ...
    def close(self) -> None: ...
    def set_led(self, led: int, value: int) -> None: ...
    def capabilities(self, verbose: bool = False, absinfo: bool = True) -> dict[int, list]: ...
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
    """The /dev/input/eventN paths whose sysfs USB ids are vendor:product, in a stable order.

    Denied ids (the PS Move) match nothing (BR-014).
    """
    if (vendor.lower(), product.lower()) in DENIED_USB_IDS:
        return []
    matches = []
    for path in list_devices():
        node = Path(path).name
        if _read_id(sysfs_root, node, "vendor") == vendor and _read_id(sysfs_root, node, "product") == product:
            matches.append(path)
    return sorted(matches)


def open_node(backend: Backend, path: str, warn: Callable[[str], None] = logger.warning) -> InputDevice | None:
    try:
        return backend.open(path)
    except OSError as e:
        warn(f"Opening pad node {path} failed: {e}")
        metrics.grab_failures_total.labels(reason="error").inc()
        return None


def reports_keys(device: InputDevice, key_codes: frozenset[int]) -> bool:
    """BR-014: the node reports at least one key code of the keymap (not a mouse or consumer node)."""
    try:
        keys = device.capabilities(absinfo=False).get(EV_KEY, [])
    except Exception as e:
        logger.warning(f"Reading the capabilities of {device.path} failed: {e}")
        return False
    return any(code in key_codes for code in keys)


async def grab_node(
    backend: Backend,
    path: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    retries: int = GRAB_RETRIES,
    delay: float = GRAB_RETRY_SECONDS,
) -> InputDevice | None:
    """Open and grab one node; retry EBUSY up to ``retries`` times. None when it cannot be grabbed."""
    device = open_node(backend, path)
    if device is None:
        return None
    return await grab_device(device, path, sleep, retries, delay)


async def grab_device(
    device: InputDevice,
    path: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    retries: int = GRAB_RETRIES,
    delay: float = GRAB_RETRY_SECONDS,
    warn: Callable[[str], None] = logger.warning,
) -> InputDevice | None:
    """Grab an open node; retry EBUSY up to ``retries`` times. None (node closed) when it cannot be grabbed."""
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
            warn(f"Grabbing pad node {path} failed ({reason}): {e}")
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
        clock_ms: Callable[[], int] = monotonic_ms,
    ) -> None:
        self._config = config
        self._controller = controller
        self._backend_factory = backend_factory
        self._backend: Backend | None = None
        self._sysfs_root = sysfs_root
        self._sleep = sleep
        self._rescan_seconds = rescan_seconds
        self._clock_ms = clock_ms
        self._denied_warned = False
        # Attach failures already warned about: a rescan every 2 s logs each one once (#2101), until an attach works.
        self._warned: set[str] = set()
        self._devices: list[InputDevice] = []

    def set_lock_led(self, on: bool) -> None:
        """Scroll Lock LED of the pad while locked (UC-015 A6); best effort on every grabbed node."""
        for device in self._devices:
            try:
                device.set_led(LED_SCROLLL, 1 if on else 0)
            except Exception as e:
                logger.debug(f"Scroll Lock LED on {device.path}: {e}")

    def _warn_once(self, message: str) -> None:
        if message in self._warned:
            logger.debug(message)
            return
        self._warned.add(message)
        logger.warning(message)

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
        if ids in DENIED_USB_IDS:
            if not self._denied_warned:
                self._denied_warned = True
                logger.warning(f"operator_pad.device={ids[0]}:{ids[1]} is a PS Move controller: never grabbed")
            return False
        try:
            paths = find_pad_nodes(ids[0], ids[1], backend.list_devices, self._sysfs_root)
        except OSError as e:
            logger.warning(f"Listing input devices failed: {e}")
            return False
        if not paths:
            return False
        keymap = self._config.keymap()
        key_codes = frozenset(keymap)
        with tracer.start_as_current_span("operator_pad.device_attach") as span:
            span.set_attribute("pad.device", f"{ids[0]}:{ids[1]}")
            devices: list[InputDevice] = []
            failed: list[str] = []
            for path in paths:
                device = open_node(backend, path, self._warn_once)
                if device is None:
                    failed.append(path)
                    continue
                if not reports_keys(device, key_codes):
                    logger.debug(f"{path} reports no key of the keymap: not a pad node, left alone")
                    with contextlib.suppress(Exception):
                        device.close()
                    continue
                if await grab_device(device, path, self._sleep, warn=self._warn_once) is None:
                    failed.append(path)
                else:
                    devices.append(device)
            span.set_attribute("pad.node_count", len(devices))
            if failed or not devices:
                # BR-014: a pad node left ungrabbed would type into the console; release all, retry at the rescan.
                self._release(devices)
                reason = f"pad nodes not grabbed: {failed}" if failed else "no pad node reports a keymap key"
                self._warn_once(f"Pad attach failed ({reason}); retrying at the next rescan")
                span.set_status(trace.Status(trace.StatusCode.ERROR, reason))
                return False
        self._devices = devices
        self._warned.clear()
        self._controller.attached(keymap)
        metrics.device_attached.set(1)
        logger.info(f"Pad attached: {[d.path for d in devices]}")
        return True

    @staticmethod
    def _release(devices: list[InputDevice]) -> None:
        for device in devices:
            with contextlib.suppress(Exception):
                device.ungrab()
            with contextlib.suppress(Exception):
                device.close()

    def detach(self) -> None:
        self._release(self._devices)
        if self._devices:
            logger.warning("Pad detached; rescanning")
        self._devices = []
        self._controller.detached()
        metrics.device_attached.set(0)

    async def read_until_gone(self) -> None:
        """Feed key events of every grabbed node to the controller in arrival order until a node fails."""
        queue: asyncio.Queue = asyncio.Queue()
        gone = object()
        dropped = object()

        async def pump(device: InputDevice) -> None:
            # Any end of the read loop (unplug, a binding error, a normal end) is the device gone (A4).
            try:
                async for event in device.async_read_loop():
                    if event.type == EV_KEY:
                        # Stamped when read, before the queue: the status reads of earlier events never skew it.
                        queue.put_nowait((event.code, event.value, self._clock_ms()))
                    elif event.type == EV_SYN and event.code == SYN_DROPPED:
                        queue.put_nowait(dropped)
            except OSError as e:  # ENODEV on unplug
                logger.info(f"Pad node {device.path} gone: {e}")
            except Exception as e:
                logger.warning(f"Reading pad node {device.path} failed: {e!r}")
            finally:
                queue.put_nowait(gone)

        pumps = [asyncio.create_task(pump(device)) for device in self._devices]
        try:
            while True:
                item = await queue.get()
                if item is gone:
                    return
                if item is dropped:
                    self._controller.events_dropped()
                    continue
                await self._controller.on_code(*item)
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
            # After a detach too (A4): a node that fails right after the attach never makes a busy loop.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._rescan_seconds)
