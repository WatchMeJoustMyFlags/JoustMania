"""Tests for psmove_pairing.usb_pairing module."""

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError as DBusReplyError

from psmove_pairing.adapter_manager import AdapterInfo
from psmove_pairing.bluez_agent import PairingAgent
from psmove_pairing.bluez_records import HID_SDP_RECORD
from psmove_pairing.pairing_backend import HidapiBackend, PairingResult, RustServiceBackend, UsbController
from psmove_pairing.registrations import PsMoveRegistration, RemovalResult
from psmove_pairing.usb_led import GREEN, OFF, RED, UsbLedCue
from psmove_pairing.usb_pairing import (
    DEFAULT_PAIRING_BACKEND,
    VARIANT_TO_PAIRING_BACKEND,
    USBPairing,
    _resolve_backend,
    _resolve_routing,
)

from .conftest import FAKE_PATH_1, MockCommandRunner, bluez_tree, devices


@pytest.fixture
def usb_pairing(mock_tracer, tmp_path):
    """Provide USBPairing instance for tests; BlueZ storage never points at the host's."""
    pairing = USBPairing(mock_tracer)
    pairing.bluetooth_dir = tmp_path / "no-bluez-storage"
    # No LED cue tasks unless a test asks for them (UC-001 BR-010: TestPairingLedCue2091)
    pairing.led_cue = create_autospec(UsbLedCue, instance=True)
    return pairing


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
        backend = _mock_backend(controllers=[UsbController(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", 0x0C5E)])
        mock_create.return_value = backend
        usb_pairing.process_controller = AsyncMock(return_value=True)

        await usb_pairing.poll()
        usb_pairing.process_controller.assert_called_once_with(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", 0x0C5E)


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
        backend = _mock_backend(controllers=[UsbController(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", 0x03D5)])
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


def _reply(error_name: str | None = None) -> Message:
    if error_name is None:
        return Message(message_type=MessageType.METHOD_RETURN, reply_serial=1)
    return Message(message_type=MessageType.ERROR, error_name=error_name, reply_serial=1, body=["boom"], signature="s")


def _trust_messages(bus) -> list:
    """Device1.Trusted writes sent on the recorded bus."""
    return [
        c.args[0]
        for c in bus.call.await_args_list
        if c.args[0].member == "Set" and list(c.args[0].body[:2]) == ["org.bluez.Device1", "Trusted"]
    ]


def _record_subprocesses(runner: MockCommandRunner) -> AsyncMock:
    """Stand-in for asyncio.create_subprocess_exec: records argv in runner.calls (bluetoothctl, systemctl, ...)."""
    return AsyncMock(side_effect=lambda *argv, **_kwargs: runner.calls.append(list(argv)))


class TestBluezTrustController:
    """bluez_trust_controller() sets Device1.Trusted on the selected adapter (UC-001 BR-007, #1445)."""

    SERIAL = "00:06:F7:AA:BB:CC"

    @pytest.fixture
    def bus(self):
        bus = create_autospec(MessageBus, instance=True)
        bus.call.return_value = _reply()
        with (
            patch("lib.bluez_dbus.get_system_bus", AsyncMock(return_value=bus)),
            patch("asyncio.create_subprocess_exec", _record_subprocesses(runner := MockCommandRunner())),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bus.runner = runner
            yield bus

    @pytest.mark.uc("UC-001", flow="main", steps=[5], br=["BR-007"])
    async def test_trust_targets_the_selected_adapter(self, usb_pairing, bus):
        """The Trusted property is set on /org/bluez/<hci>/dev_<addr> of the given adapter, not via bluetoothctl."""
        result = await usb_pairing.bluez_trust_controller(self.SERIAL, "hci1")

        assert result == "true"
        [message] = _trust_messages(bus)
        assert message.path == "/org/bluez/hci1/dev_00_06_F7_AA_BB_CC"
        assert message.body[2].value is True
        assert bus.runner.calls == []  # no bluetoothctl, which acts on its default adapter

    @pytest.mark.uc("UC-001", flow="A2", br=["BR-007"])
    async def test_trust_retries_once_on_a_bluez_error(self, usb_pairing, bus):
        bus.call.side_effect = [_reply("org.bluez.Error.Failed"), _reply()]

        result = await usb_pairing.bluez_trust_controller(self.SERIAL, "hci0")

        assert result == "true"
        assert len(_trust_messages(bus)) == 2

    @pytest.mark.uc("UC-001", flow="A2", br=["BR-007"])
    @pytest.mark.parametrize(
        "error_name",
        [
            "org.freedesktop.DBus.Error.UnknownObject",
            "org.freedesktop.DBus.Error.UnknownMethod",
            "org.freedesktop.DBus.Error.UnknownInterface",
        ],
    )
    async def test_unknown_device_is_not_retried(self, usb_pairing, bus, error_name):
        """BlueZ has no device object for the address on that adapter: fail at once, non-fatal."""
        bus.call.return_value = _reply(error_name)

        result = await usb_pairing.bluez_trust_controller(self.SERIAL, "hci0")

        assert result == "unknown_object"
        assert len(_trust_messages(bus)) == 1

    @pytest.mark.uc("UC-001", flow="A2", br=["BR-007"])
    async def test_trust_failure_is_non_fatal(self, usb_pairing, bus):
        bus.call.return_value = _reply("org.bluez.Error.Failed")

        result = await usb_pairing.bluez_trust_controller(self.SERIAL, "hci0")

        assert result == "false"
        assert len(_trust_messages(bus)) == 2

    async def test_no_system_bus_is_non_fatal(self, usb_pairing):
        with (
            patch("lib.bluez_dbus.get_system_bus", AsyncMock(side_effect=OSError("no bus"))),
            patch("asyncio.sleep", AsyncMock()),
        ):
            assert await usb_pairing.bluez_trust_controller(self.SERIAL, "hci0") == "false"


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
    subprocess creation (recorded, must stay empty) and the metric counters are patched.
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
    def env(self, usb_pairing, tmp_path):
        """Patch the system edges; yields (backend, runner, set_tree, restart).

        set_tree(value) swaps the BlueZ GetManagedObjects reply for later refreshes.
        Every D-Bus user (BlueZ and systemd via lib.bluez_dbus) shares one recorded
        bus; ``restart.assert_none()`` checks that no systemd unit was restarted
        on it (UC-001 BR-005).
        """
        backend = _mock_backend(controllers=[UsbController(FAKE_PATH_1, self.SERIAL, 0x03D5)])
        # BlueZ storage: a directory per present adapter, as bluetoothd keeps it (UC-001 BR-008)
        self.bluetooth_dir = tmp_path / "bluetooth"
        for adapter in (self.ADAPTER_BUSY, self.ADAPTER_FREE):
            (self.bluetooth_dir / adapter).mkdir(parents=True)
        usb_pairing.bluetooth_dir = self.bluetooth_dir
        runner = MockCommandRunner()
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
            patch("asyncio.create_subprocess_exec", _record_subprocesses(runner)),
            patch("asyncio.sleep", AsyncMock()),
            # Restart at the end of the pairing poll unless a test sets the quiet period (#2091)
            patch("psmove_pairing.bluez_reload.get_restart_quiet_period", return_value=0),
        ):
            yield backend, runner, set_tree, _UnitRestarts(bus)

    TRUST_PATH = "/org/bluez/hci1/dev_00_06_F7_AA_BB_CC"  # ADAPTER_FREE is hci1

    def trusted_paths(self) -> list[str]:
        return [m.path for m in _trust_messages(self.bus)]

    def fail_trust(self, error_name: str) -> None:
        """BlueZ answers every Device1.Trusted write with error_name; other calls succeed."""

        async def call(message):
            if message.member == "Set" and list(message.body[:2]) == ["org.bluez.Device1", "Trusted"]:
                return _reply(error_name)
            return _reply()

        self.bus.call.side_effect = call


class TestPairingFlow(_PairingFlowEnv):
    """UC-001 main flow and alternatives A1, A2, A4, A5."""

    @pytest.mark.uc(
        "UC-001", flow="main", steps=[2, 3, 4, 5, 6], br=["BR-001", "BR-005"], cond=["Post-S-1", "Post-S-2"]
    )
    async def test_poll_pairs_controller_known_on_the_adapter_without_restart(self, usb_pairing, env, metrics, caplog):
        """Main flow: a controller already Paired on the selected adapter is paired and trusted; no restart."""
        backend, runner, set_tree, restart = env
        known_on_free = [{"Address": self.SERIAL, "Paired": True}]
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, devices(3)), "hci1": (self.ADAPTER_FREE, known_on_free)}))

        with (
            patch("psmove_pairing.bluez_reload.get_bluez_restart_mode", return_value="when_idle"),
            caplog.at_level(logging.INFO, logger="psmove-pairing"),
        ):
            await usb_pairing.poll()

        assert any(
            self.SERIAL in r.getMessage() and "unplug USB and press PS button" in r.getMessage() for r in caplog.records
        )
        backend.pair_controller.assert_awaited_once_with(FAKE_PATH_1, self.SERIAL, self.ADAPTER_FREE)
        restart.assert_none()
        assert self._restart_units() == []
        assert usb_pairing.bluez_reloader.pending is False
        assert self.trusted_paths() == [self.TRUST_PATH]
        assert runner.calls == []
        assert self.SERIAL in usb_pairing._verified_serials
        metrics["pairing_success_total"].inc.assert_called_once()
        metrics["pairing_failed_total"].inc.assert_not_called()

    def _restart_units(self) -> list:
        return [c.args[0].body for c in self.bus.call.await_args_list if c.args[0].member == "RestartUnit"]

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-001"])
    async def test_poll_pairs_new_controller_to_least_loaded_adapter(self, usb_pairing, env, metrics, caplog):
        """A USB controller is paired to the least-loaded adapter, trusted, success counted; no BlueZ restart."""
        backend, runner, _set_tree, restart = env

        with caplog.at_level(logging.INFO, logger="psmove-pairing"):
            await usb_pairing.poll()

        # Step 6: the log tells the player to unplug and press PS.
        assert any(
            self.SERIAL in r.getMessage() and "unplug USB and press PS button" in r.getMessage() for r in caplog.records
        )

        backend.pair_controller.assert_awaited_once_with(FAKE_PATH_1, self.SERIAL, self.ADAPTER_FREE)
        restart.assert_none()
        assert self.trusted_paths() == [self.TRUST_PATH]  # BR-007: the adapter it was paired to
        assert runner.calls == []
        assert self.SERIAL in usb_pairing._verified_serials
        metrics["pairing_success_total"].inc.assert_called_once()
        metrics["pairing_failed_total"].inc.assert_not_called()

    @pytest.mark.uc("UC-001", flow="A5", br=["BR-001", "BR-004"])
    async def test_repair_to_other_adapter_removes_old_registration(self, usb_pairing, env, metrics):
        """A4 moves a known controller to the least-loaded adapter; A5 removes its old registration there."""
        backend, _runner, set_tree, restart = env
        known = [{"Address": self.SERIAL}, *devices(2)]
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, known), "hci1": (self.ADAPTER_FREE, [])}))

        await usb_pairing.poll()

        members = [c.args[0].member for c in self.bus.call.await_args_list]
        assert members.index("RemoveDevice") < members.index("Set"), "A5 removes the old registration before trust"
        assert self.trusted_paths() == [self.TRUST_PATH]

        backend.pair_controller.assert_awaited_once_with(FAKE_PATH_1, self.SERIAL, self.ADAPTER_FREE)
        removals = [c.args[0] for c in self.bus.call.await_args_list if c.args[0].member == "RemoveDevice"]
        assert [(m.path, m.body) for m in removals] == [("/org/bluez/hci0", ["/org/bluez/hci0/dev_0"])]
        restart.assert_none()
        metrics["pairing_success_total"].inc.assert_called_once()

    @pytest.mark.uc("UC-001", flow="A5", br=["BR-004"])
    async def test_failed_stale_removal_does_not_fail_pairing(self, usb_pairing, env, metrics):
        """BR-004: a BlueZ error while removing the old registration is logged; the pairing still succeeds."""
        _backend, _runner, set_tree, _restart = env
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, [{"Address": self.SERIAL}]), "hci1": (self.ADAPTER_FREE, [])}))
        self.bus.call.return_value = Message(
            message_type=MessageType.ERROR,
            error_name="org.bluez.Error.Failed",
            reply_serial=1,
            body=["boom"],
            signature="s",
        )

        await usb_pairing.poll()

        assert self.trusted_paths() == [self.TRUST_PATH, self.TRUST_PATH]  # tried, failed, retried once
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

    @pytest.mark.uc("UC-001", flow="A2", br=["BR-007"])
    async def test_trust_failure_still_completes_pairing(self, usb_pairing, env, metrics):
        """UC-001 A2: the trust fails twice; the pairing is kept and counted as success."""
        self.fail_trust("org.bluez.Error.Failed")

        result = await usb_pairing.process_controller(FAKE_PATH_1, self.SERIAL)

        assert result is True
        assert self.trusted_paths() == [self.TRUST_PATH, self.TRUST_PATH]  # retried once
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

    @pytest.mark.uc("UC-001", cond=["Pre-1", "Post-F-1"])
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

    @pytest.mark.uc("UC-001", cond=["Post-F-1"])
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
        assert runner.calls == [], runner.calls

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"], cond=["Post-S-3"])
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

    @pytest.mark.uc("UC-001", flow="A2", br=["BR-005", "BR-007"])
    async def test_never_seen_controller_trust_fails_pairing_succeeds_nothing_restarts(self, usb_pairing, env, metrics):
        """A never-seen controller: BlueZ has no device object to trust; pairing still succeeds, no restart."""
        _backend, runner, set_tree, restart = env
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, devices(3, connected=True)), "hci1": (self.ADAPTER_FREE, [])}))
        self.fail_trust("org.freedesktop.DBus.Error.UnknownObject")

        result = await usb_pairing.process_controller(FAKE_PATH_1, self.SERIAL)

        assert result is True
        assert self.trusted_paths() == [self.TRUST_PATH]  # unknown object: not retried
        metrics["pairing_success_total"].inc.assert_called_once()
        self._assert_nothing_disruptive(runner, restart)


