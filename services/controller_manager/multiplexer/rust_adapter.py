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

from lib.controller_constants import normalize_serial
from services.controller_manager.multiplexer.adapter import ControllerIOAdapter, DiscoveryFailedError
from services.controller_manager.multiplexer.hid_stream import HidStreamMixin
from services.controller_manager.multiplexer.hid_stream import (
    sensor_data_to_dict as _sensor_data_to_dict,  # noqa: F401  (tests import it here)
)

logger = logging.getLogger(__name__)

# Deadline of every unary rust-hid call (Discover, Open, Close, CloseAll; #2125).
# Without it a wedged rust-hid froze discovery for as long as it was wedged. A
# failed or timed-out Discover raises DiscoveryFailedError (not "no controllers");
# a failed Open returns False.
RPC_TIMEOUT_S = 5.0


class RustServiceAdapter(HidStreamMixin, ControllerIOAdapter):
    """I/O adapter delegating to the Rust HID gRPC service via bidir stream."""

    _stream_thread_name = "rust-adapter-stream"
    _stream_peer = "rust-hid service"

    def __init__(self):
        from proto import psmove_hid_pb2_grpc

        host = os.getenv("RUST_HID_HOST", "localhost")
        port = os.getenv("RUST_HID_PORT", "50058")
        address = f"{host}:{port}"

        # Sync channel with client metrics (outermost, #1426) and trace context propagation:
        # adapter methods are blocking.
        from lib.grpc_metrics import get_sync_metrics_interceptors
        from lib.grpc_tracing import get_sync_context_propagation_interceptors

        channel = grpc.insecure_channel(
            address,
            options=[
                ("grpc.keepalive_time_ms", 30000),
                ("grpc.keepalive_timeout_ms", 5000),
                ("grpc.keepalive_permit_without_calls", True),
                ("grpc.max_receive_message_length", 10 * 1024 * 1024),
            ],
        )
        interceptors = [
            *get_sync_metrics_interceptors(),
            *get_sync_context_propagation_interceptors(server_address=host, server_port=int(port)),
        ]
        self._channel = grpc.intercept_channel(channel, *interceptors)
        self._stub = psmove_hid_pb2_grpc.ControllerIOServiceStub(self._channel)

        # Stream state
        self._command_queue: queue.Queue = queue.Queue(maxsize=256)
        self._latest_data: dict[str, dict] = {}
        self._last_state: dict[str, dict] = {}  # cached last-known state per serial
        self._trigger_latch: set[str] = set()  # trigger seen down since the last game frame (#1837)
        self._data_lock = threading.Lock()
        # Serials closed by the multiplexer (loser / switched / gone). Polls run on
        # the event loop while discovery closes serials in a worker thread (#1447),
        # so state writes for a closed serial are dropped until it is reopened
        # or rediscovered. Guarded by _data_lock.
        self._closed: set[str] = set()
        self._init_stream_state()

        # Adapter affinity cache (serial -> BT adapter name, e.g. "hci0")
        self._adapter_names: dict[str, str] = {}

    @property
    def adapter_type(self) -> str:
        return "rust"

    def discover(
        self,
        force: bool = False,
        verify_only: bool = False,
        exclude_serials: list[str] | None = None,
    ) -> list[str]:
        from proto import psmove_hid_pb2

        self._ensure_stream()

        try:
            response = self._stub.Discover(
                psmove_hid_pb2.DiscoverRequest(
                    force=force,
                    verify_only=verify_only,
                    exclude_serials=exclude_serials or [],
                ),
                timeout=RPC_TIMEOUT_S,
            )
            serials = []
            for c in response.controllers:
                serial = normalize_serial(c.serial)
                serials.append(serial)
                if c.adapter_name:
                    self._adapter_names[serial] = c.adapter_name
                else:
                    # An empty name (sysfs lookup failed, USB) must not keep the
                    # previous hciN: the dashboard card would show a stale adapter.
                    self._adapter_names.pop(serial, None)
            with self._data_lock:
                self._closed.difference_update(serials)
            return serials
        except grpc.RpcError as e:
            logger.warning(f"Discover RPC failed: {e}")
            raise DiscoveryFailedError(str(e)) from e

    def get_adapter_for_serial(self, serial: str) -> str | None:
        return self._adapter_names.get(serial) or None

    def open(self, serial: str) -> bool:
        from proto import psmove_hid_pb2

        with self._data_lock:
            self._closed.discard(serial)
        self._ensure_stream()

        try:
            response = self._stub.Open(psmove_hid_pb2.OpenRequest(serial=serial), timeout=RPC_TIMEOUT_S)
            return response.success
        except grpc.RpcError as e:
            logger.warning(f"Open RPC failed for {serial}: {e}")
            return False

    def consume_trigger_press(self, serial: str) -> bool:
        """True when the trigger was down at any sample since the last call (#1837)."""
        with self._data_lock:
            if serial in self._trigger_latch:
                self._trigger_latch.discard(serial)
                return True
            return False

    def poll(self, serial: str) -> dict | None:
        # All state access under _data_lock: close() runs in the discovery
        # worker thread while poll() runs on the event loop (#1447).
        with self._data_lock:
            if serial in self._closed:
                return None
            new_data = self._latest_data.pop(serial, None)
            if new_data is not None:
                self._last_state[serial] = new_data
                return new_data
            # No new stream data — return cached state so callers don't count as drop
            return self._last_state.get(serial)

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
            self._last_state.pop(serial, None)
            self._trigger_latch.discard(serial)
            self._closed.add(serial)
        self._adapter_names.pop(serial, None)

        try:
            self._stub.Close(psmove_hid_pb2.CloseRequest(serial=serial), timeout=RPC_TIMEOUT_S)
        except grpc.RpcError as e:
            logger.warning(f"Close RPC failed for {serial}: {e}")

    def close_all(self) -> None:
        from proto import psmove_hid_pb2

        # Cancel the StreamIO call: grpcio >=1.83 does not cancel a dropped call (#2266).
        self._stop_stream()

        with self._data_lock:
            self._latest_data.clear()
            self._last_state.clear()
            self._trigger_latch.clear()
            self._closed.clear()
        self._adapter_names.clear()

        try:
            self._stub.CloseAll(psmove_hid_pb2.CloseAllRequest(), timeout=RPC_TIMEOUT_S)
        except grpc.RpcError as e:
            logger.warning(f"CloseAll RPC failed: {e}")
