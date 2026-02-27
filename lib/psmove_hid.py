"""
Pure Python PS Move HID Protocol Parser

Defines constants and parsers for the PS Move controller HID protocol,
replacing the need for psmoveapi C library for basic controller I/O.

The PS Move uses standard HID over Bluetooth with:
- 49-byte input reports (buttons, trigger, accelerometer, gyroscope, battery)
- 49-byte output reports (LED RGB, rumble)

References:
- https://github.com/nitsch/movern/wiki/Input-report
- https://github.com/thp/psmoveapi
"""

import struct
from enum import IntEnum, IntFlag

# PS Move USB vendor/product IDs
VENDOR_ID = 0x054C  # Sony
PRODUCT_ID_ZCM1 = 0x03D5  # PS Move (PS3 era, ZCM1)
PRODUCT_ID_ZCM2 = 0x042F  # PS Move (PS4 era, ZCM2)
PRODUCT_ID_ZCM2E = 0x0C5E  # PS Move (PS4 era, ZCM2E variant)
ALL_PRODUCT_IDS = (PRODUCT_ID_ZCM1, PRODUCT_ID_ZCM2, PRODUCT_ID_ZCM2E)

# HID report sizes
INPUT_REPORT_SIZE = 49
OUTPUT_REPORT_SIZE_ZCM1 = 49  # BT output report 0x02 (49 bytes)
OUTPUT_REPORT_SIZE_ZCM2 = 9  # Output report 0x06 (9 bytes, same as psmoveapi)
OUTPUT_REPORT_SIZE = OUTPUT_REPORT_SIZE_ZCM1  # Default (backward compat)


class Button(IntFlag):
    """PS Move button bitmask values.

    Buttons are encoded as a 3-byte little-endian bitmask in the input report
    (bytes 1-3), extended with byte 4 for the PS and Move buttons.
    """

    TRIANGLE = 0x0010
    CIRCLE = 0x0020
    CROSS = 0x0040
    SQUARE = 0x0080
    SELECT = 0x0100
    START = 0x0800
    PS = 0x010000
    MOVE = 0x080000
    T = 0x100000  # Trigger (digital)


class Battery(IntEnum):
    """PS Move battery level constants."""

    MIN = 0x00
    P20 = 0x01
    P40 = 0x02
    P60 = 0x03
    P80 = 0x04
    MAX = 0x05
    CHARGING = 0xEE
    CHARGING_DONE = 0xEF


# Battery value to percentage mapping
_BATTERY_PERCENT = {
    Battery.MIN: 0,
    Battery.P20: 20,
    Battery.P40: 40,
    Battery.P60: 60,
    Battery.P80: 80,
    Battery.MAX: 100,
    Battery.CHARGING: 100,
    Battery.CHARGING_DONE: 100,
}


def battery_to_percent(value: int) -> int:
    """Convert battery raw value to percentage (0-100)."""
    return _BATTERY_PERCENT.get(value, 50)


class Frame(IntEnum):
    """PS Move accelerometer/gyroscope frame selection.

    Each input report contains two frames of sensor data.
    """

    FIRST = 0
    SECOND = 1