class TestBluezRestartModeInPairing(_PairingFlowEnv):
    """pairing.bluez_restart_mode through a real pairing (UC-001 BR-005, A6)."""

    def _restart_units(self) -> list:
        return [c.args[0].body for c in self.bus.call.await_args_list if c.args[0].member == "RestartUnit"]

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_always_restarts_bluetoothd_after_pairing(self, usb_pairing, env, metrics):
        """always: the pre-#1446 behaviour, a RestartUnit on systemd after the pairing, before trust."""
        _backend, runner, _set_tree, _restart = env
        with patch("psmove_pairing.bluez_reload.get_bluez_restart_mode", return_value="always"):
            await usb_pairing.poll()

        assert self._restart_units() == [["bluetooth.service", "replace"]]
        assert self.trusted_paths() == [self.TRUST_PATH]
        metrics["pairing_success_total"].inc.assert_called_once()

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_when_idle_without_a_new_device_record_restarts_nothing(self, usb_pairing, env, metrics):
        """when_idle: a controller BlueZ already knows on the selected adapter needs no record, so no restart."""
        _backend, _runner, set_tree, _restart = env
        known_on_free = [{"Address": self.SERIAL, "Paired": True}]
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, devices(3)), "hci1": (self.ADAPTER_FREE, known_on_free)}))
        game_busy = AsyncMock(return_value=False)
        usb_pairing.bluez_reloader._game_busy = game_busy
        with patch("psmove_pairing.bluez_reload.get_bluez_restart_mode", return_value="when_idle"):
            await usb_pairing.poll()

        assert self._restart_units() == []
        game_busy.assert_not_awaited()
        assert usb_pairing.bluez_reloader.pending is False

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_deferred_restart_runs_on_a_poll_after_the_game_ended(self, usb_pairing, env, metrics):
        """when_idle: a restart pending while the real game runs is run by the first poll after it ended."""
        backend, _runner, _set_tree, _restart = env
        backend.get_usb_controllers.return_value = []
        states = iter([True, False])
        usb_pairing.bluez_reloader._game_busy = AsyncMock(side_effect=lambda: next(states))
        usb_pairing.bluez_reloader.pending = True

        with patch("psmove_pairing.bluez_reload.get_bluez_restart_mode", return_value="when_idle"):
            await usb_pairing.poll()  # game running: deferred
            assert self._restart_units() == []
            assert usb_pairing.bluez_reloader.pending is True
            await usb_pairing.poll()  # game ended: restarted

        assert self._restart_units() == [["bluetooth.service", "replace"]]
        assert usb_pairing.bluez_reloader.pending is False


