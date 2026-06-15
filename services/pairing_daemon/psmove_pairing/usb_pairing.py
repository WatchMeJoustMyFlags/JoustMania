"""USB controller pairing for PS Move controllers.

Delegates hardware I/O to a PairingBackend selected by feature flags.
The backend is resolved once per poll cycle for coherence and hot-switchability.
"""

import logging
import time

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .adapter_manager import AdapterManager, restart_systemd_unit
from .config import DEBUG, get_adapter_routing_default, get_poll_interval
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
from .pairing_backend import HidapiBackend, PairingBackend, RustServiceBackend
from .utils import run_command

logger = logging.getLogger("psmove-pairing")


class PairingError(Exception):
    """Synthetic exception for pairing failure span events.

    Not raised for control flow — only passed to span.record_exception()
    so failure analysis can group and alert on pairing issues.
    """


# Span attribute constants
_ATTR_CONTROLLER_SERIAL = "controller.serial"
_ATTR_ADAPTER_ADDRESS = "adapter.address"

_KNOWN_ROUTING_VALUES = {"hidapi", "python", "rust"}


def _resolve_routing() -> str:
    """Resolve the routing name from feature flags.

    Reads the bluetooth_backend default variant and validates it.
    Returns a known routing name, falling back to "hidapi" for unknown values.
    """
    routing = get_adapter_routing_default()

    if routing in _KNOWN_ROUTING_VALUES:
        return routing

    logger.warning(f"Unknown adapter routing '{routing}', falling back to hidapi")
    return "hidapi"


def _create_backend(name: str) -> PairingBackend:
    """Create a backend instance by name."""
    if name == "rust":
        return RustServiceBackend()
    return HidapiBackend()


def _resolve_backend() -> tuple[PairingBackend, str]:
    """Resolve the pairing backend from feature flags.

    Reads the bluetooth_backend default variant:
    - "hidapi" -> HidapiBackend (default)
    - "rust" -> RustServiceBackend

    Returns:
        Tuple of (backend_instance, backend_name).
    """
    name = _resolve_routing()
    return _create_backend(name), name


class USBPairing:
    """Handles USB-connected PS Move controller pairing.

    Delegates hardware I/O to a PairingBackend selected by feature flags.
    The backend is resolved once per poll cycle — all operations within
    a cycle use the same backend instance for coherence.
    """

    def __init__(self, tracer: trace.Tracer):
        self.tracer = tracer
        self.poll_count = 0
        self.adapter_manager = AdapterManager()
        # Track controllers verified this session to avoid re-processing every poll
        self._verified_serials: set[str] = set()
        # Cached backend instance — reused across polls when routing is unchanged
        self._current_backend: PairingBackend | None = None
        self._current_backend_name: str | None = None

    def _get_backend(self) -> tuple[PairingBackend, str]:
        """Return the cached backend, or resolve a new one.

        Returns the backend cached from the most recent poll() call.
        If no backend has been cached yet, resolves a fresh one.
        """
        if self._current_backend is not None and self._current_backend_name is not None:
            return self._current_backend, self._current_backend_name
        return _resolve_backend()

    async def pair_controller(self, device_path: bytes, serial: str, adapter_address: str) -> bool:
        """Pair a controller by writing the host BT address.

        Wraps the backend's pair_controller with tracing and metrics.

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
                backend, backend_name = self._get_backend()
                span.set_attribute("pairing.backend", backend_name)

                result = await backend.pair_controller(device_path, serial, adapter_address)

                duration = time.time() - start_time
                pairing_duration_seconds.observe(duration)
                span.set_attribute("pair.result", result.success)
                span.set_attribute("pair.duration_seconds", duration)
                span.set_attribute("pair.already_paired", result.already_paired)
                if result.previous_host:
                    span.set_attribute("pair.current_host", result.previous_host)

                if result.success:
                    logger.info(f"Pairing succeeded for {serial}")
                    span.set_status(Status(StatusCode.OK))
                else:
                    logger.error(f"Pairing failed for {serial}")
                    span.record_exception(PairingError(f"Backend pairing failed for {serial}"))
                    span.set_status(Status(StatusCode.ERROR, "Backend pairing failed"))

                return result.success

            except Exception as e:
                duration = time.time() - start_time
                pairing_duration_seconds.observe(duration)
                logger.error(f"Exception during pairing: {e}", exc_info=DEBUG)
                span.record_exception(e)
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
                span.record_exception(PairingError("No Bluetooth adapters available"))
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

            # Pair controller to selected adapter via backend
            if not await self.pair_controller(device_path, serial, adapter.address):
                logger.error(f"PAIRING FAILED: Controller {serial} could not be paired")
                pairing_failed_total.inc()
                span.record_exception(PairingError(f"Controller {serial} pairing failed"))
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

            # Resolve routing name, only creating a new backend when it changes
            backend_name = _resolve_routing()
            if backend_name == self._current_backend_name and self._current_backend is not None:
                backend = self._current_backend
            else:
                if self._current_backend_name is None:
                    logger.info(f"Pairing backend initialized: {backend_name}")
                elif backend_name != self._current_backend_name:
                    logger.info(f"Pairing backend switched: {self._current_backend_name} -> {backend_name}")
                backend = _create_backend(backend_name)
                self._current_backend = backend
                self._current_backend_name = backend_name
            span.set_attribute("pairing.backend", backend_name)

            # Reset gauge before enumeration (prevents stale value if backend raises)
            pairing_usb_controllers.set(0)

            # Get USB controllers using resolved backend
            controllers = await backend.get_usb_controllers()
            pairing_usb_controllers.set(len(controllers))
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
