"""The `operator` flag set readers (flags.md §10a, UC-015 Pre-2, BR-007)."""

from unittest.mock import create_autospec

import pytest
from openfeature.client import OpenFeatureClient

from services.operator_pad.config import DEFAULT_KEYMAP, PadConfig, parse_device, parse_keymap


@pytest.mark.uc("UC-015", cond=["Pre-2"])
def test_default_keymap_matches_the_verified_hardware_codes():
    assert parse_keymap(DEFAULT_KEYMAP) == {
        183: "K1",
        184: "K2",
        185: "KNOB_PRESS",
        186: "KNOB_TURN_A",
        187: "KNOB_TURN_B",
        188: "K3",
        189: "K4",
        190: "K5",
        191: "K6",
        192: "K7",
        193: "K8",
    }


@pytest.mark.uc("UC-015", cond=["Pre-2"])
@pytest.mark.parametrize("raw", ["", "K1=F13,K1=F14", "K1=F13,K2=F13", "K9=F13", "K1=G13", "K1"])
def test_malformed_keymaps_are_rejected(raw):
    assert parse_keymap(raw) is None


@pytest.mark.uc("UC-015", flow="A4")
@pytest.mark.parametrize(
    ("raw", "expected"), [("cb00:1337", ("cb00", "1337")), ("CB00:1337", ("cb00", "1337")), ("", None), ("cb00", None)]
)
def test_device_ids(raw, expected):
    assert parse_device(raw) == expected


def _client(values: dict):
    client = create_autospec(OpenFeatureClient, instance=True)
    client.get_integer_value.side_effect = lambda key, default: values.get(key, default)
    client.get_string_value.side_effect = lambda key, default: values.get(key, default)
    client.get_boolean_value.side_effect = lambda key, default: values.get(key, default)
    return client


@pytest.mark.uc("UC-015", flow="main", br=["BR-007"])
def test_timings_are_read_at_use_time_with_defaults_for_bad_values():
    values = {"operator_pad.tap_ms": 250, "operator_pad.hold_end_ms": -1}
    config = PadConfig(_client(values))
    timings = config.timings()
    assert timings.tap_ms == 250
    assert timings.hold_ms == {"K3": 1000, "K4": 1500, "K8": 3000}
    values["operator_pad.tap_ms"] = 300
    assert config.timings().tap_ms == 300


@pytest.mark.uc("UC-015", cond=["Pre-2"])
def test_malformed_keymap_flag_falls_back_to_the_default():
    config = PadConfig(_client({"operator_pad.keymap": "nonsense"}))
    assert config.keymap() == parse_keymap(DEFAULT_KEYMAP)


@pytest.mark.uc("UC-015", flow="A4")
def test_unreadable_flags_keep_the_defaults():
    client = create_autospec(OpenFeatureClient, instance=True)
    client.get_integer_value.side_effect = RuntimeError("no provider")
    client.get_string_value.side_effect = RuntimeError("no provider")
    client.get_boolean_value.side_effect = RuntimeError("no provider")
    config = PadConfig(client)
    assert config.timings().tap_ms == 400
    assert config.device() == ("cb00", "1337")
    assert config.enabled() is True
