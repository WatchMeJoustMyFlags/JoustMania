"""Pairing backend abstraction for PS Move controllers.

Defines a PairingBackend protocol with implementations for different
hardware communication strategies. This enables canary releases by
routing pairing operations to either the Python (hidapi) backend or
a future Rust gRPC service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import hidraw as hid

from lib.psmove_hid import (
    ALL_PRODUCT_IDS,
    FEATURE_REPORT_GET_BTADDR,
    FEATURE_REPORT_GET_SIZE,
    VENDOR_ID,
    build_set_btaddr_report,
    parse_btaddr_report,
)

logger = logging.getLogger("psmove-pairing")


@dataclass
class PairingResult:
    """Result of a pairing operation, returned by PairingBackend.pair_controller().

    Carries success status plus metadata for observability (span attributes).
    """

    success: bool
    already_paired: bool = False
    previous_host: str = ""


@runtime_checkable
class PairingBackend(Protocol):
    """Protocol for pairing backend implementations.

    Each backend handles USB controller enumeration and BT address
    writing using a different I/O strategy. The USBPairing class
    delegates to whichever backend is selected by feature flags.
    """

    async def get_usb_controllers(self) -> list[tuple[bytes, str]]:
        """Enumerate USB-connected PS Move controllers.

        Returns:
            List of (device_path, serial) tuples for USB controllers.
            device_path is an opaque identifier used by pair_controller().
        """
        ...

    async def pair_controller(self, device_path: bytes, serial: str, adapter_address: str) -> PairingResult:
        """Write the host BT address to a controller.

        Args:
            device_path: Opaque device identifier from get_usb_controllers().
            serial: Controller MAC address.
            adapter_address: Target Bluetooth adapter address to write.

        Returns:
            PairingResult with success status and metadata.
        """
        ...


class HidapiBackend:
    """Pairing backend using hidapi for direct HID feature report access.

    Handles USB enumeration and BT address read/write via HID reports.
    No tracing or metrics — the caller (USBPairing) handles observability.
    """

    async def get_usb_controllers(self) -> list[tuple[bytes, str]]:
        """Enumerate USB-connected PS Move controllers via hidapi.

        Filters to USB-only devices (interface_number >= 0) and reads
        each controller's BT MAC from feature report 0x04.

        Note: HID calls are synchronous blocking I/O. Wrapping in
        asyncio.to_thread() is deferred until profiling shows it matters.
        """
        usb_controllers: list[tuple[bytes, str]] = []
        devices = [d for pid in ALL_PRODUCT_IDS for d in hid.enumerate(VENDOR_ID, pid)]

        for dev_info in devices:
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
                logger.warning(f"Error reading controller at {path!r}: {e}")

        return usb_controllers

    async def pair_controller(self, device_path: bytes, serial: str, adapter_address: str) -> PairingResult:
        """Write the host BT address via HID feature report.

        Opens the device, reads the current host address, and writes
        the new adapter address. Direct HID call (~ms), no subprocess.
        """
        try:
            device = hid.device()
            device.open_path(device_path)
            try:
                report = device.get_feature_report(FEATURE_REPORT_GET_BTADDR, FEATURE_REPORT_GET_SIZE)
                if isinstance(report, list):
                    report = bytes(report)
                _, current_host = parse_btaddr_report(report)

                already_paired = current_host.upper() == adapter_address.upper()
                if already_paired:
                    logger.info(f"Controller {serial} already paired to {adapter_address}")
                else:
                    logger.info(f"Writing host address {adapter_address} (was {current_host})")

                set_report = build_set_btaddr_report(adapter_address)
                device.send_feature_report(set_report)
            finally:
                device.close()

            return PairingResult(success=True, already_paired=already_paired, previous_host=current_host)

        except Exception as e:
            logger.warning(f"HID pairing error for {serial}: {e}")
            return PairingResult(success=False)


class RustServiceBackend:
    """Stub backend for a future Rust gRPC pairing service.

    Will eventually connect to a separate Rust pairing service via gRPC.
    All methods raise NotImplementedError until #612 adds the Rust service.
    """

    async def get_usb_controllers(self) -> list[tuple[bytes, str]]:
        raise NotImplementedError("Rust pairing service not yet implemented — see #612")

    async def pair_controller(self, device_path: bytes, serial: str, adapter_address: str) -> PairingResult:
        raise NotImplementedError("Rust pairing service not yet implemented — see #612")
