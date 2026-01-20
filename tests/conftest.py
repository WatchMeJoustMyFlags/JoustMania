"""
Root pytest configuration and shared fixtures for JoustMania tests.

This file provides common fixtures and utilities used across:
- Integration tests (tests/integration/)
- Service unit tests (services/*/tests/)

Fixtures defined here are automatically available to all tests.
"""

import os
import sys
from pathlib import Path

import pytest

# Ensure project root is in path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# =============================================================================
# Path Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Return the project root directory."""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def proto_path(project_root) -> Path:
    """Return the proto package directory."""
    return project_root / "proto"


# =============================================================================
# Mock Settings Fixtures
# =============================================================================


class MockSettingsService:
    """
    Mock Settings gRPC service for testing.

    Provides a simple in-memory settings store that can be used
    in tests without requiring a running settings service.

    Usage:
        def test_something(mock_settings):
            mock_settings.settings["my_key"] = "my_value"
            # ... test code that uses settings
    """

    def __init__(self, initial_settings: dict | None = None):
        """
        Initialize mock settings.

        Args:
            initial_settings: Optional dict of initial settings
        """
        self.settings = {
            "sensitivity": "MEDIUM",
            "play_audio": "false",
            "color_lock": "false",
            "random_teams": "false",
            "force_all_start": "false",
            "num_teams": "2",
            "menu_voice": "ivy",
            "current_game": "JoustFFA",
        }
        if initial_settings:
            self.settings.update(initial_settings)

    def GetSettings(self, request=None):  # noqa: N802, ARG002
        """Mock GetSettings RPC."""
        from proto import settings_pb2

        return settings_pb2.GetSettingsResponse(settings=self.settings, success=True, error="")

    def GetSetting(self, request):  # noqa: N802
        """Mock GetSetting RPC."""
        from proto import settings_pb2

        key = request.key
        value = self.settings.get(key, "")
        return settings_pb2.GetSettingResponse(value=value, success=True, error="")

    def UpdateSetting(self, request):  # noqa: N802
        """Mock UpdateSetting RPC."""
        from proto import settings_pb2

        self.settings[request.key] = request.value
        return settings_pb2.UpdateSettingResponse(success=True, error="")


@pytest.fixture
def mock_settings():
    """Fixture providing a mock Settings service with default values."""
    return MockSettingsService()


@pytest.fixture
def mock_settings_factory():
    """
    Factory fixture for creating MockSettingsService with custom settings.

    Usage:
        def test_custom_settings(mock_settings_factory):
            settings = mock_settings_factory({"sensitivity": "FAST"})
    """

    def _factory(initial_settings: dict | None = None):
        return MockSettingsService(initial_settings)

    return _factory


# =============================================================================
# Event Collection Fixtures
# =============================================================================


class EventCollector:
    """
    Collects events for testing event-driven code.

    Useful for verifying that the correct events are published
    during game operations, menu transitions, etc.

    Usage:
        def test_game_events(event_collector):
            game.event_publisher = event_collector.publish
            # ... run game code
            assert event_collector.count_events_of_type("player_death") == 2
    """

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def publish(self, event_type: str, data: dict):
        """Collect a published event."""
        self.events.append((event_type, data))

    def get_events_of_type(self, event_type: str) -> list[dict]:
        """Get all events of a specific type."""
        return [data for et, data in self.events if et == event_type]

    def count_events_of_type(self, event_type: str) -> int:
        """Count events of a specific type."""
        return len(self.get_events_of_type(event_type))

    def has_event(self, event_type: str) -> bool:
        """Check if any event of the given type was published."""
        return any(et == event_type for et, _ in self.events)

    def get_last_event(self, event_type: str) -> dict | None:
        """Get the most recent event of a specific type."""
        events = self.get_events_of_type(event_type)
        return events[-1] if events else None

    def clear(self):
        """Clear all collected events."""
        self.events.clear()

    def __len__(self):
        return len(self.events)


@pytest.fixture
def event_collector():
    """Fixture providing an EventCollector for testing."""
    return EventCollector()


# =============================================================================
# Test Environment Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def is_ci() -> bool:
    """Check if running in CI environment."""
    return os.getenv("CI", "").lower() in ("true", "1", "yes")


@pytest.fixture(scope="session")
def mock_mode() -> bool:
    """Check if running in mock mode (no hardware)."""
    return os.getenv("CONTROLLER_BACKEND", "mock") == "mock"
