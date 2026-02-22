"""
TSDB Benchmark: Prometheus vs VictoriaMetrics comparison (Issue #575).

Runs a real JoustMania game session and compares how both TSDBs handle
identical metric workloads pushed via OTEL Collector remote_write.

This test is informational only -- no hard assertions on benchmark results.
Differences >10% in sample counts trigger warnings for investigation.

Environment variables:
    BENCHMARK_CONTROLLERS: Number of mock controllers (default 4, max 36)
    BENCHMARK_DURATION: Steady-state duration in seconds (default 30)
"""

import asyncio
import os
import statistics
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# Representative PromQL queries from Grafana dashboards.
# These exercise different query patterns (rate, histogram_quantile,
# aggregations, label matchers) to validate both backends produce
# correct and timely results for real dashboard panels.
DASHBOARD_QUERIES = [
    # Simple selectors / gauges
    ("process_resident_memory_bytes", "Process memory (gauge)"),
    ("active_controllers_total", "Active controllers (gauge)"),
    ("game_active_players", "Active players (gauge)"),
    # Rate calculations
    ("rate(process_cpu_seconds_total[5m]) * 100", "Process CPU % (rate)"),
    ("sum by(job) (rate(grpc_requests_total[1m]))", "gRPC request rate by service"),
    ('sum by(job) (rate(grpc_requests_total{status!="ok"}[1m]))', "gRPC error rate"),
    ("rate(controller_stream_updates_total[1m])", "Controller stream update rate"),
    # Histogram quantiles
    (
        "histogram_quantile(0.95, sum by(job, le) (rate(grpc_request_duration_seconds_bucket[5m])))",
        "gRPC p95 latency (histogram_quantile)",
    ),
    # Aggregations with math
    ("sum(increase(controller_stream_updates_total[1m]))", "Stream updates increase"),
    ("count(active_controllers_total >= 0)", "Count with comparison"),
    # Game-specific metrics
    ("avg(game_player_accel_magnitude)", "Avg player acceleration"),
    ("game_player_peak_accel", "Peak acceleration (gauge)"),
    ("rate(game_near_death_events_total[1m]) * 60", "Near-death events per minute"),
]

# Configurable via environment
CONTROLLER_COUNT = int(os.getenv("BENCHMARK_CONTROLLERS", "4"))
GAME_DURATION_SECONDS = int(os.getenv("BENCHMARK_DURATION", "30"))

# Maximum acceptable sample count divergence (fraction)
SAMPLE_DIVERGENCE_THRESHOLD = 0.10

# Pi 5 scaling: i9-11950H single-thread Geekbench ~1700, Cortex-A76 ~700
# Ratio ~2.5x. Memory is assumed 1:1 (workload-dependent, not CPU-bound).
PI5_CPU_SCALING_FACTOR = 2.5

# Concurrent read stress test: simulate N dashboards refreshing simultaneously,
# repeated for CYCLES refresh intervals.
CONCURRENT_DASHBOARDS = 3
READ_STRESS_CYCLES = 10

# Cardinality stress: simulate adding game_id labels across many game sessions.
# Each "game" creates N_controllers new series per metric, accumulating over
# CARDINALITY_GAMES sessions — mimicking a party night.
CARDINALITY_GAMES = 50
CARDINALITY_METRICS_PER_PLAYER = 10


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

    def query_range(
        self, promql: str, start: float, end: float, step: str = "15s"
    ) -> dict:
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

    def timed_query(self, promql: str) -> tuple[dict, float]:
        """Execute an instant query and return (result, latency_ms)."""
        t0 = time.monotonic()
        result = self.query(promql)
        latency_ms = (time.monotonic() - t0) * 1000
        return result, latency_ms

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


