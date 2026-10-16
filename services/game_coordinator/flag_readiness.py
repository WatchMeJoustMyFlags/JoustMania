"""Flag-provider readiness at game admission (#1329, UC-010 A5 / BR-004).

After a coordinator restart the OpenFeature flagd providers are not READY until
their first sync. A game created before that read its start settings
(``game_loop.update_frequency_hz``, ``poll_drop_threshold``, ...) as code
defaults with ``ProviderNotReadyError`` (dry run #2, trace
c8e9aa27347f60d5a5ac1649e8dd4233). The session now waits, bounded, for every
domain the coordinator reads before it creates the game, and records a clear
fallback on the game root span when the wait runs out.
"""

from __future__ import annotations

import asyncio
import logging
import time

from opentelemetry import trace

from lib.feature_flags import init_flag_domain, wait_for_provider_ready

logger = logging.getLogger(__name__)

# Every flag domain the game coordinator reads during a game.
COORDINATOR_FLAG_DOMAINS: tuple[str, ...] = ("system", "controller", "game", "agent", "user", "interventions")

# Upper bound on the admission wait (same budget as the controller manager's startup wait).
FLAG_PROVIDER_READY_TIMEOUT_S = 5.0


async def await_flag_providers_ready(
    span: trace.Span,
    domains: tuple[str, ...] = COORDINATOR_FLAG_DOMAINS,
    deadline_seconds: float = FLAG_PROVIDER_READY_TIMEOUT_S,
) -> list[str]:
    """Wait (bounded) until every domain's provider is READY; record the outcome on ``span``.

    Returns the domains that were still not READY when the deadline ran out (empty
    when all are ready). On a full success the runtime configuration is re-read
    from the flags, so values cached before readiness (code defaults) are replaced.
    """
    started = time.monotonic()
    for domain in domains:
        init_flag_domain(domain)

    results = await asyncio.gather(*(wait_for_provider_ready(d, deadline_seconds) for d in domains))
    not_ready = [d for d, ready in zip(domains, results, strict=True) if not ready]

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
    _refresh_runtime_config()
    return not_ready


def _refresh_runtime_config() -> None:
    """Re-read the cached runtime configuration now that providers are (or may be) READY."""
    from services.game_coordinator.runtime_config import refresh_runtime_config

    try:
        refresh_runtime_config()
    except Exception as e:  # never block a game start on a config refresh
        logger.warning(f"Runtime config refresh after provider readiness failed: {e}")
