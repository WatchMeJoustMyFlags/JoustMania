"""Bluetooth monitoring for connected devices."""

import logging
import re
import time
from pathlib import Path

from opentelemetry import trace

from lib.controller_constants import normalize_serial

from .adapter_identity import (
    DEVICE_TREE,
    SYS_CLASS_BLUETOOTH,
    AdapterIdentity,
    HciErrorTracker,
    parse_hciconfig,
    parse_hcitool_con,
)
from .config import get_bt_monitor_interval
from .metrics import (
    bluetooth_adapter_connections,
    bluetooth_device_connected,
    bluetooth_device_last_seen,
    bluetooth_device_rssi_dbm,
    pairing_bluetooth_adapter_hci_errors_total,
    pairing_bluetooth_adapter_info,
    pairing_bluetooth_link_role_info,
)
from .utils import run_command

logger = logging.getLogger("psmove-pairing")


class BluetoothMonitor:
    """Monitors Bluetooth-connected devices for RSSI and connection status.

    Uses hcitool to monitor all HCI connections - no psmove dependency.
    This avoids conflicts with controller_manager which uses psmoveapi.
    """

    def __init__(
        self,
        tracer: trace.Tracer,
        sys_class: Path = SYS_CLASS_BLUETOOTH,
        device_tree: Path = DEVICE_TREE,
    ):
        self.tracer = tracer
        self._sys_class = sys_class
        self._device_tree = device_tree
        self.monitor_count = 0
        # Track known devices for disconnect detection
        # Key: (serial, hci_adapter), Value: last_seen_timestamp
        self._known_devices: dict[tuple[str, str], float] = {}
        # Exported label sets, so a changed identity / role replaces its series (UC-035 step 6)
        self._adapter_info_labels: dict[str, dict[str, str]] = {}
        self._link_roles: dict[tuple[str, str], str] = {}
        self._hci_errors = HciErrorTracker()

    async def get_adapter_identities(self) -> list[AdapterIdentity]:
        """Identity and HCI error counters of every adapter from ``hciconfig -a``."""
        exit_code, output = await run_command(["hciconfig", "-a"])
        if exit_code != 0:
            logger.debug("hciconfig failed")
            return []
        return parse_hciconfig(output, self._sys_class, self._device_tree)

    def _export_adapter(self, identity: AdapterIdentity) -> None:
        """Export one adapter's identity series and HCI error increases."""
        labels = identity.labels()
        previous = self._adapter_info_labels.get(identity.hci)
        if previous is not None and previous != labels:
            pairing_bluetooth_adapter_info.remove(*previous.values())
        pairing_bluetooth_adapter_info.labels(**labels).set(1)
        self._adapter_info_labels[identity.hci] = labels
        for direction, value in (("rx", identity.rx_errors), ("tx", identity.tx_errors)):
            if value is None:
                continue
            increase = self._hci_errors.delta(identity.hci, direction, value)
            pairing_bluetooth_adapter_hci_errors_total.labels(hci_adapter=identity.hci, direction=direction).inc(
                increase
            )

    def _forget_adapters(self, present: set[str]) -> None:
        """Drop the identity series of adapters that are gone (unplugged)."""
        for hci in [h for h in self._adapter_info_labels if h not in present]:
            pairing_bluetooth_adapter_info.remove(*self._adapter_info_labels.pop(hci).values())

    def _export_role(self, serial: str, hci: str, role: str) -> None:
        """Export the local role on a link; a changed role replaces its series."""
        previous = self._link_roles.get((serial, hci))
        if previous is not None and previous != role:
            pairing_bluetooth_link_role_info.remove(serial, hci, previous)
        pairing_bluetooth_link_role_info.labels(serial=serial, hci_adapter=hci, role=role).set(1)
        self._link_roles[(serial, hci)] = role

    async def get_bluetooth_adapters(self) -> list[str]:
        """Get list of available HCI adapters (hci0, hci1, etc.)."""
        exit_code, output = await run_command(["hciconfig", "-a"])
        if exit_code != 0:
            logger.debug("hciconfig failed")
            return []

        # Parse adapter names from output
        # Format: "hci0:   Type: Primary  Bus: USB"
        adapters = []
        for line in output.split("\n"):
            match = re.match(r"^(hci\d+):", line)
            if match:
                adapters.append(match.group(1))

        logger.debug(f"Found Bluetooth adapters: {adapters}")
        return adapters

    async def get_adapter_links(self, hci: str) -> list[tuple[str, str]]:
        """``(mac, local role)`` of every link on a specific adapter."""
        exit_code, output = await run_command(["hcitool", "-i", hci, "con"])
        if exit_code != 0:
            logger.debug(f"hcitool con failed for {hci}")
            return []
        # Format: "< ACL 00:06:F7:XX:XX:XX handle 256 state 1 lm MASTER"
        return parse_hcitool_con(output)

    async def get_adapter_connections(self, hci: str) -> list[str]:
        """Get MAC addresses connected to a specific adapter."""
        return [mac for mac, _role in await self.get_adapter_links(hci)]

    async def get_rssi(self, hci: str, address: str) -> int | None:
        """Get RSSI for a device on a specific adapter."""
        exit_code, output = await run_command(["hcitool", "-i", hci, "rssi", address])
        if exit_code != 0:
            logger.debug(f"hcitool rssi failed for {address} on {hci}")
            return None

        # Parse RSSI value
        # Format: "RSSI return value: -45"
        match = re.search(r"RSSI return value:\s*(-?\d+)", output)
        if match:
            return int(match.group(1))

        return None

    async def monitor(self) -> None:
        """Monitor all Bluetooth-connected devices."""
        self.monitor_count += 1
        logger.debug(f"Bluetooth monitor #{self.monitor_count}")

        with self.tracer.start_as_current_span("bluetooth_monitor_cycle") as span:
            span.set_attribute("monitor.count", self.monitor_count)

            identities = await self.get_adapter_identities()
            adapters = [identity.hci for identity in identities]
            span.set_attribute("adapters.count", len(adapters))
            for identity in identities:
                self._export_adapter(identity)
            self._forget_adapters(set(adapters))

            # Track currently seen devices this cycle
            currently_seen: set[tuple[str, str]] = set()

            for hci in adapters:
                links = await self.get_adapter_links(hci)
                connections = [mac for mac, _role in links]
                roles = dict(links)

                # Update adapter connection count
                bluetooth_adapter_connections.labels(hci_adapter=hci).set(len(connections))
                logger.debug(f"{hci}: {len(connections)} devices connected")

                for bt_address in connections:
                    serial = normalize_serial(bt_address)
                    currently_seen.add((serial, hci))
                    ts = time.time()

                    # Get RSSI (hcitool needs raw colon-format address)
                    rssi = await self.get_rssi(hci, bt_address)

                    # Update metrics (normalized serial matches controller manager)
                    bluetooth_device_connected.labels(serial=serial, hci_adapter=hci).set(1)
                    bluetooth_device_last_seen.labels(serial=serial, hci_adapter=hci).set(ts)
                    self._export_role(serial, hci, roles[bt_address])

                    if rssi is not None:
                        bluetooth_device_rssi_dbm.labels(serial=serial, hci_adapter=hci).set(rssi)
                        logger.debug(f"  {serial} on {hci}: RSSI={rssi} dBm")
                    else:
                        logger.debug(f"  {serial} on {hci}: RSSI unavailable")

                    # Track for disconnect detection
                    self._known_devices[(serial, hci)] = ts

            # Check for disconnected devices
            for (serial, hci), _last_seen in self._known_devices.items():
                if (serial, hci) not in currently_seen:
                    # Device disconnected
                    bluetooth_device_connected.labels(serial=serial, hci_adapter=hci).set(0)
                    # Drop the RSSI series so a link that moved adapters is not
                    # still counted with its old adapter's last value (#1468).
                    bluetooth_device_rssi_dbm.remove(serial, hci)
                    role = self._link_roles.pop((serial, hci), None)
                    if role is not None:
                        pairing_bluetooth_link_role_info.remove(serial, hci, role)
                    logger.debug(f"Device {serial} disconnected from {hci}")
                    # Keep last_seen timestamp as-is for staleness detection

            span.set_attribute("devices.total", len(currently_seen))

    async def run_loop(self) -> None:
        """Bluetooth monitoring loop.

        Re-evaluates monitor interval from flagd each iteration for runtime tunability.
        """
        import asyncio

        logger.info(f"Starting Bluetooth monitor loop (interval: {get_bt_monitor_interval()}s)")
        while True:
            try:
                await self.monitor()
            except Exception as e:
                from .config import DEBUG

                logger.error(f"Error during Bluetooth monitor: {e}", exc_info=DEBUG)
            await asyncio.sleep(get_bt_monitor_interval())
