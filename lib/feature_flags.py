"""
Feature Flag Wrapper for JoustMania
Integrates OpenFeature with flagd provider using domain-scoped providers.

Supports multiple flag domains (system, controller, game, user, observability,
and others) via flagd's flagSetId-based domain scoping. Each domain maps to a
separate flag file and OpenFeature provider.

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
from openfeature.flag_evaluation import FlagEvaluationDetails, Reason
from openfeature.transaction_context import ContextVarsTransactionContextPropagator

from lib.flag_eval_visibility import record_flag_fallback

logger = logging.getLogger(__name__)


def _report_details_error(flag_key: str, details: FlagEvaluationDetails) -> bool:
    """Surface a non-raising error resolution and report it if present.

    OpenFeature's ``get_*_details`` never raises for a resolution error
    (TYPE_MISMATCH, FLAG_NOT_FOUND, PROVIDER_NOT_READY, ...): it returns the
    default value with ``reason == Reason.ERROR`` and a populated
    ``error_code``. The old value-only getters could not see these, so they
    surfaced as silent defaults (#921). Returns True when an error reason was
    found (and reported via :func:`record_flag_fallback`).
    """
    if details.reason == Reason.ERROR:
        error_code = getattr(details.error_code, "value", None) or str(details.error_code or "ERROR")
        record_flag_fallback(flag_key, error_code, details.error_message or "")
        return True
    return False


def _report_exception(flag_key: str, exc: Exception) -> None:
    """Report a raised evaluation exception as a fallback, classified by type."""
    record_flag_fallback(flag_key, type(exc).__name__, str(exc))


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
        domain: Domain name and flagSetId (e.g., "game")
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


def _calibration_context(game_id: str | None) -> EvaluationContext:
    """Build the EvaluationContext for an init-time calibration read (#838).

    A non-empty ``game_id`` is added as the ``gameId`` attribute so flagd
    targeting rules can vary calibration per game (A/B shadow experiments via
    flag config alone). ``None``/empty adds nothing, so an un-targeted
    calibration flag resolves IDENTICALLY to today — a pure context addition
    with no behavior change unless a targeting rule references ``gameId``.
    """
    if game_id:
        return EvaluationContext(attributes={"gameId": game_id})
    return EvaluationContext()


def read_object_flag(domain: str, flag_key: str, default: dict, game_id: str | None = None) -> dict:
    """
    Read an object-typed flag once at init time, with a hardcoded fallback.

    Init-frozen read helper for calibration flags (#766): the value is fetched
    once (e.g. in a game mode ``__init__``) and never re-evaluated mid-game.
    The domain is initialized lazily on first use, so callers do not need a
    pre-wired client. On ANY failure (provider not ready, missing flag,
    evaluation error) the supplied ``default`` is returned unchanged so the
    promotion stays behavior-neutral.

    Args:
        domain: OpenFeature domain / flagSetId (e.g. "game")
        flag_key: Object flag key (e.g. "thresholds")
        default: Fallback value returned on any error or missing flag
        game_id: Optional owning game id (#838); added as the ``gameId`` context
            attribute so flagd targeting can vary calibration per game. Omitted
            from the context when ``None``/empty — un-targeted reads are
            unchanged.

    Returns:
        The flag's object value, or ``default`` on failure.
    """
    try:
        init_flag_domain(domain)
        client = get_flag_client(domain)
        details = client.get_object_details(flag_key, default, _calibration_context(game_id))
        if _report_details_error(flag_key, details):
            return default
        value = details.value
        # The resolved value may still be the default sentinel; either is fine.
        return value if isinstance(value, dict) else default
    except Exception as e:
        _report_exception(flag_key, e)
        return default


def read_object_flag_variant(domain: str, flag_key: str, targeting_key: str, default: dict) -> dict:
    """
    Read an object-typed flag selecting a named variant via flagd targeting.

    Live sibling of :func:`read_object_flag` for selecting a *named* preset
    (#766 F6 ``pacing_profile``): the flag's targeting rule maps
    ``targetingKey == <name>`` to the matching variant, so passing
    ``targeting_key`` resolves that preset's value. Unknown names fall through
    to the flag's default variant (the flag's own targeting ``else``). On ANY
    failure the supplied ``default`` is returned.

    Args:
        domain: OpenFeature domain / flagSetId (e.g. "game")
        flag_key: Object flag key (e.g. "windows")
        targeting_key: Preset/variant selector (e.g. "calm" / "frantic")
        default: Fallback value returned on any error or missing flag

    Returns:
        The selected variant's object value, or ``default`` on failure.
    """
    try:
        init_flag_domain(domain)
        client = get_flag_client(domain)
        details = client.get_object_details(flag_key, default, EvaluationContext(targeting_key=targeting_key))
        if _report_details_error(flag_key, details):
            return default
        value = details.value
        return value if isinstance(value, dict) else default
    except Exception as e:
        _report_exception(flag_key, e)
        return default


def read_float_flag(domain: str, flag_key: str, default: float, game_id: str | None = None) -> float:
    """
    Read a number-typed flag once at init time, with a hardcoded fallback.

    Init-frozen scalar sibling of :func:`read_object_flag` (#766 F2): the value
    is fetched once (e.g. in a game mode ``__init__``) and never re-evaluated
    mid-game. The domain is initialized lazily on first use. On ANY failure
    (provider not ready, missing flag, evaluation error, non-numeric value) the
    supplied ``default`` is returned so the promotion stays behavior-neutral.

    Note: range/sanity validation is the caller's responsibility (it owns the
    semantics, e.g. "positive duration" or "fraction in (0,1)").

    Args:
        domain: OpenFeature domain / flagSetId (e.g. "game")
        flag_key: Number flag key (e.g. "death_grace_period_seconds")
        default: Fallback value returned on any error or missing flag
        game_id: Optional owning game id (#838); added as ``gameId`` context.

    Returns:
        The flag's float value, or ``default`` on failure.
    """
    try:
        init_flag_domain(domain)
        client = get_flag_client(domain)
        # Number flags must be read with the float getter: the flagd RPC
        # resolver returns TYPE_MISMATCH for get_object_value() on a numeric
        # flag, silently dropping every calibration override to its default
        # (e.g. the tournament/fight_club CI variants never took effect, #903).
        details = client.get_float_details(flag_key, default, _calibration_context(game_id))
        if _report_details_error(flag_key, details):
            return default
        value = details.value
        # bool is a subclass of int/float; reject it as a numeric flag value.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(value)
    except Exception as e:
        _report_exception(flag_key, e)
        return default


def read_string_flag(domain: str, flag_key: str, default: str) -> str:
    """
    Read a string-typed flag live, with a hardcoded fallback.

    Unlike the init-frozen ``read_*`` helpers, this is a per-call read suitable
    for policy gates that must honor live flag changes (#837 ``shadow_policy``).
    flagd's in-process resolver evaluates against the current flag set on every
    call, so each invocation reflects the latest value. On ANY failure (provider
    not ready, missing flag, evaluation error, non-string value) the supplied
    ``default`` is returned so the gate stays behavior-neutral.

    Args:
        domain: OpenFeature domain / flagSetId (e.g. "game")
        flag_key: String flag key (e.g. "shadow_policy")
        default: Fallback value returned on any error or missing flag

    Returns:
        The flag's string value, or ``default`` on failure.
    """
    try:
        init_flag_domain(domain)
        client = get_flag_client(domain)
        details = client.get_string_details(flag_key, default, EvaluationContext())
        if _report_details_error(flag_key, details):
            return default
        value = details.value
        return value if isinstance(value, str) else default
    except Exception as e:
        _report_exception(flag_key, e)
        return default


def read_int_flag(domain: str, flag_key: str, default: int, game_id: str | None = None) -> int:
    """
    Read an integer-typed flag once at init time, with a hardcoded fallback.

    Init-frozen scalar sibling of :func:`read_object_flag` (#766 F2). Same
    semantics as :func:`read_float_flag` but coerces to ``int``; non-integral
    numeric values (e.g. 2.5) are rejected in favour of the default to avoid
    surprising truncation. Range/sanity validation is the caller's job.

    Args:
        domain: OpenFeature domain / flagSetId (e.g. "game")
        flag_key: Integer flag key (e.g. "zombie.initial_count")
        default: Fallback value returned on any error or missing flag
        game_id: Optional owning game id (#838); added as ``gameId`` context.

    Returns:
        The flag's int value, or ``default`` on failure.
    """
    try:
        init_flag_domain(domain)
        client = get_flag_client(domain)
        # Number flags must be read with the integer getter: get_object_value()
        # returns TYPE_MISMATCH for a numeric flag under the flagd RPC resolver,
        # silently falling back to the default (#903). Read as a float first so
        # whole-number variants stored as floats (e.g. 2.0) still resolve, then
        # apply the integral-only coercion below.
        details = client.get_float_details(flag_key, float(default), _calibration_context(game_id))
        if _report_details_error(flag_key, details):
            return default
        value = details.value
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return default
    except Exception as e:
        _report_exception(flag_key, e)
        return default


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
