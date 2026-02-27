"""
Unit tests for lib/psmove_hid.py — Pure Python PS Move HID parser.

Tests parsing of raw HID input reports and building of output reports
using captured byte sequences. No hardware or mocking required.
"""

import struct
import sys
from pathlib import Path

# Setup paths
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from lib.psmove_hid import (
    INPUT_REPORT_SIZE,
    OUTPUT_REPORT_SIZE,
    OUTPUT_REPORT_SIZE_ZCM1,
    OUTPUT_REPORT_SIZE_ZCM2,
    Battery,
    Button,
    Frame,
    battery_to_percent,
    build_output_report,
    build_output_report_zcm2,
    parse_input_report,
)


def _build_test_report(
    buttons_lo=0,
    buttons_hi=0,
    sequence=0,
    trigger1=0,
    trigger2=0,
    battery=Battery.MAX,
    ax1=0x8000,
    ay1=0x8000,
    az1=0x8000,
    ax2=0x8000,
    ay2=0x8000,
    az2=0x8000,
    gx1=0x8000,
    gy1=0x8000,
    gz1=0x8000,
    gx2=0x8000,
    gy2=0x8000,
    gz2=0x8000,
    temperature=0,
) -> bytes:
    """Build a synthetic 49-byte HID input report for testing."""
    data = bytearray(INPUT_REPORT_SIZE)

    # Buttons: bytes 1-2 (little-endian 16-bit), byte 3 (extended)
    data[1] = buttons_lo & 0xFF
    data[2] = (buttons_lo >> 8) & 0xFF
    data[3] = buttons_hi & 0xFF

    # Sequence: byte 4
    data[4] = sequence

    # Trigger: byte 6 (frame1), byte 7 (frame2)
    data[6] = trigger1
    data[7] = trigger2

    # Battery: byte 12
    data[12] = battery

    # Accelerometer frame 1: bytes 13-18 (3x uint16 LE)
    struct.pack_into("<HHH", data, 13, ax1, ay1, az1)
    # Accelerometer frame 2: bytes 19-24
    struct.pack_into("<HHH", data, 19, ax2, ay2, az2)

    # Gyroscope frame 1: bytes 25-30
    struct.pack_into("<HHH", data, 25, gx1, gy1, gz1)
    # Gyroscope frame 2: bytes 31-36
    struct.pack_into("<HHH", data, 31, gx2, gy2, gz2)

    # Temperature: bytes 37-38
    struct.pack_into("<H", data, 37, temperature)

    return bytes(data)


class TestButtonParsing:
    """Test button bitmask parsing from input reports."""

    def test_no_buttons_pressed(self):
        data = _build_test_report()
        result = parse_input_report(data)
        assert result["buttons"] & Button.TRIANGLE == 0
        assert result["buttons"] & Button.CIRCLE == 0
        assert result["buttons"] & Button.CROSS == 0
        assert result["buttons"] & Button.SQUARE == 0
        assert result["buttons"] & Button.MOVE == 0
        assert result["buttons"] & Button.T == 0
        assert result["buttons"] & Button.PS == 0

    def test_triangle_button(self):
        data = _build_test_report(buttons_lo=Button.TRIANGLE)
        result = parse_input_report(data)
        assert result["buttons"] & Button.TRIANGLE

    def test_circle_button(self):
        data = _build_test_report(buttons_lo=Button.CIRCLE)
        result = parse_input_report(data)
        assert result["buttons"] & Button.CIRCLE

    def test_cross_button(self):
        data = _build_test_report(buttons_lo=Button.CROSS)
        result = parse_input_report(data)
        assert result["buttons"] & Button.CROSS

    def test_square_button(self):
        data = _build_test_report(buttons_lo=Button.SQUARE)
        result = parse_input_report(data)
        assert result["buttons"] & Button.SQUARE

    def test_select_button(self):
        data = _build_test_report(buttons_lo=Button.SELECT)
        result = parse_input_report(data)
        assert result["buttons"] & Button.SELECT

    def test_start_button(self):
        data = _build_test_report(buttons_lo=Button.START)
        result = parse_input_report(data)
        assert result["buttons"] & Button.START

    def test_ps_button(self):
        """PS button is in the extended byte (byte 3)."""
        data = _build_test_report(buttons_hi=(Button.PS >> 16))
        result = parse_input_report(data)
        assert result["buttons"] & Button.PS

    def test_move_button(self):
        """Move button is in the extended byte (byte 3)."""
        data = _build_test_report(buttons_hi=(Button.MOVE >> 16))
        result = parse_input_report(data)
        assert result["buttons"] & Button.MOVE

    def test_trigger_digital(self):
        """T (trigger digital) button in extended byte."""
        data = _build_test_report(buttons_hi=(Button.T >> 16))
        result = parse_input_report(data)
        assert result["buttons"] & Button.T

    def test_multiple_buttons(self):
        """Multiple buttons pressed simultaneously."""
        lo = Button.TRIANGLE | Button.CROSS
        data = _build_test_report(buttons_lo=lo)
        result = parse_input_report(data)
        assert result["buttons"] & Button.TRIANGLE
        assert result["buttons"] & Button.CROSS
        assert not (result["buttons"] & Button.CIRCLE)


