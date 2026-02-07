"""
Feature Flag Wrapper for JoustMania
Integrates OpenFeature with flagd provider using domain-scoped providers.

Supports multiple flag domains (performance, game_settings, user_preferences)
via flagd's flagSetId-based domain scoping. Each domain maps to a separate
flag file and OpenFeature provider.
"""

import logging
import os
from typing import Any

from openfeature import api
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType
from openfeature.evaluation_context import EvaluationContext

logger = logging.getLogger(__name__)

# Track initialized domains to avoid re-initialization
_initialized_domains: set[str] = set()


def init_flag_domain(domain: str, flag_set_id: str) -> None:
    """
    Initialize an OpenFeature domain with a flagd provider scoped to a flagSetId.

    Each domain gets its own provider that only sees flags with the matching
    flagSetId in their metadata. This allows multiple flag files to coexist.

    Args:
        domain: OpenFeature domain name (e.g., "game_settings")
        flag_set_id: flagd flagSetId to scope to (e.g., "game_settings")
    """
    if domain in _initialized_domains:
        logger.debug(f"Domain '{domain}' already initialized, skipping")
        return

    flagd_host = os.environ.get("FLAGD_HOST", "flagd")
    flagd_port = int(os.environ.get("FLAGD_PORT", "8015"))

    try:
        logger.info(
            f"Initializing OpenFeature domain '{domain}' (flagSetId={flag_set_id}) at {flagd_host}:{flagd_port}"
        )
        provider = FlagdProvider(
            host=flagd_host,
            port=flagd_port,
            resolver_type=ResolverType.IN_PROCESS,
            selector=f"flagSetId={flag_set_id}",
        )
        api.set_provider(provider, domain=domain)
        _initialized_domains.add(domain)
    except Exception as e:
        logger.error(f"Failed to initialize domain '{domain}': {e}")


def get_flag_client(domain: str):
    """
    Get an OpenFeature client for a specific domain.

    The client will only evaluate flags from the flag file whose metadata
    contains the matching flagSetId.

    Args:
        domain: OpenFeature domain name

    Returns:
        OpenFeature client for the domain
    """
    return api.get_client(domain=domain)


# ---------------------------------------------------------------------------
# Legacy backwards-compatible wrapper for the "performance" domain.
# Used by game_coordinator/runtime_config.py which was written before
# domain-scoped providers were introduced.
# ---------------------------------------------------------------------------


class FeatureFlagClient:
    """
    Backwards-compatible wrapper around OpenFeature SDK.

    Initializes the "performance" domain and provides simplified access
    to feature flags. Existing callers (RuntimeConfigManager) continue
    to work without changes.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        init_flag_domain("performance", "performance")
        self.client = get_flag_client("performance")

    def get_boolean_value(self, flag_key: str, default_value: bool, context: EvaluationContext | None = None) -> bool:
        """Evaluate a boolean flag."""
        return self.client.get_boolean_value(flag_key, default_value, context)

    def get_string_value(self, flag_key: str, default_value: str, context: EvaluationContext | None = None) -> str:
        """Evaluate a string flag."""
        return self.client.get_string_value(flag_key, default_value, context)

    def get_integer_value(self, flag_key: str, default_value: int, context: EvaluationContext | None = None) -> int:
        """Evaluate an integer flag."""
        return self.client.get_integer_value(flag_key, default_value, context)

    def get_float_value(self, flag_key: str, default_value: float, context: EvaluationContext | None = None) -> float:
        """Evaluate a float flag."""
        return self.client.get_float_value(flag_key, default_value, context)

    def get_object_value(self, flag_key: str, default_value: Any, context: EvaluationContext | None = None) -> Any:
        """Evaluate an object flag."""
        return self.client.get_object_value(flag_key, default_value, context)


# Global instance
_feature_flag_client = None


def get_feature_flag_client() -> FeatureFlagClient:
    """Get or create the global feature flag client instance."""
    global _feature_flag_client
    if _feature_flag_client is None:
        _feature_flag_client = FeatureFlagClient()
    return _feature_flag_client
