"""
Feature Flag Wrapper for JoustMania
Integrates OpenFeature with flagd provider using domain-scoped providers.

Supports multiple flag domains (performance, game_settings, user_preferences)
via flagd's flagSetId-based domain scoping. Each domain maps to a separate
flag file and OpenFeature provider.

Implements three-layer evaluation context merging (Issue #422):
  1. API-level: service_name, service_namespace, language, environment, hostname (set once at startup)
  2. Transaction: game_mode, controller_count (set per game session)
  3. Per-evaluation: player-specific attributes (set per flag call)

OpenFeature merges these layers automatically during evaluation, with
more specific layers taking precedence.
"""

import logging
import os
import platform

from openfeature import api
from openfeature.contrib.hook.opentelemetry import TracingHook
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType
from openfeature.evaluation_context import EvaluationContext
from openfeature.transaction_context import ContextVarsTransactionContextPropagator

logger = logging.getLogger(__name__)

# Track initialized domains to avoid re-initialization
_initialized_domains: set[str] = set()
_hooks_registered: bool = False

# Guard for one-time propagator and API-level context registration
_propagator_registered: bool = False


def _init_global_context() -> None:
    """
    Register the transaction context propagator and set API-level context.

    Called once on first domain initialization. Sets up:
    - ContextVarsTransactionContextPropagator for async-safe transaction context
    - API-level evaluation context with service identity attributes
    """
    global _propagator_registered
    if _propagator_registered:
        return

    # Register propagator for contextvars-based transaction context
    api.set_transaction_context_propagator(ContextVarsTransactionContextPropagator())
    logger.info("Registered ContextVarsTransactionContextPropagator")

    # Set API-level context with service identity (always available to all evaluations)
    service_name = os.environ.get("OTEL_SERVICE_NAME", "unknown")
    service_namespace = os.environ.get("OTEL_SERVICE_NAMESPACE", "unknown")
    environment = os.environ.get("ENVIRONMENT", "production")
    hostname = platform.node()

    api.set_evaluation_context(
        EvaluationContext(
            attributes={
                "service_name": service_name,
                "service_namespace": service_namespace,
                "language": "python",
                "environment": environment,
                "hostname": hostname,
            }
        )
    )
    logger.info(
        f"API-level evaluation context set: service_name={service_name}, "
        f"service_namespace={service_namespace}, language=python, "
        f"environment={environment}, hostname={hostname}"
    )

    _propagator_registered = True


def init_flag_domain(domain: str) -> None:
    """
    Initialize an OpenFeature domain with a flagd provider.

    The domain name doubles as the flagSetId selector -- each flag file must
    have ``"metadata": {"flagSetId": "<domain>"}`` so flagd routes flags to
    the correct provider.

    On first call, also registers the transaction context propagator and
    sets API-level evaluation context.

    Args:
        domain: Domain name and flagSetId (e.g., "game_settings")
    """
    # Initialize global context on first domain init
    _init_global_context()

    if domain in _initialized_domains:
        logger.debug(f"Domain '{domain}' already initialized, skipping")
        return

    global _hooks_registered
    if not _hooks_registered:
        api.add_hooks([TracingHook()])
        _hooks_registered = True
        logger.info("Registered OpenFeature TracingHook for OTEL integration")

    flagd_host = os.environ.get("FLAGD_HOST", "flagd")
    flagd_port = int(os.environ.get("FLAGD_PORT", "8015"))

    try:
        logger.info(f"Initializing OpenFeature domain '{domain}' at {flagd_host}:{flagd_port}")
        provider = FlagdProvider(
            host=flagd_host,
            port=flagd_port,
            resolver_type=ResolverType.IN_PROCESS,
            selector=f"flagSetId={domain}",
            keep_alive_time=600000,  # 10min — flagd's Go gRPC server enforces MinPingInterval=5min;
            # 30s pings triggered GOAWAY(ENHANCE_YOUR_CALM) → gRPC C-core epoll1 crash on ARM
        )
        api.set_provider(provider, domain=domain)
        _initialized_domains.add(domain)
    except Exception as e:
        logger.error(f"Failed to initialize domain '{domain}': {e}")


async def wait_for_provider_ready(domain: str, deadline_seconds: float = 5.0) -> bool:
    """Wait for a domain's provider to reach READY status.

    Uses an asyncio.Event triggered by the PROVIDER_READY handler.
    Returns True if ready within deadline, False otherwise.

    Args:
        domain: OpenFeature domain name
        deadline_seconds: Maximum seconds to wait (default 5.0)
    """
    import asyncio

    from openfeature.provider import ProviderEvent, ProviderStatus

    # Already ready?
    status = api.provider_registry.get_provider_status(domain)
    if status == ProviderStatus.READY:
        logger.debug(f"Domain '{domain}' provider already READY")
        return True

    ready_event = asyncio.Event()

    def _on_ready(_event_details):
        ready_event.set()

    client = api.get_client(domain=domain)
    client.add_handler(ProviderEvent.PROVIDER_READY, _on_ready)

    try:
        async with asyncio.timeout(deadline_seconds):
            await ready_event.wait()
        logger.info(f"Domain '{domain}' provider is READY")
        return True
    except TimeoutError:
        logger.warning(f"Domain '{domain}' provider not ready after {deadline_seconds}s")
        return False


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


def set_game_transaction_context(
    game_mode: str,
    controller_count: int,
    sensitivity: int | None = None,
) -> None:
    """
    Set transaction-level evaluation context for the current game session.

    This context is automatically merged into all flag evaluations within
    the current async context (coroutine/task). Uses contextvars so each
    concurrent game session gets its own context.

    Args:
        game_mode: Game mode name (e.g., "FFA", "Werewolf")
        controller_count: Number of controllers/players in the session
        sensitivity: Optional sensitivity level (0-4)
    """
    attributes: dict = {
        "game_mode": game_mode,
        "controller_count": controller_count,
    }
    if sensitivity is not None:
        attributes["sensitivity"] = sensitivity

    api.set_transaction_context(
        EvaluationContext(
            targeting_key=game_mode,
            attributes=attributes,
        )
    )
    logger.debug(
        f"Transaction context set: game_mode={game_mode}, "
        f"controller_count={controller_count}, sensitivity={sensitivity}"
    )