class TestFirstTimePairing(_PairingFlowEnv):
    """UC-001 A7 / A8 / BR-008 (#1466): a controller BlueZ does not know gets a record, loaded by a restart."""

    def _restart_units(self) -> list:
        return [c.args[0].body for c in self.bus.call.await_args_list if c.args[0].member == "RestartUnit"]

    def _info(self, adapter: str | None = None) -> Path:
        return self.bluetooth_dir / (adapter or self.ADAPTER_FREE) / self.SERIAL / "info"

    @staticmethod
    def _mode(mode: str):
        return patch("psmove_pairing.bluez_reload.get_bluez_restart_mode", return_value=mode)

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005", "BR-007", "BR-008"])
    async def test_never_seen_controller_gets_a_record_and_an_idle_restart(self, usb_pairing, env, metrics):
        """when_idle, no real game: record written on the selected adapter, bluetoothd restarted, agent
        registered again, then trusted on the selected adapter."""
        _backend, runner, _set_tree, _restart = env
        usb_pairing.bluez_reloader._game_busy = AsyncMock(return_value=False)
        order = []
        self.bus.call.side_effect = lambda m: order.append(m.member) or _reply()

        with self._mode("when_idle"):
            await usb_pairing.poll()

        info = self._info().read_text()
        assert "Trusted=true" in info and f"Product={0x03D5}" in info
        assert HID_SDP_RECORD in (self.bluetooth_dir / self.ADAPTER_FREE / "cache" / self.SERIAL).read_text()
        assert not (self.bluetooth_dir / self.ADAPTER_BUSY / self.SERIAL).exists()
        assert self._restart_units() == [["bluetooth.service", "replace"]]
        assert runner.calls == []  # no bluetoothctl: the agent is a D-Bus object (UC-001 BR-009)
        # The pairing agent, registered again after the restart
        assert order.index("RestartUnit") < order.index("RegisterAgent") < order.index("RequestDefaultAgent")
        # One restart after the poll's pairings; the trust before it may find no device object
        # yet (the record itself is trusted)
        assert order.index("Set") < order.index("RestartUnit")
        assert self.trusted_paths() == [self.TRUST_PATH]
        assert usb_pairing.bluez_reloader.pending is False
        metrics["pairing_success_total"].inc.assert_called_once()

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005", "BR-008"])
    async def test_record_restart_waits_for_the_real_game_to_end(self, usb_pairing, env, metrics):
        """when_idle during a real game: record written, no restart, nobody dropped; the first poll after
        the game ended restarts bluetoothd."""
        backend, _runner, _set_tree, _restart = env
        states = iter([True, True, False])
        usb_pairing.bluez_reloader._game_busy = AsyncMock(side_effect=lambda: next(states))

        with self._mode("when_idle"):
            await usb_pairing.poll()  # pairing: record written, game running -> deferred
            assert self._info().is_file()
            assert self._restart_units() == []
            assert usb_pairing.bluez_reloader.pending is True

            backend.get_usb_controllers.return_value = []  # player unplugged it
            await usb_pairing.poll()  # game still running
            assert self._restart_units() == []
            await usb_pairing.poll()  # game ended
        assert self._restart_units() == [["bluetooth.service", "replace"]]
        assert usb_pairing.bluez_reloader.pending is False
        metrics["pairing_success_total"].inc.assert_called_once()

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005", "BR-008"])
    async def test_never_writes_the_record_but_restarts_nothing(self, usb_pairing, env, metrics):
        _backend, runner, _set_tree, restart = env
        with self._mode("never"):
            await usb_pairing.poll()

        assert self._info().is_file()
        restart.assert_none()
        assert runner.calls == []

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-008"])
    async def test_known_on_the_selected_adapter_gets_no_record(self, usb_pairing, env, metrics):
        _backend, _runner, set_tree, restart = env
        set_tree(
            bluez_tree(
                {
                    "hci0": (self.ADAPTER_BUSY, devices(3)),
                    "hci1": (self.ADAPTER_FREE, [{"Address": self.SERIAL, "Trusted": True}]),
                }
            )
        )
        with self._mode("when_idle"):
            await usb_pairing.poll()

        assert not self._info().exists()
        restart.assert_none()

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-004", "BR-008"])
    async def test_controller_moved_from_another_adapter_gets_a_record_on_the_new_one(self, usb_pairing, env, metrics):
        """A4/A5: known on hci0 only, moved to hci1 -> hci1 gets the record (BlueZ does not know it there)."""
        _backend, _runner, set_tree, _restart = env
        set_tree(
            bluez_tree(
                {"hci0": (self.ADAPTER_BUSY, [{"Address": self.SERIAL}, *devices(2)]), "hci1": (self.ADAPTER_FREE, [])}
            )
        )
        usb_pairing.bluez_reloader._game_busy = AsyncMock(return_value=False)
        with self._mode("when_idle"):
            await usb_pairing.poll()

        assert self._info().is_file()
        assert self._restart_units() == [["bluetooth.service", "replace"]]

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-008"])
    async def test_existing_unloaded_record_still_asks_for_the_restart(self, usb_pairing, env, metrics):
        """Written under never earlier (or a deferred restart was dropped): kept as is, reload requested."""
        _backend, _runner, _set_tree, _restart = env
        self._info().parent.mkdir()
        self._info().write_text("[General]\nName=Motion Controller\n")
        usb_pairing.bluez_reloader._game_busy = AsyncMock(return_value=False)
        with self._mode("when_idle"):
            await usb_pairing.poll()

        assert self._info().read_text() == "[General]\nName=Motion Controller\n"
        assert self._restart_units() == [["bluetooth.service", "replace"]]

    @pytest.mark.uc("UC-001", flow="A8", br=["BR-008"])
    @pytest.mark.parametrize("product_id", [0, 0x042F])
    async def test_unknown_product_gets_no_record_and_no_restart(self, usb_pairing, env, metrics, product_id):
        """A8: e.g. an older rust-hid that reports no product id; the pairing is kept."""
        backend, _runner, _set_tree, restart = env
        backend.get_usb_controllers.return_value = [UsbController(FAKE_PATH_1, self.SERIAL, product_id)]
        usb_pairing.bluez_reloader._game_busy = AsyncMock(return_value=False)
        with self._mode("when_idle"):
            await usb_pairing.poll()

        assert not self._info().exists()
        restart.assert_none()
        metrics["pairing_success_total"].inc.assert_called_once()

    @pytest.mark.uc("UC-001", flow="A8", br=["BR-008"])
    async def test_adapter_without_bluez_storage_gets_no_record(self, usb_pairing, env, metrics):
        _backend, _runner, _set_tree, restart = env
        (self.bluetooth_dir / self.ADAPTER_FREE).rmdir()
        with self._mode("when_idle"):
            await usb_pairing.poll()

        assert not (self.bluetooth_dir / self.ADAPTER_FREE).exists()
        restart.assert_none()
        metrics["pairing_success_total"].inc.assert_called_once()


