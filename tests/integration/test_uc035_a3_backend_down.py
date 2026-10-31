"""UC-035 A3 / NFR-024: with the agent down, the collector still delivers (#1650).

The collector fans traces and metrics out to Jaeger, VictoriaMetrics and the
agent (``otlp/agent``, best effort: 2 s timeout, no retries, bounded queue).
The agent is profile-gated and not started by default (only tc007 starts it,
and removes it at teardown), so the collector's ``otlp/agent`` exporter points
at an unreachable backend: exactly A3. If an earlier module left the agent running,
the ``agent_down`` fixture stops it and restarts it at teardown, so the test
holds either way. A headless mock game then runs and ends, and what the
collector delivered is read back from the backends' own query APIs:

- VictoriaMetrics holds ``game_trace_correlation{game_id=...}`` for the live game;
- Jaeger holds the game's root span, found by ``game.id``;
- the collector container is still running;
- the root span is findable by its trace id with its spans, the place an operator
  starts to find a game's errors (Post-S-1; Loki is gone, FR-082 Obsolete);
- Jaeger gets no new agent spans, so the dashboard's Agent and Experiments tabs
  show no new rows (A3; envoy's own error for ``/experiments`` is the contract
  test ``test_agent_down_envoy_answers_experiments_with_its_own_error``).

Jaeger and VictoriaMetrics are queried on the loopback-only ports that
``docker-compose.override.yml`` publishes for tests (C-015).
"""

import json
import subprocess
import time
import urllib.parse
import urllib.request

import pytest

from proto import game_coordinator_pb2
from tests.integration import jaeger_v3
from tests.integration.helpers import (
    COORDINATOR_SERVICE,
    JAEGER_API,
    async_poll_until,
    build_start_config,
    get_game_client,
    jaeger_root_spans,
    setup_mock_controllers,
    start_game_headless,
)

VICTORIA_METRICS_API = "http://127.0.0.1:8428/api/v1"
GAME_MODE = "JoustFFA"
TAG = "uc035-a3"
BACKEND_TIMEOUT_SECONDS = 60.0
EVENT_TIMEOUT_SECONDS = 15.0


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - loopback test URL
        return json.load(response)


def _nonempty(fetch, game_id: str) -> bool:
    """True when ``fetch(game_id)`` returns rows; a backend not answering yet counts as empty."""
    try:
        return bool(fetch(game_id))
    except OSError:
        return False


def _compose(docker_compose, *args: str) -> str:
    cmd = [*docker_compose.docker_compose_command(), *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, check=True
    ).stdout.strip()


def _container_running(docker_compose, service: str) -> bool:
    container_id = _compose(docker_compose, "ps", "-q", service)
    if not container_id:
        return False
    state = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    return state == "true"


def _correlation_series(game_id: str) -> list:
    query = urllib.parse.quote(f'game_trace_correlation{{game_id="{game_id}"}}')
    return _get_json(f"{VICTORIA_METRICS_API}/query?query={query}")["data"]["result"]


def _loop_rate_ratio(game_id: str) -> float:
    """The game's measured loop rate over the configured rate; 0 while a series is missing."""

    def value(selector: str) -> float:
        query = urllib.parse.quote(f"max({selector})")
        result = _get_json(f"{VICTORIA_METRICS_API}/query?query={query}")["data"][
            "result"
        ]
        return float(result[0]["value"][1]) if result else 0.0

    try:
        configured = value("game_current_update_frequency_hz")
        actual = value(f'game_actual_update_frequency_hz{{game_id="{game_id}"}}')
    except OSError:
        return 0.0
    return actual / configured if configured else 0.0


def _trace_spans(trace_id: str) -> list:
    """Spans of one trace fetched by its id (query API v3); an unknown trace counts as empty."""
    return jaeger_v3.get_trace(JAEGER_API, trace_id).get("spans", [])


def _spans_since(service: str, start_s: float) -> list:
    """``service`` spans Jaeger holds that started at or after ``start_s`` (query API v3).

    ``jaeger_v3`` answers ``[]`` only for v3's "No traces found"; any other error
    status raises, so an unreachable or failing store never reads as "no spans".
    """
    traces = jaeger_v3.find_traces(JAEGER_API, service, limit=100)
    return [
        span
        for trace in traces
        for span in trace["spans"]
        if span["startTime"] >= int(start_s * 1e6)
    ]


