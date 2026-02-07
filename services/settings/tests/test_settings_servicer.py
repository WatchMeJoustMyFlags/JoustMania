"""
Unit tests for Settings gRPC Servicer.

Tests the core SettingsServicer functionality without full gRPC server.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import Mock

import pytest
import yaml

from proto import settings_pb2
from services.settings.servicer import SETTINGS_SCHEMA, SettingsServicer


class TestSettingsServicer:
    """Unit tests for SettingsServicer class."""

    @pytest.fixture
    def temp_settings_file(self):
        """Create a temporary settings file."""
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        yield path
        # Cleanup
        if os.path.exists(path):
            os.remove(path)
        if os.path.exists(path + ".tmp"):
            os.remove(path + ".tmp")

    @pytest.fixture
    def servicer(self, temp_settings_file):
        """Create a SettingsServicer instance with temp file."""
        return SettingsServicer(settings_file=temp_settings_file)

    def test_initialization(self, servicer):
        """Test servicer initializes with default settings."""
        assert servicer.settings is not None
        assert isinstance(servicer.settings, dict)
        assert len(servicer.settings) > 0
        assert "sensitivity" in servicer.settings
        assert "current_game" in servicer.settings

    def test_default_settings(self, servicer):
        """Test default settings are loaded from schema."""
        defaults = servicer.get_default_settings()

        # Check all schema keys present
        for key in SETTINGS_SCHEMA:
            assert key in defaults

        # Check specific defaults
        assert defaults["sensitivity"] == 2  # Sensitivity.MEDIUM
        assert defaults["instructions"] is True
        assert defaults["current_game"] == "JoustFFA"

    def test_load_settings_no_file(self, temp_settings_file):
        """Test loading when no settings file exists."""
        # Remove file if it exists
        if os.path.exists(temp_settings_file):
            os.remove(temp_settings_file)

        servicer = SettingsServicer(settings_file=temp_settings_file)

        # Should use defaults
        assert servicer.settings == servicer.get_default_settings()

        # Should create file
        assert os.path.exists(temp_settings_file)

    def test_load_settings_with_file(self, temp_settings_file):
        """Test loading settings from existing file."""
        # Write test settings
        test_settings = {"sensitivity": 3, "instructions": False, "current_game": "Werewolf"}
        with open(temp_settings_file, "w") as f:
            yaml.dump(test_settings, f)

        servicer = SettingsServicer(settings_file=temp_settings_file)

        # Should load from file
        assert servicer.settings["sensitivity"] == 3
        assert servicer.settings["instructions"] is False
        assert servicer.settings["current_game"] == "Werewolf"

    def test_load_settings_invalid_values(self, temp_settings_file):
        """Test loading with invalid values uses defaults."""
        # Write invalid settings
        invalid_settings = {
            "sensitivity": 999,  # Out of range
            "menu_voice": "invalid",  # Not in allowed values
            "current_game": "JoustFFA",  # Valid value
        }
        with open(temp_settings_file, "w") as f:
            yaml.dump(invalid_settings, f)

        servicer = SettingsServicer(settings_file=temp_settings_file)

        # Invalid values should use defaults
        assert servicer.settings["sensitivity"] == 2  # Default
        assert servicer.settings["menu_voice"] == "ivy"  # Default
        # Valid value should be preserved
        assert servicer.settings["current_game"] == "JoustFFA"

    def test_save_settings(self, servicer, temp_settings_file):
        """Test saving settings to file."""
        servicer.settings["sensitivity"] = 4
        servicer.save_settings()

        # Read back from file
        with open(temp_settings_file) as f:
            saved = yaml.safe_load(f)

        assert saved["sensitivity"] == 4

    def test_save_settings_atomic(self, servicer, temp_settings_file):
        """Test atomic save using temp file."""
        servicer.settings["sensitivity"] = 3
        servicer.save_settings()

        # Temp file should not exist after save
        assert not os.path.exists(temp_settings_file + ".tmp")

        # Main file should exist
        assert os.path.exists(temp_settings_file)

    def test_validate_setting_value_int(self, servicer):
        """Test validation for integer settings."""
        # Valid
        valid, error = servicer.validate_setting_value("sensitivity", 2)
        assert valid is True
        assert error == ""

        # Below minimum
        valid, error = servicer.validate_setting_value("sensitivity", -1)
        assert valid is False
        assert "below minimum" in error.lower()

        # Above maximum
        valid, error = servicer.validate_setting_value("sensitivity", 10)
        assert valid is False
        assert "above maximum" in error.lower()

        # Wrong type
        valid, error = servicer.validate_setting_value("sensitivity", "2")
        assert valid is False
        assert "expected" in error.lower()

    def test_validate_setting_value_bool(self, servicer):
        """Test validation for boolean settings."""
        # Valid
        valid, error = servicer.validate_setting_value("instructions", True)
        assert valid is True

        valid, error = servicer.validate_setting_value("instructions", False)
        assert valid is True

        # Wrong type
        valid, error = servicer.validate_setting_value("instructions", "true")
        assert valid is False

    def test_validate_setting_value_str_allowed(self, servicer):
        """Test validation for string with allowed values."""
        # Valid
        valid, error = servicer.validate_setting_value("menu_voice", "ivy")
        assert valid is True

        valid, error = servicer.validate_setting_value("menu_voice", "aaron")
        assert valid is True

        # Invalid
        valid, error = servicer.validate_setting_value("menu_voice", "invalid")
        assert valid is False
        assert "not in allowed values" in error.lower()

    def test_validate_setting_value_list(self, servicer):
        """Test validation for list settings."""
        # Valid
        valid, error = servicer.validate_setting_value("random_modes", ["JoustFFA", "Werewolf"])
        assert valid is True

        # Invalid game in list
        valid, error = servicer.validate_setting_value("random_modes", ["InvalidGame"])
        assert valid is False
        assert "invalid game mode" in error.lower()

    def test_validate_setting_value_unknown(self, servicer):
        """Test validation for unknown setting."""
        valid, error = servicer.validate_setting_value("unknown_key", "value")
        assert valid is False
        assert "unknown setting" in error.lower()

    def test_clear_one_controller_preserves_others(self):
        """Test clearing one controller doesn't affect others."""
        # This test doesn't apply to settings - removed