class TestDeferredRestartUnderPairingLock(_PairingFlowEnv):
    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_poll_runs_the_pending_restart_under_the_pairing_lock(self, usb_pairing, env, metrics):
        """The deferred restart never interleaves with a pairing or an unpair (UC-005 A6)."""
        backend, _runner, _set_tree, _restart = env
        backend.get_usb_controllers.return_value = []
        lock_held = []

        async def run_pending():
            lock_held.append(usb_pairing._pairing_lock.locked())
            return "restarted"

        usb_pairing.bluez_reloader.pending = True
        usb_pairing.bluez_reloader.run_pending = run_pending
        await usb_pairing.poll()

        assert lock_held == [True]
        assert not usb_pairing._pairing_lock.locked()

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_unpair_is_refused_while_the_pending_restart_runs(self, usb_pairing, env, metrics):
        from psmove_pairing.usb_pairing import PairingInProgressError

        backend, _runner, _set_tree, _restart = env
        backend.get_usb_controllers.return_value = []
        refused = []

        async def run_pending():
            try:
                await usb_pairing.unpair(self.SERIAL)
            except PairingInProgressError:
                refused.append(True)
            return "restarted"

        usb_pairing.bluez_reloader.pending = True
        usb_pairing.bluez_reloader.run_pending = run_pending
        await usb_pairing.poll()

        assert refused == [True]


