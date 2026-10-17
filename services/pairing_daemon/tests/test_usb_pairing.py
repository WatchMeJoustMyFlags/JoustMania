"""Tests for psmove_pairing.usb_pairing module."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus

from psmove_pairing.adapter_manager import AdapterInfo
from psmove_pairing.pairing_backend import HidapiBackend, PairingResult, RustServiceBackend
from psmove_pairing.usb_pairing import (
    DEFAULT_PAIRING_BACKEND,
    VARIANT_TO_PAIRING_BACKEND,
    USBPairing,
    _resolve_backend,
    _resolve_routing,
)

from .conftest import FAKE_PATH_1, MockCommandRunner, bluez_tree, devices


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


# Shipped flag file: the daemon's mapping must cover exactly its variants.
_CONTROLLER_FLAGS = Path(__file__).resolve().parents[3] / "services" / "flagd" / "controller.json"


def _bluetooth_backend_flag() -> dict:
    return json.loads(_CONTROLLER_FLAGS.read_text())["flags"]["bluetooth_backend"]


class TestResolveBackend:
    """bluetooth_backend variant -> pairing backend (UC-001 BR-003, A3; #1320)."""

    @pytest.mark.uc("UC-001", flow="main", steps=[4], br=["BR-003"])
    def test_every_flag_variant_maps_to_a_pairing_backend(self):
        """Every bluetooth_backend variant in controller.json has a defined daemon behaviour."""
        variants = set(_bluetooth_backend_flag()["variants"].values())
        assert variants == set(VARIANT_TO_PAIRING_BACKEND), (
            "bluetooth_backend variants and the pairing daemon mapping drifted apart: "
            f"flag={sorted(variants)}, daemon={sorted(VARIANT_TO_PAIRING_BACKEND)}"
        )

    @pytest.mark.uc("UC-001", flow="main", steps=[4], br=["BR-003"])
    @pytest.mark.parametrize(
        ("variant", "expected_name", "expected_type"),
        [
            ("rust", "rust", RustServiceBackend),
            ("python", "python", HidapiBackend),
            ("unstable", "python", HidapiBackend),
        ],
    )
    @patch.object(RustServiceBackend, "__init__", lambda _self: None)
    def test_variant_selects_backend_without_warning(self, variant, expected_name, expected_type):
        """Each known variant selects its backend and logs no warning."""
        with (
            patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value=variant),
            patch("psmove_pairing.usb_pairing.logger") as mock_logger,
        ):
            backend, name = _resolve_backend()
        assert isinstance(backend, expected_type)
        assert name == expected_name
        mock_logger.warning.assert_not_called()

    @pytest.mark.uc("UC-001", flow="main", steps=[4], br=["BR-003"])
    @patch.object(RustServiceBackend, "__init__", lambda _self: None)
    def test_shipped_default_variant_is_rust(self):
        """The shipped default variant pairs through rust-hid (#1371)."""
        flag = _bluetooth_backend_flag()
        default_value = flag["variants"][flag["defaultVariant"]]
        with patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value=default_value):
            backend, name = _resolve_backend()
        assert name == DEFAULT_PAIRING_BACKEND == "rust"
        assert isinstance(backend, RustServiceBackend)

    @pytest.mark.uc("UC-001", flow="A3", br=["BR-003"])
    @pytest.mark.parametrize("value", ["bogus", "", "hidapi", "RUST"])
    @patch.object(RustServiceBackend, "__init__", lambda _self: None)
    def test_unknown_value_falls_back_to_rust(self, value):
        """A value that is not a flag variant falls back to rust-hid with a warning."""
        with (
            patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value=value),
            patch("psmove_pairing.usb_pairing.logger") as mock_logger,
        ):
            backend, name = _resolve_backend()
        assert isinstance(backend, RustServiceBackend)
        assert name == "rust"
        mock_logger.warning.assert_any_call(f"Unknown bluetooth_backend '{value}', falling back to rust")

    def test_resolve_routing_returns_names_without_constructing_backends(self):
        """_resolve_routing() maps variants to names only."""
        with patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value="unstable"):
            assert _resolve_routing() == "python"
        with patch("psmove_pairing.usb_pairing.get_adapter_routing_default", return_value="rust"):
            assert _resolve_routing() == "rust"


