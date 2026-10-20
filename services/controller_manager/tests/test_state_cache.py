"""
Unit tests for StateCache.

Tests the controller state caching mechanism:
- Cache hit/miss logic
- Snapshot hashing
- Protobuf message building
- Cache clearing

Issue #209: Improve test coverage for critical game flow
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from lib.controller_constants import AxisKey, ButtonKey, ControllerInfoKey, StateKey
from services.controller_manager.state_cache import StateCache


class MockMonitoring:
    """Mock controller monitoring."""

    def __init__(self):
        self.last_battery_check = 0.0

    def check_battery_levels(self, tracked):
        pass


class TestStateCacheInit:
    """Tests for StateCache initialization."""

    def test_init_creates_empty_cache(self):
        """StateCache should start with empty cache."""
        cache = StateCache(MockMonitoring())
        assert cache._cache == {}

    def test_init_creates_pools(self):
        """StateCache should create message pools."""
        cache = StateCache(MockMonitoring())
        assert cache._controller_state_pool is not None
        assert cache._vector3_pool is not None

    def test_init_empty_controller_states(self):
        """StateCache should start with empty controller_states."""
        cache = StateCache(MockMonitoring())
        assert cache._controller_states == {}


class TestStateCacheSetControllerStates:
    """Tests for set_controller_states method."""

    def test_set_controller_states(self):
        """set_controller_states should store reference."""
        cache = StateCache(MockMonitoring())
        states = {"serial_1": {"battery": 80}}

        cache.set_controller_states(states)

        assert cache._controller_states is states


class TestStateCacheClearController:
    """Tests for clear_controller method."""

    def test_clear_controller_removes_cache(self):
        """clear_controller should remove cached state."""
        cache = StateCache(MockMonitoring())
        cache._cache["serial_1"] = {"cached_state": MagicMock(), "snapshot_hash": "hash123"}

        cache.clear_controller("serial_1")

        assert "serial_1" not in cache._cache

    def test_clear_controller_nonexistent_safe(self):
        """clear_controller should not raise for nonexistent serial."""
        cache = StateCache(MockMonitoring())

        # Should not raise
        cache.clear_controller("nonexistent")

        assert "nonexistent" not in cache._cache


class TestStateCacheSnapshotHash:
    """Tests for _snapshot_hash method."""

    def test_snapshot_hash_with_state(self):
        """_snapshot_hash should include state data when available."""
        cache = StateCache(MockMonitoring())
        cache._controller_states = {
            "serial_1": {
                ButtonKey.TRIGGER: True,
                ButtonKey.MOVE: False,
                ButtonKey.CROSS: False,
                ButtonKey.CIRCLE: False,
                ButtonKey.SQUARE: False,
                ButtonKey.TRIANGLE: False,
                ButtonKey.PS: False,
                StateKey.ACCEL: {AxisKey.X: 0.1, AxisKey.Y: 0.2, AxisKey.Z: 1.0},
                StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
            }
        }
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 1}

        result = cache._snapshot_hash("serial_1", info)

        assert "80" in result  # Battery
        assert "True" in result  # Trigger pressed
        assert "0.10" in result  # Accel X
        assert "1" in result  # Team

    def test_snapshot_hash_without_state(self):
        """_snapshot_hash should work with info only when no state."""
        cache = StateCache(MockMonitoring())
        cache._controller_states = {}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 2}

        result = cache._snapshot_hash("serial_1", info)

        assert result == "80|2|None||0"  # no LED written yet (#1301), no name, move_num 0

    def test_snapshot_hash_deterministic(self):
        """Same input should produce same hash."""
        cache = StateCache(MockMonitoring())
        cache._controller_states = {
            "serial_1": {
                ButtonKey.TRIGGER: False,
                ButtonKey.MOVE: False,
                ButtonKey.CROSS: False,
                ButtonKey.CIRCLE: False,
                ButtonKey.SQUARE: False,
                ButtonKey.TRIANGLE: False,
                ButtonKey.PS: False,
                StateKey.ACCEL: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0},
                StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
            }
        }
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0}

        hash1 = cache._snapshot_hash("serial_1", info)
        hash2 = cache._snapshot_hash("serial_1", info)

        assert hash1 == hash2

    def test_snapshot_hash_changes_with_button(self):
        """Hash should change when button state changes."""
        cache = StateCache(MockMonitoring())
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0}

        cache._controller_states = {
            "serial_1": {
                ButtonKey.TRIGGER: False,
                ButtonKey.MOVE: False,
                ButtonKey.CROSS: False,
                ButtonKey.CIRCLE: False,
                ButtonKey.SQUARE: False,
                ButtonKey.TRIANGLE: False,
                ButtonKey.PS: False,
                StateKey.ACCEL: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0},
                StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
            }
        }
        hash1 = cache._snapshot_hash("serial_1", info)

        # Change button state
        cache._controller_states["serial_1"][ButtonKey.TRIGGER] = True
        hash2 = cache._snapshot_hash("serial_1", info)

        assert hash1 != hash2


class TestStateCacheBuildOrGetCached:
    """Tests for build_or_get_cached_state method."""

    @patch("services.controller_manager.metrics.state_cache_hits_total")
    @patch("services.controller_manager.metrics.state_cache_misses_total")
    def test_cache_miss_on_first_call(self, mock_misses, mock_hits):
        """First call should be a cache miss."""
        cache = StateCache(MockMonitoring())
        cache._controller_states = {
            "serial_1": {
                ButtonKey.TRIGGER: False,
                ButtonKey.MOVE: False,
                ButtonKey.CROSS: False,
                ButtonKey.CIRCLE: False,
                ButtonKey.SQUARE: False,
                ButtonKey.TRIANGLE: False,
                ButtonKey.PS: False,
                ButtonKey.SELECT: False,
                ButtonKey.START: False,
                StateKey.ACCEL: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0},
                StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
            }
        }
        info = {
            ControllerInfoKey.BATTERY: 80,
            ControllerInfoKey.TEAM: 0,
            ControllerInfoKey.MOVE_NUM: 0,
            ControllerInfoKey.NAME: "Test",
        }

        result = cache.build_or_get_cached_state("serial_1", info)

        assert result is not None
        mock_misses.inc.assert_called_once()

    @patch("services.controller_manager.metrics.state_cache_hits_total")
    @patch("services.controller_manager.metrics.state_cache_misses_total")
    def test_cache_hit_on_second_call(self, mock_misses, mock_hits):
        """Second call with same state should be cache hit."""
        cache = StateCache(MockMonitoring())
        cache._controller_states = {
            "serial_1": {
                ButtonKey.TRIGGER: False,
                ButtonKey.MOVE: False,
                ButtonKey.CROSS: False,
                ButtonKey.CIRCLE: False,
                ButtonKey.SQUARE: False,
                ButtonKey.TRIANGLE: False,
                ButtonKey.PS: False,
                ButtonKey.SELECT: False,
                ButtonKey.START: False,
                StateKey.ACCEL: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0},
                StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
            }
        }
        info = {
            ControllerInfoKey.BATTERY: 80,
            ControllerInfoKey.TEAM: 0,
            ControllerInfoKey.MOVE_NUM: 0,
            ControllerInfoKey.NAME: "Test",
        }

        # First call - miss
        cache.build_or_get_cached_state("serial_1", info)

        # Second call - hit
        result = cache.build_or_get_cached_state("serial_1", info)

        assert result is not None
        mock_hits.inc.assert_called_once()

    @patch("services.controller_manager.metrics.state_cache_hits_total")
    @patch("services.controller_manager.metrics.state_cache_misses_total")
    def test_cache_miss_after_state_change(self, mock_misses, mock_hits):
        """Cache should miss after state changes."""
        cache = StateCache(MockMonitoring())
        cache._controller_states = {
            "serial_1": {
                ButtonKey.TRIGGER: False,
                ButtonKey.MOVE: False,
                ButtonKey.CROSS: False,
                ButtonKey.CIRCLE: False,
                ButtonKey.SQUARE: False,
                ButtonKey.TRIANGLE: False,
                ButtonKey.PS: False,
                ButtonKey.SELECT: False,
                ButtonKey.START: False,
                StateKey.ACCEL: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0},
                StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
            }
        }
        info = {
            ControllerInfoKey.BATTERY: 80,
            ControllerInfoKey.TEAM: 0,
            ControllerInfoKey.MOVE_NUM: 0,
            ControllerInfoKey.NAME: "Test",
        }

        # First call
        cache.build_or_get_cached_state("serial_1", info)

        # Change state
        cache._controller_states["serial_1"][ButtonKey.TRIGGER] = True

        # Second call - should miss due to state change
        cache.build_or_get_cached_state("serial_1", info)

        assert mock_misses.inc.call_count == 2


class TestStateCacheBuildMessage:
    """Tests for _build_controller_state_message method."""

    def test_build_message_with_state(self):
        """_build_controller_state_message should populate all fields."""
        cache = StateCache(MockMonitoring())
        cache._controller_states = {
            "serial_1": {
                ButtonKey.TRIGGER: True,
                ButtonKey.MOVE: False,
                ButtonKey.CROSS: True,
                ButtonKey.CIRCLE: False,
                ButtonKey.SQUARE: True,
                ButtonKey.TRIANGLE: False,
                ButtonKey.PS: True,
                ButtonKey.SELECT: False,
                ButtonKey.START: True,
                StateKey.ACCEL: {AxisKey.X: 0.1, AxisKey.Y: 0.2, AxisKey.Z: 1.0},
                StateKey.GYRO: {AxisKey.X: 0.01, AxisKey.Y: 0.02, AxisKey.Z: 0.03},
            }
        }
        info = {
            ControllerInfoKey.BATTERY: 85,
            ControllerInfoKey.TEAM: 2,
            ControllerInfoKey.MOVE_NUM: 3,
            ControllerInfoKey.NAME: "TestController",
        }

        result = cache._build_controller_state_message("serial_1", info)

        assert result.serial == "serial_1"
        assert result.battery == 85
        assert result.team == 2
        assert result.move_num == 3
        assert result.name == "TestController"
        assert result.trigger_pressed is True
        assert result.move_pressed is False
        assert result.cross_pressed is True
        assert result.ps_pressed is True
        assert result.start_pressed is True

    def test_build_message_without_state(self):
        """_build_controller_state_message should handle missing state."""
        cache = StateCache(MockMonitoring())
        cache._controller_states = {}
        info = {
            ControllerInfoKey.BATTERY: 80,
            ControllerInfoKey.TEAM: 0,
            ControllerInfoKey.MOVE_NUM: 0,
            ControllerInfoKey.NAME: "",
        }

        result = cache._build_controller_state_message("serial_1", info)

        assert result.serial == "serial_1"
        assert result.battery == 80
        assert result.trigger_pressed is False
        assert result.move_pressed is False


class TestStateCacheRename:
    """UC-002 A2 step 3: a rename reaches the controller state a game reads."""

    @patch("services.controller_manager.metrics.state_cache_hits_total")
    @patch("services.controller_manager.metrics.state_cache_misses_total")
    @pytest.mark.uc("UC-002", flow="A2", steps=[3])
    def test_renamed_controller_state_carries_new_name(self, mock_misses, mock_hits):
        """After a rename the state a new game reads has the new name, even for a still controller."""
        cache = StateCache(MockMonitoring())
        cache._controller_states = {
            "serial_1": {
                **dict.fromkeys(ButtonKey, False),
                StateKey.ACCEL: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0},
                StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
            }
        }
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.NAME: "Old Name"}

        assert cache.build_or_get_cached_state("serial_1", info).name == "Old Name"

        info[ControllerInfoKey.NAME] = "New Name"
        assert cache.build_or_get_cached_state("serial_1", info).name == "New Name"

    @patch("services.controller_manager.metrics.state_cache_hits_total")
    @patch("services.controller_manager.metrics.state_cache_misses_total")
    @pytest.mark.uc("UC-002", flow="A2", steps=[3])
    def test_renamed_controller_without_hardware_state_carries_new_name(self, mock_misses, mock_hits):
        """The no-state branch of the snapshot hash also changes on a rename."""
        cache = StateCache(MockMonitoring())
        cache._controller_states = {}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.NAME: "Old Name"}

        assert cache.build_or_get_cached_state("serial_1", info).name == "Old Name"

        info[ControllerInfoKey.NAME] = "New Name"
        assert cache.build_or_get_cached_state("serial_1", info).name == "New Name"


class TestStateCacheServesEveryField:
    """UC-002 main step 2: the state served to the gameplay stream matches the live controller (#1638).

    A still controller keeps accel/gyro unchanged, so only the snapshot hash decides whether
    a change of SELECT, START or move number reaches the stream.
    """

    @staticmethod
    def _still_controller_state() -> dict:
        return {
            **dict.fromkeys(ButtonKey, False),
            StateKey.ACCEL: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0},
            StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
        }

    @patch("services.controller_manager.metrics.state_cache_hits_total")
    @patch("services.controller_manager.metrics.state_cache_misses_total")
    @pytest.mark.uc("UC-002", flow="main", steps=[2])
    @pytest.mark.parametrize(
        ("button", "field"),
        [(ButtonKey.SELECT, "select_pressed"), (ButtonKey.START, "start_pressed")],
    )
    def test_button_flip_on_still_controller_is_served(self, mock_misses, mock_hits, button, field):
        cache = StateCache(MockMonitoring())
        cache._controller_states = {"serial_1": self._still_controller_state()}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.MOVE_NUM: 1}

        assert getattr(cache.build_or_get_cached_state("serial_1", info), field) is False

        cache._controller_states["serial_1"][button] = True
        assert getattr(cache.build_or_get_cached_state("serial_1", info), field) is True

        cache._controller_states["serial_1"][button] = False
        assert getattr(cache.build_or_get_cached_state("serial_1", info), field) is False

    @patch("services.controller_manager.metrics.state_cache_hits_total")
    @patch("services.controller_manager.metrics.state_cache_misses_total")
    @pytest.mark.uc("UC-002", flow="main", steps=[2])
    def test_move_num_change_on_still_controller_is_served(self, mock_misses, mock_hits):
        cache = StateCache(MockMonitoring())
        cache._controller_states = {"serial_1": self._still_controller_state()}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.MOVE_NUM: 1}

        assert cache.build_or_get_cached_state("serial_1", info).move_num == 1

        info[ControllerInfoKey.MOVE_NUM] = 2
        assert cache.build_or_get_cached_state("serial_1", info).move_num == 2

    @patch("services.controller_manager.metrics.state_cache_hits_total")
    @patch("services.controller_manager.metrics.state_cache_misses_total")
    @pytest.mark.uc("UC-002", flow="main", steps=[2])
    def test_move_num_change_without_hardware_state_is_served(self, mock_misses, mock_hits):
        cache = StateCache(MockMonitoring())
        cache._controller_states = {}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.MOVE_NUM: 1}

        assert cache.build_or_get_cached_state("serial_1", info).move_num == 1

        info[ControllerInfoKey.MOVE_NUM] = 2
        assert cache.build_or_get_cached_state("serial_1", info).move_num == 2
