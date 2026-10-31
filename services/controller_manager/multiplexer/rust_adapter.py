"""Controller I/O adapter backed by the Rust HID gRPC service (rust-hid, port 50058).

The implementation is shared with the python-hid adapter: ``HidServiceAdapter``
(``hid_service_adapter.py``, #2352).
"""

from __future__ import annotations

import logging

from services.controller_manager.multiplexer.hid_service_adapter import (
    RPC_TIMEOUT_S,  # noqa: F401  (importers read the deadline here)
    HidServiceAdapter,
)
from services.controller_manager.multiplexer.hid_stream import (
    sensor_data_to_dict as _sensor_data_to_dict,  # noqa: F401  (tests import it here)
)

logger = logging.getLogger(__name__)


class RustServiceAdapter(HidServiceAdapter):
    """I/O adapter delegating to the Rust HID gRPC service via bidir stream."""

    _adapter_type = "rust"
    _env_prefix = "RUST_HID"
    _default_port = "50058"
    _logger = logger
    _stream_thread_name = "rust-adapter-stream"
    _stream_peer = "rust-hid service"
