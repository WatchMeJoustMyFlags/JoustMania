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


# Shadow/real split for the agent's game-flag experiments (#931/#932). These
# MUST stay byte-identical to services/agent/experiment/targeting.go's
# GameKindReal ("real") — the experiment writer scopes targeting on
# {"!=": [{"var":"game_kind"}, "real"]}, so anything that is NOT "real" resolves
# the experimental variant. "real" is therefore the protected, fail-safe value.
GAME_KIND_VAR = "game_kind"
GAME_KIND_REAL = "real"
GAME_KIND_SHADOW = "shadow"

# Experiment/cohort attribution (#975, epic #982). Two finer-grained labels
# WITHIN the shadow game_kind: which experiment a shadow game belongs to and
# which arm (treatment) it is in. These mirror the game_kind dual-path exactly
# (eval-context var -> telemetry attrs/labels -> agent ingest), but they are
# NEVER a replacement for the real/shadow safety bit — every experiment game is
# still a shadow game in the game_kind sense.
#
# Real-by-default carries over for free: the API-level context has NO
# experiment_id, so a non-experiment (real or unlabeled shadow) game's
# experiment targeting condition is false by construction — an absent
# experiment_id means "not in any experiment", exactly how an absent game_kind
# defaults to the protected "real". These constants MUST stay byte-identical to
# services/agent/experiment/targeting.go's experiment-scoped JSONLogic (#977).
EXPERIMENT_ID_VAR = "experiment_id"  # absent => not in any experiment
ARM_VAR = "arm"  # "experimental" | "control"
ARM_EXPERIMENTAL = "experimental"
ARM_CONTROL = "control"

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
                # Fail-safe baseline for the shadow/real split (#932). The agent's
                # experiment writer scopes on "game_kind != real" (targeting.go),
                # so a context MISSING game_kind would resolve the experimental
                # variant — a real game could silently run an experiment. Defaulting
                # the always-available API-level context to "real" means any game
                # that does not explicitly opt into shadow is protected by
                # construction; only a shadow session overrides this per-session.
                GAME_KIND_VAR: GAME_KIND_REAL,
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


def read_string_flag(domain: str, flag_key: str, default: str, game_id: str | None = None) -> str:
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
        game_id: Optional owning game id (#838/#1109); added as the ``gameId``
            context so flagd targeting can scope the value per game. ``None``/empty
            resolves identically to today (pure context addition).

    Returns:
        The flag's string value, or ``default`` on failure.
    """
    try:
        init_flag_domain(domain)
        client = get_flag_client(domain)
        details = client.get_string_details(flag_key, default, _calibration_context(game_id))
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


def set_game_session_kind_context(
    game_kind: str = GAME_KIND_REAL,
    experiment_id: str | None = None,
    arm: str | None = None,
) -> None:
    """Set the game_kind (and optional experiment identity) on the current async
    context's transaction context.

    The shadow/real split (#932) must be in place BEFORE a game mode's __init__
    runs its init-frozen calibration reads (thresholds, grace period, per-mode
    config) — those reads evaluate in this same contextvars context, so if
    game_kind weren't set yet they would fall back to the API-level "real"
    default and a shadow session would miss its experiments. This minimal setter
    establishes the split at the session boundary; :func:`set_game_transaction_context`
    later overwrites the transaction context with the full game session attributes
    (carrying the same game_kind + experiment identity).

    The experiment identity (#975) rides the SAME contextvars boundary for the
    same reason: a flag whose override is keyed on ``experiment_id`` and read at
    ``__init__`` would otherwise miss its experiment. ``experiment_id`` / ``arm``
    are set ONLY when provided — a real game (or a shadow game not bound to an
    experiment) leaves them absent, so its experiment targeting is false by
    construction (real-by-default).

    Args:
        game_kind: ``"shadow"`` for a shadow session, ``"real"`` (default) for a
            protected real/primary game. MUST match targeting.go's GameKindReal.
        experiment_id: Experiment this shadow game belongs to (#975), e.g.
            ``"exp_<id>"``; omitted for a non-experiment game.
        arm: ``"experimental"`` | ``"control"`` (#975); omitted for a
            non-experiment game.
    """
    attributes: dict = {GAME_KIND_VAR: game_kind}
    if experiment_id is not None:
        attributes[EXPERIMENT_ID_VAR] = experiment_id
    if arm is not None:
        attributes[ARM_VAR] = arm
    api.set_transaction_context(EvaluationContext(attributes=attributes))
    logger.debug(
        f"Session-kind transaction context set: game_kind={game_kind}, experiment_id={experiment_id}, arm={arm}"
    )


def set_game_transaction_context(
    game_mode: str,
    controller_count: int,
    sensitivity: int | None = None,
    game_kind: str = GAME_KIND_REAL,
    experiment_id: str | None = None,
    arm: str | None = None,
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
        game_kind: Shadow/real marker for the agent experiment split (#932).
            Defaults to ``"real"`` (the protected baseline) so an unlabeled game
            never resolves an experiment; ONLY a shadow session passes
            ``"shadow"``. The transaction context overrides the API-level default
            for this async context alone, so a shadow session's experiments stay
            scoped to that session. The value MUST match targeting.go's
            GameKindReal/"shadow" (see GAME_KIND_* constants).
        experiment_id: Experiment this shadow game belongs to (#975), e.g.
            ``"exp_<id>"``. Carried on the SAME transaction context as game_kind
            so experiment-scoped flag overrides resolve for this session alone.
            Omitted (absent) for a non-experiment game — an absent experiment_id
            means "not in any experiment", so the experiment targeting condition
            is false by construction (real-by-default, exactly like game_kind).
        arm: ``"experimental"`` | ``"control"`` (#975); the treatment this game
            is in within ``experiment_id``. Omitted for a non-experiment game.
            MUST match targeting.go's ARM constants (see ARM_* above).
    """
    attributes: dict = {
        "game_mode": game_mode,
        "controller_count": controller_count,
        GAME_KIND_VAR: game_kind,
    }
    if sensitivity is not None:
        attributes["sensitivity"] = sensitivity
    if experiment_id is not None:
        attributes[EXPERIMENT_ID_VAR] = experiment_id
    if arm is not None:
        attributes[ARM_VAR] = arm

    api.set_transaction_context(
        EvaluationContext(
            targeting_key=game_mode,
            attributes=attributes,
        )
    )
    logger.debug(
        f"Transaction context set: game_mode={game_mode}, "
        f"controller_count={controller_count}, sensitivity={sensitivity}, "
        f"game_kind={game_kind}, experiment_id={experiment_id}, arm={arm}"
    )