@pytest.mark.asyncio
class TestSettingsServicerAsync:
    """Async tests for SettingsServicer."""

    @pytest.fixture
    def temp_settings_file(self):
        """Create a temporary settings file."""
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        yield path
        if os.path.exists(path):
            os.remove(path)
        if os.path.exists(path + ".tmp"):
            os.remove(path + ".tmp")

    @pytest.fixture
    def servicer(self, temp_settings_file):
        """Create a SettingsServicer instance."""
        return SettingsServicer(settings_file=temp_settings_file)

    async def test_publish_change(self, servicer):
        """Test publishing changes to subscribers."""
        # Create mock subscriber
        sub_queue = asyncio.Queue(maxsize=10)
        servicer.subscribers["test_sub"] = sub_queue

        # Publish change
        await servicer.publish_change("sensitivity", 2, 3, "test")

        # Check event received
        assert not sub_queue.empty()
        event = await sub_queue.get()
        assert event.key == "sensitivity"
        assert event.old_value == "2"
        assert event.new_value == "3"
        assert event.source == "test"

    async def test_publish_change_full_queue(self, servicer):
        """Test publishing when subscriber queue is full."""
        # Create full queue
        sub_queue = asyncio.Queue(maxsize=1)
        await sub_queue.put("dummy")
        servicer.subscribers["test_sub"] = sub_queue

        # Publish should not raise exception
        await servicer.publish_change("sensitivity", 2, 3, "test")

        # Subscriber should still exist
        assert "test_sub" in servicer.subscribers