class TestReviewFindings1476(_PairingFlowEnv):
    """Review of #1476: one restart per poll, temporary devices, records lost while deferred, pairing.trusted."""

    def _restart_units(self) -> list:
        return [c.args[0].body for c in self.bus.call.await_args_list if c.args[0].member == "RestartUnit"]

    def _info(self, serial: str | None = None) -> Path:
        return self.bluetooth_dir / self.ADAPTER_FREE / (serial or self.SERIAL) / "info"

    @staticmethod
    def _mode(mode: str):
        return patch("psmove_pairing.bluez_reload.get_bluez_restart_mode", return_value=mode)

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005", "BR-008"])
    async def test_three_new_controllers_in_one_poll_restart_bluetoothd_once(self, usb_pairing, env, metrics):
        backend, _runner, _set_tree, _restart = env
        serials = [f"00:06:F7:00:01:0{i}" for i in range(3)]
        backend.get_usb_controllers.return_value = [UsbController(FAKE_PATH_1, s, 0x03D5) for s in serials]
        usb_pairing.bluez_reloader._game_busy = AsyncMock(return_value=False)

        with self._mode("when_idle"):
            await usb_pairing.poll()

        assert self._restart_units() == [["bluetooth.service", "replace"]]
        assert metrics["pairing_success_total"].inc.call_count == 3

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-008"])
    async def test_temporary_device_on_the_selected_adapter_still_gets_a_record(self, usb_pairing, env, metrics):
        """A discovered or temporary Device1 (not Paired, Bonded or Trusted) is not a stored registration."""
        _backend, _runner, set_tree, _restart = env
        temporary = [{"Address": self.SERIAL, "Paired": False, "Trusted": False}]
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, devices(3)), "hci1": (self.ADAPTER_FREE, temporary)}))
        with self._mode("never"):
            await usb_pairing.poll()

        assert self._info().is_file()

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-008"])
    @pytest.mark.parametrize("flag", ["Paired", "Bonded", "Trusted"])
    async def test_stored_registration_on_the_selected_adapter_needs_no_record(self, usb_pairing, env, metrics, flag):
        _backend, _runner, set_tree, _restart = env
        stored = [{"Address": self.SERIAL, flag: True}]
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, devices(3)), "hci1": (self.ADAPTER_FREE, stored)}))
        with self._mode("when_idle"):
            await usb_pairing.poll()

        assert not self._info().exists()

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005", "BR-008"])
    async def test_record_lost_during_a_game_is_rewritten_and_a_replug_pairs_again(self, usb_pairing, env, metrics):
        backend, _runner, _set_tree, _restart = env
        busy = {"value": True}
        usb_pairing.bluez_reloader._game_busy = AsyncMock(side_effect=lambda: busy["value"])

        with self._mode("when_idle"):
            await usb_pairing.poll()  # paired, record written, restart deferred
            assert self._info().is_file()
            assert self._restart_units() == []

            await usb_pairing.poll()  # still on USB: not paired again
            assert backend.pair_controller.await_count == 1

            self._info().unlink()  # bluetoothd dropped the unloaded record
            backend.get_usb_controllers.return_value = []
            await usb_pairing.poll()  # unplugged: no longer "verified"
            assert self.SERIAL not in usb_pairing._verified_serials

            backend.get_usb_controllers.return_value = [UsbController(FAKE_PATH_1, self.SERIAL, 0x03D5)]
            await usb_pairing.poll()  # replugged: paired again, record written again
            assert backend.pair_controller.await_count == 2
            assert self._info().is_file()

            self._info().unlink()  # lost again before the game ends
            busy["value"] = False
            await usb_pairing.poll()  # game ended: record restored, then one restart

        assert self._info().is_file()
        assert self._restart_units() == [["bluetooth.service", "replace"]]

    @pytest.mark.uc("UC-001", flow="A2", br=["BR-007"])
    @pytest.mark.parametrize(
        ("error_name", "expected"),
        [
            (None, "true"),
            ("org.bluez.Error.Failed", "false"),
            ("org.freedesktop.DBus.Error.UnknownObject", "unknown_object"),
        ],
    )
    async def test_trust_outcome_is_a_span_attribute(
        self, usb_pairing, env, metrics, mock_tracer, error_name, expected
    ):
        if error_name:
            self.fail_trust(error_name)
        with self._mode("never"):
            await usb_pairing.poll()

        span = mock_tracer.start_as_current_span.return_value
        assert ("pairing.trusted", expected) in [c.args for c in span.set_attribute.call_args_list]


