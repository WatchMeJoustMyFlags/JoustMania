"""
Tests for A/B testing experiment tracking (Part of #21).
"""

from unittest.mock import MagicMock, patch

from openfeature.evaluation_context import EvaluationContext

from lib.experiments import evaluate_experiment_flag, track_experiment_assignment, update_active_participants


@patch("services.game_coordinator.metrics.experiment_variant_assigned")
def test_track_experiment_assignment(mock_counter):
    """Test that experiment assignments are tracked."""
    track_experiment_assignment("test_experiment", "treatment")

    mock_counter.labels.assert_called_once_with(
        experiment="test_experiment",
        variant="treatment",
    )
    mock_counter.labels().inc.assert_called_once()


@patch("services.game_coordinator.metrics.experiment_variant_assigned")
def test_track_experiment_assignment_with_context(mock_counter):
    """Test that experiment assignments are tracked with player context."""
    context = EvaluationContext(
        targeting_key="AA:BB:CC:DD:EE:FF",
        attributes={"serial": "AA:BB:CC:DD:EE:FF"},
    )

    track_experiment_assignment("test_experiment", "control", context)

    mock_counter.labels.assert_called_once_with(
        experiment="test_experiment",
        variant="control",
    )
    mock_counter.labels().inc.assert_called_once()


@patch("services.game_coordinator.metrics.experiment_variant_assigned")
def test_evaluate_experiment_flag_boolean(_mock_counter):
    """Test evaluating a boolean experiment flag."""
    mock_client = MagicMock()
    mock_client.get_boolean_value.return_value = True

    value, variant = evaluate_experiment_flag(
        mock_client,
        "experiment_high_frequency_rollout",
        False,  # default
        None,
    )

    assert value is True
    assert variant == "treatment"  # Non-default value
    mock_client.get_boolean_value.assert_called_once_with(
        "experiment_high_frequency_rollout",
        False,
        None,
    )


@patch("services.game_coordinator.metrics.experiment_variant_assigned")
def test_evaluate_experiment_flag_float(_mock_counter):
    """Test evaluating a float experiment flag."""
    mock_client = MagicMock()
    mock_client.get_float_value.return_value = -0.2  # treatment

    value, variant = evaluate_experiment_flag(
        mock_client,
        "experiment_aggressive_thresholds",
        0.0,  # default
        None,
    )

    assert value == -0.2
    assert variant == "treatment"
    mock_client.get_float_value.assert_called_once_with(
        "experiment_aggressive_thresholds",
        0.0,
        None,
    )


@patch("services.game_coordinator.metrics.experiment_variant_assigned")
def test_evaluate_experiment_flag_control(_mock_counter):
    """Test evaluating a flag that returns control variant."""
    mock_client = MagicMock()
    mock_client.get_boolean_value.return_value = False  # Same as default

    value, variant = evaluate_experiment_flag(
        mock_client,
        "experiment_test",
        False,  # default
        None,
    )

    assert value is False
    assert variant == "control"  # Matches default


@patch("services.game_coordinator.metrics.experiment_variant_assigned")
def test_evaluate_experiment_flag_error_fallback(_mock_counter):
    """Test that evaluation errors fall back to control."""
    mock_client = MagicMock()
    mock_client.get_boolean_value.side_effect = Exception("flagd error")

    value, variant = evaluate_experiment_flag(
        mock_client,
        "experiment_test",
        False,  # default
        None,
    )

    assert value is False
    assert variant == "control"


@patch("services.game_coordinator.metrics.experiment_active_participants")
def test_update_active_participants(mock_gauge):
    """Test updating active participants gauge."""
    update_active_participants("test_experiment", 5)

    mock_gauge.labels.assert_called_once_with(experiment="test_experiment")
    mock_gauge.labels().set.assert_called_once_with(5)
