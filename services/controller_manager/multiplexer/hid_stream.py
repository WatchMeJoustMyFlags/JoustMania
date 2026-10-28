"""StreamIO lifecycle shared by the rust-hid and python-hid adapters.

Both adapters hold one bidirectional ``StreamIO`` call to their HID service,
read by a background thread. Since grpcio 1.83 a call that is merely dropped
is no longer cancelled, so the call is cancelled explicitly: ``close_all()``
cancels it, and the reader thread cancels its own call whenever it ends for
another reason (the stream failed, a frame it could not convert), before the
adapter can reopen, #2266. Cancelling a call that already finished is a no-op.
Without that the HID service kept serving a dropped stream until the channel
closed.

Every started stream gets a generation number. A thread whose generation is no
longer current stops reading, stops sending commands and leaves the adapter's
stream state alone, so a stream that ends late never marks the current one as
stopped. Locks: ``_stream_lock`` guards the stream handle, thread and
generation; it is never held together with ``_data_lock``, and ``cancel()`` and
``join()`` run outside it, so stopping a stream cannot block the 100 Hz poll.

``close_all()`` is final (#2275): it sets ``_stream_closed`` under
``_stream_lock``, and both ``_ensure_stream()`` and the reader check it under
the same lock before they open a call, so a discovery pass that races shutdown
opens no stream after it. A command that an exiting iterator took from the queue
but never handed to gRPC goes to ``_unsent_commands``, which the next iterator
sends first, so a rumble taken in the last ``get()`` window is not lost; a
command already yielded counts as sent and is never handed back (no duplicate).
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from typing import Any

import grpc

from lib.controller_constants import AxisKey, ButtonKey, StateKey, normalize_serial
from services.controller_manager.multiplexer.press_latch import latch_presses

logger = logging.getLogger(__name__)

# How long close_all() waits for the reader thread after cancelling its call.
STOP_JOIN_TIMEOUT_S = 2.0
# How long a reopen waits for the previous reader thread.
REOPEN_JOIN_TIMEOUT_S = 1.0
# How long one command-iterator wait on the queue lasts before it re-checks its stream.
COMMAND_POLL_S = 0.05


class HidStreamMixin:
    """Owns the ``StreamIO`` call of a HID adapter: start, read, cancel."""

    _stream_thread_name = "hid-adapter-stream"
    _stream_peer = "HID service"

    # Set by the adapter's __init__ (or a test helper): _stub, _command_queue,
    # _latest_data, _trigger_latch, _data_lock, _closed.
    _stub: Any
    _command_queue: queue.Queue
    _latest_data: dict[str, dict]
    _trigger_latch: set[str]
    _data_lock: threading.Lock
    _closed: set[str]

    def _init_stream_state(self) -> None:
        self._stream_lock = threading.Lock()
        self._stream_thread: threading.Thread | None = None
        self._stream_call: Any = None
        self._stream_generation = 0
        self._stream_running = False
        self._stream_closed = False  # set by _close_stream() (close_all): no stream opens again
        # Commands an exiting iterator took but never yielded; the next iterator sends them first.
        self._unsent_commands: deque = deque()
        # Buttons seen down since the last poll, per serial (#2312); guarded by _data_lock.
        self._press_latch: dict[str, set[str]] = {}

    def _stream_is_current(self, generation: int) -> bool:
        return self._stream_running and self._stream_generation == generation

    def _ensure_stream(self) -> None:
        """Start the bidir stream if not already running."""
        with self._stream_lock:
            if self._stream_running or self._stream_closed:
                return
            old_thread = self._stream_thread
            self._stream_generation += 1
            generation = self._stream_generation
            self._stream_running = True
            thread = threading.Thread(
                target=self._stream_loop,
                args=(generation,),
                name=self._stream_thread_name,
                daemon=True,
            )
            self._stream_thread = thread

        # Wait for the old thread to finish before starting a new one
        if old_thread is not None:
            old_thread.join(timeout=REOPEN_JOIN_TIMEOUT_S)
        thread.start()
        logger.info(f"Started bidir stream to {self._stream_peer}")

    def _close_stream(self) -> None:
        """Stop the stream for good (close_all): nothing opens a stream afterwards (#2275)."""
        with self._stream_lock:
            self._stream_closed = True
        self._stop_stream()

    def _stop_stream(self) -> None:
        """Stop the stream: cancel the call and wait for the reader. Idempotent."""
        with self._stream_lock:
            self._stream_running = False
            self._stream_generation += 1  # the old thread's finally no longer touches state
            call, self._stream_call = self._stream_call, None
            thread, self._stream_thread = self._stream_thread, None

        if call is not None:
            call.cancel()
        # is_alive(): a thread _ensure_stream() created but has not started yet cannot be
        # joined; once started it sees the stop before it opens a call.
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=STOP_JOIN_TIMEOUT_S)

    def _stream_loop(self, generation: int | None = None) -> None:
        """Background thread: read one StreamIO call until it ends or is cancelled."""
        if generation is None:
            generation = self._stream_generation

        def command_iterator():
            while self._stream_is_current(generation):
                cmd = self._take_command()
                if cmd is None:
                    continue
                if not self._stream_is_current(generation):
                    # Taken while this stream stopped: gRPC would drop it, so the next
                    # stream sends it, ahead of anything queued after it (#2275).
                    self._unsent_commands.appendleft(cmd)
                    return
                yield cmd

        call = None
        try:
            with self._stream_lock:
                opening = self._stream_is_current(generation)
            if not opening:
                return  # stopped or closed (close bumps the generation) before the open: open nothing (#2275)
            call = self._stub.StreamIO(command_iterator())
            with self._stream_lock:
                current = self._stream_is_current(generation)
                if current:
                    self._stream_call = call
            if not current:
                # Stopped while the call was being opened: nobody else holds it.
                call.cancel()
                return
            for sensor_data in call:
                if self._stream_generation != generation:
                    break
                serial = normalize_serial(sensor_data.serial)
                state = sensor_data_to_dict(sensor_data)
                state[StateKey.SERIAL] = serial
                with self._data_lock:
                    if serial not in self._closed:
                        self._latest_data[serial] = state
                        if state.get(ButtonKey.TRIGGER):
                            # Latched until a game frame delivers it (#1837).
                            self._trigger_latch.add(serial)
                        latch_presses(self._press_latch, serial, state)  # until the next poll (#2312)
        except grpc.RpcError as e:
            if self._stream_is_current(generation):
                logger.warning(f"StreamIO error: {e}")
        finally:
            # Whatever ended the reader (stop, stream error, a bad frame), its call must
            # not stay live on the HID service; a no-op when the call already finished.
            # Cancelled before _stream_running drops, so a reopen never overlaps it.
            if call is not None:
                call.cancel()
            with self._stream_lock:
                if self._stream_generation == generation:
                    self._stream_running = False
                    if self._stream_call is call:
                        self._stream_call = None
            logger.info("Bidir stream ended")

    def _take_command(self):
        """The next command to send: a handed-back one first, else the queue (None after a quiet wait)."""
        try:
            return self._unsent_commands.popleft()
        except IndexError:
            pass
        try:
            return self._command_queue.get(timeout=COMMAND_POLL_S)
        except queue.Empty:
            return None


def sensor_data_to_dict(data) -> dict:
    """Convert a SensorData proto message to the state dict expected by MultiplexerBackend."""
    return {
        StateKey.SERIAL: data.serial,
        StateKey.BATTERY: data.battery,
        StateKey.TRIGGER: data.trigger,
        ButtonKey.MOVE: data.button_move,
        ButtonKey.TRIGGER: data.button_trigger,
        ButtonKey.PS: data.button_ps,
        ButtonKey.CROSS: data.button_cross,
        ButtonKey.CIRCLE: data.button_circle,
        ButtonKey.SQUARE: data.button_square,
        ButtonKey.TRIANGLE: data.button_triangle,
        ButtonKey.SELECT: data.button_select,
        ButtonKey.START: data.button_start,
        StateKey.ACCEL: {
            AxisKey.X: data.accel_x,
            AxisKey.Y: data.accel_y,
            AxisKey.Z: data.accel_z,
        },
        StateKey.GYRO: {
            AxisKey.X: data.gyro_x,
            AxisKey.Y: data.gyro_y,
            AxisKey.Z: data.gyro_z,
        },
        StateKey.TEMPERATURE: data.temperature,
    }