@pytest.fixture
def agent_down(docker_compose):
    """Make sure the agent is unreachable; restart it at teardown if this stopped it."""
    was_running = _container_running(docker_compose, "agent")
    if was_running:
        _compose(docker_compose, "stop", "agent")
    try:
        yield
    finally:
        if was_running:
            _compose(docker_compose, "start", "agent")


@pytest.mark.asyncio
@pytest.mark.uc("UC-035", flow="A3")
@pytest.mark.uc("UC-035", cond=["Post-S-1", "Post-F-1"])
@pytest.mark.nfr("NFR-024")
async def test_agent_down_collector_still_delivers_to_jaeger_and_victoria_metrics(
    docker_compose, headless_cleanup, agent_down
):
    """UC-035 A3: the agent is unreachable; traces reach Jaeger, metrics reach VictoriaMetrics."""
    assert not _container_running(docker_compose, "agent"), (
        "precondition: the agent must be down"
    )
    serials = await setup_mock_controllers(
        docker_compose, count=3, tag=headless_cleanup.add_tag(TAG)
    )
    game_client, channel = await get_game_client(docker_compose)
    collector = None
    test_start_s = time.time()
    try:
        game_id, collector = await start_game_headless(
            game_client, build_start_config(GAME_MODE, serials)
        )
        headless_cleanup.add_game(game_id)

        # Metrics: the live game's correlation series reaches VictoriaMetrics.
        assert await async_poll_until(
            lambda: _nonempty(_correlation_series, game_id),
            timeout=BACKEND_TIMEOUT_SECONDS,
            interval=1.0,
        ), (
            f"VictoriaMetrics has no game_trace_correlation for {game_id} while the agent is down"
        )

        # Post-F-1: the loop runs at its configured (NFR-003) rate while the agent is down.
        assert await async_poll_until(
            lambda: _loop_rate_ratio(game_id) >= 0.5,
            timeout=BACKEND_TIMEOUT_SECONDS,
            interval=1.0,
        ), (
            f"game {game_id} loop ran below half its configured rate while the agent was down"
        )

        await game_client.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(reason="uc035_a3", game_id=game_id)
        )
        await collector.wait_for_any_event(
            ["game_ended", "game_force_ended", "game_error"],
            timeout=EVENT_TIMEOUT_SECONDS,
        )
    finally:
        if collector is not None:
            await collector.stop()
        await channel.close()

    # Traces: the finished game's root span reaches Jaeger.
    assert await async_poll_until(
        lambda: _nonempty(jaeger_root_spans, game_id),
        timeout=BACKEND_TIMEOUT_SECONDS,
        interval=1.0,
    ), f"Jaeger has no root span for game {game_id} while the agent is down"

    # Positive control for the "no new agent spans" check below: the same reader
    # sees the coordinator's spans from this run, so an empty agent result is real.
    assert _spans_since(COORDINATOR_SERVICE, test_start_s), (
        "the span reader finds no coordinator spans of this run"
    )
    assert _spans_since("agent", test_start_s) == [], (
        "the agent is down: Jaeger must hold no new agent spans"
    )

    assert _container_running(docker_compose, "otel-collector"), (
        "the collector must survive the agent outage"
    )

    # Post-S-1: the game's spans are found by the root span's trace id.
    root = jaeger_root_spans(game_id)[0]
    assert await async_poll_until(
        lambda: _nonempty(_trace_spans, root["traceID"]),
        timeout=BACKEND_TIMEOUT_SECONDS,
        interval=1.0,
    ), f"Jaeger has no spans for trace {root['traceID']} of game {game_id}"
    assert root["spanID"] in {span["spanID"] for span in _trace_spans(root["traceID"])}


@pytest.mark.uc("UC-035", flow="A3")
def test_agent_down_no_new_agent_spans_reach_jaeger(docker_compose, agent_down):
    """UC-035 A3: with the agent down, the Agent and Experiments tabs' Jaeger source gets no new agent spans."""
    assert not _container_running(docker_compose, "agent"), (
        "precondition: the agent must be down"
    )
    down_since = time.time()
    # Jaeger answers (raises on any error status), so an empty result means no spans.
    assert isinstance(jaeger_v3.services(JAEGER_API), list)
    time.sleep(5)
    assert _spans_since("agent", down_since) == []