def _run_concurrent_read_stress(
    clients: list[TSDBClient],
    queries: list[tuple[str, str]],
    cycles: int,
) -> dict[str, list[float]]:
    """Fire all queries concurrently across multiple TSDB clients.

    Simulates *len(clients)* dashboards each running all *queries*
    simultaneously, repeated for *cycles* refresh intervals.

    Returns ``{client.name: [latency_ms, ...]}`` with one entry per
    query per cycle.
    """
    latencies: dict[str, list[float]] = {c.name: [] for c in clients}

    def _single_query(client: TSDBClient, promql: str) -> tuple[str, float]:
        t0 = time.monotonic()
        client.query(promql)
        return client.name, (time.monotonic() - t0) * 1000

    for cycle in range(cycles):
        futures = []
        with ThreadPoolExecutor(max_workers=len(clients) * len(queries)) as pool:
            for client in clients:
                for promql, _desc in queries:
                    futures.append(pool.submit(_single_query, client, promql))

            for fut in as_completed(futures):
                try:
                    name, ms = fut.result()
                    latencies[name].append(ms)
                except Exception:
                    pass

    return latencies


def _percentiles(values: list[float]) -> tuple[float, float, float]:
    """Return (p50, p95, p99) from a list of values."""
    if not values:
        return (0.0, 0.0, 0.0)
    s = sorted(values)
    p50 = statistics.median(s)
    p95 = s[int(len(s) * 0.95)]
    p99 = s[int(len(s) * 0.99)]
    return (p50, p95, p99)


def _build_otlp_payload(
    game_idx: int, controllers: int, metrics_per_player: int, timestamp_ns: int
) -> dict:
    """Build an OTLP JSON metrics payload for one game session.

    Creates ``controllers * metrics_per_player`` gauge data points, each
    with a unique ``{game_id, serial}`` label combination.
    """
    data_points = []
    for ctrl in range(controllers):
        for m in range(metrics_per_player):
            data_points.append(
                {
                    "asDouble": 1.0 + (game_idx * 0.01) + (ctrl * 0.001),
                    "timeUnixNano": str(timestamp_ns),
                    "attributes": [
                        {
                            "key": "game_id",
                            "value": {"stringValue": f"game_{game_idx:04d}"},
                        },
                        {
                            "key": "serial",
                            "value": {"stringValue": f"MOCK{ctrl:04d}"},
                        },
                        {
                            "key": "metric_idx",
                            "value": {"stringValue": str(m)},
                        },
                    ],
                }
            )

    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "cardinality-bench"},
                        }
                    ]
                },
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "bench_player_gauge",
                                "gauge": {"dataPoints": data_points},
                            }
                        ]
                    }
                ],
            }
        ]
    }


