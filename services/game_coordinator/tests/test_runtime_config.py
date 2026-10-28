import json
import logging
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from services.game_coordinator.runtime_config import GamePerformanceConfig, RuntimeConfigManager

SYSTEM_FLAGS = Path(__file__).resolve().parents[3] / "services" / "flagd" / "system.json"


@pytest.mark.nfr("NFR-003")
@pytest.mark.uc("UC-030", flow="main", steps=[2])
def test_runtime_config_defaults():
    """UC-030 step 2 / NFR-003: without flags the game streams at 60 Hz (countdown and rainbow defaults too)."""
    manager = RuntimeConfigManager()
    config = manager.get_config()

    assert isinstance(config, GamePerformanceConfig)
    assert config.update_frequency_hz == 60
    assert config.countdown_phase_duration_ms == 750
    assert config.winner_rainbow_duration_ms == 3000


@pytest.mark.nfr("NFR-003")
@pytest.mark.uc("UC-030", flow="main", steps=[2])
def test_system_flag_default_rate_is_60_hz():
    """UC-030 step 2 / NFR-003: the shipped flag defaults to 60 Hz (variants 15/30/60), matching the code default."""
    flag = json.loads(SYSTEM_FLAGS.read_text())["flags"]["game_loop.update_frequency_hz"]
    assert flag["variants"] == {"low": 15, "medium": 30, "high": 60}
    assert flag["variants"][flag["defaultVariant"]] == 60 == GamePerformanceConfig().update_frequency_hz


def test_runtime_config_no_countdown_duration_seconds():
    """Test that countdown_duration_seconds field no longer exists (Issue #464)."""
    config = GamePerformanceConfig()
    assert not hasattr(config, "countdown_duration_seconds")


@patch("openfeature.api.add_handler")
@patch("lib.feature_flags.get_flag_client")
def test_runtime_config_flag_updates(mock_get_client, mock_add_handler):
    """Test that config updates when flags are evaluated."""
    # Setup mock client (used for all of system, controller and game domains)
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    # Configure mock evaluations
    # get_integer_value is called for: game_loop.update_frequency_hz, poll_drop_threshold,
    # countdown_phase_duration_ms, winner_rainbow_duration_ms
    mock_client.get_integer_value.side_effect = [30, 3, 500, 1000]

    manager = RuntimeConfigManager()
    config = manager.get_config()

    # Verify event handler was registered
    mock_add_handler.assert_called_once()

    # Verify mock was called with correct keys and EvaluationContext
    mock_client.get_integer_value.assert_any_call("game_loop.update_frequency_hz", 60, ANY)
    mock_client.get_integer_value.assert_any_call("poll_drop_threshold", 10, ANY)
    mock_client.get_integer_value.assert_any_call("countdown_phase_duration_ms", 750, ANY)
    mock_client.get_integer_value.assert_any_call("winner_rainbow_duration_ms", 3000, ANY)

    # Verify values were updated
    assert config.update_frequency_hz == 30
    assert config.countdown_phase_duration_ms == 500
    assert config.winner_rainbow_duration_ms == 1000


@patch("openfeature.api.add_handler")
@patch("lib.feature_flags.get_flag_client")
def test_countdown_skip_via_flag(mock_get_client, _mock_add_handler):
    """Test that countdown_phase_duration_ms=0 (skip variant) works via flags."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    # Return 0 for countdown_phase_duration_ms (the "skip" variant)
    mock_client.get_integer_value.side_effect = [60, 3, 0, 3000]

    manager = RuntimeConfigManager()
    config = manager.get_config()

    assert config.countdown_phase_duration_ms == 0


@patch("openfeature.api.add_handler")
def test_runtime_config_flag_error_fallback(_mock_add_handler, caplog):
    """Test that config stays at default if flag evaluation fails."""
    with patch("lib.feature_flags.get_flag_client") as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.get_integer_value.side_effect = Exception("flagd unreachable")

        manager = RuntimeConfigManager()
        with caplog.at_level(logging.WARNING):
            config = manager.get_config()

        assert "Failed to evaluate flags" in caplog.text
        assert config.update_frequency_hz == 60  # Stayed at default
        assert config.countdown_phase_duration_ms == 750  # Stayed at default
        assert config.winner_rainbow_duration_ms == 3000  # Stayed at default


@patch("openfeature.api.add_handler")
@patch("lib.feature_flags.get_flag_client")
def test_on_flags_changed_event(mock_get_client, _mock_add_handler, caplog):
    """Test that _on_flags_changed updates config when event fires."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    # Initial values
    mock_client.get_integer_value.side_effect = [60, 3, 750, 3000]

    manager = RuntimeConfigManager()
    config = manager.get_config()
    assert config.update_frequency_hz == 60
    assert config.countdown_phase_duration_ms == 750

    # Simulate flag change
    mock_client.get_integer_value.side_effect = [30, 3, 500, 1000]

    # Trigger event handler
    mock_event = MagicMock()
    mock_event.flags_changed = ["game_loop.update_frequency_hz", "countdown_phase_duration_ms"]

    with caplog.at_level(logging.INFO):
        manager._on_flags_changed(mock_event)

    # Verify config was updated
    config = manager.get_config()
    assert config.update_frequency_hz == 30
    assert config.countdown_phase_duration_ms == 500
    assert config.winner_rainbow_duration_ms == 1000
    assert "Feature flags changed" in caplog.text


