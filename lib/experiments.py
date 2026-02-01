"""
A/B Testing Experiment Tracking (Part of #21)

Helper functions for tracking feature flag experiments and measuring results.
"""

import logging
from typing import Any

from openfeature.evaluation_context import EvaluationContext

logger = logging.getLogger(__name__)


def track_experiment_assignment(
    experiment_name: str,
    variant: str,
    context: EvaluationContext | None = None,
) -> None:
    """
    Track that a player was assigned to an experiment variant.

    Args:
        experiment_name: Name of the experiment (e.g., "aggressive_thresholds")
        variant: Variant name (e.g., "control", "treatment")
        context: Optional evaluation context with player info
    """
    try:
        # Import here to avoid circular dependency
        from services.game_coordinator import metrics

        metrics.experiment_variant_assigned.labels(
            experiment=experiment_name,
            variant=variant,
        ).inc()

        if context and hasattr(context, "targeting_key"):
            logger.info(
                f"Experiment assignment: {context.targeting_key} → "
                f"{experiment_name}={variant}"
            )
        else:
            logger.info(f"Experiment assignment: {experiment_name}={variant}")

    except Exception as e:
        # Don't fail the game if experiment tracking fails
        logger.warning(f"Failed to track experiment assignment: {e}")


def evaluate_experiment_flag(
    flag_client: Any,
    experiment_name: str,
    default_value: Any,
    context: EvaluationContext | None = None,
) -> tuple[Any, str]:
    """
    Evaluate an experiment flag and track the assignment.

    Args:
        flag_client: FeatureFlagClient instance
        experiment_name: Name of the experiment flag
        default_value: Default value if flag evaluation fails
        context: Evaluation context with player info

    Returns:
        Tuple of (flag_value, variant_name)

    Example:
        >>> from lib.feature_flags import get_feature_flag_client
        >>> from lib.experiments import evaluate_experiment_flag
        >>>
        >>> client = get_feature_flag_client()
        >>> enabled, variant = evaluate_experiment_flag(
        ...     client,
        ...     "experiment_high_frequency_rollout",
        ...     False,
        ...     player_context
        ... )
        >>> if enabled:
        ...     update_frequency = 60
    """
    try:
        # Get the flag value based on type
        if isinstance(default_value, bool):
            value = flag_client.get_boolean_value(experiment_name, default_value, context)
        elif isinstance(default_value, int):
            value = flag_client.get_integer_value(experiment_name, default_value, context)
        elif isinstance(default_value, float):
            value = flag_client.get_float_value(experiment_name, default_value, context)
        elif isinstance(default_value, str):
            value = flag_client.get_string_value(experiment_name, default_value, context)
        else:
            value = flag_client.get_object_value(experiment_name, default_value, context)

        # Determine variant name
        # In a real implementation, we'd get this from the evaluation details
        # For now, use a simple heuristic
        variant = "control" if value == default_value else "treatment"

        # Track the assignment
        track_experiment_assignment(experiment_name, variant, context)

        return value, variant

    except Exception as e:
        logger.warning(f"Failed to evaluate experiment flag {experiment_name}: {e}")
        return default_value, "control"


def update_active_participants(experiment_name: str, count: int) -> None:
    """
    Update the gauge tracking active participants in an experiment.

    Args:
        experiment_name: Name of the experiment
        count: Current number of active participants
    """
    try:
        from services.game_coordinator import metrics

        metrics.experiment_active_participants.labels(
            experiment=experiment_name
        ).set(count)
    except Exception as e:
        logger.warning(f"Failed to update experiment participants: {e}")
