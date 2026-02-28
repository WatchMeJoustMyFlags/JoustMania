"""Tests for RustServiceAdapter stub."""

import pytest

from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter


class TestRustServiceAdapter:
    """All methods raise NotImplementedError until Rust service is implemented."""

    def test_adapter_type(self):
        adapter = RustServiceAdapter()
        assert adapter.adapter_type == "rust"

    def test_discover_raises(self):
        adapter = RustServiceAdapter()
        with pytest.raises(NotImplementedError, match="Rust I/O service not yet implemented"):
            adapter.discover()

    def test_open_raises(self):
        adapter = RustServiceAdapter()
        with pytest.raises(NotImplementedError, match="Rust I/O service not yet implemented"):
            adapter.open("AA:BB:CC:DD:EE:FF")

    def test_poll_raises(self):
        adapter = RustServiceAdapter()
        with pytest.raises(NotImplementedError, match="Rust I/O service not yet implemented"):
            adapter.poll("AA:BB:CC:DD:EE:FF")

    def test_set_output_raises(self):
        adapter = RustServiceAdapter()
        with pytest.raises(NotImplementedError, match="Rust I/O service not yet implemented"):
            adapter.set_output("AA:BB:CC:DD:EE:FF", 255, 0, 0, 128)

    def test_close_raises(self):
        adapter = RustServiceAdapter()
        with pytest.raises(NotImplementedError, match="Rust I/O service not yet implemented"):
            adapter.close("AA:BB:CC:DD:EE:FF")
