"""
Pytest fixtures for pairing daemon tests.

Mocks hidraw module and provides test utilities.
"""

import os
import sys
from types import ModuleType
from unittest.mock import MagicMock

# Disable OTEL metrics before importing modules that use them
from lib.otel_metrics import disable_metrics_for_tests

disable_metrics_for_tests()

# Disable OTEL tracing
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["OTEL_TRACES_EXPORTER"] = "none"

# Mock hidraw module before it gets imported (same pattern as test_hidapi_adapter.py)
_mock_hid = ModuleType("hidraw")
_mock_hid.enumerate = MagicMock(return_value=[])
_mock_hid.device = MagicMock
sys.modules.setdefault("hidraw", _mock_hid)

import pytest


class MockCommandRunner:
    """Mock for run_command() that returns predefined outputs.

    Usage:
        runner = MockCommandRunner()
        runner.add_response(["lsusb"], (0, "Bus 001 Device 003: ID 054c:03d5 Sony Corp."))

        with patch("psmove_pairing.usb_pairing.run_command", runner):
            result = await check_usb_controllers()
    """

    def __init__(self):
        self.responses: dict[tuple[str, ...], tuple[int, str]] = {}
        self.default_response: tuple[int, str] = (0, "")
        self.calls: list[list[str]] = []

    def add_response(self, cmd: list[str], response: tuple[int, str]) -> None:
        """Add a response for a specific command."""
        self.responses[tuple(cmd)] = response

    def add_prefix_response(self, cmd_prefix: list[str], response: tuple[int, str]) -> None:
        """Add a response that matches commands starting with the given prefix."""
        key = ("__PREFIX__", *cmd_prefix)
        self.responses[key] = response

    async def __call__(self, cmd: list[str], capture_stderr: bool = True, **kwargs) -> tuple[int, str]:
        """Return mocked response for command."""
        self.calls.append(cmd)

        # Exact match
        key = tuple(cmd)
        if key in self.responses:
            return self.responses[key]

        # Prefix match
        for resp_key, response in self.responses.items():
            if resp_key and resp_key[0] == "__PREFIX__":
                prefix = resp_key[1:]
                if tuple(cmd[: len(prefix)]) == prefix:
                    return response

        return self.default_response


@pytest.fixture
def mock_runner():
    """Provide a MockCommandRunner for tests."""
    return MockCommandRunner()


@pytest.fixture
def mock_tracer():
    """Provide a mock OpenTelemetry tracer."""
    tracer = MagicMock()
    span = MagicMock()
    span.__enter__ = MagicMock(return_value=span)
    span.__exit__ = MagicMock(return_value=False)
    tracer.start_as_current_span.return_value = span
    return tracer


# Sample command outputs for testing
SAMPLE_LSUSB_NO_PSMOVE = """\
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
"""

SAMPLE_LSUSB_WITH_PSMOVE = """\
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 001 Device 003: ID 054c:03d5 Sony Corp. Motion Controller
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
"""

SAMPLE_HCICONFIG = """\
hci0:   Type: Primary  Bus: USB
        BD Address: DC:A6:32:AA:BB:CC  ACL MTU: 1021:8  SCO MTU: 64:1
        UP RUNNING PSCAN ISCAN
        RX bytes:123456 acl:7890 sco:0 events:1234 errors:0
        TX bytes:123456 acl:7890 sco:0 commands:567 errors:0

hci1:   Type: Primary  Bus: USB
        BD Address: 00:1A:7D:DD:EE:FF  ACL MTU: 310:10  SCO MTU: 64:8
        UP RUNNING PSCAN ISCAN
        RX bytes:654321 acl:8901 sco:0 events:2345 errors:0
        TX bytes:654321 acl:8901 sco:0 commands:678 errors:0
"""

SAMPLE_HCITOOL_CON = """\
Connections:
        < ACL 00:06:F7:AA:BB:CC handle 256 state 1 lm MASTER
        < ACL 00:06:F7:DD:EE:FF handle 257 state 1 lm MASTER
"""

SAMPLE_HCITOOL_CON_EMPTY = """\
Connections:
"""

SAMPLE_HCITOOL_RSSI = """\
RSSI return value: -45
"""


# HID test constants shared across test_pairing_backend.py and test_usb_pairing.py
FAKE_PATH_1 = b"/dev/hidraw3"
FAKE_PATH_2 = b"/dev/hidraw4"

# Feature report 0x04 for controller AA:BB:CC:DD:EE:FF with host 11:22:33:44:55:66
# Controller MAC LSB-first: FF EE DD CC BB AA
# Host MAC LSB-first: 66 55 44 33 22 11
SAMPLE_FEATURE_REPORT = bytes(
    [0x04, 0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA, 0x00, 0x00, 0x00, 0x66, 0x55, 0x44, 0x33, 0x22, 0x11]
)

# Zero host (unpaired)
SAMPLE_FEATURE_REPORT_UNPAIRED = bytes(
    [0x04, 0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
)


def make_dev_info(
    path: bytes = FAKE_PATH_1,
    vendor_id: int = 0x054C,
    product_id: int = 0x03D5,
    interface_number: int = 0,
) -> dict:
    """Create a fake hid.enumerate() device info dict."""
    return {
        "path": path,
        "vendor_id": vendor_id,
        "product_id": product_id,
        "interface_number": interface_number,
        "serial_number": "",
    }


@pytest.fixture
def sample_lsusb_no_psmove():
    return SAMPLE_LSUSB_NO_PSMOVE


@pytest.fixture
def sample_lsusb_with_psmove():
    return SAMPLE_LSUSB_WITH_PSMOVE


@pytest.fixture
def sample_hciconfig():
    return SAMPLE_HCICONFIG


@pytest.fixture
def sample_hcitool_con():
    return SAMPLE_HCITOOL_CON


@pytest.fixture
def sample_hcitool_rssi():
    return SAMPLE_HCITOOL_RSSI