class TestGetUSBControllers:
    """Tests for poll() USB controller enumeration via backend."""

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="python")
    @pytest.mark.asyncio
    async def test_no_controllers(self, _mock_routing, mock_create, usb_pairing):
        """Poll with no controllers delegates to backend and finds none."""
        backend = _mock_backend(controllers=[])
        mock_create.return_value = backend

        await usb_pairing.poll()
        backend.get_usb_controllers.assert_called_once()

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="python")
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
        usb_pairing._current_backend_name = "python"

        result = await usb_pairing.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is True
        backend.pair_controller.assert_called_once_with(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")

    @pytest.mark.asyncio
    async def test_pairing_failure(self, usb_pairing):
        """Returns False when backend pairing fails."""
        backend = _mock_backend()
        backend.pair_controller.return_value = PairingResult(success=False)
        usb_pairing._current_backend = backend
        usb_pairing._current_backend_name = "python"

        result = await usb_pairing.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is False

    @pytest.mark.asyncio
    async def test_backend_exception(self, usb_pairing):
        """Returns False when backend raises an exception."""
        backend = _mock_backend()
        backend.pair_controller.side_effect = RuntimeError("Hardware error")
        usb_pairing._current_backend = backend
        usb_pairing._current_backend_name = "python"

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
        usb_pairing._current_backend_name = "python"

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
        mock_resolve.return_value = (backend, "python")

        # _current_backend is None (outside poll cycle)
        assert usb_pairing._current_backend is None
        result = await usb_pairing.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is True
        mock_resolve.assert_called_once()


class TestBackendCoherence:
    """Tests for poll-cycle backend coherence."""

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="python")
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
        usb_pairing.bluez_trust_controller = AsyncMock(return_value=True)

        await usb_pairing.poll()

        # _create_backend called exactly once (in poll), not again in pair_controller
        mock_create.assert_called_once()
        # But pair_controller still delegated to the same backend
        backend.pair_controller.assert_called_once()

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="python")
    @pytest.mark.asyncio
    async def test_backend_cached_after_poll(self, _mock_routing, mock_create, usb_pairing):
        """Backend instance is cached after poll for reuse."""
        backend = _mock_backend()
        mock_create.return_value = backend

        await usb_pairing.poll()
        assert usb_pairing._current_backend is backend
        assert usb_pairing._current_backend_name == "python"

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="python")
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
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="python")
    @pytest.mark.asyncio
    async def test_poll_increments_count(self, _mock_routing, mock_create, usb_pairing):
        """Poll count is incremented each cycle."""
        mock_create.return_value = _mock_backend()

        initial_count = usb_pairing.poll_count
        await usb_pairing.poll()
        assert usb_pairing.poll_count == initial_count + 1

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="python")
    @pytest.mark.asyncio
    async def test_poll_sets_backend_span_attribute(self, _mock_routing, mock_create, usb_pairing):
        """Poll sets pairing.backend span attribute."""
        mock_create.return_value = _mock_backend()

        await usb_pairing.poll()

        span = usb_pairing.tracer.start_as_current_span.return_value.__enter__.return_value
        span.set_attribute.assert_any_call("pairing.backend", "python")

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="python")
    @pytest.mark.asyncio
    async def test_poll_logs_initial_backend(self, _mock_routing, mock_create, usb_pairing):
        """Logs initial backend on first poll."""
        mock_create.return_value = _mock_backend()

        with patch("psmove_pairing.usb_pairing.logger") as mock_logger:
            await usb_pairing.poll()
            mock_logger.info.assert_any_call("Pairing backend initialized: python")

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing")
    @pytest.mark.asyncio
    async def test_poll_logs_backend_switch(self, mock_routing, mock_create, usb_pairing):
        """Logs when backend switches between polls."""
        python_backend = _mock_backend()
        rust_backend = _mock_backend()

        # First poll: python
        mock_routing.return_value = "python"
        mock_create.return_value = python_backend
        await usb_pairing.poll()
        assert usb_pairing._current_backend_name == "python"

        # Second poll: rust
        mock_routing.return_value = "rust"
        mock_create.return_value = rust_backend
        with patch("psmove_pairing.usb_pairing.logger") as mock_logger:
            await usb_pairing.poll()
            mock_logger.info.assert_any_call("Pairing backend switched: python -> rust")

    @patch("psmove_pairing.usb_pairing._create_backend")
    @patch("psmove_pairing.usb_pairing._resolve_routing", return_value="python")
    @pytest.mark.asyncio
    async def test_poll_skips_when_no_controllers(self, _mock_routing, mock_create, usb_pairing):
        """Poll skips processing when no USB controllers found."""
        mock_create.return_value = _mock_backend(controllers=[])

        await usb_pairing.poll()
        # No process_controller calls expected


