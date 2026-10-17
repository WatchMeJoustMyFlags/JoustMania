"""Adapter identity, HCI error counters and link roles (UC-035 BR-004, FR-122, #1431)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from psmove_pairing.adapter_identity import (
    UNKNOWN,
    AdapterIdentity,
    HciErrorTracker,
    is_devicetree_radio,
    parse_hciconfig,
    parse_hcitool_con,
    usb_id,
)
from psmove_pairing.bluetooth_monitor import BluetoothMonitor

from .conftest import MockCommandRunner

HCICONFIG_TWO = """\
hci1:	Type: Primary  Bus: USB
	BD Address: 00:1a:7d:da:71:13  ACL MTU: 310:10  SCO MTU: 64:8
	UP RUNNING PSCAN
	RX bytes:123456 acl:7890 sco:0 events:1234 errors:3
	TX bytes:65432 acl:7000 sco:0 commands:567 errors:1
	Features: 0xff 0xff 0x8f 0xfe 0xdb 0xff 0x5b 0x87
	Link mode: SLAVE ACCEPT
	Name: 'joust'
	HCI Version: 4.0 (0x6)  Revision: 0x22bb
	LMP Version: 4.0 (0x6)  Subversion: 0x22bb
	Manufacturer: Cambridge Silicon Radio (10)

hci0:	Type: Primary  Bus: UART
	BD Address: D8:3A:DD:00:11:22  ACL MTU: 1021:8  SCO MTU: 64:1
	DOWN
	RX bytes:0 acl:0 sco:0 events:0 errors:0
	TX bytes:0 acl:0 sco:0 commands:0 errors:0
"""

HCITOOL_CON_ROLES = """\
Connections:
	< ACL 00:06:F7:AA:BB:CC handle 256 state 1 lm MASTER AUTH ENCRYPT
	< ACL 00:06:F7:DD:EE:FF handle 257 state 1 lm SLAVE
	> ACL 00:06:F7:11:22:33 handle 258 state 1 lm PERIPHERAL
	< ACL 00:06:F7:44:55:66 handle 259 state 1