class TestSettingsServicerRPCs:
    """Test gRPC service methods."""

    @pytest.fixture
    def temp_settings_file(self):
        """Create a temporary settings file."""
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        yield path
        if os.path.exists(path):
            os.remove(path)
        if os.path.exists(path + ".tmp"):
            os.remove(path + ".tmp")

    @pytest.fixture
    def servicer(self, temp_settings_file):
        """Create a SettingsServicer instance."""
        return SettingsServicer(settings_file=temp_settings_file)

    @pytest.fixture
    def mock_context(self):
        """Create a mock gRPC context."""
        context = Mock()
        context.is_active = Mock(return_value=True)
        context.cancelled = Mock(return_value=False)
        return context

    def test_get_settings(self, servicer, mock_context):
        """Test GetSettings RPC."""
        request = settings_pb2.GetSettingsRequest()
        response = servicer.GetSettings(request, mock_context)

        assert response.success is True
        assert response.error == ""
        assert len(response.settings) > 0
        assert "sensitivity" in response.settings
        assert "current_game" in response.settings

    def test_get_setting_exists(self, servicer, mock_context):
        """Test GetSetting RPC for existing setting."""
        request = settings_pb2.GetSettingRequest(key="sensitivity")
        response = servicer.GetSetting(request, mock_context)

        assert response.success is True
        assert response.error == ""
        assert response.key == "sensitivity"
        assert response.value == "2"  # Default value

    def test_get_setting_not_exists(self, servicer, mock_context):
        """Test GetSetting RPC for non-existent setting."""
        request = settings_pb2.GetSettingRequest(key="nonexistent")
        response = servicer.GetSetting(request, mock_context)

        assert response.success is False
        assert "not found" in response.error.lower()
        assert response.value == ""

    def test_get_setting_bool_lowercase(self, servicer, mock_context):
        """Test GetSetting RPC returns booleans as lowercase strings."""
        # Test true value
        servicer.settings["instructions"] = True
        request = settings_pb2.GetSettingRequest(key="instructions")
        response = servicer.GetSetting(request, mock_context)

        assert response.success is True
        assert response.value == "true"  # Must be lowercase, not "True"

        # Test false value
        servicer.settings["instructions"] = False
        response = servicer.GetSetting(request, mock_context)

        assert response.success is True
        assert response.value == "false"  # Must be lowercase, not "False"

    def test_get_settings_bool_lowercase(self, servicer, mock_context):
        """Test GetSettings RPC returns booleans as lowercase strings."""
        servicer.settings["instructions"] = True
        servicer.settings["force_all_start"] = False

        request = settings_pb2.GetSettingsRequest()
        response = servicer.GetSettings(request, mock_context)

        assert response.success is True
        assert response.settings["instructions"] == "true"  # Lowercase
        assert response.settings["force_all_start"] == "false"  # Lowercase


@pytest.mark.asyncio
class TestSettingsServicerRPCsAsync:
    """Async tests for gRPC service methods."""

    @pytest.fixture
    def temp_settings_file(self):
        """Create a temporary settings file."""
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        yield path
        if os.path.exists(path):
            os.remove(path)
        if os.path.exists(path + ".tmp"):
            os.remove(path + ".tmp")

    @pytest.fixture
    def servicer(self, temp_settings_file):
        """Create a SettingsServicer instance."""
        return SettingsServicer(settings_file=temp_settings_file)

    @pytest.fixture
    def mock_context(self):
        """Create a mock gRPC context."""
        context = Mock()
        context.is_active = Mock(return_value=True)
        context.cancelled = Mock(return_value=False)
        return context

    async def test_update_setting_valid(self, servicer, mock_context):
        """Test UpdateSetting RPC with valid value."""
        request = settings_pb2.UpdateSettingRequest(key="sensitivity", value="3", source="test")
        response = await servicer.UpdateSetting(request, mock_context)

        assert response.success is True
        assert response.error == ""
        assert response.old_value == "2"
        assert response.new_value == "3"

        # Verify setting actually changed
        assert servicer.settings["sensitivity"] == 3

    async def test_update_setting_invalid_value(self, servicer, mock_context):
        """Test UpdateSetting RPC with invalid value."""
        request = settings_pb2.UpdateSettingRequest(
            key="sensitivity",
            value="999",  # Out of range
            source="test",
        )
        response = await servicer.UpdateSetting(request, mock_context)

        assert response.success is False
        assert "above maximum" in response.error.lower()

        # Setting should not change
        assert servicer.settings["sensitivity"] == 2

    async def test_update_setting_unknown_key(self, servicer, mock_context):
        """Test UpdateSetting RPC with unknown key."""
        request = settings_pb2.UpdateSettingRequest(key="nonexistent", value="value", source="test")
        response = await servicer.UpdateSetting(request, mock_context)

        assert response.success is False
        assert "unknown setting" in response.error.lower()

    async def test_update_setting_bool_parsing(self, servicer, mock_context):
        """Test UpdateSetting RPC parses boolean strings correctly."""
        # Test 'true'
        request = settings_pb2.UpdateSettingRequest(key="instructions", value="true", source="test")
        response = await servicer.UpdateSetting(request, mock_context)
        assert response.success is True
        assert servicer.settings["instructions"] is True

        # Test 'false'
        request = settings_pb2.UpdateSettingRequest(key="instructions", value="false", source="test")
        response = await servicer.UpdateSetting(request, mock_context)
        assert response.success is True
        assert servicer.settings["instructions"] is False

    async def test_update_setting_publishes_event(self, servicer, mock_context):
        """Test UpdateSetting publishes change event."""
        # Add subscriber
        sub_queue = asyncio.Queue()
        servicer.subscribers["test"] = sub_queue

        # Update setting
        request = settings_pb2.UpdateSettingRequest(key="sensitivity", value="4", source="test")
        await servicer.UpdateSetting(request, mock_context)

        # Check event published
        assert not sub_queue.empty()
        event = await sub_queue.get()
        assert event.key == "sensitivity"
        assert event.new_value == "4"


