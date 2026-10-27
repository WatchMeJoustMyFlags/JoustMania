"""LED cue on a PS Move on USB while the daemon pairs it (UC-001 BR-010, #2091).

The LED is the primary feedback channel at a venue. The daemon writes the
PS Move output report 0x06 (LED colour, no rumble) to the controller's USB
HID node itself, whichever pairing backend is selected: the node is the
``device_path`` the backend listed, and the daemon runs privileged with /dev.

- pulse: slow white pulse while the controller waits for or goes through pairing
- paired: three short green flashes, then off (the host can unplug it)
- failed: steady red until the next attempt or until it leaves USB

A write that fails (e.g. the controller was unplugged) ends that cue quietly;
the cue never fails a pairing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine, Iterable

import hidraw as hid

from lib.psmove_hid import build_output_report

logger = logging.getLogger("psmove-pairing")

# Bound at import: tests patch asyncio.sleep globally, and a cue loop must still yield
_real_sleep = asyncio.sleep

Rgb = tuple[int, int, int]
OFF: Rgb = (0, 0, 0)
GREEN: Rgb = (0, 255, 0)
RED: Rgb = (255, 0, 0)

FRAME_SECONDS = 0.1
PULSE_PERIOD_SECONDS = 2.0
PULSE_MIN, PULSE_MAX = 16, 255
FLASH_COUNT = 3
FLASH_ON_SECONDS = 0.25
FLASH_OFF_SECONDS = 0.25
# The PS Move turns its LED off when it is not refreshed for a few seconds
REFRESH_SECONDS = 1.0


def write_led(device_path: bytes, rgb: Rgb) -> None:
    """Write one LED colour to a PS Move on USB through its hidraw node (blocking)."""
    device = hid.device()
    device.open_path(device_path)
    try:
        device.write(build_output_report(*rgb))
    finally:
        device.close()


def pulse_level(elapsed: float) -> int:
    """Brightness of the white pulse ``elapsed`` seconds into it (triangle wave)."""
    phase = (elapsed % PULSE_PERIOD_SECONDS) / PULSE_PERIOD_SECONDS
    ramp = 1.0 - abs(2.0 * phase - 1.0)  # 0 -> 1 -> 0 over one period
    return round(PULSE_MIN + (PULSE_MAX - PULSE_MIN) * ramp)


class UsbLedCue:
    """One cue task per USB device path: ``pairing`` -> ``paired`` / ``failed``."""

    def __init__(
        self,
        write: Callable[[bytes, Rgb], None] = write_led,
        sleep: Callable[[float], Awaitable[None]] = _real_sleep,
    ):
        self._write = write
        self._sleep = sleep
        self._tasks: dict[bytes, asyncio.Task] = {}
        # Last cue started per device path ("pairing", "paired", "failed")
        self.states: dict[bytes, str] = {}

    def pairing(self, device_path: bytes) -> None:
        """Pulse while the controller waits for or goes through pairing."""
        if self.states.get(device_path) == "pairing" and self._running(device_path):
            return
        self._start(device_path, "pairing", lambda: self._pulse(device_path))

    def paired(self, device_path: bytes) -> None:
        """Three short green flashes, then off."""
        self._start(device_path, "paired", lambda: self._flash(device_path))

    def failed(self, device_path: bytes) -> None:
        """Steady red until the next cue or until the controller leaves USB."""
        self._start(device_path, "failed", lambda: self._hold(device_path, RED))

    def stop(self, device_path: bytes) -> None:
        """End the cue of a controller (it left USB)."""
        self._cancel(device_path)
        self.states.pop(device_path, None)

    def stop_pulse(self, device_path: bytes) -> None:
        """End a pulse of a controller that is skipped after all (e.g. unpaired meanwhile, UC-005 A8)."""
        if self.states.get(device_path) == "pairing":
            self.stop(device_path)

    def keep_only(self, device_paths: Iterable[bytes]) -> None:
        """End the cues of controllers no longer listed on USB."""
        keep = set(device_paths)
        for path in [p for p in self.states if p not in keep]:
            self.stop(path)

    async def close(self) -> None:
        """Cancel every cue (daemon shutdown)."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self.states.clear()

    def _running(self, device_path: bytes) -> bool:
        task = self._tasks.get(device_path)
        return task is not None and not task.done()

    def _cancel(self, device_path: bytes) -> None:
        task = self._tasks.pop(device_path, None)
        if task is not None and not task.done():
            task.cancel()

    def _start(self, device_path: bytes, state: str, cue: Callable[[], Coroutine[None, None, None]]) -> None:
        self._cancel(device_path)
        self.states[device_path] = state
        self._tasks[device_path] = asyncio.get_running_loop().create_task(self._guarded(device_path, cue))

    async def _guarded(self, device_path: bytes, cue: Callable[[], Coroutine[None, None, None]]) -> None:
        try:
            await cue()
        except OSError as e:
            logger.debug(f"LED cue on {device_path!r} ended: {e}")
        except Exception as e:  # never let a cue fail the daemon
            logger.debug(f"LED cue on {device_path!r} failed: {e}")

    async def _show(self, device_path: bytes, rgb: Rgb) -> None:
        await asyncio.to_thread(self._write, device_path, rgb)

    async def _pulse(self, device_path: bytes) -> None:
        elapsed = 0.0
        while True:
            level = pulse_level(elapsed)
            await self._show(device_path, (level, level, level))
            await self._sleep(FRAME_SECONDS)
            elapsed += FRAME_SECONDS

    async def _flash(self, device_path: bytes) -> None:
        for _ in range(FLASH_COUNT):
            await self._show(device_path, GREEN)
            await self._sleep(FLASH_ON_SECONDS)
            await self._show(device_path, OFF)
            await self._sleep(FLASH_OFF_SECONDS)

    async def _hold(self, device_path: bytes, rgb: Rgb) -> None:
        while True:
            await self._show(device_path, rgb)
            await self._sleep(REFRESH_SECONDS)
