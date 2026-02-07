"""
Feature Flag Wrapper for JoustMania
Integrates OpenFeature with flagd provider using domain-scoped providers.

Supports multiple flag domains (performance, game_settings, user_preferences)
via flagd's flagSetId-based domain scoping. Each domain maps to a separate
flag file and OpenFeature provider.
"""

import logging
import os

from openfeature import api
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType

logger = logging.getLogger(__name__)

# Track initialized domains to avoid re-initialization
_initialized_domains: set[str] = set()


def init_flag_domain(domain: str) -> None:
    """
    Initialize an OpenFeature domain with a flagd provider.

    The domain name doubles as the flagSetId selector — each flag file must
    have ``"metadata": {"flagSetId": "<domain>"}`` so flagd routes flags to
    the correct provider.

    Args:
        domain: Domain name and flagSetId (e.g., "game_settings")
    """
    if domain in _initialized_domains:
        logger.debug(f"Domain '{domain}' already initialized, skipping")
        return

    flagd_host = os.environ.get("FLAGD_HOST", "flagd")
    flagd_port = int(os.environ.get("FLAGD_PORT", "8015"))

    try:
        logger.info(f"Initializing OpenFeature domain '{domain}' at {flagd_host}:{flagd_port}")
        provider = FlagdProvider(
            host=flagd_host,
            port=flagd_port,
            resolver_type=ResolverType.IN_PROCESS,
            selector=f"flagSetId={domain}",
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
