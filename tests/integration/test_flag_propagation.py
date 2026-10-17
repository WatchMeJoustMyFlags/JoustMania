"""
NFR-050 write propagation: flag-service write -> value served to live readers.

Measures, against the real flag service and flagd of the integration stack, how
long a runtime flag write takes from the flag service accepting it to flagd
serving the new value (epic #1407; this replaces the file-poll path of NFR-014,
which no runtime write takes any more). Two readers are polled side by side:

- the in-process resolver (sync stream on port 8015), the path every Python
  service reads flags through (``lib/feature_flags``), so this is the latency a
  service observes;
- the RPC evaluation API (port 8013, cache disabled), the path tests and tools
  use.

How:
- The probe flag ``flagservice.propagation_probe`` (observability set, a free
  string no service reads, flags.md §13.5) gets a fresh nonce per sample through
  ``PUT /observability/flagservice.propagation_probe``. Every write still wakes
  every set's readers (the one-source fan-out, architecture note §5.4).
- Latency = first poll that sees the nonce - the moment the PUT returned (the
  flag service accepted and pushed the write). Writes are spaced by a random
  0.2-0.6 s.

The assertion bounds are the NFR-050 targets with headroom for shared CI
runners; the printed distribution, every raw sample included, is the
measurement. NFR-050's "8 live games" load condition is not reproduced here.
"""

import os
import random
import statistics
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tests.integration.helpers import flagd_probe_client  # noqa: E402

PROBE_SET = "observability"
PROBE_FLAG = "flagservice.propagation_probe"
SAMPLES = 20
POLL_INTERVAL_SECONDS = 0.005
SAMPLE_TIMEOUT_SECONDS = 5.0
# NFR-050: p95 <= 200 ms, p99 <= 500 ms. CI bounds with headroom.
P95_BOUND_SECONDS = 0.5
MAX_BOUND_SECONDS = 1.5


def _percentile(ordered: list[float], pct: float) -> float:
    return ordered[min(len(ordered) - 1, max(0, round(pct * len(ordered)) - 1))]


def measure_write_propagation(
    docker_compose, flag_service, samples: int = SAMPLES
) -> dict[str, list[float | None]]:
    """Return per-reader latencies (seconds) for ``samples`` flag-service writes.

    A sample is ``None`` for a reader that did not serve the write within
    SAMPLE_TIMEOUT_SECONDS (a lost push, not a slow one).
    """
    latencies: dict[str, list[float | None]] = {"in_process": [], "rpc": []}
    with (
        flagd_probe_client(
            docker_compose, PROBE_SET, resolver="in_process"
        ) as in_process,
        flagd_probe_client(docker_compose, PROBE_SET, resolver="rpc") as rpc,
    ):
        readers = {"in_process": in_process, "rpc": rpc}

        # Prime: both readers are connected and serve a probe value before timing.
        prime = uuid.uuid4().hex
        flag_service.put(PROBE_SET, PROBE_FLAG, value=prime)
        deadline = time.monotonic() + 30.0
        while any(
            r.get_string_value(PROBE_FLAG, "", None) != prime for r in readers.values()
        ):
            assert time.monotonic() < deadline, (
                "flagd never served the primed probe value"
            )
            time.sleep(0.05)

        for _ in range(samples):
            time.sleep(random.uniform(0.2, 0.6))
            nonce = uuid.uuid4().hex
            flag_service.put(PROBE_SET, PROBE_FLAG, value=nonce)
            written_at = time.monotonic()
            pending = dict(readers)
            deadline = written_at + SAMPLE_TIMEOUT_SECONDS
            while pending and time.monotonic() < deadline:
                for name, reader in list(pending.items()):
                    if reader.get_string_value(PROBE_FLAG, "", None) == nonce:
                        latencies[name].append(time.monotonic() - written_at)
                        del pending[name]
                time.sleep(POLL_INTERVAL_SECONDS)
            for name in pending:
                latencies[name].append(None)
    return latencies


def format_distribution(latencies: dict[str, list[float | None]]) -> str:
    """Summary line plus every raw sample per reader, for the CI log."""
    samples = max((len(v) for v in latencies.values()), default=0)
    lines = [
        f"--- NFR-050 write propagation, {samples} flag-service writes to {PROBE_SET}/{PROBE_FLAG} ---"
    ]
    for name, values in latencies.items():
        seen = sorted(v for v in values if v is not None)
        raw = " ".join("MISS" if v is None else f"{v * 1000:.0f}" for v in values)
        if seen:
            lines.append(
                f"  {name:<10} n={len(seen)} misses={len(values) - len(seen)}  min {seen[0] * 1000:.0f} ms"
                f"  median {statistics.median(seen) * 1000:.0f} ms  p95 {_percentile(seen, 0.95) * 1000:.0f} ms"
                f"  max {seen[-1] * 1000:.0f} ms"
            )
        else:
            lines.append(f"  {name:<10} n=0 misses={len(values)}")
        lines.append(f"  {name:<10} raw ms: {raw}")
    return "\n".join(lines)


@pytest.mark.nfr("NFR-050")
@pytest.mark.uc("UC-053", flow="A1")
def test_flag_service_write_reaches_readers(docker_compose, flag_service, capsys):
    latencies = measure_write_propagation(docker_compose, flag_service)
    report = format_distribution(latencies)
    # Write past pytest's capture so a passing CI run's log carries the measurement too.
    with capsys.disabled():
        print("\n" + report)

    for name, values in latencies.items():
        missed = [i for i, v in enumerate(values) if v is None]
        assert not missed, (
            f"{name}: write never served for sample(s) {missed} within {SAMPLE_TIMEOUT_SECONDS}s\n{report}"
        )
        seen = sorted(v for v in values if v is not None)
        assert _percentile(seen, 0.95) < P95_BOUND_SECONDS, (
            f"{name}: slow propagation, p95 {_percentile(seen, 0.95):.3f}s >= {P95_BOUND_SECONDS}s\n{report}"
        )
        assert seen[-1] < MAX_BOUND_SECONDS, (
            f"{name}: slow propagation, max {seen[-1]:.3f}s >= {MAX_BOUND_SECONDS}s\n{report}"
        )
