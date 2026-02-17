"""
Pure Python Bluetooth RSSI reader via HCI sockets.

Reads signal strength (RSSI) for connected Bluetooth devices using
raw HCI sockets, replacing the need for the deprecated `hcitool rssi` binary.

Requires:
- Linux with Bluetooth support
- NET_ADMIN capability (or root) for raw HCI socket access
- AF_BLUETOOTH socket support in the kernel

Usage:
    reader = HciRssiReader()
    rssi = reader.get_rssi("AA:BB:CC:DD:EE:FF")
    if rssi is not None:
        print(f"RSSI: {rssi} dBm")
"""

import contextlib
import logging
import socket
import struct

logger = logging.getLogger(__name__)

# Bluetooth socket constants
AF_BLUETOOTH = 31
BTPROTO_HCI = 1

# HCI ioctl commands
HCIGETDEVLIST = 0x800448D2  # Not needed for single adapter
HCIGETCONNINFO = 0x800448D2  # Get connection info by address

# HCI command opcodes
HCI_OP_READ_RSSI = 0x1405  # OGF 0x05 (Status), OCF 0x05

# HCI event codes
HCI_EVENT_PKT = 0x04
EVT_CMD_COMPLETE = 0x0E


def _bdaddr_to_bytes(address: str) -> bytes:
    """Convert 'AA:BB:CC:DD:EE:FF' to 6-byte little-endian address."""
    parts = address.split(":")
    if len(parts) != 6:
        raise ValueError(f"Invalid Bluetooth address: {address}")
    return bytes(int(p, 16) for p in reversed(parts))


class HciRssiReader:
    """Read RSSI values for connected Bluetooth devices via HCI.

    Opens a raw HCI socket to send Read_RSSI HCI commands.
    Each call to get_rssi() sends a command and reads the response.
    """

    def __init__(self, hci_dev: int = 0):
        """Initialize HCI RSSI reader.

        Args:
            hci_dev: HCI device index (default 0 for hci0).
        """
        self._hci_dev = hci_dev
        self._sock: socket.socket | None = None

    def _ensure_socket(self) -> socket.socket:
        """Open or return the cached HCI socket."""
        if self._sock is not None:
            return self._sock

        try:
            sock = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
            # Bind to the HCI device
            sock.bind((self._hci_dev,))
            # Set a timeout for reads
            sock.settimeout(1.0)

            # Set HCI filter to receive command complete events
            # struct hci_filter { uint32_t type_mask; uint32_t event_mask[2]; uint16_t opcode; }
            # We want: type_mask = (1 << HCI_EVENT_PKT), event_mask bit for EVT_CMD_COMPLETE
            type_mask = 1 << HCI_EVENT_PKT
            event_mask_lo = 1 << EVT_CMD_COMPLETE
            event_mask_hi = 0
            opcode = 0  # Accept any opcode
            hci_filter = struct.pack("<IIIh", type_mask, event_mask_lo, event_mask_hi, opcode)
            sock.setsockopt(0, 2, hci_filter)  # SOL_HCI, HCI_FILTER

            self._sock = sock
            return sock
        except OSError as e:
            logger.debug(f"Failed to open HCI socket: {e}")
            raise

    def _get_connection_handle(self, address: str) -> int | None:
        """Get the ACL connection handle for a Bluetooth address.

        Reads /sys/kernel/debug/bluetooth/hci0/conn_info or uses
        hci_get_route + connect info ioctl. Falls back to scanning
        the HCI connection list.

        Args:
            address: Bluetooth address in AA:BB:CC:DD:EE:FF format.

        Returns:
            Connection handle (uint16) or None if not connected.
        """
        # Read connection info from /sys/kernel/debug/bluetooth
        # This is the simplest approach and doesn't require special ioctls
        try:
            with open(f"/sys/kernel/debug/bluetooth/hci{self._hci_dev}/conn_info") as f:
                for line in f:
                    # Format: "< ACL AA:BB:CC:DD:EE:FF handle N state X ..."
                    line = line.strip()
                    if address.upper() in line.upper():
                        parts = line.split()
                        for i, part in enumerate(parts):
                            if part == "handle" and i + 1 < len(parts):
                                return int(parts[i + 1])
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug(f"Error reading conn_info: {e}")

        # Fallback: use HCI_Get_Connection_Info ioctl
        try:
            sock = self._ensure_socket()
            addr_bytes = _bdaddr_to_bytes(address)

            # struct hci_conn_info_req { bdaddr_t bdaddr; uint8_t type; }
            # followed by: struct hci_conn_info { uint16_t handle; ... }
            # type = 1 (ACL)
            conn_info_req = addr_bytes + struct.pack("<B", 1)
            # Pad to expected size (the kernel writes back into this buffer)
            conn_info_req += b"\x00" * 32

            import fcntl

            # HCIGETCONNINFO = _IOR('H', 213, int) - platform-specific
            # On most Linux: 0x800448D5
            result = fcntl.ioctl(sock.fileno(), 0x800448D5, conn_info_req)
            # The handle is at offset 7 (after 6-byte bdaddr + 1-byte type)
            return struct.unpack_from("<H", result, 7)[0]

        except Exception as e:
            logger.debug(f"Failed to get connection handle for {address}: {e}")
            return None

    def get_rssi(self, address: str) -> int | None:
        """Read RSSI for a connected Bluetooth device.

        Args:
            address: Bluetooth address in AA:BB:CC:DD:EE:FF format.

        Returns:
            RSSI value in dBm (typically -100 to 20), or None if unavailable.
        """
        try:
            handle = self._get_connection_handle(address)
            if handle is None:
                return None

            sock = self._ensure_socket()

            # Send HCI Read_RSSI command
            # HCI command packet: type(1) + opcode(2) + param_len(1) + handle(2)
            cmd = struct.pack("<BHbH", 0x01, HCI_OP_READ_RSSI, 2, handle)
            sock.send(cmd)

            # Read response
            data = sock.recv(256)
            if len(data) < 7:
                return None

            # Parse HCI event: type(1) + event(1) + plen(1) + ncmd(1) + opcode(2) + status(1) + handle(2) + rssi(1)
            if data[0] != HCI_EVENT_PKT or data[1] != EVT_CMD_COMPLETE:
                return None

            # Check opcode matches
            resp_opcode = struct.unpack_from("<H", data, 4)[0]
            if resp_opcode != HCI_OP_READ_RSSI:
                return None

            status = data[6]
            if status != 0:
                logger.debug(f"Read RSSI failed for {address}: status={status}")
                return None

            # RSSI is a signed byte at offset 9
            return struct.unpack_from("<b", data, 9)[0]

        except TimeoutError:
            logger.debug(f"Timeout reading RSSI for {address}")
            return None
        except OSError as e:
            logger.debug(f"Error reading RSSI for {address}: {e}")
            # Reset socket on error
            self.close()
            return None

    def close(self) -> None:
        """Close the HCI socket."""
        if self._sock is not None:
            with contextlib.suppress(Exception):
                self._sock.close()
            self._sock = None