class TestBluezTrustController:
    """Tests for bluez_trust_controller()."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-001", flow="main", steps=[5])
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
    @pytest.mark.uc("UC-001", flow="A4")
    async def test_already_in_bluez_runs_full_pairing(self, usb_pairing):
        """Test that 'already paired' controllers still go through pairing to verify host address."""
        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
        usb_pairing.adapter_manager.check_if_not_paired = MagicMock(return_value=False)
        adapter = AdapterInfo(hci="hci0", address="11:22:33:44:55:66", name="adapter-hci0", device_count=0)
        usb_pairing.adapter_manager.select_least_loaded_adapter = MagicMock(return_value=adapter)
        usb_pairing.pair_controller = AsyncMock(return_value=True)
        usb_pairing.bluez_trust_controller = AsyncMock(return_value=True)

        result = await usb_pairing.process_controller(FAKE_PATH_1, "00:06:F7:AA:BB:CC")

        assert result is True
        usb_pairing.pair_controller.assert_called_once_with(FAKE_PATH_1, "00:06:F7:AA:BB:CC", "11:22:33:44:55:66")
        usb_pairing.bluez_trust_controller.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-001", flow="A1")
    async def test_verified_serial_skipped_on_subsequent_poll(self, usb_pairing):
        """Test that a controller verified this session is skipped on next poll."""
        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
        usb_pairing.adapter_manager.check_if_not_paired = MagicMock(return_value=True)
        adapter = AdapterInfo(hci="hci0", address="11:22:33:44:55:66", name="adapter-hci0", device_count=0)
        usb_pairing.adapter_manager.select_least_loaded_adapter = MagicMock(return_value=adapter)
        usb_pairing.pair_controller = AsyncMock(return_value=True)
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


_SYSTEMD_UNIT_METHODS = {"RestartUnit", "StopUnit", "StartUnit", "ReloadOrRestartUnit", "TryRestartUnit"}


class _UnitRestarts:
    """systemd unit (re)starts sent on the recorded system bus."""

    def __init__(self, bus):
        self._bus = bus

    def messages(self) -> list:
        return [
            c.args[0]
            for c in self._bus.call.await_args_list
            if c.args[0].destination == "org.freedesktop.systemd1" or c.args[0].member in _SYSTEMD_UNIT_METHODS
        ]

    def assert_none(self) -> None:
        assert not self.messages(), f"pairing restarted a systemd unit: {[m.body for m in self.messages()]}"


class _PairingFlowEnv:
    """UC-001 end to end through process_controller / poll (shared fixtures).

    Real AdapterManager (BlueZ D-Bus tree faked), real pair_controller and
    bluez_trust_controller; only the backend's HID write, the system D-Bus,
    bluetoothctl and the metric counters are patched.
    """

    ADAPTER_BUSY = "AA:00:00:00:00:00"
    ADAPTER_FREE = "BB:00:00:00:00:00"
    SERIAL = "00:06:F7:AA:BB:CC"

    @pytest.fixture
    def metrics(self):
        names = ("pairing_attempts_total", "pairing_success_total", "pairing_failed_total")
        with (
            patch("psmove_pairing.usb_pairing.pairing_attempts_total") as attempts,
            patch("psmove_pairing.usb_pairing.pairing_success_total") as success,
            patch("psmove_pairing.usb_pairing.pairing_failed_total") as failed,
        ):
            yield dict(zip(names, (attempts, success, failed), strict=True))

    @pytest.fixture
    def env(self, usb_pairing):
        """Patch the system edges; yields (backend, runner, set_tree, restart).

        set_tree(value) swaps the BlueZ GetManagedObjects reply for later refreshes.
        Every D-Bus user (BlueZ and systemd via lib.bluez_dbus) shares one recorded
        bus; ``restart.assert_none()`` checks that no systemd unit was restarted
        on it (UC-001 BR-005).
        """
        backend = _mock_backend(controllers=[(FAKE_PATH_1, self.SERIAL)])
        runner = MockCommandRunner()
        runner.add_response(["bluetoothctl", "trust", self.SERIAL], (0, "trust succeeded"))
        tree = {"value": bluez_tree({"hci0": (self.ADAPTER_BUSY, devices(3)), "hci1": (self.ADAPTER_FREE, [])})}

        async def managed_objects(_bus):
            return tree["value"]

        def set_tree(value):
            tree["value"] = value

        bus = create_autospec(MessageBus, instance=True)
        bus.call.return_value = Message(message_type=MessageType.METHOD_RETURN, reply_serial=1)
        self.bus = bus

        with (
            patch("psmove_pairing.adapter_manager_modern.get_system_bus", AsyncMock(return_value=bus)),
            patch("psmove_pairing.adapter_manager_modern.get_managed_objects", side_effect=managed_objects),
            patch("psmove_pairing.registrations.get_system_bus", AsyncMock(return_value=bus)),
            patch("psmove_pairing.registrations.get_managed_objects", side_effect=managed_objects),
            patch("psmove_pairing.registrations.DEFAULT_BLUETOOTH_DIR", Path("/nonexistent-bluetooth-state")),
            patch("psmove_pairing.usb_pairing._resolve_routing", return_value="python"),
            patch("psmove_pairing.usb_pairing._create_backend", return_value=backend),
            patch("lib.bluez_dbus.get_system_bus", AsyncMock(return_value=bus)),
            patch("psmove_pairing.usb_pairing.run_command", runner),
            patch("asyncio.sleep", AsyncMock()),
        ):
            yield backend, runner, set_tree, _UnitRestarts(bus)


class TestPairingFlow(_PairingFlowEnv):
    """UC-001 main flow and alternatives A1, A2, A4, A5."""

    @pytest.mark.uc("UC-001", flow="main", steps=[2, 3, 4, 5, 6], br=["BR-001"])
    async def test_poll_pairs_new_controller_to_least_loaded_adapter(self, usb_pairing, env, metrics):
        """A USB controller is paired to the least-loaded adapter, trusted, success counted; no BlueZ restart."""
        backend, runner, _set_tree, restart = env

        await usb_pairing.poll()

        backend.pair_controller.assert_awaited_once_with(FAKE_PATH_1, self.SERIAL, self.ADAPTER_FREE)
        restart.assert_none()
        assert ["bluetoothctl", "trust", self.SERIAL] in runner.calls
        assert self.SERIAL in usb_pairing._verified_serials
        metrics["pairing_success_total"].inc.assert_called_once()
        metrics["pairing_failed_total"].inc.assert_not_called()

    @pytest.mark.uc("UC-001", flow="A5", br=["BR-001", "BR-004"])
    async def test_repair_to_other_adapter_removes_old_registration(self, usb_pairing, env, metrics):
        """A4 moves a known controller to the least-loaded adapter; A5 removes its old registration there."""
        backend, runner, set_tree, restart = env
        known = [{"Address": self.SERIAL}, *devices(2)]
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, known), "hci1": (self.ADAPTER_FREE, [])}))
        removals_before_trust = []
        trust = ["bluetoothctl", "trust", self.SERIAL]

        async def record_trust(cmd, **_kwargs):
            if cmd == trust:
                removals_before_trust.append(
                    sum(c.args[0].member == "RemoveDevice" for c in self.bus.call.await_args_list)
                )
            return await MockCommandRunner.__call__(runner, cmd)

        with patch("psmove_pairing.usb_pairing.run_command", record_trust):
            await usb_pairing.poll()

        assert removals_before_trust == [1], "A5 removes the old registration before the trust step"

        backend.pair_controller.assert_awaited_once_with(FAKE_PATH_1, self.SERIAL, self.ADAPTER_FREE)
        removals = [c.args[0] for c in self.bus.call.await_args_list if c.args[0].member == "RemoveDevice"]
        assert [(m.path, m.body) for m in removals] == [("/org/bluez/hci0", ["/org/bluez/hci0/dev_0"])]
        restart.assert_none()
        metrics["pairing_success_total"].inc.assert_called_once()

    @pytest.mark.uc("UC-001", flow="A5", br=["BR-004"])
    async def test_failed_stale_removal_does_not_fail_pairing(self, usb_pairing, env, metrics):
        """BR-004: a BlueZ error while removing the old registration is logged; the pairing still succeeds."""
        _backend, runner, set_tree, _restart = env
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, [{"Address": self.SERIAL}]), "hci1": (self.ADAPTER_FREE, [])}))
        self.bus.call.return_value = Message(
            message_type=MessageType.ERROR,
            error_name="org.bluez.Error.Failed",
            reply_serial=1,
            body=["boom"],
            signature="s",
        )

        await usb_pairing.poll()

        assert ["bluetoothctl", "trust", self.SERIAL] in runner.calls
        metrics["pairing_success_total"].inc.assert_called_once()
        metrics["pairing_failed_total"].inc.assert_not_called()

    @pytest.mark.uc("UC-001", flow="A1")
    async def test_verified_controller_is_not_paired_again(self, usb_pairing, env, metrics):
        """UC-001 A1: a serial verified in this daemon session is skipped on the next poll."""
        backend, _runner, _set_tree, _restart = env

        await usb_pairing.poll()
        await usb_pairing.poll()

        backend.pair_controller.assert_awaited_once()
        metrics["pairing_attempts_total"].inc.assert_called_once()

    @pytest.mark.uc("UC-001", flow="A2")
    async def test_trust_failure_still_completes_pairing(self, usb_pairing, env, metrics):
        """UC-001 A2: bluetoothctl trust fails twice; the pairing is kept and counted as success."""
        _backend, runner, _set_tree, _restart = env
        runner.add_response(["bluetoothctl", "trust", self.SERIAL], (1, "Failed to trust"))

        result = await usb_pairing.process_controller(FAKE_PATH_1, self.SERIAL)

        assert result is True
        assert runner.calls.count(["bluetoothctl", "trust", self.SERIAL]) == 2  # retried once
        assert self.SERIAL in usb_pairing._verified_serials
        metrics["pairing_success_total"].inc.assert_called_once()
        metrics["pairing_failed_total"].inc.assert_not_called()

    @pytest.mark.uc("UC-001", flow="A4", br=["BR-001"])
    async def test_controller_known_to_bluez_is_repaired_to_least_loaded_adapter(self, usb_pairing, env, metrics):
        """UC-001 A4: a controller BlueZ already knows (on the busy adapter) is paired again and moves."""
        backend, _runner, set_tree, _restart = env
        known = [{"Address": self.SERIAL}, *devices(2)]
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, known), "hci1": (self.ADAPTER_FREE, devices(1))}))

        result = await usb_pairing.process_controller(FAKE_PATH_1, self.SERIAL)

        assert result is True
        backend.pair_controller.assert_awaited_once_with(FAKE_PATH_1, self.SERIAL, self.ADAPTER_FREE)
        metrics["pairing_success_total"].inc.assert_called_once()

    async def test_no_adapter_fails_and_next_poll_retries(self, usb_pairing, env, metrics):
        """Failure postcondition: no adapter -> failed_total, not verified; the next poll pairs it."""
        backend, _runner, set_tree, _restart = env
        set_tree({"/org/bluez": {}})

        await usb_pairing.poll()

        backend.pair_controller.assert_not_awaited()
        assert self.SERIAL not in usb_pairing._verified_serials
        metrics["pairing_failed_total"].inc.assert_called_once()

        set_tree(bluez_tree({"hci0": (self.ADAPTER_FREE, [])}))
        await usb_pairing.poll()

        backend.pair_controller.assert_awaited_once_with(FAKE_PATH_1, self.SERIAL, self.ADAPTER_FREE)
        metrics["pairing_success_total"].inc.assert_called_once()

    async def test_pair_failure_fails_and_next_poll_retries(self, usb_pairing, env, metrics):
        """Failure postcondition: the backend write fails -> failed_total; the next poll retries."""
        backend, _runner, _set_tree, restart = env
        backend.pair_controller.side_effect = [PairingResult(success=False), PairingResult(success=True)]

        await usb_pairing.poll()

        restart.assert_none()
        assert self.SERIAL not in usb_pairing._verified_serials
        metrics["pairing_failed_total"].inc.assert_called_once()

        await usb_pairing.poll()

        assert backend.pair_controller.await_count == 2
        assert self.SERIAL in usb_pairing._verified_serials
        metrics["pairing_success_total"].inc.assert_called_once()


_DISRUPTIVE_COMMANDS = {"systemctl", "service", "hciconfig", "btmgmt", "rfkill"}


class TestPairingNeverRestartsBluez(_PairingFlowEnv):
    """UC-001 A6 / BR-005 (#1446): pairing during a game leaves connected controllers connected.

    The D-Bus bus, bluetoothctl and lib's systemd helper are all observed: pairing
    must not restart bluetoothd, power an adapter off or on, or remove anyone else's
    registration. The daemon reads no game state, so real and shadow games are alike.
    """

    def _assert_nothing_disruptive(self, runner, restart, own_removals: int = 0) -> None:
        restart.assert_none()
        messages = [c.args[0] for c in self.bus.call.await_args_list]
        powered = [m for m in messages if m.member == "Set" and "Powered" in (m.body or [])]
        assert not powered, "no adapter power-cycle"
        assert sum(m.member == "RemoveDevice" for m in messages) == own_removals
        assert not [c for c in runner.calls if c and c[0] in _DISRUPTIVE_COMMANDS], runner.calls
        assert all(c[:2] == ["bluetoothctl", "trust"] for c in runner.calls), runner.calls

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_pairing_while_controllers_are_connected_restarts_nothing(self, usb_pairing, env, metrics):
        """Three controllers play over Bluetooth on hci0; a fourth is paired over USB without disturbing them."""
        backend, runner, set_tree, restart = env
        playing = devices(3, connected=True)
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, playing), "hci1": (self.ADAPTER_FREE, [])}))

        await usb_pairing.poll()

        backend.pair_controller.assert_awaited_once_with(FAKE_PATH_1, self.SERIAL, self.ADAPTER_FREE)
        metrics["pairing_success_total"].inc.assert_called_once()
        self._assert_nothing_disruptive(runner, restart)

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-004", "BR-005"])
    async def test_repair_during_a_game_removes_only_its_own_stale_registration(self, usb_pairing, env, metrics):
        """Re-pairing a controller mid-game moves it; only its old registration goes, nothing restarts."""
        backend, runner, set_tree, restart = env
        playing = [{"Address": self.SERIAL}, *devices(3, connected=True)]
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, playing), "hci1": (self.ADAPTER_FREE, [])}))

        await usb_pairing.poll()

        backend.pair_controller.assert_awaited_once_with(FAKE_PATH_1, self.SERIAL, self.ADAPTER_FREE)
        self._assert_nothing_disruptive(runner, restart, own_removals=1)
        removed = [c.args[0].body for c in self.bus.call.await_args_list if c.args[0].member == "RemoveDevice"]
        assert removed == [["/org/bluez/hci0/dev_0"]]  # dev_0 is self.SERIAL, not a playing controller

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    def test_usb_pairing_has_no_bluetooth_restart_step(self, usb_pairing):
        """The restart step from the psmoveapi era is gone (it wrote BlueZ files; #610 stopped that)."""
        assert not hasattr(usb_pairing, "restart_bluetooth_service")