class TestFileIOEdgeCases:
    """Test file I/O edge cases for settings persistence."""

    @pytest.fixture
    def temp_settings_file(self):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        yield path
        if os.path.exists(path):
            os.remove(path)
        if os.path.exists(path + ".tmp"):
            os.remove(path + ".tmp")

    def test_corrupt_yaml_falls_back_to_defaults(self, temp_settings_file):
        """Test that corrupt YAML causes fallback to defaults."""
        with open(temp_settings_file, "w") as f:
            f.write("{{invalid yaml: [")

        servicer = SettingsServicer(settings_file=temp_settings_file)

        defaults = servicer.get_default_settings()
        assert servicer.settings == defaults

    def test_empty_yaml_file_uses_defaults(self, temp_settings_file):
        """Test that an empty YAML file uses defaults."""
        with open(temp_settings_file, "w") as f:
            f.write("")

        servicer = SettingsServicer(settings_file=temp_settings_file)

        defaults = servicer.get_default_settings()
        assert servicer.settings == defaults

    def test_extra_keys_in_yaml_ignored(self, temp_settings_file):
        """Test that extra keys in YAML are ignored, valid ones loaded."""
        test_settings = {"extra_key": "value", "sensitivity": 3}
        with open(temp_settings_file, "w") as f:
            yaml.dump(test_settings, f)

        servicer = SettingsServicer(settings_file=temp_settings_file)

        assert "extra_key" not in servicer.settings
        assert servicer.settings["sensitivity"] == 3

    def test_save_creates_file_if_missing(self, temp_settings_file):
        """Test that save creates the file if it was missing."""
        if os.path.exists(temp_settings_file):
            os.remove(temp_settings_file)

        servicer = SettingsServicer(settings_file=temp_settings_file)

        assert os.path.exists(temp_settings_file)
        with open(temp_settings_file) as f:
            saved = yaml.safe_load(f)
        assert saved["sensitivity"] == servicer.settings["sensitivity"]

    def test_permission_error_handled_gracefully(self, temp_settings_file):
        """Test that PermissionError on load doesn't crash, uses defaults."""
        from unittest.mock import patch

        with patch("builtins.open", side_effect=PermissionError("Permission denied")):
            servicer = SettingsServicer(settings_file=temp_settings_file)

        defaults = servicer.get_default_settings()
        assert servicer.settings == defaults


