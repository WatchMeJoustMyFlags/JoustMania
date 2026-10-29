"""Controller I/O adapter backed by the Python HID gRPC service (python-hid, port 50059).

Same class as the rust adapter (``HidServiceAdapter``, ``hid_service_adapter.py``,
#2352), so a canary compares rust-hid and python-hid apples to apples.
"""

from __future__ import annotations

import logging

from services.controller_manager.multiplexer.hid_service_adapter import HidServiceAdapter

logger = logging.getLogger(__name__)


class PythonHidAdapter(HidServiceAdapter):
    """I/O adapter delegating to the Python HID gRPC service via bidir stream."""

    _adapter_type = "python"
    _env_prefix = "PYTHON_HID"
    _default_port = "50059"
    _logger = logger
    _stream_thread_name = "python-adapter-stream"
    _stream_peer = "python service"
