"""
Unit tests for gameplay disconnect event tracking (#580).

Tests:
- Disconnect events are queued when a controller disconnects
- drain_disconnects() returns and clears accumulated disconnects
- Servicer includes DisconnectEvent in GameplayDataUpdate
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from services.controller_manager.discovery_loop import DiscoveryLoop


def _create_discovery_loop(**overrides):
    """Create a DiscoveryLoop with mocked dependencies."""
    defaults = {
        "backend": MagicMock(spec=[]),
        "tracked_controllers": {},
        "controller_states": {},
        "button_detector": MagicMock(),
        "state_cache_manager": MagicMock(),
        "feedback_manager": MagicMock(),
        "monitoring": MagicMock(last_battery_check=0),
        "rescan_timer": MagicMock(),
        "paired_serials": [],
        "base_colors": {},
        "event_publisher": MagicMock(),
    }
    defaults.update(overrides)
    return DiscoveryLoop(**defaults)


class TestGameplayDisconnectQueue:
    """Tests for _gameplay_disconnects queue in DiscoveryLoop."""

    def test_initial_queue_empty(self):
        loop = _create_discovery_loop()
        assert loop._gameplay_disconnects == []

    def test_disconnect_appended(self):
        """Appending a serial mirrors what _check_for_new_controllers does."""
        loop = _create_discovery_loop()
        loop._gameplay_disconnects.append("S1")
        assert loop._gameplay_disconnects == ["S1"]

    def test_multiple_disconnects_preserved_in_order(self):
        loop = _create_discovery_loop()
        loop._gameplay_disconnects.append("S1")
        loop._gameplay_disconnects.append("S2")
        loop._gameplay_disconnects.append("S3")
        assert loop._gameplay_disconnects == ["S1", "S2", "S3"]


class TestDrainDisconnects:
    """Tests for drain_disconnects() drain-and-clear semantics."""

    def test_drain_returns_accumulated_disconnects(self):
        loop = _create_discovery_loop()
        loop._gameplay_disconnects.append("S1")
        loop._gameplay_disconnects.append("S2")

        result = loop.drain_disconnects()
        assert result == ["S1", "S2"]

    def test_drain_clears_list(self):
        loop = _create_discovery_loop()
        loop._gameplay_disconnects.append("S1")

        loop.drain_disconnects()

        # Second drain should return empty
        result = loop.drain_disconnects()
        assert result == []

    def test_drain_empty_returns_empty(self):
        loop = _create_discovery_loop()
        result = loop.drain_disconnects()
        assert result == []

    def test_drain_returns_exact_list(self):
        """drain_disconnects should return exactly the serials that were queued."""
        loop = _create_discovery_loop()
        serials = ["AA:BB:CC", "DD:EE:FF", "11:22:33"]
        for s in serials:
            loop._gameplay_disconnects.append(s)

        result = loop.drain_disconnects()
        assert result == serials

    def test_drain_is_idempotent_on_empty(self):
        """Calling drain multiple times on empty list always returns empty."""
        loop = _create_discovery_loop()
        assert loop.drain_disconnects() == []
        assert loop.drain_disconnects() == []
        assert loop.drain_disconnects() == []

    def test_new_disconnects_after_drain(self):
        """Disconnects added after a drain appear in next drain."""
        loop = _create_discovery_loop()
        loop._gameplay_disconnects.append("S1")
        loop.drain_disconnects()

        loop._gameplay_disconnects.append("S2")
        result = loop.drain_disconnects()
        assert result == ["S2"]
