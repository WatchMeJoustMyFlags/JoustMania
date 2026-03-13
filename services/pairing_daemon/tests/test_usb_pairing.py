"""Tests for psmove_pairing.usb_pairing module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from psmove_pairing.adapter_manager import AdapterInfo
from psmove_pairing.pairing_backend import HidapiBackend, PairingResult, RustServiceBackend
from psmove_pairing.usb_pairing import USBPairing, _resolve_backend, _resolve_routing

from .conftest import FAKE_PATH_1, MockCommandRunner


@pytest.fixture
def usb_pairing(mock_tracer):
    """Provide USBPairing instance for tests."""
    return USBPairing(mock_tracer)


def _mock_backend(controllers=None):
    """Create a mock HidapiBackend with predefined controllers."""
    backend = MagicMock(spec=HidapiBackend)
    backend.get_usb_controllers = AsyncMock(return_value=controllers or [])
    backend.pair_controller = AsyncMock(return_value=PairingResult(success=True))
    return backend


class TestResolveBackend:
    """Tests for _resolve_routing() and _resolve_backend() flag-based backend selection."""

    def test_default_returns_hidapi(self):
        """Default routing returns HidapiBackend."""
        with patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value="hidapi"):
            backend, name = _resolve_backend()
        assert isinstance(backend, HidapiBackend)
        assert name == "hidapi"

    @patch.object(RustServiceBackend, "__init__", lambda _self: None)
    def test_rust_returns_rust_backend(self):
        """Routing 'rust' returns RustServiceBackend."""
        with patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value="rust"):
            backend, name = _resolve_backend()
        assert isinstance(backend, RustServiceBackend)
        assert name == "rust"

    def test_unknown_falls_back_to_hidapi(self):
        """Unknown routing value falls back to HidapiBackend."""
        with patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value="unknown"):
            backend, name = _resolve_backend()
        assert isinstance(backend, HidapiBackend)
        assert name == "hidapi"

    def test_empty_string_falls_back_to_hidapi(self):
        """Empty string routing falls back to HidapiBackend."""
        with patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value=""):
            backend, name = _resolve_backend()
        assert isinstance(backend, HidapiBackend)
        assert name == "hidapi"

    def test_unknown_logs_warning(self):
        """Unknown routing value logs a warning."""
        with (
            patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value="bogus"),
            patch("psmove_pairing.usb_pairing.logger") as mock_logger,
        ):
            _resolve_backend()
            mock_logger.warning.assert_any_call("Unknown adapter routing 'bogus', falling back to hidapi")

    def test_resolve_routing_returns_known_values(self):
        """_resolve_routing() returns known routing names without constructing backends."""
        with patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value="hidapi"):
            assert _resolve_routing() == "hidapi"
        with patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value="rust"):
            assert _resolve_routing() == "rust"

    @patch.object(RustServiceBackend, "__init__", lambda _self: None)
    def test_rust_selection_creates_backend_without_warning(self):
        """Selecting rust backend creates RustServiceBackend without warnings."""
        with (
            patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value="rust"),
            patch("psmove_pairing.usb_pairing.logger") as mock_logger,
        ):
            _resolve_backend()
            mock_logger.warning.assert_not_called()


class TestGetUSBControllers:
    """Tests for poll() USB controller enumeration via backend."""

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="hidapi")
    @pytest.mark.asyncio
    async def test_no_controllers(self, _mock_routing, mock_create, usb_pairing):
        """Poll with no controllers delegates to backend and finds none."""
        backend = _mock_backend(controllers=[])
        mock_create.return_value = backend

        await usb_pairing.poll()
        backend.get_usb_controllers.assert_called_once()

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="hidapi")
    @pytest.mark.asyncio
    async def test_controllers_found(self, _mock_routing, mock_create, usb_pairing):
        """Poll with controllers delegates to process_controller."""
        backend = _mock_backend(controllers=[(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF")])
        mock_create.return_value = backend
        usb_pairing.process_controller = AsyncMock(return_value=True)

        await usb_pairing.poll()
        usb_pairing.process_controller.assert_called_once_with(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF")


class TestPairController:
    """Tests for pair_controller() with backend delegation."""

    @pytest.mark.asyncio
    async def test_successful_pairing(self, usb_pairing):
        """Delegates to backend and returns True on success."""
        backend = _mock_backend()
        backend.pair_controller.return_value = PairingResult(
            success=True, already_paired=False, previous_host="00:00:00:00:00:00"
        )
        usb_pairing._current_backend = backend
        usb_pairing._current_backend_name = "hidapi"

        result = await usb_pairing.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is True
        backend.pair_controller.assert_called_once_with(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")

    @pytest.mark.asyncio
    async def test_pairing_failure(self, usb_pairing):
        """Returns False when backend pairing fails."""
        backend = _mock_backend()
        backend.pair_controller.return_value = PairingResult(success=False)
        usb_pairing._current_backend = backend
        usb_pairing._current_backend_name = "hidapi"

        result = await usb_pairing.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is False

    @pytest.mark.asyncio
    async def test_backend_exception(self, usb_pairing):
        """Returns False when backend raises an exception."""
        backend = _mock_backend()
        backend.pair_controller.side_effect = RuntimeError("Hardware error")
        usb_pairing._current_backend = backend
        usb_pairing._current_backend_name = "hidapi"

        result = await usb_pairing.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is False

    @pytest.mark.asyncio
    async def test_span_attributes_from_pairing_result(self, usb_pairing):
        """Span attributes include already_paired and current_host from PairingResult."""
        backend = _mock_backend()
        backend.pair_controller.return_value = PairingResult(
            success=True, already_paired=True, previous_host="AA:BB:CC:DD:EE:FF"
        )
        usb_pairing._current_backend = backend
        usb_pairing._current_backend_name = "hidapi"

        await usb_pairing.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")

        span = usb_pairing.tracer.start_as_current_span.return_value.__enter__.return_value
        span.set_attribute.assert_any_call("pair.already_paired", True)
        span.set_attribute.assert_any_call("pair.current_host", "AA:BB:CC:DD:EE:FF")

    @patch("psmove_pairing.usb_pairing._resolve_backend")
    @pytest.mark.asyncio
    async def test_resolves_fresh_outside_poll_cycle(self, mock_resolve, usb_pairing):
        """When called outside a poll cycle, resolves backend fresh."""
        backend = _mock_backend()
        backend.pair_controller.return_value = PairingResult(success=True)
        mock_resolve.return_value = (backend, "hidapi")

        # _current_backend is None (outside poll cycle)
        assert usb_pairing._current_backend is None
        result = await usb_pairing.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is True
        mock_resolve.assert_called_once()


class TestBackendCoherence:
    """Tests for poll-cycle backend coherence."""

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="hidapi")
    @pytest.mark.asyncio
    async def test_same_backend_used_for_enumerate_and_pair(self, _mock_routing, mock_create, usb_pairing):
        """Backend resolved once in poll() is reused by pair_controller()."""
        backend = _mock_backend(controllers=[(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF")])
        backend.pair_controller.return_value = PairingResult(success=True)
        mock_create.return_value = backend

        # Mock out the rest of process_controller's dependencies
        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
        usb_pairing.adapter_manager.check_if_not_paired = MagicMock(return_value=True)
        adapter = AdapterInfo(hci="hci0", address="11:22:33:44:55:66", name="adapter-hci0", device_count=0)
        usb_pairing.adapter_manager.select_least_loaded_adapter = MagicMock(return_value=adapter)
        usb_pairing.restart_bluetooth_service = AsyncMock()
        usb_pairing.bluez_trust_controller = AsyncMock(return_value=True)

        await usb_pairing.poll()

        # _create_backend called exactly once (in poll), not again in pair_controller
        mock_create.assert_called_once()
        # But pair_controller still delegated to the same backend
        backend.pair_controller.assert_called_once()

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="hidapi")
    @pytest.mark.asyncio
    async def test_backend_cached_after_poll(self, _mock_routing, mock_create, usb_pairing):
        """Backend instance is cached after poll for reuse."""
        backend = _mock_backend()
        mock_create.return_value = backend

        await usb_pairing.poll()
        assert usb_pairing._current_backend is backend
        assert usb_pairing._current_backend_name == "hidapi"

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="hidapi")
    @pytest.mark.asyncio
    async def test_backend_instance_reused_across_polls(self, _mock_routing, mock_create, usb_pairing):
        """Same backend instance is reused when routing is unchanged."""
        backend = _mock_backend()
        mock_create.return_value = backend

        await usb_pairing.poll()
        assert usb_pairing._current_backend is backend

        await usb_pairing.poll()
        # Routing unchanged — _create_backend only called once, instance reused
        mock_create.assert_called_once()
        assert usb_pairing._current_backend is backend


class TestPoll:
    """Tests for poll() with backend delegation."""

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="hidapi")
    @pytest.mark.asyncio
    async def test_poll_increments_count(self, _mock_routing, mock_create, usb_pairing):
        """Poll count is incremented each cycle."""
        mock_create.return_value = _mock_backend()

        initial_count = usb_pairing.poll_count
        await usb_pairing.poll()
        assert usb_pairing.poll_count == initial_count + 1

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="hidapi")
    @pytest.mark.asyncio
    async def test_poll_sets_backend_span_attribute(self, _mock_routing, mock_create, usb_pairing):
        """Poll sets pairing.backend span attribute."""
        mock_create.return_value = _mock_backend()

        await usb_pairing.poll()

        span = usb_pairing.tracer.start_as_current_span.return_value.__enter__.return_value
        span.set_attribute.assert_any_call("pairing.backend", "hidapi")

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="hidapi")
    @pytest.mark.asyncio
    async def test_poll_logs_initial_backend(self, _mock_routing, mock_create, usb_pairing):
        """Logs initial backend on first poll."""
        mock_create.return_value = _mock_backend()

        with patch("psmove_pairing.usb_pairing.logger") as mock_logger:
            await usb_pairing.poll()
            mock_logger.info.assert_any_call("Pairing backend initialized: hidapi")

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing")
    @pytest.mark.asyncio
    async def test_poll_logs_backend_switch(self, mock_routing, mock_create, usb_pairing):
        """Logs when backend switches between polls."""
        hidapi_backend = _mock_backend()
        rust_backend = _mock_backend()

        # First poll: hidapi
        mock_routing.return_value = "hidapi"
        mock_create.return_value = hidapi_backend
        await usb_pairing.poll()
        assert usb_pairing._current_backend_name == "hidapi"

        # Second poll: rust
        mock_routing.return_value = "rust"
        mock_create.return_value = rust_backend
        with patch("psmove_pairing.usb_pairing.logger") as mock_logger:
            await usb_pairing.poll()
            mock_logger.info.assert_any_call("Pairing backend switched: hidapi -> rust")

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="hidapi")
    @pytest.mark.asyncio
    async def test_poll_skips_when_no_controllers(self, _mock_routing, mock_create, usb_pairing):
        """Poll skips processing when no USB controllers found."""
        mock_create.return_value = _mock_backend(controllers=[])

        await usb_pairing.poll()
        # No process_controller calls expected


class TestRestartBluetoothService:
    """Tests for restart_bluetooth_service()."""

    @pytest.mark.asyncio
    async def test_restarts_via_systemd_dbus(self, usb_pairing):
        """Test that bluetooth service is restarted via D-Bus systemd interface."""
        with patch("psmove_pairing.usb_pairing.restart_systemd_unit", new_callable=AsyncMock) as mock_restart:
            with patch("asyncio.sleep", return_value=None):
                await usb_pairing.restart_bluetooth_service()
                mock_restart.assert_called_once_with("bluetooth.service")

    @pytest.mark.asyncio
    async def test_handles_dbus_failure(self, usb_pairing):
        """Test that D-Bus failure is logged but doesn't raise."""
        with patch(
            "psmove_pairing.usb_pairing.restart_systemd_unit",
            new_callable=AsyncMock,
            side_effect=Exception("D-Bus error"),
        ):
            # Should not raise
            await usb_pairing.restart_bluetooth_service()


