"""Integration tests for backend wiring -- verifies MultiplexerBackend
correctly routes through adapters in a Docker Compose environment.

These tests validate the plumbing between controller_manager and its
adapters (hidapi + rust-hid gRPC), not actual hardware interaction.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from lib.telemetry import disable_telemetry_for_tests
from lib.otel_metrics import disable_metrics_for_tests

disable_telemetry_for_tests()
disable_metrics_for_tests()


class TestBackendWiring:
    """Verify adapter creation and routing logic."""

    def test_multiplexer_accepts_multiple_adapters(self):
        """MultiplexerBackend initializes with a list of adapters."""
        from services.controller_manager.multiplexer.mock_adapter import MockAdapter
        from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

        mock1 = MockAdapter(num_controllers=2)
        mock2 = MockAdapter(num_controllers=1)
        backend = MultiplexerBackend(adapters=[mock1, mock2])
        assert len(backend.adapters) == 2

    def test_multiplexer_requires_at_least_one_adapter(self):
        """MultiplexerBackend raises ValueError with empty adapter list."""
        from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

        with pytest.raises(ValueError, match="at least one adapter"):
            MultiplexerBackend(adapters=[])

    @pytest.mark.asyncio
    async def test_mock_adapter_discovery_and_state(self):
        """MockAdapter discovers controllers and returns state dicts."""
        from services.controller_manager.multiplexer.mock_adapter import MockAdapter
        from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

        adapter = MockAdapter(num_controllers=3)
        backend = MultiplexerBackend(adapters=[adapter])

        ok = await backend.initialize()
        assert ok is True

        serials = backend.get_connected_controllers()
        assert len(serials) == 3

        # Each controller should return a valid state dict
        for serial in serials:
            state = await backend.get_controller_state(serial)
            assert state is not None
            assert "serial" in state
            assert "accel" in state

        await backend.shutdown()

    @pytest.mark.asyncio
    async def test_led_and_rumble_round_trip(self):
        """Setting LED color and rumble stores state correctly."""
        from services.controller_manager.multiplexer.mock_adapter import MockAdapter
        from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

        adapter = MockAdapter(num_controllers=1)
        backend = MultiplexerBackend(adapters=[adapter])
        await backend.initialize()

        serials = backend.get_connected_controllers()
        serial = serials[0]

        # Set LED
        result = await backend.set_led_color(serial, 255, 0, 128)
        assert result is True

        # Set rumble
        result = await backend.set_rumble(serial, 200)
        assert result is True

        # Verify stored state
        assert backend._led_colors[serial] == (255, 0, 128)
        assert backend._rumble[serial] == 200

        await backend.shutdown()

    @pytest.mark.asyncio
    async def test_disconnect_cleans_up_state(self):
        """Disconnecting a controller removes all its tracked state."""
        from services.controller_manager.multiplexer.mock_adapter import MockAdapter
        from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

        adapter = MockAdapter(num_controllers=2)
        backend = MultiplexerBackend(adapters=[adapter])
        await backend.initialize()

        serials = backend.get_connected_controllers()
        serial_to_remove = serials[0]

        # Set some state first
        await backend.set_led_color(serial_to_remove, 100, 200, 50)

        # Disconnect
        result = await backend.disconnect_controller(serial_to_remove)
        assert result is True

        # State should be cleaned up
        assert serial_to_remove not in backend._led_colors
        assert serial_to_remove not in backend._serial_to_adapter

        await backend.shutdown()

    @pytest.mark.asyncio
    async def test_mock_adapter_state_includes_motion_data(self):
        """MockAdapter state includes accel and gyro with x/y/z axes."""
        from services.controller_manager.multiplexer.mock_adapter import MockAdapter
        from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

        adapter = MockAdapter(num_controllers=1)
        backend = MultiplexerBackend(adapters=[adapter])
        await backend.initialize()

        serials = backend.get_connected_controllers()
        state = await backend.get_controller_state(serials[0])

        assert state is not None
        assert "x" in state["accel"]
        assert "y" in state["accel"]
        assert "z" in state["accel"]
        assert "x" in state["gyro"]
        assert "y" in state["gyro"]
        assert "z" in state["gyro"]

        await backend.shutdown()


class TestRustAdapterWiring:
    """Verify RustServiceAdapter construction and interface compliance."""

    def test_rust_adapter_type(self):
        """RustServiceAdapter reports correct adapter_type."""
        from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter

        adapter = RustServiceAdapter()
        assert adapter.adapter_type == "rust"

    def test_rust_adapter_is_controller_io_adapter(self):
        """RustServiceAdapter implements ControllerIOAdapter."""
        from services.controller_manager.multiplexer.adapter import ControllerIOAdapter
        from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter

        adapter = RustServiceAdapter()
        assert isinstance(adapter, ControllerIOAdapter)

    def test_rust_adapter_discover_raises_not_implemented(self):
        """RustServiceAdapter.discover() raises NotImplementedError (stub)."""
        from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter

        adapter = RustServiceAdapter()
        with pytest.raises(NotImplementedError, match="Rust I/O service not yet implemented"):
            adapter.discover()

    def test_rust_adapter_interface_methods_exist(self):
        """RustServiceAdapter has all required ControllerIOAdapter methods."""
        from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter

        adapter = RustServiceAdapter()
        assert hasattr(adapter, "discover")
        assert hasattr(adapter, "open")
        assert hasattr(adapter, "close")
        assert hasattr(adapter, "poll")
        assert hasattr(adapter, "set_output")
        assert callable(adapter.discover)
        assert callable(adapter.open)
        assert callable(adapter.close)
        assert callable(adapter.poll)
        assert callable(adapter.set_output)


class TestHidapiAdapterWiring:
    """Verify HidapiAdapter construction and interface compliance."""

    def test_hidapi_adapter_type(self):
        """HidapiAdapter reports correct adapter_type."""
        from services.controller_manager.multiplexer.hidapi_adapter import HidapiAdapter

        adapter = HidapiAdapter()
        assert adapter.adapter_type == "hidapi"

    def test_hidapi_adapter_interface(self):
        """HidapiAdapter implements required ControllerIOAdapter methods."""
        from services.controller_manager.multiplexer.adapter import ControllerIOAdapter
        from services.controller_manager.multiplexer.hidapi_adapter import HidapiAdapter

        adapter = HidapiAdapter()
        assert isinstance(adapter, ControllerIOAdapter)

        # Check all abstract methods are implemented
        assert hasattr(adapter, "discover")
        assert hasattr(adapter, "open")
        assert hasattr(adapter, "close")
        assert hasattr(adapter, "close_all")
        assert hasattr(adapter, "poll")
        assert hasattr(adapter, "set_output")


class TestAdapterRoutingWiring:
    """Verify adapter routing and affinity plumbing."""

    def test_multiplexer_tracks_serial_to_adapter(self):
        """MultiplexerBackend maps serials to their owning adapter."""
        from services.controller_manager.multiplexer.mock_adapter import MockAdapter
        from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

        adapter = MockAdapter(num_controllers=2)
        backend = MultiplexerBackend(adapters=[adapter])

        # Discover and open controllers
        serials = adapter.discover()
        for serial in serials:
            adapter.open(serial)
            backend._serial_to_adapter[serial] = adapter

        # Verify all serials map to the adapter
        for serial in serials:
            assert backend._serial_to_adapter[serial] is adapter
            assert backend.get_adapter_type(serial) == "mock"

    def test_unknown_serial_returns_unknown_type(self):
        """get_adapter_type returns 'unknown' for untracked serial."""
        from services.controller_manager.multiplexer.mock_adapter import MockAdapter
        from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

        adapter = MockAdapter(num_controllers=0)
        backend = MultiplexerBackend(adapters=[adapter])

        assert backend.get_adapter_type("nonexistent_serial") == "unknown"

    def test_adapter_by_type_lookup(self):
        """MultiplexerBackend builds adapter_type -> adapter lookup."""
        from services.controller_manager.multiplexer.mock_adapter import MockAdapter
        from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

        adapter = MockAdapter(num_controllers=0)
        backend = MultiplexerBackend(adapters=[adapter])

        assert "mock" in backend._adapter_by_type
        assert backend._adapter_by_type["mock"] is adapter

    @pytest.mark.asyncio
    async def test_shutdown_clears_all_state(self):
        """shutdown() removes all tracked state."""
        from services.controller_manager.multiplexer.mock_adapter import MockAdapter
        from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

        adapter = MockAdapter(num_controllers=2)
        backend = MultiplexerBackend(adapters=[adapter])
        await backend.initialize()

        serials = backend.get_connected_controllers()
        assert len(serials) > 0

        # Set some state
        await backend.set_led_color(serials[0], 255, 0, 0)
        await backend.set_rumble(serials[0], 100)

        await backend.shutdown()

        # All state should be cleared
        assert len(backend._serial_to_adapter) == 0
        assert len(backend._led_colors) == 0
        assert len(backend._rumble) == 0

    @pytest.mark.asyncio
    async def test_connect_controller_assigns_adapter(self):
        """connect_controller() assigns the adapter for a new serial."""
        from services.controller_manager.multiplexer.mock_adapter import MockAdapter
        from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

        adapter = MockAdapter(num_controllers=0)
        backend = MultiplexerBackend(adapters=[adapter])
        await backend.initialize()

        result = await backend.connect_controller("test_serial_001")
        assert result is True
        assert "test_serial_001" in backend._serial_to_adapter
        assert backend._serial_to_adapter["test_serial_001"] is adapter

        await backend.shutdown()