class TestValidationEdgeCases:
    """Test validation boundary conditions and edge cases."""

    @pytest.fixture
    def servicer(self):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        servicer = SettingsServicer(settings_file=path)
        yield servicer
        if os.path.exists(path):
            os.remove(path)
        if os.path.exists(path + ".tmp"):
            os.remove(path + ".tmp")

    def test_sensitivity_boundary_min(self, servicer):
        """Test sensitivity at minimum boundary (0) is valid."""
        valid, error = servicer.validate_setting_value("sensitivity", 0)
        assert valid is True
        assert error == ""

    def test_sensitivity_boundary_max(self, servicer):
        """Test sensitivity at maximum boundary (4) is valid."""
        valid, error = servicer.validate_setting_value("sensitivity", 4)
        assert valid is True
        assert error == ""

    def test_num_teams_boundary_min(self, servicer):
        """Test num_teams at minimum boundary (2) is valid."""
        valid, error = servicer.validate_setting_value("num_teams", 2)
        assert valid is True
        assert error == ""

    def test_num_teams_boundary_max(self, servicer):
        """Test num_teams at maximum boundary (6) is valid."""
        valid, error = servicer.validate_setting_value("num_teams", 6)
        assert valid is True
        assert error == ""

    def test_nonstop_time_limit_boundary_max(self, servicer):
        """Test nonstop_time_limit at maximum boundary (3600) is valid."""
        valid, error = servicer.validate_setting_value("nonstop_time_limit", 3600)
        assert valid is True
        assert error == ""

    def test_negative_int_fails_validation(self, servicer):
        """Test that negative integer fails validation with below minimum."""
        valid, error = servicer.validate_setting_value("sensitivity", -1)
        assert valid is False
        assert "below minimum" in error.lower()

    def test_empty_string_rejected_for_menu_voice(self, servicer):
        """Test that empty string is rejected for menu_voice."""
        valid, error = servicer.validate_setting_value("menu_voice", "")
        assert valid is False
        assert "not in allowed values" in error.lower()

    def test_whitespace_string_rejected_for_menu_voice(self, servicer):
        """Test that whitespace string is rejected for menu_voice."""
        valid, error = servicer.validate_setting_value("menu_voice", "  ")
        assert valid is False
        assert "not in allowed values" in error.lower()

    def test_empty_list_valid_for_random_modes(self, servicer):
        """Test that empty list is valid for random_modes."""
        valid, error = servicer.validate_setting_value("random_modes", [])
        assert valid is True
        assert error == ""

    def test_excluded_games_rejected_in_random_modes(self, servicer):
        """Test that JoustTeams is rejected in random_modes."""
        valid, error = servicer.validate_setting_value("random_modes", ["JoustTeams"])
        assert valid is False
        assert "invalid game mode" in error.lower()

    def test_random_rejected_in_random_modes(self, servicer):
        """Test that Random is rejected in random_modes."""
        valid, error = servicer.validate_setting_value("random_modes", ["Random"])
        assert valid is False
        assert "invalid game mode" in error.lower()


@pytest.mark.asyncio
class TestMultipleSubscribers:
    """Test multiple subscriber behavior."""

    @pytest.fixture
    def servicer(self):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        servicer = SettingsServicer(settings_file=path)
        yield servicer
        if os.path.exists(path):
            os.remove(path)
        if os.path.exists(path + ".tmp"):
            os.remove(path + ".tmp")

    async def test_three_subscribers_all_receive_event(self, servicer):
        """Test that all three subscribers receive a published event."""
        queues = []
        for i in range(3):
            q = asyncio.Queue(maxsize=10)
            servicer.subscribers[f"sub_{i}"] = q
            queues.append(q)

        await servicer.publish_change("sensitivity", 2, 3, "test")

        for i, q in enumerate(queues):
            assert not q.empty(), f"Subscriber sub_{i} did not receive event"
            event = await q.get()
            assert event.key == "sensitivity"
            assert event.old_value == "2"
            assert event.new_value == "3"

    async def test_broken_subscriber_cleaned_up(self, servicer):
        """Test that a broken subscriber is removed after publish."""
        good_queue = asyncio.Queue(maxsize=10)
        servicer.subscribers["good"] = good_queue

        broken_queue = Mock()
        broken_queue.put_nowait = Mock(side_effect=Exception("broken"))
        servicer.subscribers["broken"] = broken_queue

        await servicer.publish_change("sensitivity", 2, 3, "test")

        assert "broken" not in servicer.subscribers
        assert "good" in servicer.subscribers
        assert not good_queue.empty()
        event = await good_queue.get()
        assert event.key == "sensitivity"

    async def test_subscriber_removed_after_disconnect(self, servicer):
        """Test that publishing after subscriber removal causes no error."""
        q = asyncio.Queue(maxsize=10)
        servicer.subscribers["temp"] = q

        del servicer.subscribers["temp"]

        await servicer.publish_change("sensitivity", 2, 3, "test")
        assert "temp" not in servicer.subscribers