class TestBluezTrustController:
    """Tests for bluez_trust_controller()."""

    @pytest.mark.asyncio
    async def test_trust_succeeds(self, usb_pairing):
        """Test successful trust."""
        runner = MockCommandRunner()
        runner.add_response(["bluetoothctl", "trust", "AA:BB:CC:DD:EE:FF"], (0, "trust succeeded"))

        with patch("psmove_pairing.usb_pairing.run_command", runner):
            result = await usb_pairing.bluez_trust_controller("AA:BB:CC:DD:EE:FF")
            assert result is True

    @pytest.mark.asyncio
    async def test_trust_retries_on_failure(self, usb_pairing):
        """Test that trust retries once after failure."""
        call_count = 0

        async def mock_run_command(cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (1, "Unable to open mgmt_socket")
            return (0, "trust succeeded")

        with patch("psmove_pairing.usb_pairing.run_command", mock_run_command):
            with patch("asyncio.sleep", return_value=None):
                result = await usb_pairing.bluez_trust_controller("AA:BB:CC:DD:EE:FF")
                assert result is True
                assert call_count == 2

    @pytest.mark.asyncio
    async def test_trust_failure_is_non_fatal(self, usb_pairing):
        """Test that trust failure returns False but doesn't raise."""
        runner = MockCommandRunner()
        runner.add_response(["bluetoothctl", "trust", "AA:BB:CC:DD:EE:FF"], (1, "failed"))

        with patch("psmove_pairing.usb_pairing.run_command", runner):
            with patch("asyncio.sleep", return_value=None):
                result = await usb_pairing.bluez_trust_controller("AA:BB:CC:DD:EE:FF")
                assert result is False


class TestProcessController:
    """Tests for process_controller()."""

    @pytest.mark.asyncio
    async def test_already_in_bluez_runs_full_pairing(self, usb_pairing):
        """Test that 'already paired' controllers still go through pairing to verify host address."""
        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
        usb_pairing.adapter_manager.check_if_not_paired = MagicMock(return_value=False)
        adapter = AdapterInfo(hci="hci0", address="11:22:33:44:55:66", name="adapter-hci0", device_count=0)
        usb_pairing.adapter_manager.select_least_loaded_adapter = MagicMock(return_value=adapter)
        usb_pairing.pair_controller = AsyncMock(return_value=True)
        usb_pairing.restart_bluetooth_service = AsyncMock()
        usb_pairing.bluez_trust_controller = AsyncMock(return_value=True)

        result = await usb_pairing.process_controller(FAKE_PATH_1, "00:06:F7:AA:BB:CC")

        assert result is True
        usb_pairing.pair_controller.assert_called_once_with(FAKE_PATH_1, "00:06:F7:AA:BB:CC", "11:22:33:44:55:66")
        usb_pairing.restart_bluetooth_service.assert_called_once()
        usb_pairing.bluez_trust_controller.assert_called_once()

    @pytest.mark.asyncio
    async def test_verified_serial_skipped_on_subsequent_poll(self, usb_pairing):
        """Test that a controller verified this session is skipped on next poll."""
        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
        usb_pairing.adapter_manager.check_if_not_paired = MagicMock(return_value=True)
        adapter = AdapterInfo(hci="hci0", address="11:22:33:44:55:66", name="adapter-hci0", device_count=0)
        usb_pairing.adapter_manager.select_least_loaded_adapter = MagicMock(return_value=adapter)
        usb_pairing.pair_controller = AsyncMock(return_value=True)
        usb_pairing.restart_bluetooth_service = AsyncMock()
        usb_pairing.bluez_trust_controller = AsyncMock(return_value=True)

        # First call: full pairing
        result1 = await usb_pairing.process_controller(FAKE_PATH_1, "00:06:F7:AA:BB:CC")
        assert result1 is True

        # Second call: should skip
        result2 = await usb_pairing.process_controller(FAKE_PATH_1, "00:06:F7:AA:BB:CC")
        assert result2 is False
        # pair_controller only called once (first time)
        usb_pairing.pair_controller.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_adapters_available(self, usb_pairing):
        """Test when no Bluetooth adapters are available."""
        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
        usb_pairing.adapter_manager.check_if_not_paired = MagicMock(return_value=True)
        usb_pairing.adapter_manager.select_least_loaded_adapter = MagicMock(return_value=None)

        result = await usb_pairing.process_controller(FAKE_PATH_1, "00:06:F7:AA:BB:CC")
        assert result is False