class TestDeferredRestartRobustness1490(_PairingFlowEnv):
    """#1490: a pending bluetoothd restart survives a failed enumeration and a failed restart (UC-001 BR-005)."""

    def _restart_units(self) -> list:
        return [c.args[0].body for c in self.bus.call.await_args_list if c.args[0].member == "RestartUnit"]

    def _info(self) -> Path:
        return self.bluetooth_dir / self.ADAPTER_FREE / self.SERIAL / "info"

    @staticmethod
    def _mode(mode: str):
        return patch("psmove_pairing.bluez_reload.get_bluez_restart_mode", return_value=mode)

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_deferred_restart_runs_while_rust_hid_is_down(self, usb_pairing, env, metrics):
        """The game ended but USB enumeration fails (rust-hid unreachable): the restart still runs."""
        backend, _runner, _set_tree, _restart = env
        busy = {"value": True}
        usb_pairing.bluez_reloader._game_busy = AsyncMock(side_effect=lambda: busy["value"])

        with self._mode("when_idle"):
            await usb_pairing.poll()  # paired, record written, restart deferred
            assert self._restart_units() == []

            busy["value"] = False
            backend.get_usb_controllers.side_effect = RuntimeError("rust-hid unreachable")
            with pytest.raises(RuntimeError, match="rust-hid unreachable"):
                await usb_pairing.poll()

        assert self._restart_units() == [["bluetooth.service", "replace"]]
        assert usb_pairing.bluez_reloader.pending is False

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005", "BR-008"])
    async def test_failed_restart_is_retried_and_a_replug_still_pairs_again(self, usb_pairing, env, metrics):
        """A failed restart keeps the record pending and the controller re-pairable after a replug."""
        backend, _runner, _set_tree, _restart = env
        usb_pairing.bluez_reloader._game_busy = AsyncMock(return_value=False)
        restart = AsyncMock(side_effect=[RuntimeError("systemd said no"), None])
        usb_pairing.bluez_reloader._restart = restart
        now = {"t": 0.0}
        usb_pairing.bluez_reloader._clock = lambda: now["t"]

        with self._mode("when_idle"):
            await usb_pairing.poll()  # paired, record written, restart fails
            assert restart.await_count == 1
            assert usb_pairing.bluez_reloader.pending is True
            assert self.SERIAL in usb_pairing._replug_retries

            self._info().unlink()  # bluetoothd dropped the unloaded record
            backend.get_usb_controllers.return_value = []
            await usb_pairing.poll()  # unplugged; the retry waits (backoff)
            assert restart.await_count == 1
            assert self.SERIAL not in usb_pairing._verified_serials  # a replug pairs it again

            now["t"] = 10.0
            await usb_pairing.poll()  # the retry rewrites the record, then restarts

        assert restart.await_count == 2
        assert self._info().is_file()
        assert usb_pairing.bluez_reloader.pending is False

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_failed_restart_after_a_deferral_keeps_the_replug_retry(self, usb_pairing, env, metrics):
        backend, _runner, _set_tree, _restart = env
        busy = {"value": True}
        usb_pairing.bluez_reloader._game_busy = AsyncMock(side_effect=lambda: busy["value"])
        usb_pairing.bluez_reloader._restart = AsyncMock(side_effect=RuntimeError("systemd said no"))

        with self._mode("when_idle"):
            await usb_pairing.poll()  # deferred
            assert self.SERIAL in usb_pairing._replug_retries
            busy["value"] = False
            await usb_pairing.poll()  # still on USB; restart fails

        assert self.SERIAL in usb_pairing._replug_retries
        assert usb_pairing.bluez_reloader.pending is True

    @pytest.mark.uc("UC-001", flow="A8", br=["BR-008"])
    async def test_deleting_an_unusable_info_and_replugging_pairs_it_anew(self, usb_pairing, env, metrics):
        """An empty info blocks the record (never overwritten); once the operator deletes it, a replug fixes it."""
        backend, _runner, _set_tree, _restart = env
        usb_pairing.bluez_reloader._game_busy = AsyncMock(return_value=False)
        self._info().parent.mkdir()
        self._info().write_text("")

        with self._mode("when_idle"):
            await usb_pairing.poll()  # A8: record not usable, no restart
            assert self._info().read_text() == ""
            assert self._restart_units() == []

            await usb_pairing.poll()  # still plugged in: verified, not paired again (A1)
            assert backend.pair_controller.await_count == 1

            self._info().unlink()  # the operator deletes it, as the error says
            backend.get_usb_controllers.return_value = []
            await usb_pairing.poll()  # unplugged
            backend.get_usb_controllers.return_value = [UsbController(FAKE_PATH_1, self.SERIAL, 0x03D5)]
            await usb_pairing.poll()  # plugged in again: paired anew, record written, restarted

        assert backend.pair_controller.await_count == 2
        assert "Trusted=true" in self._info().read_text()
        assert self._restart_units() == [["bluetooth.service", "replace"]]

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_cancelled_poll_runs_no_pending_restart(self, usb_pairing, env, metrics):
        """A daemon shutting down (poll cancelled mid-enumeration) restarts nothing."""
        backend, _runner, _set_tree, _restart = env
        backend.get_usb_controllers.side_effect = asyncio.CancelledError
        run_pending = AsyncMock(return_value="restarted")
        usb_pairing.bluez_reloader.pending = True
        usb_pairing.bluez_reloader.run_pending = run_pending

        with pytest.raises(asyncio.CancelledError):
            await usb_pairing.poll()

        run_pending.assert_not_awaited()
        assert self._restart_units() == []


