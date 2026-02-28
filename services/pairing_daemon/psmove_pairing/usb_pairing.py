"""USB controller pairing for PS Move controllers.

Uses hidapi (hidraw backend) for direct HID feature report access,
replacing the psmoveapi C library and its GIL-blocking pair_custom().
"""

import logging
import time

import hidraw as hid
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from lib.psmove_hid import (
    ALL_PRODUCT_IDS,
    FEATURE_REPORT_GET_BTADDR,
    FEATURE_REPORT_GET_SIZE,
    VENDOR_ID,
    build_set_btaddr_report,
    parse_btaddr_report,
)

from .adapter_manager import AdapterManager, restart_systemd_unit
from .config import DEBUG, get_poll_interval
from .metrics import (
    pairing_adapter_device_count,
    pairing_adapter_selected_total,
    pairing_attempts_total,
    pairing_duration_seconds,
    pairing_failed_total,
    pairing_polls_total,
    pairing_success_total,
    pairing_usb_controllers,
)
from .utils import run_command

logger = logging.getLogger("psmove-pairing")

# Span attribute constants
_ATTR_CONTROLLER_SERIAL = "controller.serial"
_ATTR_ADAPTER_ADDRESS = "adapter.address"


class USBPairing:
    """Handles USB-connected PS Move controller pairing.

    Uses hidapi to read/write HID feature reports for BT address
    management, replacing the psmoveapi C library.
    """

    def __init__(self, tracer: trace.Tracer):
        self.tracer = tracer
        self.poll_count = 0
        self.adapter_manager = AdapterManager()
        # Track controllers verified this session to avoid re-processing every poll
        self._verified_serials: set[str] = set()

    def get_usb_controllers(self) -> list[tuple[bytes, str]]:
        """Get list of USB-connected controllers using hidapi.

        Enumerates HID devices matching PS Move vendor/product IDs,
        filters to USB-only (interface_number >= 0), and reads the
        controller's BT MAC from feature report 0x04.

        Returns:
            List of tuples (device_path, serial) for USB-connected controllers.
        """
        pairing_usb_controllers.set(0)

        usb_controllers: list[tuple[bytes, str]] = []
        devices = [d for pid in ALL_PRODUCT_IDS for d in hid.enumerate(VENDOR_ID, pid)]

        for dev_info in devices:
            # BT devices return interface_number == -1; USB returns >= 0
            if dev_info.get("interface_number", -1) < 0:
                continue

            path = dev_info["path"]
            try:
                device = hid.device()
                device.open_path(path)
                try:
                    report = device.get_feature_report(FEATURE_REPORT_GET_BTADDR, FEATURE_REPORT_GET_SIZE)
                    if isinstance(report, list):
                        report = bytes(report)
                    serial, _ = parse_btaddr_report(report)
                    usb_controllers.append((path, serial.upper()))
                    logger.debug(f"USB controller at {path!r}: {serial}")
                finally:
                    device.close()
            except Exception as e:
                logger.debug(f"Error reading controller at {path!r}: {e}")

        pairing_usb_controllers.set(len(usb_controllers))
        return usb_controllers

    async def pair_controller(self, device_path: bytes, serial: str, adapter_address: str) -> bool:
        """Pair a controller by writing the host BT address via HID feature report.

        Opens the device, reads the current host address, and writes the
        new adapter address if it differs. Direct HID call (~ms), no
        subprocess or GIL issues.

        Args:
            device_path: HID device path from enumerate()
            serial: Controller MAC address
            adapter_address: Target Bluetooth adapter address

        Returns:
            True if pairing succeeded.
        """
        logger.info(f"Pairing controller {serial} to adapter {adapter_address}...")

        with self.tracer.start_as_current_span("pair_controller") as span:
            span.set_attribute(_ATTR_CONTROLLER_SERIAL, serial)
            span.set_attribute(_ATTR_ADAPTER_ADDRESS, adapter_address)
            start_time = time.time()

            try:
                device = hid.device()
                device.open_path(device_path)
                try:
                    # Read current host address
                    report = device.get_feature_report(FEATURE_REPORT_GET_BTADDR, FEATURE_REPORT_GET_SIZE)
                    if isinstance(report, list):
                        report = bytes(report)
                    _, current_host = parse_btaddr_report(report)
                    span.set_attribute("pair.current_host", current_host)

                    if current_host.upper() == adapter_address.upper():
                        logger.info(f"Controller {serial} already paired to {adapter_address}")
                        span.set_attribute("pair.already_paired", True)
                    else:
                        logger.info(f"Writing host address {adapter_address} (was {current_host})")

                    # Write new host address (idempotent — safe to write even if same)
                    set_report = build_set_btaddr_report(adapter_address)
                    device.send_feature_report(set_report)
                finally:
                    device.close()

                duration = time.time() - start_time
                pairing_duration_seconds.observe(duration)
                span.set_attribute("pair.result", True)
                span.set_attribute("pair.duration_seconds", duration)
                logger.info(f"Pairing succeeded for {serial}")
                span.set_status(Status(StatusCode.OK))
                return True

            except Exception as e:
                duration = time.time() - start_time
                pairing_duration_seconds.observe(duration)
                logger.error(f"Exception during pairing: {e}", exc_info=DEBUG)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                return False

    async def restart_bluetooth_service(self) -> None:
        """Restart the host's BlueZ bluetooth service via D-Bus systemd interface.

        BlueZ only reads device files from /var/lib/bluetooth/ at startup,
        so a full service restart is needed after writing new pairing data.
        Adapter power-cycling alone is not sufficient.
        """
        import asyncio

        logger.info("Restarting bluetooth service via D-Bus...")
        try:
            await restart_systemd_unit("bluetooth.service")
            # Wait for BlueZ to fully restart and re-read device files
            await asyncio.sleep(3)
            logger.info("Bluetooth service restarted successfully")
        except Exception as e:
            logger.error(f"Failed to restart bluetooth service: {e}")

    async def bluez_trust_controller(self, serial: str) -> bool:
        """Trust a controller in BlueZ so it can connect later.

        Pre-registers the device as trusted so when the user unplugs USB and
        presses PS, BlueZ will accept the incoming connection. For ZCM2
        controllers, the bluetoothctl agent (registered at daemon startup)
        handles the PIN exchange at connection time.

        We only run ``trust`` here — ``pair`` cannot work because the
        controller is still on USB and not broadcasting over Bluetooth.

        Retries once after a short delay since BlueZ may still be restarting.
        """
        import asyncio

        logger.info(f"Running bluetoothctl trust for {serial}...")
        for attempt in range(2):
            exit_code, output = await run_command(["bluetoothctl", "trust", serial])
            if exit_code == 0:
                logger.info(f"bluetoothctl trust succeeded for {serial}")
                return True
            logger.warning(f"bluetoothctl trust attempt {attempt + 1} failed: {output}")
            if attempt == 0:
                await asyncio.sleep(2)

        # Trust failure is non-fatal — the agent can still handle PIN at connect time
        logger.warning(f"bluetoothctl trust failed for {serial}, continuing anyway")
        return False

    async def process_controller(self, device_path: bytes, serial: str) -> bool:
        """Process a single USB-connected controller with load-balanced adapter selection."""
        with self.tracer.start_as_current_span("process_controller") as span:
            span.set_attribute(_ATTR_CONTROLLER_SERIAL, serial)

            # Refresh adapter state and check if already paired
            await self.adapter_manager.refresh_adapters()

            # Skip controllers we've already verified this session
            if serial in self._verified_serials:
                span.set_attribute("skipped", True)
                span.set_attribute("skip_reason", "verified_this_session")
                return False

            already_in_bluez = not self.adapter_manager.check_if_not_paired(serial)
            if already_in_bluez:
                logger.info(f"Controller {serial} in BlueZ device list, verifying host address")
                span.set_attribute("verify_existing", True)
            else:
                logger.info(f"Found unpaired USB controller: {serial}")
            span.set_attribute("skipped", False)
            pairing_attempts_total.inc()

            # Select least-loaded adapter for load balancing
            adapter = self.adapter_manager.select_least_loaded_adapter()

            if not adapter:
                logger.error("No Bluetooth adapters available for pairing")
                pairing_failed_total.inc()
                span.set_status(Status(StatusCode.ERROR, "No adapters available"))
                return False

            logger.info(
                f"Load balancing: selected adapter {adapter.address} "
                f"({adapter.hci}) with {adapter.device_count} existing devices"
            )
            span.set_attribute(_ATTR_ADAPTER_ADDRESS, adapter.address)
            span.set_attribute("adapter.device_count", adapter.device_count)
            span.set_attribute("adapter.hci", adapter.hci)
            pairing_adapter_selected_total.labels(adapter=adapter.address).inc()
            pairing_adapter_device_count.labels(adapter=adapter.address).set(adapter.device_count)

            # Pair controller to selected adapter via HID feature report
            if not await self.pair_controller(device_path, serial, adapter.address):
                logger.error(f"PAIRING FAILED: Controller {serial} could not be paired")
                pairing_failed_total.inc()
                span.set_status(Status(StatusCode.ERROR, "Pairing failed"))
                return False

            # Restart BlueZ so it re-reads the new device files
            await self.restart_bluetooth_service()

            # Trust device in BlueZ (for ZCM2, agent handles PIN at connect time)
            await self.bluez_trust_controller(serial)

            # Mark as verified so we don't re-process every poll cycle
            self._verified_serials.add(serial)

            # Success message
            logger.info(
                f"PAIRING SUCCESS: Controller {serial} paired to adapter "
                f"{adapter.address} - unplug USB and press PS button"
            )

            pairing_success_total.inc()
            span.set_status(Status(StatusCode.OK))
            return True

    async def poll(self) -> None:
        """Perform one USB polling cycle."""
        self.poll_count += 1
        pairing_polls_total.inc()
        logger.debug(f"Poll #{self.poll_count}")

        with self.tracer.start_as_current_span("poll_cycle") as span:
            span.set_attribute("poll.count", self.poll_count)

            # Get USB controllers using hidapi
            controllers = self.get_usb_controllers()
            span.set_attribute("controllers.count", len(controllers))

            if not controllers:
                logger.debug("No USB controllers found")
                return

            # Process each controller
            for device_path, serial in controllers:
                await self.process_controller(device_path, serial)

    async def run_loop(self) -> None:
        """USB polling loop.

        Re-evaluates poll interval from flagd each iteration for runtime tunability.
        """
        import asyncio

        logger.info(f"Starting USB poll loop (interval: {get_poll_interval()}s)")

        while True:
            try:
                await self.poll()
            except Exception as e:
                logger.error(f"Error during USB poll: {e}", exc_info=DEBUG)
            await asyncio.sleep(get_poll_interval())
