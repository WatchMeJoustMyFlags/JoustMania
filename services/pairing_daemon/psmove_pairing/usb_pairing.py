"""USB controller pairing for PS Move controllers.

Uses the psmove Python bindings (like the original JoustMania) for reliable
adapter selection via pair_custom().
"""

import logging
import time

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

# Import config first to set up psmoveapi path
from .config import DEBUG, POLL_INTERVAL

# Now import psmove (path was set up by config)
try:
    import psmove
except ImportError as e:
    raise ImportError(
        "psmove module not found. Ensure PSMOVEAPI_BUILD_PATH is set correctly "
        "and psmoveapi is built with Python bindings."
    ) from e

import dbus

from .adapter_manager import AdapterManager
from .metrics import (
    calibration_duration_seconds,
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

    Uses the psmove Python bindings with pair_custom() for reliable
    adapter selection, matching the original JoustMania approach.
    """

    def __init__(self, tracer: trace.Tracer, psmove_path: str):
        self.tracer = tracer
        self.psmove_cli = psmove_path  # Keep for calibration
        self.poll_count = 0
        self.adapter_manager = AdapterManager()

    def get_usb_controllers_psmove(self) -> list[tuple[int, str]]:
        """Get list of USB-connected controllers using psmove library.

        Returns:
            List of tuples (index, serial) for USB-connected controllers
        """
        connected = psmove.count_connected()
        logger.debug(f"psmove.count_connected() = {connected}")
        pairing_usb_controllers.set(0)  # Will update below

        usb_controllers = []
        for i in range(connected):
            try:
                move = psmove.PSMove(i)
                if move.connection_type == psmove.Conn_USB:
                    serial = move.get_serial()
                    if serial:
                        usb_controllers.append((i, serial.upper()))
                        logger.debug(f"USB controller {i}: {serial}")
            except Exception as e:
                logger.debug(f"Error accessing controller {i}: {e}")

        pairing_usb_controllers.set(len(usb_controllers))
        return usb_controllers

    async def pair_controller_psmove(self, move_index: int, serial: str, adapter_address: str) -> bool:
        """Pair a controller using psmove Python library's pair_custom().

        For ZCM2 (PS4-era) controllers, pair_custom() blocks indefinitely in
        its internal PIN agent (which can't work in Docker because systemctl
        fails). Since the C code holds the GIL, threading timeouts don't work.

        We run pair_custom() in a subprocess with a timeout. The controller's
        BT address and BlueZ device files are written before the PIN agent
        starts, so killing the subprocess after timeout is safe. Our own
        bluetoothctl agent handles PIN after BlueZ is restarted.

        Args:
            move_index: Controller index from psmove.count_connected()
            serial: Controller MAC address
            adapter_address: Target Bluetooth adapter address

        Returns:
            True if pairing succeeded (or timed out after address was written)
        """
        import asyncio

        logger.info(f"Pairing controller {serial} to adapter {adapter_address}...")

        with self.tracer.start_as_current_span("pair_controller") as span:
            span.set_attribute(_ATTR_CONTROLLER_SERIAL, serial)
            span.set_attribute(_ATTR_ADAPTER_ADDRESS, adapter_address)
            start_time = time.time()

            try:
                move = psmove.PSMove(move_index)
                if move.connection_type != psmove.Conn_USB:
                    logger.warning(f"Controller {serial} not connected via USB")
                    span.set_status(Status(StatusCode.ERROR, "Not USB connected"))
                    return False
                # Release the PSMove handle before subprocess uses it
                del move

                # Run pair_custom in a subprocess to avoid GIL blocking
                script = (
                    "import psmove; "
                    f"m = psmove.PSMove({move_index}); "
                    f"r = m.pair_custom('{adapter_address}'); "
                    "print('OK' if r else 'FAIL')"
                )
                proc = await asyncio.create_subprocess_exec(
                    "python",
                    "-c",
                    script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
                    output = stdout.decode().strip()
                    result = output == "OK"
                except TimeoutError:
                    logger.info(
                        f"pair_custom() timed out for {serial} (ZCM2 PIN agent blocking) "
                        "- address written, will handle PIN via bluetoothctl agent"
                    )
                    proc.kill()
                    await proc.wait()
                    # Timeout is expected for ZCM2 — address was already written
                    result = True

                duration = time.time() - start_time
                pairing_duration_seconds.observe(duration)

                span.set_attribute("pair.result", result)
                span.set_attribute("pair.duration_seconds", duration)

                if result:
                    logger.info(f"pair_custom() succeeded for {serial}")
                    span.set_status(Status(StatusCode.OK))
                    return True
                logger.error(f"pair_custom() returned False for {serial}")
                span.set_status(Status(StatusCode.ERROR, "pair_custom failed"))
                return False

            except Exception as e:
                duration = time.time() - start_time
                pairing_duration_seconds.observe(duration)
                logger.error(f"Exception during pairing: {e}", exc_info=DEBUG)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                return False

    async def calibrate_controller(self, serial: str) -> bool:
        """Calibrate the controller using CLI tool."""
        logger.info("Calibrating controller...")

        with self.tracer.start_as_current_span("calibrate_controller") as span:
            span.set_attribute(_ATTR_CONTROLLER_SERIAL, serial)
            start_time = time.time()

            exit_code, output = await run_command([self.psmove_cli, "calibrate"])
            duration = time.time() - start_time
            calibration_duration_seconds.observe(duration)

            logger.debug(f"Calibrate output: {output}")
            span.set_attribute("calibrate.exit_code", exit_code)
            span.set_attribute("calibrate.duration_seconds", duration)

            if exit_code != 0:
                logger.warning(f"Calibration returned non-zero: {exit_code}")
                span.set_status(Status(StatusCode.ERROR, "Calibration returned non-zero"))
            else:
                span.set_status(Status(StatusCode.OK))

            return exit_code == 0

    async def restart_bluetooth_service(self) -> None:
        """Restart the host's BlueZ bluetooth service via D-Bus systemd interface.

        BlueZ only reads device files from /var/lib/bluetooth/ at startup,
        so a full service restart is needed after writing new pairing data.
        Adapter power-cycling alone is not sufficient.
        """
        import asyncio

        logger.info("Restarting bluetooth service via D-Bus...")
        try:
            bus = dbus.SystemBus()
            systemd = bus.get_object("org.freedesktop.systemd1", "/org/freedesktop/systemd1")
            manager = dbus.Interface(systemd, "org.freedesktop.systemd1.Manager")
            manager.RestartUnit("bluetooth.service", "replace")
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

    async def process_controller(self, move_index: int, serial: str) -> bool:
        """Process a single USB-connected controller with load-balanced adapter selection."""
        with self.tracer.start_as_current_span("process_controller") as span:
            span.set_attribute(_ATTR_CONTROLLER_SERIAL, serial)

            # Refresh adapter state and check if already paired
            self.adapter_manager.refresh_adapters()

            if not self.adapter_manager.check_if_not_paired(serial):
                logger.info(f"Controller {serial} already paired, ensuring trusted")
                span.set_attribute("skipped", True)
                span.set_attribute("skip_reason", "already_paired")
                # Still trust — controller may be paired but not yet trusted
                # (e.g. trust failed on a previous attempt due to BlueZ timing)
                await self.bluez_trust_controller(serial)
                return False

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

            # Pair controller to selected adapter using Python bindings
            if not await self.pair_controller_psmove(move_index, serial, adapter.address):
                logger.error(f"PAIRING FAILED: Controller {serial} could not be paired")
                pairing_failed_total.inc()
                span.set_status(Status(StatusCode.ERROR, "Pairing failed"))
                return False

            # Restart BlueZ so it re-reads the new device files
            await self.restart_bluetooth_service()

            # Trust device in BlueZ (for ZCM2, agent handles PIN at connect time)
            await self.bluez_trust_controller(serial)

            # Calibrate
            await self.calibrate_controller(serial)

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

            # Get USB controllers using psmove library
            controllers = self.get_usb_controllers_psmove()
            span.set_attribute("controllers.count", len(controllers))

            if not controllers:
                logger.debug("No USB controllers found")
                return

            # Process each controller
            for move_index, serial in controllers:
                await self.process_controller(move_index, serial)

    async def run_loop(self) -> None:
        """USB polling loop."""
        import asyncio

        logger.info(f"Starting USB poll loop (interval: {POLL_INTERVAL}s)")
        logger.info(f"Using psmove Python bindings (psmove.Conn_USB={psmove.Conn_USB})")

        while True:
            try:
                await self.poll()
            except Exception as e:
                logger.error(f"Error during USB poll: {e}", exc_info=DEBUG)
            await asyncio.sleep(POLL_INTERVAL)