class TestPairingAgentFollowsUsbPairing(_PairingFlowEnv):
    """The agent answers for a controller from its USB pairing until it is unpaired (UC-001 BR-009, #1492)."""

    DEVICE = "/org/bluez/hci1/dev_00_06_F7_AA_BB_CC"

    @staticmethod
    def _pin(agent: PairingAgent):
        return PairingAgent.RequestPinCode.__wrapped__(agent, TestPairingAgentFollowsUsbPairing.DEVICE)

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_agent_answers_from_usb_pairing_until_unpair(self, usb_pairing, env, metrics):
        _backend, _runner, set_tree, _restart = env
        set_tree(bluez_tree({"hci0": (self.ADAPTER_BUSY, devices(3, connected=True)), "hci1": (self.ADAPTER_FREE, [])}))
        agent = PairingAgent()
        agent.is_known = usb_pairing.paired_this_session

        with pytest.raises(DBusReplyError):
            self._pin(agent)  # not paired over USB yet

        await usb_pairing.poll()
        assert self._pin(agent) == "0000"

        removed = RemovalResult(PsMoveRegistration(self.ADAPTER_FREE, self.SERIAL), "removed")
        with patch("psmove_pairing.usb_pairing.remove_controller_registrations", AsyncMock(return_value=[removed])):
            await usb_pairing.unpair(self.SERIAL.lower())

        with pytest.raises(DBusReplyError) as rejected:
            self._pin(agent)
        assert rejected.value.type == "org.bluez.Error.Rejected"

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_replug_retry_keeps_the_agent_answering(self, usb_pairing, env, metrics):
        """A controller whose record waits for a restart leaves the verified set on unplug, not the agent's list."""
        usb_pairing._paired_addresses.add(self.SERIAL)
        usb_pairing._verified_serials.add(self.SERIAL)
        usb_pairing._replug_retries.add(self.SERIAL)
        usb_pairing._usb_paths = {}

        usb_pairing._forget_unplugged_deferred()

        assert self.SERIAL not in usb_pairing._verified_serials
        assert usb_pairing.paired_this_session(self.SERIAL)


class TestRestartWaitsForQuietUsb2091(_PairingFlowEnv):
    """#2091: one bluetoothd restart for a stack of pairings, once USB has been quiet (UC-001 BR-005, A7)."""

    SERIALS = ("00:06:F7:00:00:01", "00:06:F7:00:00:02", "00:06:F7:00:00:03")

    def _restart_units(self) -> list:
        return [c.args[0].body for c in self.bus.call.await_args_list if c.args[0].member == "RestartUnit"]

    @staticmethod
    def _flags(quiet: float = 20):
        return (
            patch("psmove_pairing.bluez_reload.get_bluez_restart_mode", return_value="when_idle"),
            patch("psmove_pairing.bluez_reload.get_restart_quiet_period", return_value=quiet),
        )

    async def _poll_at(self, usb_pairing, backend, now, t: float, serial: str | None) -> None:
        now["t"] = t
        backend.get_usb_controllers.return_value = (
            [UsbController(f"/dev/hidraw{serial[-1]}".encode(), serial, 0x03D5)] if serial else []
        )
        await usb_pairing.poll()

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005", "BR-008"])
    async def test_three_pairings_in_a_row_cost_one_restart(self, usb_pairing, env, metrics):
        """Plug, pair, unplug, next: no restart while controllers come and go; one after 20 s of quiet USB."""
        backend, _runner, _set_tree, _restart = env
        usb_pairing.bluez_reloader._game_busy = AsyncMock(return_value=False)
        now = {"t": 0.0}
        usb_pairing.bluez_reloader._clock = lambda: now["t"]
        mode, quiet = self._flags()

        with mode, quiet:
            t = 0.0
            for serial in self.SERIALS:
                await self._poll_at(usb_pairing, backend, now, t, serial)  # paired, record written
                await self._poll_at(usb_pairing, backend, now, t + 10, None)  # unplugged
                assert self._restart_units() == [], f"restarted while pairing {serial}"
                t += 20
            # Last controller seen at t=40; quiet until t=60
            await self._poll_at(usb_pairing, backend, now, 55, None)
            assert self._restart_units() == []
            await self._poll_at(usb_pairing, backend, now, 60, None)

        assert self._restart_units() == [["bluetooth.service", "replace"]]
        assert usb_pairing.bluez_reloader.pending is False
        for serial in self.SERIALS:  # each got its record on the free adapter (A7)
            assert (self.bluetooth_dir / self.ADAPTER_FREE / serial / "info").is_file()
        assert metrics["pairing_success_total"].inc.call_count == 3

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"], cond=["Post-S-3"])
    async def test_quiet_usb_still_waits_for_the_real_game(self, usb_pairing, env, metrics):
        """Idle game but a pairing on USB: wait. Quiet USB but a busy game: deferred. Both clear: restart."""
        backend, _runner, _set_tree, _restart = env
        busy = {"value": False}
        usb_pairing.bluez_reloader._game_busy = AsyncMock(side_effect=lambda: busy["value"])
        now = {"t": 0.0}
        usb_pairing.bluez_reloader._clock = lambda: now["t"]
        mode, quiet = self._flags()

        with mode, quiet:
            serial = self.SERIALS[0]
            await self._poll_at(usb_pairing, backend, now, 0, serial)  # paired, still plugged in
            await self._poll_at(usb_pairing, backend, now, 10, serial)  # paired 10 s ago: holds the restart
            assert self._restart_units() == []

            busy["value"] = True
            await self._poll_at(usb_pairing, backend, now, 40, None)  # unplugged, game started
            await self._poll_at(usb_pairing, backend, now, 90, None)  # quiet, game running: deferred
            assert self._restart_units() == []
            assert usb_pairing.bluez_reloader.pending is True

            busy["value"] = False
            await self._poll_at(usb_pairing, backend, now, 100, None)  # game over: next idle poll restarts

        assert self._restart_units() == [["bluetooth.service", "replace"]]

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_a_paired_move_left_charging_does_not_hold_the_restart(self, usb_pairing, env, metrics):
        """Move 1 stays on USB to charge while Move 2 is paired and unplugged: restart 20 s after Move 2."""
        backend, _runner, _set_tree, _restart = env
        usb_pairing.bluez_reloader._game_busy = AsyncMock(return_value=False)
        now = {"t": 0.0}
        usb_pairing.bluez_reloader._clock = lambda: now["t"]
        charging, second = (UsbController(f"/dev/hidraw{s[-1]}".encode(), s, 0x03D5) for s in self.SERIALS[:2])

        async def poll_at(t: float, *controllers: UsbController) -> None:
            now["t"] = t
            backend.get_usb_controllers.return_value = list(controllers)
            await usb_pairing.poll()

        mode, quiet = self._flags()
        with mode, quiet:
            await poll_at(0, charging)  # Move 1 paired, left on USB
            await poll_at(10, charging, second)  # Move 2 paired next to it
            await poll_at(20, charging)  # Move 2 unplugged; Move 1 charges on
            await poll_at(29, charging)
            assert self._restart_units() == []
            await poll_at(30, charging)  # 20 s after Move 2 settled

        assert self._restart_units() == [["bluetooth.service", "replace"]]
        assert usb_pairing.bluez_reloader.pending is False

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_quiet_period_zero_restarts_at_the_end_of_the_pairing_poll(self, usb_pairing, env, metrics):
        backend, _runner, _set_tree, _restart = env
        usb_pairing.bluez_reloader._game_busy = AsyncMock(return_value=False)
        mode, quiet = self._flags(quiet=0)
        with mode, quiet:
            await usb_pairing.poll()  # controller still on USB
        assert self._restart_units() == [["bluetooth.service", "replace"]]