"""


def _fake_usb_sysfs(root: Path, hci: str, vendor: str, product: str) -> Path:
    """Build /sys/class/bluetooth/<hci>/device -> .../usb1/1-1/1-1:1.0 with idVendor on 1-1."""
    usb_device = root / "devices" / "usb1" / "1-1"
    interface = usb_device / "1-1:1.0"
    interface.mkdir(parents=True)
    (usb_device / "idVendor").write_text(vendor + "\n")
    (usb_device / "idProduct").write_text(product + "\n")
    sys_class = root / "class" / "bluetooth" / hci
    sys_class.mkdir(parents=True)
    (sys_class / "device").symlink_to(interface)
    return root / "class" / "bluetooth"


class TestParseHciconfig:
    @pytest.mark.uc("UC-035", br=["BR-004"])
    def test_identity_from_hci_manufacturer_and_usb(self, tmp_path):
        sys_class = _fake_usb_sysfs(tmp_path, "hci1", "0A12", "0001")
        adapters = {a.hci: a for a in parse_hciconfig(HCICONFIG_TWO, sys_class, tmp_path / "dt")}

        csr = adapters["hci1"]
        assert csr.labels() == {
            "hci_adapter": "hci1",
            "address": "00:1A:7D:DA:71:13",
            "manufacturer": "Cambridge Silicon Radio",
            "manufacturer_id": "10",
            "hci_version": "4.0",
            "bus": "USB",
            "usb_id": "0a12:0001",
            "onboard": "false",
        }
        assert (csr.rx_errors, csr.tx_errors) == (3, 1)

    @pytest.mark.uc("UC-035", br=["BR-004"])
    def test_down_uart_adapter_is_onboard_with_unknown_identity(self, tmp_path):
        adapters = {a.hci: a for a in parse_hciconfig(HCICONFIG_TWO, tmp_path / "none", tmp_path / "dt")}

        uart = adapters["hci0"]
        assert uart.onboard is True
        assert uart.bus == "UART"
        assert uart.manufacturer == UNKNOWN
        assert uart.manufacturer_id == UNKNOWN
        assert uart.hci_version == UNKNOWN
        assert uart.usb_id == UNKNOWN
        assert (uart.rx_errors, uart.tx_errors) == (0, 0)

    @pytest.mark.uc("UC-035", br=["BR-004"])
    def test_minimal_block_has_no_error_counters(self, tmp_path):
        (adapter,) = parse_hciconfig("hci0:   Type: Primary  Bus: USB\n", tmp_path, tmp_path)
        assert adapter == AdapterIdentity(hci="hci0", bus="USB")

    @pytest.mark.uc("UC-035", br=["BR-004"])
    def test_usb_id_unknown_without_sysfs(self, tmp_path):
        assert usb_id("hci9", tmp_path) == UNKNOWN

    @pytest.mark.uc("UC-035", br=["BR-004"])
    def test_pi_devicetree_radio_is_onboard_even_on_usb_numbering(self, tmp_path):
        # Device tree: aliases/bluetooth -> /soc/serial/bluetooth, and the HCI device's parent links to that node.
        dt = tmp_path / "dt"
        node = dt / "soc" / "serial" / "bluetooth"
        node.mkdir(parents=True)
        (dt / "aliases").mkdir()
        (dt / "aliases" / "bluetooth").write_text("/soc/serial/bluetooth\0")
        (dt / "model").write_text("Raspberry Pi 5 Model B Rev 1.0\0")
        serial_dev = tmp_path / "devices" / "serial0" / "serial0-0"
        serial_dev.mkdir(parents=True)
        (serial_dev / "of_node").symlink_to(node)
        sys_class = tmp_path / "class" / "bluetooth"
        (sys_class / "hci3").mkdir(parents=True)
        (sys_class / "hci3" / "device").symlink_to(serial_dev)

        assert is_devicetree_radio("hci3", sys_class, dt) is True
        (dt / "model").write_text("Some Laptop\0")
        assert is_devicetree_radio("hci3", sys_class, dt) is False


class TestParseHcitoolCon:
    @pytest.mark.uc("UC-035", br=["BR-004"])
    def test_local_role_per_link(self):
        assert parse_hcitool_con(HCITOOL_CON_ROLES) == [
            ("00:06:f7:aa:bb:cc", "central"),
            ("00:06:f7:dd:ee:ff", "peripheral"),
            ("00:06:f7:11:22:33", "peripheral"),
            ("00:06:f7:44:55:66", UNKNOWN),
        ]

    @pytest.mark.uc("UC-035", br=["BR-004"])
    def test_no_links(self):
        assert parse_hcitool_con("Connections:\n") == []


class TestHciErrorTracker:
    @pytest.mark.uc("UC-035", br=["BR-004"])
    def test_first_reading_is_the_baseline_then_increases_count(self):
        tracker = HciErrorTracker()
        assert tracker.delta("hci0", "rx", 5) == 0
        assert tracker.delta("hci0", "rx", 5) == 0
        assert tracker.delta("hci0", "rx", 8) == 3
        assert tracker.delta("hci0", "tx", 2) == 0

    @pytest.mark.uc("UC-035", br=["BR-004"])
    def test_adapter_reset_counts_the_new_value(self):
        tracker = HciErrorTracker()
        tracker.delta("hci0", "rx", 10)
        assert tracker.delta("hci0", "rx", 2) == 2


MONITOR = "psmove_pairing.bluetooth_monitor"


class TestMonitorExports:
    @pytest.fixture
    def monitor(self, mock_tracer, tmp_path):
        return BluetoothMonitor(mock_tracer, sys_class=tmp_path / "none", device_tree=tmp_path / "dt")

    @staticmethod
    def _runner(hciconfig: str, con: str) -> MockCommandRunner:
        runner = MockCommandRunner()
        runner.add_response(["hciconfig", "-a"], (0, hciconfig))
        runner.add_prefix_response(["hcitool", "-i", "hci1", "con"], (0, con))
        runner.add_prefix_response(["hcitool", "-i", "hci0", "con"], (0, "Connections:\n"))
        runner.add_prefix_response(["hcitool", "-i", "hci1", "rssi"], (0, "RSSI return value: -50"))
        return runner

    @pytest.mark.uc("UC-035", steps=[6], br=["BR-004"])
    @pytest.mark.asyncio
    async def test_cycle_exports_identity_errors_and_roles(self, monitor):
        con = "Connections:\n\t< ACL 00:06:F7:AA:BB:CC handle 256 state 1 lm MASTER\n"
        with (
            patch(f"{MONITOR}.run_command", self._runner(HCICONFIG_TWO, con)),
            patch(f"{MONITOR}.pairing_bluetooth_adapter_info") as info,
            patch(f"{MONITOR}.pairing_bluetooth_adapter_hci_errors_total") as errors,
            patch(f"{MONITOR}.pairing_bluetooth_link_role_info") as roles,
        ):
            await monitor.monitor()

        exported = [c.kwargs for c in info.labels.call_args_list]
        assert {e["hci_adapter"] for e in exported} == {"hci0", "hci1"}
        assert next(e for e in exported if e["hci_adapter"] == "hci1")["manufacturer_id"] == "10"
        info.labels.return_value.set.assert_called_with(1)
        errors.labels.assert_any_call(hci_adapter="hci1", direction="rx")
        errors.labels.assert_any_call(hci_adapter="hci1", direction="tx")
        roles.labels.assert_called_once_with(serial="0006F7AABBCC", hci_adapter="hci1", role="central")
        roles.labels.return_value.set.assert_called_once_with(1)

    @pytest.mark.uc("UC-035", br=["BR-004"])
    @pytest.mark.asyncio
    async def test_hci_error_increase_between_cycles_is_counted(self, monitor):
        later = HCICONFIG_TWO.replace("errors:3", "errors:7")
        with patch(f"{MONITOR}.pairing_bluetooth_adapter_hci_errors_total") as errors:
            with patch(f"{MONITOR}.run_command", self._runner(HCICONFIG_TWO, "Connections:\n")):
                await monitor.monitor()
            errors.reset_mock()
            with patch(f"{MONITOR}.run_command", self._runner(later, "Connections:\n")):
                await monitor.monitor()

        increments = {
            (c.kwargs["hci_adapter"], c.kwargs["direction"]): inc.args[0]
            for c, inc in zip(errors.labels.call_args_list, errors.labels.return_value.inc.call_args_list, strict=True)
        }
        assert increments[("hci1", "rx")] == 4
        assert increments[("hci1", "tx")] == 0

    @pytest.mark.uc("UC-035", br=["BR-004"])
    @pytest.mark.asyncio
    async def test_role_change_and_disconnect_replace_and_remove_the_series(self, monitor):
        central = "Connections:\n\t< ACL 00:06:F7:AA:BB:CC handle 256 state 1 lm MASTER\n"
        peripheral = "Connections:\n\t< ACL 00:06:F7:AA:BB:CC handle 256 state 1 lm SLAVE\n"
        with patch(f"{MONITOR}.pairing_bluetooth_link_role_info") as roles:
            for con in (central, peripheral):
                with patch(f"{MONITOR}.run_command", self._runner(HCICONFIG_TWO, con)):
                    await monitor.monitor()
            roles.remove.assert_called_once_with("0006F7AABBCC", "hci1", "central")

            with patch(f"{MONITOR}.run_command", self._runner(HCICONFIG_TWO, "Connections:\n")):
                await monitor.monitor()
            roles.remove.assert_called_with("0006F7AABBCC", "hci1", "peripheral")

    @pytest.mark.uc("UC-035", br=["BR-004"])
    @pytest.mark.asyncio
    async def test_unplugged_adapter_drops_its_identity(self, monitor):
        only_hci0 = HCICONFIG_TWO[HCICONFIG_TWO.index("hci0:") :]
        with patch(f"{MONITOR}.pairing_bluetooth_adapter_info") as info:
            with patch(f"{MONITOR}.run_command", self._runner(HCICONFIG_TWO, "Connections:\n")):
                await monitor.monitor()
            with patch(f"{MONITOR}.run_command", self._runner(only_hci0, "Connections:\n")):
                await monitor.monitor()

        removed = info.remove.call_args.args
        assert removed[0] == "hci1"
        assert removed[3] == "10"