class TestTriggerParsing:
    """Test analog trigger value parsing."""

    def test_trigger_zero(self):
        data = _build_test_report(trigger1=0, trigger2=0)
        result = parse_input_report(data)
        assert result["trigger"] == (0, 0)

    def test_trigger_full(self):
        data = _build_test_report(trigger1=255, trigger2=255)
        result = parse_input_report(data)
        assert result["trigger"] == (255, 255)

    def test_trigger_frames_independent(self):
        data = _build_test_report(trigger1=100, trigger2=200)
        result = parse_input_report(data)
        assert result["trigger"] == (100, 200)


class TestAccelerometerParsing:
    """Test accelerometer frame parsing."""

    def test_at_rest_zero(self):
        """0x8000 offset should produce 0 after subtraction."""
        data = _build_test_report(ax1=0x8000, ay1=0x8000, az1=0x8000)
        result = parse_input_report(data)
        assert result["accel"][0] == (0, 0, 0)

    def test_positive_acceleration(self):
        """Values above 0x8000 produce positive readings."""
        data = _build_test_report(ax1=0x9000, ay1=0x8000, az1=0x8000)
        result = parse_input_report(data)
        assert result["accel"][0][0] == 0x1000  # 4096
        assert result["accel"][0][1] == 0
        assert result["accel"][0][2] == 0

    def test_negative_acceleration(self):
        """Values below 0x8000 produce negative readings."""
        data = _build_test_report(ax1=0x7000, ay1=0x8000, az1=0x8000)
        result = parse_input_report(data)
        assert result["accel"][0][0] == -0x1000  # -4096

    def test_two_frames_independent(self):
        """Frame 1 and Frame 2 accelerometer data are independent."""
        data = _build_test_report(ax1=0x9000, ay1=0x8000, az1=0x8000, ax2=0x7000, ay2=0x8000, az2=0x8000)
        result = parse_input_report(data)
        assert result["accel"][0][0] == 0x1000  # Frame 1: +4096
        assert result["accel"][1][0] == -0x1000  # Frame 2: -4096

    def test_gravity_on_z_axis(self):
        """Simulate ~1g on z-axis (raw ~4096 units = 1g)."""
        # 0x8000 + 4096 = 0x9000
        data = _build_test_report(az1=0x9000)
        result = parse_input_report(data)
        assert result["accel"][0][2] == 4096


class TestGyroscopeParsing:
    """Test gyroscope frame parsing."""

    def test_stationary(self):
        """0x8000 offset should produce 0."""
        data = _build_test_report()
        result = parse_input_report(data)
        assert result["gyro"][0] == (0, 0, 0)
        assert result["gyro"][1] == (0, 0, 0)

    def test_rotation(self):
        data = _build_test_report(gx1=0x8100, gy1=0x7F00, gz1=0x8000)
        result = parse_input_report(data)
        assert result["gyro"][0][0] == 0x100  # 256
        assert result["gyro"][0][1] == -0x100  # -256
        assert result["gyro"][0][2] == 0


class TestBatteryParsing:
    """Test battery value parsing."""

    def test_battery_raw_value(self):
        data = _build_test_report(battery=Battery.MAX)
        result = parse_input_report(data)
        assert result["battery"] == Battery.MAX

    def test_battery_charging(self):
        data = _build_test_report(battery=Battery.CHARGING)
        result = parse_input_report(data)
        assert result["battery"] == Battery.CHARGING

    def test_battery_min(self):
        data = _build_test_report(battery=Battery.MIN)
        result = parse_input_report(data)
        assert result["battery"] == Battery.MIN