@pytest.mark.uc("UC-053", flow="A9")
@patch("openfeature.api.add_handler")
@patch("lib.feature_flags.get_flag_client")
def test_intervention_only_changes_log_at_debug(mock_get_client, _mock_add_handler, caplog):
    """#1552: every flag-service write re-stamps the interventions set and the
    allow-list, so most pushes name only intervention flags; those are not
    runtime config and stay out of the INFO log."""
    mock_get_client.return_value = MagicMock()
    manager = RuntimeConfigManager()

    event = MagicMock()
    event.flags_changed = ["end_game", "volume_override", "interventions_allowed"]
    with caplog.at_level(logging.INFO, logger="services.game_coordinator.runtime_config"):
        manager._on_flags_changed(event)
    assert "Feature flags changed" not in caplog.text

    event.flags_changed = ["interventions_allowed", "perception.zone_still_max"]
    with caplog.at_level(logging.INFO, logger="services.game_coordinator.runtime_config"):
        manager._on_flags_changed(event)
    assert "Feature flags changed" in caplog.text


@patch("openfeature.api.add_handler")
@patch("lib.feature_flags.get_flag_client")
def test_on_flags_changed_no_flag_list(mock_get_client, _mock_add_handler, caplog):
    """Test that _on_flags_changed works when flags_changed is empty."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_integer_value.side_effect = [45, 3, 750, 3000]

    manager = RuntimeConfigManager()

    # Trigger event handler without flags_changed attribute
    mock_event = MagicMock()
    mock_event.flags_changed = []

    # Reset side_effect for second refresh
    mock_client.get_integer_value.side_effect = [45, 3, 750, 3000]

    with caplog.at_level(logging.INFO):
        manager._on_flags_changed(mock_event)

    assert "unspecified flags" in caplog.text


def test_get_config_manager_singleton():
    """Test get_config_manager returns singleton."""
    from services.game_coordinator.runtime_config import get_config_manager

    manager1 = get_config_manager()
    manager2 = get_config_manager()

    assert manager1 is manager2


def test_setup_feature_flags_import_error(caplog):
    """Test that ImportError in _setup_feature_flags is handled."""
    with patch("lib.feature_flags.get_flag_client", side_effect=ImportError("no module")):
        with caplog.at_level(logging.WARNING):
            manager = RuntimeConfigManager()

        assert manager.system_client is None
        assert manager.game_client is None
        assert "Could not initialize feature flags" in caplog.text


@patch("openfeature.api.add_handler")
def test_setup_feature_flags_generic_error(_mock_add_handler, caplog):
    """Test that generic exceptions in _setup_feature_flags are handled."""
    with patch("lib.feature_flags.get_flag_client", side_effect=RuntimeError("startup failed")):
        with caplog.at_level(logging.ERROR):
            manager = RuntimeConfigManager()

        assert manager.system_client is None
        assert manager.game_client is None
        assert "Failed to initialize feature flags" in caplog.text


@patch("openfeature.api.add_handler")
@patch("lib.feature_flags.get_flag_client")
def test_refresh_from_flags_with_metrics(mock_get_client, _mock_add_handler):
    """Test that _refresh_from_flags tracks metrics on changes."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    # First call returns defaults, second call returns changed values
    # Each refresh evaluates: update_frequency_hz, poll_drop_threshold,
    # countdown_phase_duration_ms, winner_rainbow_duration_ms
    mock_client.get_integer_value.side_effect = [60, 3, 750, 3000, 45, 5, 500, 1000]

    with (
        patch("services.game_coordinator.metrics.config_changes_total") as mock_changes,
        patch("services.game_coordinator.metrics.current_update_frequency_hz") as mock_gauge,
    ):
        manager = RuntimeConfigManager()

        # Verify initial setup called metrics
        assert mock_gauge.set.called

        # Trigger another refresh with different values
        manager._refresh_from_flags()

        # Should track config changes for all changed parameters
        mock_changes.labels.assert_any_call(parameter="update_frequency_hz")
        mock_changes.labels.assert_any_call(parameter="countdown_phase_duration_ms")
        mock_changes.labels.assert_any_call(parameter="winner_rainbow_duration_ms")