class TestPairingLedCue2091(_PairingFlowEnv):
    """#2091: the controller on USB shows the pairing state on its LED (UC-001 BR-010), fake device."""

    SECOND = "00:06:F7:AA:BB:DD"
    SECOND_PATH = b"/dev/hidraw9"

    @pytest.fixture
    async def device(self, usb_pairing):
        from psmove_pairing.usb_led import _real_sleep

        frames: dict[bytes, list] = {}

        async def tick(_seconds):
            await _real_sleep(0)

        usb_pairing.led_cue = UsbLedCue(write=lambda path, rgb: frames.setdefault(path, []).append(rgb), sleep=tick)
        yield frames
        await usb_pairing.led_cue.close()

    @pytest.mark.uc("UC-001", flow="main", steps=[2, 6], br=["BR-010"])
    async def test_pulse_while_waiting_then_green_when_paired(self, usb_pairing, env, metrics, device):
        """Both listed controllers pulse from the start of the poll; each flashes green once paired."""
        backend, _runner, _set_tree, _restart = env
        backend.get_usb_controllers.return_value = [
            UsbController(FAKE_PATH_1, self.SERIAL, 0x03D5),
            UsbController(self.SECOND_PATH, self.SECOND, 0x03D5),
        ]
        seen = []

        async def pair(path, serial, adapter):
            seen.append(dict(usb_pairing.led_cue.states))
            return PairingResult(success=True)

        backend.pair_controller.side_effect = pair
        await usb_pairing.poll()

        # While the first one pairs, both pulse (the second waits for its turn)
        first, second = (FAKE_PATH_1, self.SERIAL), (self.SECOND_PATH, self.SECOND)
        assert seen[0] == {first: "pairing", second: "pairing"}
        assert seen[1][first] == "paired" and seen[1][second] == "pairing"
        await asyncio.gather(*usb_pairing.led_cue._tasks.values())
        for key in (first, second):
            assert usb_pairing.led_cue.states[key] == "paired"
            assert device[key[0]][-6:] == [GREEN, OFF] * 3

        # Next poll: both verified (A1): no new cue; unplugged: the cues are gone
        await usb_pairing.poll()
        assert usb_pairing.led_cue.states == {first: "paired", second: "paired"}
        backend.get_usb_controllers.return_value = []
        await usb_pairing.poll()
        assert usb_pairing.led_cue.states == {}

    @pytest.mark.uc("UC-001", br=["BR-010"], cond=["Post-F-1"])
    async def test_failed_pairing_shows_red_and_the_retry_pulses(self, usb_pairing, env, metrics, device):
        backend, _runner, _set_tree, _restart = env
        backend.pair_controller.return_value = PairingResult(success=False)

        await usb_pairing.poll()
        assert usb_pairing.led_cue.states[(FAKE_PATH_1, self.SERIAL)] == "failed"
        from psmove_pairing.usb_led import _real_sleep  # asyncio.sleep is patched in env

        for _ in range(1000):
            if RED in device.get(FAKE_PATH_1, []):
                break
            await _real_sleep(0.001)
        assert RED in device[FAKE_PATH_1]
        assert GREEN not in device[FAKE_PATH_1]

        seen = []

        async def pair(_path, _serial, _adapter):
            seen.append(usb_pairing.led_cue.states[(FAKE_PATH_1, self.SERIAL)])
            return PairingResult(success=True)

        backend.pair_controller.side_effect = pair
        await usb_pairing.poll()
        assert seen == ["pairing"]
        assert usb_pairing.led_cue.states[(FAKE_PATH_1, self.SERIAL)] == "paired"

    @pytest.mark.uc("UC-001", flow="A8", br=["BR-008", "BR-010"])
    async def test_unwritable_record_shows_red(self, usb_pairing, env, metrics, device):
        """Product id not reported (A8): the pairing is kept, but the controller cannot connect: red."""
        backend, _runner, _set_tree, _restart = env
        backend.get_usb_controllers.return_value = [UsbController(FAKE_PATH_1, self.SERIAL, 0)]
        await usb_pairing.poll()
        assert usb_pairing.led_cue.states[(FAKE_PATH_1, self.SERIAL)] == "failed"
        metrics["pairing_success_total"].inc.assert_called_once()