@pytest.mark.asyncio
class TestSubscribeToChangesStreaming:
    """Test the SubscribeToChanges streaming RPC."""

    @pytest.fixture
    def servicer(self):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        servicer = SettingsServicer(settings_file=path)
        yield servicer
        if os.path.exists(path):
            os.remove(path)
        if os.path.exists(path + ".tmp"):
            os.remove(path + ".tmp")

    async def test_async_generator_yields_events(self, servicer):
        """Test that SubscribeToChanges yields events from the queue."""
        call_count = 0

        mock_context = Mock()

        def mock_cancelled():
            nonlocal call_count
            call_count += 1
            return call_count > 1

        mock_context.cancelled = mock_cancelled

        request = settings_pb2.SubscribeRequest()

        async def inject_event():
            for _ in range(50):
                if len(servicer.subscribers) > 0:
                    sub_id = next(iter(servicer.subscribers))
                    event = settings_pb2.SettingChangeEvent(
                        key="sensitivity",
                        old_value="2",
                        new_value="3",
                        source="test",
                    )
                    servicer.subscribers[sub_id].put_nowait(event)
                    return
                await asyncio.sleep(0.02)

        task = asyncio.create_task(inject_event())

        events = []
        async for event in servicer.SubscribeToChanges(request, mock_context):
            events.append(event)
            break

        await task

        assert len(events) == 1
        assert events[0].key == "sensitivity"
        assert events[0].new_value == "3"

    async def test_subscriber_cleaned_up_on_context_cancel(self, servicer):
        """Test subscriber is removed when context is cancelled."""
        mock_context = Mock()
        mock_context.cancelled = Mock(return_value=True)

        request = settings_pb2.SubscribeRequest()

        events = []
        async for event in servicer.SubscribeToChanges(request, mock_context):
            events.append(event)

        assert len(events) == 0
        assert len(servicer.subscribers) == 0


@pytest.mark.asyncio
class TestConcurrentUpdates:
    """Test concurrent update behavior."""

    @pytest.fixture
    def temp_settings_file(self):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        yield path
        if os.path.exists(path):
            os.remove(path)
        if os.path.exists(path + ".tmp"):
            os.remove(path + ".tmp")

    @pytest.fixture
    def servicer(self, temp_settings_file):
        return SettingsServicer(settings_file=temp_settings_file)

    @pytest.fixture
    def mock_context(self):
        context = Mock()
        context.is_active = Mock(return_value=True)
        context.cancelled = Mock(return_value=False)
        return context

    async def test_concurrent_updates_all_succeed(self, servicer, mock_context):
        """Test that concurrent updates all succeed."""
        values = ["0", "1", "2", "3", "4"]

        async def update(val):
            request = settings_pb2.UpdateSettingRequest(key="sensitivity", value=val, source="test")
            return await servicer.UpdateSetting(request, mock_context)

        responses = await asyncio.gather(*[update(v) for v in values])

        for resp in responses:
            assert resp.success is True

        assert servicer.settings["sensitivity"] in [0, 1, 2, 3, 4]

    async def test_event_published_for_each_update(self, servicer, mock_context):
        """Test that each sequential update publishes an event."""
        sub_queue = asyncio.Queue()
        servicer.subscribers["test"] = sub_queue

        values = ["0", "3", "4"]
        for val in values:
            request = settings_pb2.UpdateSettingRequest(key="sensitivity", value=val, source="test")
            await servicer.UpdateSetting(request, mock_context)

        assert sub_queue.qsize() == 3

        events = []
        while not sub_queue.empty():
            events.append(await sub_queue.get())

        assert events[0].key == "sensitivity"
        assert events[0].new_value == "0"
        assert events[1].new_value == "3"
        assert events[2].new_value == "4"