class TestBatteryToPercent:
    """Test battery_to_percent conversion function."""

    def test_min_is_zero(self):
        assert battery_to_percent(Battery.MIN) == 0

    def test_20_percent(self):
        assert battery_to_percent(Battery.P20) == 20

    def test_40_percent(self):
        assert battery_to_percent(Battery.P40) == 40

    def test_60_percent(self):
        assert battery_to_percent(Battery.P60) == 60

    def test_80_percent(self):
        assert battery_to_percent(Battery.P80) == 80

    def test_max_is_100(self):
        assert battery_to_percent(Battery.MAX) == 100

    def test_charging_is_100(self):
        assert battery_to_percent(Battery.CHARGING) == 100

    def test_charging_done_is_100(self):
        assert battery_to_percent(Battery.CHARGING_DONE) == 100

    def test_unknown_value_is_50(self):
        assert battery_to_percent(0xFF) == 50


class TestTemperatureParsing:
    """Test temperature value parsing."""

    def test_temperature_12bit(self):
        data = _build_test_report(temperature=0x0ABC)
        result = parse_input_report(data)
        assert result["temperature"] == 0x0ABC

    def test_temperature_masked_to_12bits(self):
        """Only lower 12 bits should be used."""
        data = _build_test_report(temperature=0xFABC)
        result = parse_input_report(data)
        assert result["temperature"] == 0x0ABC  # Upper nibble masked


class TestSequenceParsing:
    """Test sequence number parsing."""

    def test_sequence_number(self):
        data = _build_test_report(sequence=42)
        result = parse_input_report(data)
        assert result["sequence"] == 42


class TestInputReportEdgeCases:
    """Test edge cases for input report parsing."""

    def test_report_too_short_raises(self):
        """Reports shorter than 49 bytes should raise ValueError."""
        import pytest

        with pytest.raises(ValueError, match="too short"):
            parse_input_report(b"\x00" * 10)

    def test_strips_leading_report_id(self):
        """Leading 0x01 report ID byte should be stripped."""
        inner = _build_test_report(trigger2=42)
        data = b"\x01" + inner
        result = parse_input_report(data)
        assert result["trigger"][1] == 42

    def test_exact_49_bytes(self):
        """Exactly 49 bytes should parse successfully."""
        data = _build_test_report()
        assert len(data) == 49
        result = parse_input_report(data)
        assert "buttons" in result


class TestOutputReport:
    """Test output report building."""

    def test_output_report_size(self):
        report = build_output_report()
        assert len(report) == OUTPUT_REPORT_SIZE

    def test_output_report_type_byte(self):
        report = build_output_report()
        assert report[0] == 0x02

    def test_rgb_values(self):
        report = build_output_report(r=255, g=128, b=64)
        assert report[2] == 255
        assert report[3] == 128
        assert report[4] == 64

    def test_rumble_value(self):
        report = build_output_report(rumble=200)
        assert report[6] == 200

    def test_all_zeros(self):
        report = build_output_report(r=0, g=0, b=0, rumble=0)
        assert report[2] == 0
        assert report[3] == 0
        assert report[4] == 0
        assert report[6] == 0

    def test_clamps_values(self):
        """Values outside 0-255 should be clamped."""
        report = build_output_report(r=300, g=-10, b=256, rumble=999)
        assert report[2] == 255  # r clamped
        assert report[3] == 0  # g clamped
        assert report[4] == 255  # b clamped
        assert report[6] == 255  # rumble clamped

    def test_padding_is_zeros(self):
        """Bytes after rumble should be zero."""
        report = build_output_report(r=255, g=255, b=255, rumble=255)
        for i in range(7, OUTPUT_REPORT_SIZE):
            assert report[i] == 0, f"Byte {i} should be 0, got {report[i]}"


