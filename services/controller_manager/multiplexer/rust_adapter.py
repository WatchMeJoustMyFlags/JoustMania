"""Controller I/O adapter backed by the Rust HID gRPC service.

Connects to the rust-hid service (port 50058) via a sync gRPC channel.
Lifecycle RPCs (discover, open, close) are unary. Real-time I/O (poll,
set_output) uses a bidirectional stream: the server pushes SensorData and
the client sends IOCommands for LED/rumble.

All methods are sync (blocking) — called via asyncio.to_thread() by
MultiplexerBackend, matching the ControllerIOAdapter contract.
"""

from __future__ import annotations

import logging
import os
import queue
import threading

import grpc

from lib.controller_constants import AxisKey, ButtonKey, StateKey
from services.controller_manager.multiplexer.adapter import ControllerIOAdapter

logger = logging.getLogger(__name__)


class RustServiceAdapter(ControllerIOAdapter):
    """I/O adapter delegating to the Rust HID gRPC service via bidir stream."""

    def __init__(self):
        from proto import psmove_hid_pb2_grpc

        host = os.getenv("RUST_HID_HOST", "localhost")
        port = os.getenv("RUST_HID_PORT", "50058")
        address = f"{host}:{port}"

        # Sync channel — adapter methods are blocking
        self._channel = grpc.insecure_channel(
            address,
            options=[
                ("grpc.keepalive_time_ms", 30000),
                ("grpc.keepalive_timeout_ms", 5000),
                ("grpc.keepalive_permit_without_calls", True),
                ("grpc.max_receive_message_length", 10 * 1024 * 1024),
            ],
        )
        self._stub = psmove_hid_pb2_grpc.ControllerIOServiceStub(self._channel)

        # Stream state
        self._command_queue: queue.Queue = queue.Queue(maxsize=256)
        self._latest_data: dict[str, dict] = {}
        self._data_lock = threading.Lock()
        self._stream_thread: threading.Thread | None = None
        self._stream_running = False

        # Adapter affinity cache (serial -> BT adapter name, e.g. "hci0")
        self._adapter_names: dict[str, str] = {}

    @property
    def adapter_type(self) -> str:
        return "rust"

    def _ensure_stream(self) -> None:
        """Start the bidir stream if not already running."""
        if self._stream_running:
            return

        self._stream_running = True
        self._stream_thread = threading.Thread(
            target=self._stream_loop,
            name="rust-adapter-stream",
            daemon=True,
        )
        self._stream_thread.start()
        logger.info("Started bidir stream to rust-hid service")

    def _stream_loop(self) -> None:
        """Background thread: manage the bidir StreamIO stream."""

        def command_iterator():
            while self._stream_running:
                try:
                    cmd = self._command_queue.get(timeout=0.05)
                    yield cmd
                except queue.Empty:
                    continue

        try:
            responses = self._stub.StreamIO(command_iterator())
            for sensor_data in responses:
                state = _sensor_data_to_dict(sensor_data)
                with self._data_lock:
                    self._latest_data[sensor_data.serial] = state
        except grpc.RpcError as e:
            if self._stream_running:
                logger.warning(f"StreamIO error: {e}")
        finally:
            self._stream_running = False
            logger.info("Bidir stream ended")

    def discover(self, force: bool = False, verify_only: bool = False) -> list[str]:
        from proto import psmove_hid_pb2

        self._ensure_stream()

        try:
            response = self._stub.Discover(psmove_hid_pb2.DiscoverRequest(force=force, verify_only=verify_only))
            serials = []
            for c in response.controllers:
                serials.append(c.serial)
                if c.adapter_name:
                    self._adapter_names[c.serial] = c.adapter_name
            return serials
        except grpc.RpcError as e:
            logger.warning(f"Discover RPC failed: {e}")
            return []

    def get_adapter_for_serial(self, serial: str) -> str | None:
        return self._adapter_names.get(serial) or None

    def open(self, serial: str) -> bool:
        from proto import psmove_hid_pb2

        self._ensure_stream()

        try:
            response = self._stub.Open(psmove_hid_pb2.OpenRequest(serial=serial))
            return response.success
        except grpc.RpcError as e:
            logger.warning(f"Open RPC failed for {serial}: {e}")
            return False

    def poll(self, serial: str) -> dict | None:
        with self._data_lock:
            return self._latest_data.pop(serial, None)

    def set_output(self, serial: str, r: int, g: int, b: int, rumble: int) -> bool:
        from proto import psmove_hid_pb2

        if not self._stream_running:
            return False

        try:
            self._command_queue.put_nowait(
                psmove_hid_pb2.IOCommand(
                    serial=serial,
                    r=r,
                    g=g,
                    b=b,
                    rumble=rumble,
                )
            )
            return True
        except queue.Full:
            logger.debug(f"IOCommand queue full for {serial}")
            return False

    def close(self, serial: str) -> None:
        from proto import psmove_hid_pb2

        with self._data_lock:
            self._latest_data.pop(serial, None)
        self._adapter_names.pop(serial, None)

        try:
            self._stub.Close(psmove_hid_pb2.CloseRequest(serial=serial))
        except grpc.RpcError as e:
            logger.warning(f"Close RPC failed for {serial}: {e}")

    def close_all(self) -> None:
        from proto import psmove_hid_pb2

        # Stop the stream
        self._stream_running = False
        if self._stream_thread:
            self._stream_thread.join(timeout=2.0)
            self._stream_thread = None

        with self._data_lock:
            self._latest_data.clear()
        self._adapter_names.clear()

        try:
            self._stub.CloseAll(psmove_hid_pb2.CloseAllRequest())
        except grpc.RpcError as e:
            logger.warning(f"CloseAll RPC failed: {e}")


def _sensor_data_to_dict(data) -> dict:
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
