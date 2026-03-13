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
    """Pairing backend that delegates to the Rust HID gRPC service.

    Connects to the rust-hid service (port 50058) for USB controller
    enumeration and BT address writing. The Rust service handles HID
    device I/O directly, replacing the Python hidapi calls.
    """

    def __init__(self):
        import os

        from proto import psmove_hid_pb2_grpc

        from lib.grpc_utils import create_channel

        host = os.getenv("RUST_HID_HOST", "localhost")
        port = os.getenv("RUST_HID_PORT", "50058")
        self._channel = create_channel(f"{host}:{port}")
        self._stub = psmove_hid_pb2_grpc.PairingServiceStub(self._channel)

    async def get_usb_controllers(self) -> list[tuple[bytes, str]]:
        """Enumerate USB-connected PS Move controllers via Rust HID service."""
        from proto import psmove_hid_pb2

        response = await self._stub.GetUSBControllers(psmove_hid_pb2.GetUSBControllersRequest())
        return [(c.device_path.encode(), c.serial) for c in response.controllers]

    async def pair_controller(self, device_path: bytes, serial: str, adapter_address: str) -> PairingResult:
        """Write the host BT address via Rust HID service."""
        from proto import psmove_hid_pb2

        path_str = device_path.decode() if isinstance(device_path, bytes) else device_path
        response = await self._stub.PairController(
            psmove_hid_pb2.PairControllerRequest(
                device_path=path_str,
                serial=serial,
                adapter_address=adapter_address,
            )
        )
        return PairingResult(
            success=response.success,
            already_paired=response.already_paired,
            previous_host=response.previous_host,
        )
