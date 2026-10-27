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


# A cue belongs to one controller at one hidraw node: a node number reused by another
# controller between two polls starts a new cue (#2091 review)
CueKey = tuple[bytes, str]  # (device_path, serial)


class _SupersededError(Exception):
    """A newer cue owns the hidraw node; this cue writes nothing more."""


class UsbLedCue:
    """One cue task per controller on USB: ``pairing`` -> ``paired`` / ``failed``.

    Writes to one hidraw node run one at a time and only for the cue started
    last on it: a cancelled cue's write already handed to a thread finishes
    before the next cue writes, and no frame of the old cue follows it.
    """

    def __init__(
        self,
        write: Callable[[bytes, Rgb], None] = write_led,
        sleep: Callable[[float], Awaitable[None]] = _real_sleep,
    ):
        self._write = write
        self._sleep = sleep
        self._tasks: dict[CueKey, asyncio.Task] = {}
        # Last cue started per controller ("pairing", "paired", "failed")
        self.states: dict[CueKey, str] = {}
        # Per hidraw node: the lock held across a write, and the generation of the cue allowed to write
        self._locks: dict[bytes, asyncio.Lock] = {}
        self._generations: dict[bytes, int] = {}

    def pairing(self, device_path: bytes, serial: str) -> None:
        """Pulse while the controller waits for or goes through pairing."""
        key = (device_path, serial)
        if self.states.get(key) == "pairing" and self._running(key):
            return
        self._start(key, "pairing", self._pulse)

    def paired(self, device_path: bytes, serial: str) -> None:
        """Three short green flashes, then off."""
        self._start((device_path, serial), "paired", self._flash)

    def failed(self, device_path: bytes, serial: str) -> None:
        """Steady red until the next cue or until the controller leaves USB."""
        self._start((device_path, serial), "failed", self._hold_red)

    def stop(self, device_path: bytes, serial: str) -> None:
        """End the cue of a controller (it left USB)."""
        self._stop((device_path, serial))

    def stop_pulse(self, device_path: bytes, serial: str) -> None:
        """End a pulse of a controller that is skipped after all (e.g. unpaired meanwhile, UC-005 A8)."""
        if self.states.get((device_path, serial)) == "pairing":
            self._stop((device_path, serial))

    def keep_only(self, controllers: Iterable[CueKey]) -> None:
        """End the cues of controllers no longer listed on USB, or now listed under another node."""
        keep = set(controllers)
        for key in [k for k in self.states if k not in keep]:
            self._stop(key)

    async def close(self) -> None:
        """Cancel every cue (daemon shutdown)."""
        tasks = list(self._tasks.values())
        for path in {key[0] for key in self._tasks}:
            self._next_generation(path)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self.states.clear()

    def _running(self, key: CueKey) -> bool:
        task = self._tasks.get(key)
        return task is not None and not task.done()

    def _next_generation(self, device_path: bytes) -> int:
        generation = self._generations.get(device_path, 0) + 1
        self._generations[device_path] = generation
        return generation

    def _stop(self, key: CueKey) -> None:
        if key in self.states or key in self._tasks:
            self._next_generation(key[0])
        self._cancel(key)
        self.states.pop(key, None)

    def _cancel(self, key: CueKey) -> None:
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    def _start(self, key: CueKey, state: str, cue: Callable[[bytes, int], Coroutine[None, None, None]]) -> None:
        device_path = key[0]
        # One cue per hidraw node: a cue of another controller on this node ends too
        for other in [k for k in {*self._tasks, *self.states} if k[0] == device_path]:
            self._cancel(other)
            self.states.pop(other, None)
        generation = self._next_generation(device_path)
        self.states[key] = state
        self._tasks[key] = asyncio.get_running_loop().create_task(self._guarded(key, cue, generation))

    async def _guarded(
        self, key: CueKey, cue: Callable[[bytes, int], Coroutine[None, None, None]], generation: int
    ) -> None:
        try:
            await cue(key[0], generation)
        except _SupersededError:
            pass
        except OSError as e:
            logger.debug(f"LED cue on {key[0]!r} ended: {e}")
        except Exception as e:  # never let a cue fail the daemon
            logger.debug(f"LED cue on {key[0]!r} failed: {e}")

    async def _show(self, device_path: bytes, generation: int, rgb: Rgb) -> None:
        lock = self._locks.setdefault(device_path, asyncio.Lock())
        async with lock:
            if self._generations.get(device_path) != generation:
                raise _SupersededError
            write = asyncio.ensure_future(asyncio.to_thread(self._write, device_path, rgb))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                # A thread cannot be stopped: keep the node locked until this frame landed
                with contextlib.suppress(Exception):
                    await write
                raise

    async def _pulse(self, device_path: bytes, generation: int) -> None:
        elapsed = 0.0
        while True:
            level = pulse_level(elapsed)
            await self._show(device_path, generation, (level, level, level))
            await self._sleep(FRAME_SECONDS)
            elapsed += FRAME_SECONDS

    async def _flash(self, device_path: bytes, generation: int) -> None:
        for _ in range(FLASH_COUNT):
            await self._show(device_path, generation, GREEN)
            await self._sleep(FLASH_ON_SECONDS)
            await self._show(device_path, generation, OFF)
            await self._sleep(FLASH_OFF_SECONDS)

    async def _hold_red(self, device_path: bytes, generation: int) -> None:
        while True:
            await self._show(device_path, generation, RED)
            await self._sleep(REFRESH_SECONDS)
