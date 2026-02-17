"""
Unit tests for lib/hci_rssi.py — Pure Python HCI RSSI reader.

Tests the helper functions and class structure. Actual HCI socket
operations require Bluetooth hardware and are not tested here.
"""

import sys
from pathlib import Path

import pytest

# Setup paths
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from lib.hci_rssi import _bdaddr_to_bytes


class TestBdaddrToBytes:
    """Test Bluetooth address string to bytes conversion."""

    def test_standard_address(self):
        result = _bdaddr_to_bytes("AA:BB:CC:DD:EE:FF")
        # Little-endian: reversed byte order
        assert result == bytes([0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA])

    def test_all_zeros(self):
        result = _bdaddr_to_bytes("00:00:00:00:00:00")
        assert result == bytes([0x00] * 6)

    def test_case_insensitive(self):
        upper = _bdaddr_to_bytes("AA:BB:CC:DD:EE:FF")
        lower = _bdaddr_to_bytes("aa:bb:cc:dd:ee:ff")
        assert upper == lower

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid Bluetooth address"):
            _bdaddr_to_bytes("AA:BB:CC")

    def test_invalid_no_colons_raises(self):
        with pytest.raises(ValueError, match="Invalid Bluetooth address"):
            _bdaddr_to_bytes("AABBCCDDEEFF")
