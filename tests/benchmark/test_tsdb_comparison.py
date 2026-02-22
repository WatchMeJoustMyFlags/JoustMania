"""
TSDB Benchmark: Prometheus vs VictoriaMetrics comparison (Issue #575).

Runs a real JoustMania game session and compares how both TSDBs handle
identical metric workloads pushed via OTEL Collector remote_write.

This test is informational only -- no hard assertions on benchmark results.
Differences >10% in sample counts trigger warnings for investigation.
"""

import asyncio
import time
import warnings

import docker
import httpx
import pytest
from tabulate import tabulate

from tests.integration.helpers import (
    GameEventCollector,
    force_end_game,
    get_game_client,
    setup_mock_controllers,
    start_game_via_menu,
)

# Metrics to compare across both backends
BENCHMARK_METRICS = [
    "controller_accel_magnitude",
    "active_controllers_total",
    "controller_stream_updates_total",
    "process_cpu_seconds_total",
    "game_active_players",
]

# Steady-state game duration in seconds
GAME_DURATION_SECONDS = 30

# Maximum acceptable sample count divergence (fraction)
SAMPLE_DIVERGENCE_THRESHOLD = 0.10


class TSDBClient:
    """Thin wrapper around the Prometheus HTTP query API.

    Works for both Prometheus and VictoriaMetrics since they share the
    same ``/api/v1/`` query interface.
    """

    def __init__(self, base_url: str, name: str):
        self.base_url = base_url.rstrip("/")
        self.name = name
        self._client = httpx.Client(timeout=10.0)

    def query(self, promql: str) -> dict:
        """Execute an instant query."""
        resp = self._client.get(
            f"{self.base_url}/api/v1/query",
            params={"query": promql},
        )
        resp.raise_for_status()
        return resp.json()

    def query_range(self, promql: str, start: float, end: float, step: str = "15s") -> dict:
        """Execute a range query."""
        resp = self._client.get(
            f"{self.base_url}/api/v1/query_range",
            params={
                "query": promql,
                "start": start,
                "end": end,
                "step": step,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def series(self, match: str) -> dict:
        """Query series metadata."""
        resp = self._client.get(
            f"{self.base_url}/api/v1/series",
            params={"match[]": match},
        )
        resp.raise_for_status()
        return resp.json()

    def sample_count(self, metric: str, start: float, end: float) -> int:
        """Count total samples for *metric* in the given time window.

        Uses a range query with a 1s step and sums all returned data
        points across every series that matches the metric name.
        """
        data = self.query_range(metric, start, end, step="1s")
        total = 0
        for result in data.get("data", {}).get("result", []):
            total += len(result.get("values", []))
        return total

    def series_count(self, metric: str) -> int:
        """Count the number of distinct series for *metric*."""
        data = self.series(metric)
        return len(data.get("data", []))

    def close(self):
        self._client.close()


def _get_container_stats(container_name: str) -> dict:
    """Get CPU% and memory usage for a Docker container.

    Returns ``{"cpu_percent": float, "memory_mb": float}`` or zeros on
    error (container not found, stats unavailable, etc.).
    """
    try:
        client = docker.from_env()
        container = client.containers.get(container_name)
        stats = container.stats(stream=False)

        # CPU percent
        cpu_delta = (
            stats["cpu_stats"]["cpu_usage"]["total_usage"]
            - stats["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        system_delta = (
            stats["cpu_stats"]["system_cpu_usage"]
            - stats["precpu_stats"]["system_cpu_usage"]
        )
        cpu_percent = 0.0
        if system_delta > 0:
            num_cpus = stats["cpu_stats"].get("online_cpus", 1)
            cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0

        # Memory
        memory_bytes = stats.get("memory_stats", {}).get("usage", 0)
        memory_mb = memory_bytes / (1024 * 1024)

        return {"cpu_percent": round(cpu_percent, 2), "memory_mb": round(memory_mb, 2)}
    except Exception as exc:
        warnings.warn(f"Could not get stats for {container_name}: {exc}")
        return {"cpu_percent": 0.0, "memory_mb": 0.0}


@pytest.mark.timeout(180)
async def test_tsdb_comparison(docker_compose):
    """Run a JoustFFA game and compare Prometheus vs VictoriaMetrics."""

    # ------------------------------------------------------------------
    # 1. Set up controllers and start a game
    # ------------------------------------------------------------------
    serials = await setup_mock_controllers(docker_compose, count=4)
    print(f"\nControllers ready: {serials}")

    game_client, game_channel = await get_game_client(docker_compose)

    async with GameEventCollector(game_client) as collector:
        await start_game_via_menu(
            docker_compose,
            game_mode="JoustFFA",
            timeout=25.0,
            event_collector=collector,
        )
        print("Game started -- entering steady-state phase")

        # ------------------------------------------------------------------
        # 2. Wait for metrics to appear in both backends
        # ------------------------------------------------------------------
        prom = TSDBClient("http://localhost:9090", "Prometheus")
        vm = TSDBClient("http://localhost:8428", "VictoriaMetrics")

        metric_to_check = "active_controllers_total"
        for attempt in range(20):
            prom_result = prom.query(metric_to_check)
            vm_result = vm.query(metric_to_check)
            prom_has = len(prom_result.get("data", {}).get("result", [])) > 0
            vm_has = len(vm_result.get("data", {}).get("result", [])) > 0
            if prom_has and vm_has:
                print(f"Both backends have '{metric_to_check}' (attempt {attempt + 1})")
                break
            await asyncio.sleep(1)
        else:
            warnings.warn(
                f"'{metric_to_check}' not found in both backends after 20s "
                f"(Prometheus={prom_has}, VM={vm_has})"
            )

        # ------------------------------------------------------------------
        # 3. Steady-state: let the game run and collect metrics
        # ------------------------------------------------------------------
        window_start = time.time()
        print(f"Steady-state window: {GAME_DURATION_SECONDS}s ...")
        await asyncio.sleep(GAME_DURATION_SECONDS)
        window_end = time.time()
        print("Steady-state complete")

        # ------------------------------------------------------------------
        # 4. End the game
        # ------------------------------------------------------------------
        await force_end_game(game_client, collector, timeout=10)
        print("Game ended")

    await game_channel.close()

    # Allow a brief flush period for in-flight remote writes
    await asyncio.sleep(3)

    # ------------------------------------------------------------------
    # 5. Query both backends for benchmark metrics
    # ------------------------------------------------------------------
    rows = []
    divergence_warnings = []

    for metric in BENCHMARK_METRICS:
        prom_samples = prom.sample_count(metric, window_start, window_end)
        vm_samples = vm.sample_count(metric, window_start, window_end)
        prom_series = prom.series_count(metric)
        vm_series = vm.series_count(metric)

        # Compute divergence
        max_samples = max(prom_samples, vm_samples, 1)
        divergence = abs(prom_samples - vm_samples) / max_samples

        rows.append([
            metric,
            prom_samples,
            vm_samples,
            prom_series,
            vm_series,
            f"{divergence:.1%}",
        ])

        if divergence > SAMPLE_DIVERGENCE_THRESHOLD:
            divergence_warnings.append(
                f"{metric}: divergence {divergence:.1%} "
                f"(Prometheus={prom_samples}, VM={vm_samples})"
            )

    # ------------------------------------------------------------------
    # 6. Container resource usage
    # ------------------------------------------------------------------
    prom_stats = _get_container_stats("joustmania-prometheus")
    vm_stats = _get_container_stats("joustmania-victoria-metrics")

    resource_rows = [
        ["CPU %", prom_stats["cpu_percent"], vm_stats["cpu_percent"]],
        ["Memory (MB)", prom_stats["memory_mb"], vm_stats["memory_mb"]],
    ]

    prom.close()
    vm.close()

    # ------------------------------------------------------------------
    # 7. Print results
    # ------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("TSDB Benchmark Results: Prometheus vs VictoriaMetrics")
    print(f"Steady-state window: {GAME_DURATION_SECONDS}s, 4 controllers, JoustFFA")
    print("=" * 80)

    print("\nMetric Samples:")
    print(tabulate(
        rows,
        headers=["Metric", "Prom Samples", "VM Samples", "Prom Series", "VM Series", "Divergence"],
        tablefmt="grid",
    ))

    print("\nResource Usage (snapshot at end of benchmark):")
    print(tabulate(
        resource_rows,
        headers=["Resource", "Prometheus", "VictoriaMetrics"],
        tablefmt="grid",
    ))

    # ------------------------------------------------------------------
    # 8. Warn (do NOT assert) on divergence
    # ------------------------------------------------------------------
    if divergence_warnings:
        msg = (
            f"Sample count divergence >{SAMPLE_DIVERGENCE_THRESHOLD:.0%} "
            f"detected in {len(divergence_warnings)} metric(s):\n"
            + "\n".join(f"  - {w}" for w in divergence_warnings)
        )
        warnings.warn(msg)

    print("\nBenchmark complete (informational only -- no hard assertions).")
