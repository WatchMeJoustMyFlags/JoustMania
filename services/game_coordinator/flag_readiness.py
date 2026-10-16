"""Flag-provider readiness at game admission (#1329, UC-010 A5 / BR-004).

After a coordinator restart the OpenFeature flagd providers are NOT_READY until
their first sync. A game created before that read its start settings
(``game_loop.update_frequency_hz``, ``poll_drop_threshold``, ...) as code
defaults with ``ProviderNotReadyError`` (dry run #2, trace
c8e9aa27347f60d5a5ac1649e8dd4233). The session now waits, bounded, for every
domain that is still NOT_READY before it creates the game, and records a clear
fallback on the game root span when the wait runs out.

Only NOT_READY is waited on. With the in-process resolver a STALE or ERROR
provider (flagd outage after a sync) keeps serving its last-synced flags, so
waiting would only delay every game by the full bound.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time

from openfeature.provider import ProviderStatus
from opentelemetry import trace

from lib.feature_flags import init_flag_domain, provider_status, wait_for_provider_ready

logger = logging.getLogger(__name__)

# Every flag domain the game coordinator reads during a game. ``observability``
# is left out on purpose: lib.telemetry / lib.otel_metrics read it at process
# start (export intervals), never per game, and their own fail-safe defaults
# apply; it does not decide how a game plays.
COORDINATOR_FLAG_DOMAINS: tuple[str, ...] = ("system", "controller", "game", "agent", "user", "interventions")

# Upper bound on the admission wait (same budget as the controller manager's startup wait).
FLAG_PROVIDER_READY_TIMEOUT_S = 5.0


def _not_ready(domain: str) -> bool:
    return provider_status(domain) == ProviderStatus.NOT_READY


async def await_flag_providers_ready(
    span: trace.Span,
    domains: tuple[str, ...] = COORDINATOR_FLAG_DOMAINS,
    deadline_seconds: float = FLAG_PROVIDER_READY_TIMEOUT_S,
) -> list[str]:
    """Wait (bounded) for the domains still NOT_READY; record the outcome on ``span``.

    Returns the domains still NOT_READY when the deadline ran out (empty when
    none). Never raises: a failure of the wait itself is logged and treated as
    "nothing to report", because a game start must never be blocked by it.
    """
    try:
        return await _await_flag_providers_ready(span, domains, deadline_seconds)
    except Exception as e:
        logger.warning(f"Flag-provider readiness wait failed, starting the game anyway: {e}")
        return []


async def _await_flag_providers_ready(span: trace.Span, domains: tuple[str, ...], deadline_seconds: float) -> list[str]:
    started = time.monotonic()
    for domain in domains:
        init_flag_domain(domain)

    pending = [d for d in domains if _not_ready(d)]
    if pending:
        results = await asyncio.gather(*(wait_for_provider_ready(d, deadline_seconds) for d in pending))
        not_ready = [d for d, ready in zip(pending, results, strict=True) if not ready]
    else:
        not_ready = []

    span.set_attribute("feature_flag.providers_wait_ms", int((time.monotonic() - started) * 1000))
    if not_ready:
        logger.warning(
            f"Flag providers not ready after {deadline_seconds}s: {not_ready}; "
            "starting the game with code defaults for those domains"
        )
        span.add_event(
            "feature_flag.providers_not_ready",
            attributes={
                "feature_flag.domains": not_ready,
                "feature_flag.wait_seconds": deadline_seconds,
                "feature_flag.fallback": "code_defaults",
            },
        )
    if pending:
        # Values the process-wide runtime config cached while a provider was
        # NOT_READY are code defaults: re-read them. Run in an EMPTY context so
        # this session's shadow/experiment transaction context (game_kind,
        # experiment_id, arm) cannot leak into the process-wide config.
        contextvars.Context().run(_refresh_runtime_config)
    return not_ready


def _refresh_runtime_config() -> None:
    """Re-read the cached runtime configuration now that providers are (or may be) READY."""
    from services.game_coordinator.runtime_config import refresh_runtime_config

    try:
        refresh_runtime_config()
    except Exception as e:  # never block a game start on a config refresh
        logger.warning(f"Runtime config refresh after provider readiness failed: {e}")