class TestOutputReportZCM2:
    """Test ZCM2 (PS4-era) output report building."""

    def test_output_report_size(self):
        report = build_output_report_zcm2()
        assert len(report) == OUTPUT_REPORT_SIZE_ZCM2

    def test_output_report_type_byte(self):
        report = build_output_report_zcm2()
        assert report[0] == 0x06

    def test_rgb_values(self):
        report = build_output_report_zcm2(r=255, g=128, b=64)
        assert report[2] == 255
        assert report[3] == 128
        assert report[4] == 64

    def test_rumble_value(self):
        report = build_output_report_zcm2(rumble=200)
        assert report[6] == 200

    def test_all_zeros(self):
        report = build_output_report_zcm2(r=0, g=0, b=0, rumble=0)
        assert report[2] == 0
        assert report[3] == 0
        assert report[4] == 0
        assert report[6] == 0

    def test_clamps_values(self):
        """Values outside 0-255 should be clamped."""
        report = build_output_report_zcm2(r=300, g=-10, b=256, rumble=999)
        assert report[2] == 255  # r clamped
        assert report[3] == 0  # g clamped
        assert report[4] == 255  # b clamped
        assert report[6] == 255  # rumble clamped

    def test_padding_is_zeros(self):
        """Bytes after rumble should be zero."""
        report = build_output_report_zcm2(r=255, g=255, b=255, rumble=255)
        for i in range(7, OUTPUT_REPORT_SIZE_ZCM2):
            assert report[i] == 0, f"Byte {i} should be 0, got {report[i]}"

    def test_reserved_byte_1_is_zero(self):
        """Byte 1 (reserved) should always be zero."""
        report = build_output_report_zcm2(r=255, g=255, b=255, rumble=255)
        assert report[1] == 0

    def test_reserved_byte_5_is_zero(self):
        """Byte 5 (rumble2) should always be zero."""
        report = build_output_report_zcm2(r=255, g=255, b=255, rumble=255)
        assert report[5] == 0

    def test_same_rgb_layout_as_zcm1(self):
        """ZCM2 and ZCM1 share the same R/G/B/rumble byte positions."""
        zcm1 = build_output_report(r=100, g=150, b=200, rumble=50)
        zcm2 = build_output_report_zcm2(r=100, g=150, b=200, rumble=50)
        # Same byte positions for color and rumble data
        assert zcm1[2:5] == zcm2[2:5]  # R, G, B
        assert zcm1[6] == zcm2[6]  # rumble

    def test_different_report_id_from_zcm1(self):
        """ZCM2 uses report ID 0x06, not 0x02."""
        zcm1 = build_output_report()
        zcm2 = build_output_report_zcm2()
        assert zcm1[0] == 0x02
        assert zcm2[0] == 0x06

    def test_different_size_from_zcm1(self):
        """ZCM2 report is 9 bytes, ZCM1 is 49 bytes."""
        zcm1 = build_output_report()
        zcm2 = build_output_report_zcm2()
        assert len(zcm1) == OUTPUT_REPORT_SIZE_ZCM1
        assert len(zcm2) == OUTPUT_REPORT_SIZE_ZCM2


class TestOutputReportSizeConstants:
    """Test output report size constants are consistent."""

    def test_zcm1_size_matches_default(self):
        assert OUTPUT_REPORT_SIZE == OUTPUT_REPORT_SIZE_ZCM1

    def test_zcm1_is_49(self):
        assert OUTPUT_REPORT_SIZE_ZCM1 == 49

    def test_zcm2_is_9(self):
        assert OUTPUT_REPORT_SIZE_ZCM2 == 9


class TestButtonIntFlag:
    """Test Button IntFlag values and operations."""

    def test_button_values_match_protocol(self):
        assert Button.TRIANGLE == 0x0010
        assert Button.CIRCLE == 0x0020
        assert Button.CROSS == 0x0040
        assert Button.SQUARE == 0x0080
        assert Button.SELECT == 0x0100
        assert Button.START == 0x0800
        assert Button.PS == 0x010000
        assert Button.MOVE == 0x080000
        assert Button.T == 0x100000

    def test_button_combination(self):
        combined = Button.TRIANGLE | Button.CIRCLE
        assert combined & Button.TRIANGLE
        assert combined & Button.CIRCLE
        assert not (combined & Button.CROSS)


class TestFrameEnum:
    """Test Frame enum values."""

    def test_frame_values(self):
        assert Frame.FIRST == 0
        assert Frame.SECOND == 1
