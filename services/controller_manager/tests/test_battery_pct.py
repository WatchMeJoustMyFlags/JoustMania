"""
Tests for battery percentage normalization added in #730 / #722 §7.

controller_battery_pct exposes a 0-100 value derived from either the 0-5
psmoveapi scale (x20) or the Rust HID 0-100 path (passthrough). The 0-5
controller_battery_level metric is unchanged for compatibility.
"""

import pytest

from services.controller_manager.metrics import battery_to_pct


class TestBatteryToPct:
    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (0, 0.0),
            (1, 20.0),
            (2, 40.0),
            (3, 60.0),
            (4, 80.0),
            (5, 100.0),
        ],
    )
    def test_scale_0_5_maps_to_percent(self, level, expected):
        assert battery_to_pct(level) == expected

    @pytest.mark.parametrize(
        ("hid_value", "expected"),
        [
            (10, 10.0),
            (42, 42.0),
            (75, 75.0),
            (100, 100.0),
        ],
    )
    def test_hid_0_100_passthrough(self, hid_value, expected):
        # Values above the 0-5 range are assumed to already be percentages
        assert battery_to_pct(hid_value) == expected

    def test_clamped_to_100(self):
        # psmoveapi charging/unknown sentinels (e.g. 0xEE) clamp to full
        assert battery_to_pct(238) == 100.0

    def test_clamped_to_zero(self):
        assert battery_to_pct(-1) == 0.0

    def test_boundary_five_is_full_not_five_percent(self):
        # 5 is the top of the coarse scale, not 5% on the HID scale
        assert battery_to_pct(5) == 100.0

    def test_fractional_level(self):
        assert battery_to_pct(2.5) == 50.0
