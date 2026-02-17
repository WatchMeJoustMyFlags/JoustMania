"""Tests for lib/flag_config_writer.py"""

import json
import sys
from pathlib import Path

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib.flag_config_writer import FlagConfigWriter

SAMPLE_FLAGS = {
    "flags": {
        "sensitivity": {
            "state": "ENABLED",
            "variants": {
                "ultra_slow": 0,
                "slow": 1,
                "medium": 2,
                "fast": 3,
                "ultra_fast": 4,
            },
            "defaultVariant": "medium",
        },
        "game_mode": {
            "state": "ENABLED",
            "variants": {
                "ffa": "JoustFFA",
                "teams": "JoustTeams",
            },
            "defaultVariant": "ffa",
        },
    }
}


def _write_flag_file(tmp_path, data=None):
    """Helper to create a flag file with sample data."""
    path = tmp_path / "flags.json"
    path.write_text(json.dumps(data or SAMPLE_FLAGS, indent=2) + "\n")
    return str(path)


class TestUpdateDefaultVariant:
    """Tests for FlagConfigWriter.update_default_variant."""

    def test_update_success(self, tmp_path):
        path = _write_flag_file(tmp_path)
        writer = FlagConfigWriter(path)

        result = writer.update_default_variant("sensitivity", "fast")

        assert result is True
        data = json.loads(Path(path).read_text())
        assert data["flags"]["sensitivity"]["defaultVariant"] == "fast"

    def test_flag_not_found(self, tmp_path):
        path = _write_flag_file(tmp_path)
        writer = FlagConfigWriter(path)

        result = writer.update_default_variant("nonexistent", "fast")

        assert result is False

    def test_variant_not_found(self, tmp_path):
        path = _write_flag_file(tmp_path)
        writer = FlagConfigWriter(path)

        result = writer.update_default_variant("sensitivity", "nonexistent")

        assert result is False

    def test_file_not_found(self):
        writer = FlagConfigWriter("/nonexistent/path/flags.json")

        result = writer.update_default_variant("sensitivity", "fast")

        assert result is False

    def test_invalid_json(self, tmp_path):
        path = tmp_path / "flags.json"
        path.write_text("not valid json{{{")
        writer = FlagConfigWriter(str(path))

        result = writer.update_default_variant("sensitivity", "fast")

        assert result is False

    def test_atomic_write_produces_valid_json(self, tmp_path):
        path = _write_flag_file(tmp_path)
        writer = FlagConfigWriter(path)

        writer.update_default_variant("sensitivity", "fast")

        # File should be valid JSON after update
        data = json.loads(Path(path).read_text())
        assert "flags" in data
        assert data["flags"]["sensitivity"]["defaultVariant"] == "fast"
        # Other flags should be unchanged
        assert data["flags"]["game_mode"]["defaultVariant"] == "ffa"


class TestGetVariantNames:
    """Tests for FlagConfigWriter.get_variant_names."""

    def test_success(self, tmp_path):
        path = _write_flag_file(tmp_path)
        writer = FlagConfigWriter(path)

        result = writer.get_variant_names("sensitivity")

        assert result == ["ultra_slow", "slow", "medium", "fast", "ultra_fast"]

    def test_missing_flag(self, tmp_path):
        path = _write_flag_file(tmp_path)
        writer = FlagConfigWriter(path)

        result = writer.get_variant_names("nonexistent")

        assert result == []

    def test_file_error(self):
        writer = FlagConfigWriter("/nonexistent/path/flags.json")

        result = writer.get_variant_names("sensitivity")

        assert result == []


class TestGetCurrentVariant:
    """Tests for FlagConfigWriter.get_current_variant."""

    def test_success(self, tmp_path):
        path = _write_flag_file(tmp_path)
        writer = FlagConfigWriter(path)

        result = writer.get_current_variant("sensitivity")

        assert result == "medium"

    def test_missing_flag(self, tmp_path):
        path = _write_flag_file(tmp_path)
        writer = FlagConfigWriter(path)

        result = writer.get_current_variant("nonexistent")

        assert result is None


class TestCycleVariant:
    """Tests for FlagConfigWriter.cycle_variant."""

    def test_cycle_forward(self, tmp_path):
        path = _write_flag_file(tmp_path)
        writer = FlagConfigWriter(path)

        # medium -> fast
        result = writer.cycle_variant("sensitivity", forward=True)

        assert result == "fast"
        assert writer.get_current_variant("sensitivity") == "fast"

    def test_cycle_backward(self, tmp_path):
        path = _write_flag_file(tmp_path)
        writer = FlagConfigWriter(path)

        # medium -> slow
        result = writer.cycle_variant("sensitivity", forward=False)

        assert result == "slow"
        assert writer.get_current_variant("sensitivity") == "slow"

    def test_cycle_wraps_around(self, tmp_path):
        path = _write_flag_file(tmp_path)
        writer = FlagConfigWriter(path)

        # Set to last variant, then cycle forward to wrap
        writer.update_default_variant("sensitivity", "ultra_fast")
        result = writer.cycle_variant("sensitivity", forward=True)

        assert result == "ultra_slow"

    def test_cycle_nonexistent_flag(self, tmp_path):
        path = _write_flag_file(tmp_path)
        writer = FlagConfigWriter(path)

        result = writer.cycle_variant("nonexistent")

        assert result is None
