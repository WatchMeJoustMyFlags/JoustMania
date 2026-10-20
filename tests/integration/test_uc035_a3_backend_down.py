"""UC-035 A3 / NFR-024: with the agent down, the collector still delivers (#1650).

The collector fans traces and metrics out to Jaeger, VictoriaMetrics and the
agent (``otlp/agent``, best effort: 2 s timeout, no retries, bounded queue).
The agent is profile-gated and not started in the core shard, so the
collector's ``otlp/agent`` exporter points at an unreachable backend there:
exactly A3, and cheaper than the agent shard (no agent image build). That is why
this module is in the "core" shard. If an earlier module left the agent running,
the ``agent_down`` fixture stops it and restarts it at teardown, so the test
holds either way. A headless mock game then runs and ends, and what the
collector delivered is read back from the backends' own query APIs:

- VictoriaMetrics holds ``game_trace_correlation{game_id=...}`` for the live game;
- Jaeger holds the game's root span, found by ``game.id``;
- the collector container is still running.

Jaeger and VictoriaMetrics are queried on the loopback-only ports that
``docker-compose.override.yml`` publishes for tests (C-015).
"""

import json
import subprocess
import urllib.parse
import urllib.request

import pytest

from proto import game_coordinator_pb2
from tests.integration.helpers import (
    async_poll_until,
    build_start_config,
    get_game_client,
    setup_mock_controllers,
    start_game_headless,
)

JAEGER_API = "http://127.0.0.1:16686/jaeger/api"
VICTORIA_METRICS_API = "http://127.0.0.1:8428/api/v1"
COORDINATOR = "game-coordinator-service"
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
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True).stdout.strip()


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


def _jaeger_root_spans(game_id: str) -> list:
    tags = urllib.parse.quote(json.dumps({"game.id": game_id}))
    traces = _get_json(f"{JAEGER_API}/traces?service={COORDINATOR}&tags={tags}&limit=20")["data"]
    return [
        span
        for trace in traces
        for span in trace["spans"]
        if not span.get("references") and any(t["key"] == "game.id" and t["value"] == game_id for t in span["tags"])
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
@pytest.mark.nfr("NFR-024")
async def test_agent_down_collector_still_delivers_to_jaeger_and_victoria_metrics(
    docker_compose, headless_cleanup, agent_down
):
    """UC-035 A3: the agent is unreachable; traces reach Jaeger, metrics reach VictoriaMetrics."""
    assert not _container_running(docker_compose, "agent"), "precondition: the agent must be down"

    serials = await setup_mock_controllers(docker_compose, count=3, tag=headless_cleanup.add_tag(TAG))
    game_client, channel = await get_game_client(docker_compose)
    collector = None
    try:
        game_id, collector = await start_game_headless(game_client, build_start_config(GAME_MODE, serials))
        headless_cleanup.add_game(game_id)

        # Metrics: the live game's correlation series reaches VictoriaMetrics.
        assert await async_poll_until(
            lambda: _nonempty(_correlation_series, game_id), timeout=BACKEND_TIMEOUT_SECONDS, interval=1.0
        ), f"VictoriaMetrics has no game_trace_correlation for {game_id} while the agent is down"

        await game_client.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="uc035_a3", game_id=game_id))
        await collector.wait_for_any_event(
            ["game_ended", "game_force_ended", "game_error"], timeout=EVENT_TIMEOUT_SECONDS
        )
    finally:
        if collector is not None:
            await collector.stop()
        await channel.close()

    # Traces: the finished game's root span reaches Jaeger.
    assert await async_poll_until(
        lambda: _nonempty(_jaeger_root_spans, game_id), timeout=BACKEND_TIMEOUT_SECONDS, interval=1.0
    ), f"Jaeger has no root span for game {game_id} while the agent is down"

    assert _container_running(docker_compose, "otel-collector"), "the collector must survive the agent outage"