def _push_cardinality_via_otlp(
    collector_url: str, games: int, controllers: int, metrics_per_player: int
) -> tuple[int, float]:
    """Push synthetic high-cardinality metrics through the OTEL Collector.

    Uses the OTLP HTTP endpoint so data fans out to both Prometheus and
    VictoriaMetrics via the existing pipeline.

    Returns ``(total_series_created, push_duration_seconds)``.
    """
    client = httpx.Client(timeout=30.0)
    timestamp_ns = int(time.time() * 1e9)
    total_series = 0
    t0 = time.monotonic()

    for game_idx in range(games):
        payload = _build_otlp_payload(
            game_idx, controllers, metrics_per_player, timestamp_ns
        )
        resp = client.post(
            f"{collector_url}/v1/metrics",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            warnings.warn(
                f"OTLP push failed for game {game_idx}: "
                f"{resp.status_code} {resp.text[:200]}"
            )
            break
        total_series += controllers * metrics_per_player
        # Advance timestamp by 15s per game to create distinct samples
        timestamp_ns += 15_000_000_000

    duration = time.monotonic() - t0
    client.close()
    return total_series, duration


@pytest.mark.timeout(420)
async def test_tsdb_comparison(docker_compose):
    """Run a JoustFFA game and compare Prometheus vs VictoriaMetrics."""

    # ------------------------------------------------------------------
    # 1. Set up controllers and start a game
    # ------------------------------------------------------------------
    serials = await setup_mock_controllers(docker_compose, count=CONTROLLER_COUNT)
    print(
        f"\nControllers ready: {len(serials)} controllers ({serials[:3]}{'...' if len(serials) > 3 else ''})"
    )

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

        rows.append(
            [
                metric,
                prom_samples,
                vm_samples,
                prom_series,
                vm_series,
                f"{divergence:.1%}",
            ]
        )

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

    # Pi 5 theoretical projection (CPU only — memory is workload-dependent)
    pi5_rows = [
        [
            "CPU % (Pi 5 est.)",
            round(prom_stats["cpu_percent"] * PI5_CPU_SCALING_FACTOR, 2),
            round(vm_stats["cpu_percent"] * PI5_CPU_SCALING_FACTOR, 2),
        ],
        ["Memory (MB)", prom_stats["memory_mb"], vm_stats["memory_mb"]],
    ]

    # ------------------------------------------------------------------
    # 7. Dashboard PromQL query validation
    # ------------------------------------------------------------------
    query_rows = []
    query_warnings = []

    for promql, description in DASHBOARD_QUERIES:
        prom_result, prom_ms = prom.timed_query(promql)
        vm_result, vm_ms = vm.timed_query(promql)

        prom_status = prom_result.get("status", "error")
        vm_status = vm_result.get("status", "error")
        prom_series_n = len(prom_result.get("data", {}).get("result", []))
        vm_series_n = len(vm_result.get("data", {}).get("result", []))

        status = "OK"
        if prom_status != "success" or vm_status != "success":
            status = "ERR"
            query_warnings.append(f"{description}: Prom={prom_status}, VM={vm_status}")
        elif prom_series_n == 0 and vm_series_n == 0:
            status = "EMPTY"

        query_rows.append(
            [
                description,
                f"{prom_ms:.0f}",
                f"{vm_ms:.0f}",
                prom_series_n,
                vm_series_n,
                status,
            ]
        )

    # ------------------------------------------------------------------
    # 8. Concurrent read stress test
    # ------------------------------------------------------------------
    # Each "dashboard" is a separate httpx client hitting the same backend.
    # We create N clients per backend to simulate N dashboards refreshing
    # all DASHBOARD_QUERIES in parallel, repeated for READ_STRESS_CYCLES.
    print(
        f"\nRead stress test: {CONCURRENT_DASHBOARDS} dashboards x "
        f"{len(DASHBOARD_QUERIES)} queries x {READ_STRESS_CYCLES} cycles ..."
    )

    prom_clients = [
        TSDBClient("http://localhost:9090", "Prometheus")
        for _ in range(CONCURRENT_DASHBOARDS)
    ]
    vm_clients = [
        TSDBClient("http://localhost:8428", "VictoriaMetrics")
        for _ in range(CONCURRENT_DASHBOARDS)
    ]

    stress_latencies = _run_concurrent_read_stress(
        prom_clients + vm_clients, DASHBOARD_QUERIES, READ_STRESS_CYCLES
    )

    for c in prom_clients + vm_clients:
        c.close()

    # Aggregate per-backend (all dashboard clients combined)
    prom_latencies = stress_latencies["Prometheus"]
    vm_latencies = stress_latencies["VictoriaMetrics"]

    prom_p50, prom_p95, prom_p99 = _percentiles(prom_latencies)
    vm_p50, vm_p95, vm_p99 = _percentiles(vm_latencies)

    total_queries = len(DASHBOARD_QUERIES) * CONCURRENT_DASHBOARDS * READ_STRESS_CYCLES
    stress_rows = [
        ["Total queries", total_queries, total_queries],
        ["p50 (ms)", f"{prom_p50:.1f}", f"{vm_p50:.1f}"],
        ["p95 (ms)", f"{prom_p95:.1f}", f"{vm_p95:.1f}"],
        ["p99 (ms)", f"{prom_p99:.1f}", f"{vm_p99:.1f}"],
        [
            "Errors",
            total_queries - len(prom_latencies),
            total_queries - len(vm_latencies),
        ],
    ]

    # Snapshot resource usage under read load
    prom_read_stats = _get_container_stats("joustmania-prometheus")
    vm_read_stats = _get_container_stats("joustmania-victoria-metrics")

    read_resource_rows = [
        [
            "CPU % (under read load)",
            prom_read_stats["cpu_percent"],
            vm_read_stats["cpu_percent"],
        ],
        [
            "CPU % (Pi 5 est.)",
            round(prom_read_stats["cpu_percent"] * PI5_CPU_SCALING_FACTOR, 2),
            round(vm_read_stats["cpu_percent"] * PI5_CPU_SCALING_FACTOR, 2),
        ],
        ["Memory (MB)", prom_read_stats["memory_mb"], vm_read_stats["memory_mb"]],
    ]

    print("Read stress test complete")

    # ------------------------------------------------------------------
    # 9. Cardinality stress test
    # ------------------------------------------------------------------
    # Push synthetic metrics with unique game_id labels through the OTEL
    # Collector, simulating a party night with many game sessions.
    # Each game creates controllers * metrics_per_player new series.
    expected_series = (
        CARDINALITY_GAMES * CONTROLLER_COUNT * CARDINALITY_METRICS_PER_PLAYER
    )
    print(
        f"\nCardinality stress: {CARDINALITY_GAMES} games x "
        f"{CONTROLLER_COUNT} controllers x {CARDINALITY_METRICS_PER_PLAYER} metrics "
        f"= {expected_series:,} synthetic series ..."
    )

    series_pushed, push_duration = _push_cardinality_via_otlp(
        "http://localhost:4318",
        CARDINALITY_GAMES,
        CONTROLLER_COUNT,
        CARDINALITY_METRICS_PER_PLAYER,
    )
    print(f"Pushed {series_pushed:,} series in {push_duration:.1f}s")

    # Allow collector to flush to both backends
    await asyncio.sleep(5)

    # Re-create clients (previous ones were closed)
    prom_card = TSDBClient("http://localhost:9090", "Prometheus")
    vm_card = TSDBClient("http://localhost:8428", "VictoriaMetrics")

    # Measure series count for the synthetic metric
    prom_card_series = prom_card.series_count("bench_player_gauge")
    vm_card_series = vm_card.series_count("bench_player_gauge")

    # Query the high-cardinality metric with different patterns
    cardinality_queries = [
        ("bench_player_gauge", "Select all series"),
        ("count(bench_player_gauge) by (game_id)", "Count by game_id"),
        ("avg(bench_player_gauge) by (serial)", "Avg by serial"),
        (
            "count(count(bench_player_gauge) by (game_id))",
            "Count distinct games",
        ),
    ]

    card_query_rows = []
    for promql, desc in cardinality_queries:
        _, prom_ms = prom_card.timed_query(promql)
        _, vm_ms = vm_card.timed_query(promql)
        card_query_rows.append([desc, f"{prom_ms:.1f}", f"{vm_ms:.1f}"])

    # Concurrent reads against high-cardinality data
    prom_card_clients = [
        TSDBClient("http://localhost:9090", "Prometheus")
        for _ in range(CONCURRENT_DASHBOARDS)
    ]
    vm_card_clients = [
        TSDBClient("http://localhost:8428", "VictoriaMetrics")
        for _ in range(CONCURRENT_DASHBOARDS)
    ]

    card_stress = _run_concurrent_read_stress(
        prom_card_clients + vm_card_clients,
        cardinality_queries,
        READ_STRESS_CYCLES,
    )

    for c in prom_card_clients + vm_card_clients:
        c.close()

    prom_card_lat = card_stress["Prometheus"]
    vm_card_lat = card_stress["VictoriaMetrics"]
    prom_card_p50, prom_card_p95, prom_card_p99 = _percentiles(prom_card_lat)
    vm_card_p50, vm_card_p95, vm_card_p99 = _percentiles(vm_card_lat)

    cardinality_summary_rows = [
        ["Series pushed", series_pushed, series_pushed],
        ["Series ingested", prom_card_series, vm_card_series],
        ["Push duration (s)", f"{push_duration:.1f}", f"{push_duration:.1f}"],
    ]

    card_concurrent_rows = [
        ["p50 (ms)", f"{prom_card_p50:.1f}", f"{vm_card_p50:.1f}"],
        ["p95 (ms)", f"{prom_card_p95:.1f}", f"{vm_card_p95:.1f}"],
        ["p99 (ms)", f"{prom_card_p99:.1f}", f"{vm_card_p99:.1f}"],
    ]

    # Resource snapshot after cardinality injection
    prom_card_stats = _get_container_stats("joustmania-prometheus")
    vm_card_stats = _get_container_stats("joustmania-victoria-metrics")

    card_resource_rows = [
        ["CPU %", prom_card_stats["cpu_percent"], vm_card_stats["cpu_percent"]],
        [
            "CPU % (Pi 5 est.)",
            round(prom_card_stats["cpu_percent"] * PI5_CPU_SCALING_FACTOR, 2),
            round(vm_card_stats["cpu_percent"] * PI5_CPU_SCALING_FACTOR, 2),
        ],
        ["Memory (MB)", prom_card_stats["memory_mb"], vm_card_stats["memory_mb"]],
    ]

    prom_card.close()
    vm_card.close()
    print("Cardinality stress test complete")

    prom.close()
    vm.close()

    # ------------------------------------------------------------------
    # 10. Print results
    # ------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("TSDB Benchmark Results: Prometheus vs VictoriaMetrics")
    print(
        f"Steady-state window: {GAME_DURATION_SECONDS}s, {CONTROLLER_COUNT} controllers, JoustFFA"
    )
    print("=" * 80)

    print("\nMetric Samples:")
    print(
        tabulate(
            rows,
            headers=[
                "Metric",
                "Prom Samples",
                "VM Samples",
                "Prom Series",
                "VM Series",
                "Divergence",
            ],
            tablefmt="grid",
        )
    )

    print("\nResource Usage (snapshot at end of benchmark):")
    print(
        tabulate(
            resource_rows,
            headers=["Resource", "Prometheus", "VictoriaMetrics"],
            tablefmt="grid",
        )
    )

    print(
        f"\nPi 5 Projection (CPU x{PI5_CPU_SCALING_FACTOR} — "
        "i9-11950H single-thread ~2.5x faster than Cortex-A76):"
    )
    print(
        tabulate(
            pi5_rows,
            headers=["Resource", "Prometheus", "VictoriaMetrics"],
            tablefmt="grid",
        )
    )

    print("\nDashboard Query Validation:")
    print(
        tabulate(
            query_rows,
            headers=[
                "Query",
                "Prom (ms)",
                "VM (ms)",
                "Prom Series",
                "VM Series",
                "Status",
            ],
            tablefmt="grid",
        )
    )

    print(
        f"\nConcurrent Read Stress ({CONCURRENT_DASHBOARDS} dashboards x "
        f"{READ_STRESS_CYCLES} cycles):"
    )
    print(
        tabulate(
            stress_rows,
            headers=["Metric", "Prometheus", "VictoriaMetrics"],
            tablefmt="grid",
        )
    )

    print("\nResource Usage Under Read Load:")
    print(
        tabulate(
            read_resource_rows,
            headers=["Resource", "Prometheus", "VictoriaMetrics"],
            tablefmt="grid",
        )
    )

    print(
        f"\nCardinality Stress ({CARDINALITY_GAMES} games x "
        f"{CONTROLLER_COUNT} controllers x {CARDINALITY_METRICS_PER_PLAYER} metrics/player):"
    )
    print(
        tabulate(
            cardinality_summary_rows,
            headers=["Metric", "Prometheus", "VictoriaMetrics"],
            tablefmt="grid",
        )
    )

    print("\nQuery Latency on High-Cardinality Data (sequential):")
    print(
        tabulate(
            card_query_rows,
            headers=["Query", "Prom (ms)", "VM (ms)"],
            tablefmt="grid",
        )
    )

    print(
        f"\nConcurrent Reads on High-Cardinality Data "
        f"({CONCURRENT_DASHBOARDS} dashboards x {READ_STRESS_CYCLES} cycles):"
    )
    print(
        tabulate(
            card_concurrent_rows,
            headers=["Metric", "Prometheus", "VictoriaMetrics"],
            tablefmt="grid",
        )
    )

    print("\nResource Usage After Cardinality Injection:")
    print(
        tabulate(
            card_resource_rows,
            headers=["Resource", "Prometheus", "VictoriaMetrics"],
            tablefmt="grid",
        )
    )

    # ------------------------------------------------------------------
    # 11. Warn (do NOT assert) on divergence / query errors
    # ------------------------------------------------------------------
    if divergence_warnings:
        msg = (
            f"Sample count divergence >{SAMPLE_DIVERGENCE_THRESHOLD:.0%} "
            f"detected in {len(divergence_warnings)} metric(s):\n"
            + "\n".join(f"  - {w}" for w in divergence_warnings)
        )
        warnings.warn(msg)

    if query_warnings:
        msg = (
            f"Dashboard query errors in {len(query_warnings)} query(ies):\n"
            + "\n".join(f"  - {w}" for w in query_warnings)
        )
        warnings.warn(msg)

    print("\nBenchmark complete (informational only -- no hard assertions).")