@patch("openfeature.api.add_handler")
@patch("lib.feature_flags.get_flag_client")
def test_on_flags_changed_with_metrics(mock_get_client, _mock_add_handler):
    """Test that _on_flags_changed increments metrics."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_integer_value.side_effect = [60, 3, 750, 3000]

    with patch("services.game_coordinator.metrics.flag_configuration_changes_total") as mock_counter:
        manager = RuntimeConfigManager()

        # Reset for next refresh
        mock_client.get_integer_value.side_effect = [60, 3, 750, 3000]

        # Trigger event
        mock_event = MagicMock()
        mock_event.flags_changed = ["some_flag"]
        manager._on_flags_changed(mock_event)

        # Should increment counter
        mock_counter.inc.assert_called()


def test_refresh_from_flags_no_client():
    """Test that _refresh_from_flags does nothing when system_client is None."""
    manager = RuntimeConfigManager()
    manager.system_client = None

    # Should not raise exception
    manager._refresh_from_flags()


@patch("openfeature.api.add_handler")
@patch("lib.feature_flags.get_flag_client")
def test_game_flags_read_on_init(mock_get_client, _mock_add_handler):
    """Test that game domain flags are read during initialization."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    mock_client.get_integer_value.side_effect = [60, 3, 500, 5000]

    manager = RuntimeConfigManager()
    config = manager.get_config()

    # Both game domain flags should be read
    assert config.countdown_phase_duration_ms == 500
    assert config.winner_rainbow_duration_ms == 5000


@patch("openfeature.api.add_handler")
@patch("lib.feature_flags.get_flag_client")
def test_game_client_initialized(mock_get_client, _mock_add_handler):
    """Test that game_client is initialized alongside the system and controller clients."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_integer_value.side_effect = [60, 3, 750, 3000]

    manager = RuntimeConfigManager()

    assert manager.system_client is not None
    assert manager.controller_client is not None
    assert manager.game_client is not None


def test_runtime_config_flag_keys_match_the_keys_read():
    """RUNTIME_CONFIG_FLAG_KEYS must list every exact key _refresh_from_flags reads (#1552)."""
    import inspect
    import re

    from services.game_coordinator.runtime_config import RUNTIME_CONFIG_FLAG_KEYS

    source = inspect.getsource(RuntimeConfigManager._refresh_from_flags)
    read_keys = set(re.findall(r'get_\w+_value\(\s*"([^"]+)"', source))
    assert read_keys == set(RUNTIME_CONFIG_FLAG_KEYS)


@pytest.mark.nfr("NFR-003")
@pytest.mark.parametrize("bad", [0, -5, float("nan"), float("inf"), "fast", None])
@patch("openfeature.api.add_handler")
@patch("lib.feature_flags.get_flag_client")
def test_invalid_update_frequency_falls_back_to_60(mock_get_client, _mock_add_handler, bad, caplog):
    """#2356: a non-positive, non-finite or non-numeric rate falls back to 60 Hz (no division by zero)."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_integer_value.side_effect = lambda key, default, _ctx: (
        bad if key == "game_loop.update_frequency_hz" else default
    )

    with caplog.at_level(logging.WARNING):
        manager = RuntimeConfigManager()
        manager._refresh_from_flags()

    assert manager.get_config().update_frequency_hz == 60
    warnings = [r for r in caplog.records if "update_frequency_hz" in r.getMessage() and r.levelno == logging.WARNING]
    assert len(warnings) == 1