def parse_input_report(data: bytes) -> dict:
    """Parse a PS Move HID input report into structured data.

    Args:
        data: Raw HID input report bytes (49 bytes).
              May include a leading report ID byte (0x01) which is stripped.

    Returns:
        Dict with parsed controller state:
        - buttons: int (Button bitmask)
        - trigger: tuple[int, int] (frame1, frame2 analog values 0-255)
        - accel: tuple[tuple[int,int,int], tuple[int,int,int]]
            Two frames of (ax, ay, az) in raw signed 16-bit units
        - gyro: tuple[tuple[int,int,int], tuple[int,int,int]]
            Two frames of (gx, gy, gz) in raw signed 16-bit units
        - battery: int (raw battery value, use battery_to_percent() to convert)
        - temperature: int (raw 12-bit temperature sensor value)
        - sequence: int (report sequence number)

    Raises:
        ValueError: If data is too short to parse.
    """
    # Strip leading report ID byte (0x01) if present
    if len(data) > INPUT_REPORT_SIZE and data[0] == 0x01:
        data = data[1:]

    if len(data) < INPUT_REPORT_SIZE:
        raise ValueError(f"Input report too short: {len(data)} bytes (need {INPUT_REPORT_SIZE})")

    # Buttons: bytes 1-3 as little-endian, plus byte 4 shifted up
    # Byte 0 is typically 0x00 or sequence
    # Bytes 1-2: main buttons (little-endian 16-bit)
    # Byte 3: extended buttons
    btn_lo = data[1] | (data[2] << 8)
    btn_hi = data[3]
    buttons = btn_lo | (btn_hi << 16)

    # Sequence number: byte 4
    sequence = data[4]

    # Trigger analog: byte 6 (frame 1), byte 7 (frame 2)
    trigger1 = data[6]
    trigger2 = data[7]

    # Battery: byte 12
    battery = data[12]

    # Accelerometer frame 1: bytes 13-18 (3x uint16 little-endian)
    ax1, ay1, az1 = struct.unpack_from("<HHH", data, 13)
    # Accelerometer frame 2: bytes 19-24
    ax2, ay2, az2 = struct.unpack_from("<HHH", data, 19)

    # Convert to signed: raw values are offset by 0x8000
    ax1 -= 0x8000
    ay1 -= 0x8000
    az1 -= 0x8000
    ax2 -= 0x8000
    ay2 -= 0x8000
    az2 -= 0x8000

    # Gyroscope frame 1: bytes 25-30 (3x uint16 little-endian)
    gx1, gy1, gz1 = struct.unpack_from("<HHH", data, 25)
    # Gyroscope frame 2: bytes 31-36
    gx2, gy2, gz2 = struct.unpack_from("<HHH", data, 31)

    # Convert to signed
    gx1 -= 0x8000
    gy1 -= 0x8000
    gz1 -= 0x8000
    gx2 -= 0x8000
    gy2 -= 0x8000
    gz2 -= 0x8000

    # Temperature: bytes 37-38 (12-bit value)
    temp_raw = struct.unpack_from("<H", data, 37)[0]
    temperature = temp_raw & 0x0FFF

    return {
        "buttons": buttons,
        "trigger": (trigger1, trigger2),
        "accel": ((ax1, ay1, az1), (ax2, ay2, az2)),
        "gyro": ((gx1, gy1, gz1), (gx2, gy2, gz2)),
        "battery": battery,
        "temperature": temperature,
        "sequence": sequence,
    }


def _clamp_byte(value: int) -> int:
    """Clamp an integer to the 0-255 range."""
    return max(0, min(255, value))


def build_output_report(r: int = 0, g: int = 0, b: int = 0, rumble: int = 0) -> bytes:
    """Build a ZCM1 (PS3-era) PS Move HID output report for LED and rumble control.

    Uses BT output report 0x02 (49 bytes), which is the Bluetooth-specific
    output report supported by ZCM1 controllers.

    Args:
        r: Red LED intensity (0-255)
        g: Green LED intensity (0-255)
        b: Blue LED intensity (0-255)
        rumble: Rumble motor intensity (0-255)

    Returns:
        49-byte output report with report ID 0x02.
    """
    r = _clamp_byte(r)
    g = _clamp_byte(g)
    b = _clamp_byte(b)
    rumble = _clamp_byte(rumble)

    report = bytearray(OUTPUT_REPORT_SIZE_ZCM1)
    report[0] = 0x02  # BT output report type for LED/rumble (ZCM1)
    # Byte 1: 0x00 (reserved)
    report[2] = r
    report[3] = g
    report[4] = b
    # Byte 5: 0x00 (reserved)
    report[6] = rumble
    # Bytes 7-48: 0x00 (padding)
    return bytes(report)


def build_output_report_zcm2(r: int = 0, g: int = 0, b: int = 0, rumble: int = 0) -> bytes:
    """Build a ZCM2 (PS4-era) PS Move HID output report for LED and rumble control.

    Uses output report 0x06 (9 bytes), which is the SetLEDs report supported
    by both ZCM1 and ZCM2 controllers. The ZCM2 does not support the
    BT-specific report 0x02 used by ZCM1.

    The byte layout matches psmoveapi's PSMove_Data_LEDs struct:
        [0x06, 0x00, R, G, B, 0x00, rumble, 0x00, 0x00]

    Args:
        r: Red LED intensity (0-255)
        g: Green LED intensity (0-255)
        b: Blue LED intensity (0-255)
        rumble: Rumble motor intensity (0-255)

    Returns:
        9-byte output report with report ID 0x06.
    """
    r = _clamp_byte(r)
    g = _clamp_byte(g)
    b = _clamp_byte(b)
    rumble = _clamp_byte(rumble)

    report = bytearray(OUTPUT_REPORT_SIZE_ZCM2)
    report[0] = 0x06  # SetLEDs report type (works for both ZCM1 and ZCM2)
    # Byte 1: 0x00 (reserved)
    report[2] = r
    report[3] = g
    report[4] = b
    # Byte 5: 0x00 (rumble2, reserved)
    report[6] = rumble
    # Bytes 7-8: 0x00 (padding)
    return bytes(report)
