"""
NFR-014 flag propagation latency: flag-file edit -> value served to live readers.

Measures, against the real flagd of the integration stack, how long an in-place
edit of a bind-mounted flag file takes to reach a reader. Two readers are polled
side by side:

- the in-process resolver (sync stream on port 8015), the path every Python
  service reads flags through (``lib/feature_flags``), so this is the latency a
  service observes;
- the RPC evaluation API (port 8013, cache disabled), the path tests and tools
  use.

How:
- A dedicated probe flag (``nfr014_propagation_probe``) is added to
  ``observability.json``; no service reads it, so the measurement has no side
  effects. The file is snapshotted and restored byte for byte.
- Each sample writes a new nonce IN PLACE (open/truncate/write, the repo
  convention: a temp+rename write breaks single-file bind mounts), then polls
  both readers every 5 ms until they serve the nonce. Latency = first poll that
  sees the nonce - end of the write.
- flagd v0.16 in docker has no inotify; its file watcher stats the file every
  1 s and reloads on an mtime change. So the expected latency is roughly
  uniform over [0, 1 s] plus reload and stream time. Writes are spaced by a
  random 1.1-2.1 s so they land at different phases of that poll; equally
  spaced writes would phase-lock and report one point of the distribution.

The assertion bounds are deliberately loose (CI runners are shared); the printed
distribution is the measurement.
"""

import json
import os
import random
import statistics
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tests.integration.helpers import active_flag_file, flagd_probe_client  # noqa: E402

PROBE_FLAG = "nfr014_propagation_probe"
FLAG_SET = "observability"
SAMPLES = 12
POLL_INTERVAL_SECONDS = 0.005
SAMPLE_TIMEOUT_SECONDS = 10.0
# flagd's 1 s stat poll + reload + sync stream. Upper bounds, not targets.
MEDIAN_BOUND_SECONDS = 1.5
MAX_BOUND_SECONDS = 3.0


def _write_in_place(path: str, text: str) -> float:
    """Write ``text`` into ``path`` without replacing the inode; return the end time."""
    with open(path, "w") as f:
        f.write(text)
        f.flush()
    return time.monotonic()


def _with_probe(base_doc: dict, nonce: str) -> str:
    doc = json.loads(json.dumps(base_doc))
    doc["flags"][PROBE_FLAG] = {
        "state": "ENABLED",
        "variants": {"current": nonce},
        "defaultVariant": "current",
    }
    return json.dumps(doc, indent=2) + "\n"


def _percentile(ordered: list[float], pct: float) -> float:
    return ordered[min(len(ordered) - 1, max(0, round(pct * len(ordered)) - 1))]


def measure_flag_propagation(
    docker_compose, samples: int = SAMPLES
) -> dict[str, list[float]]:
    """Return per-reader latencies (seconds) for ``samples`` in-place flag edits."""
    path = active_flag_file(FLAG_SET)
    with open(path) as f:
        original_text = f.read()
    base_doc = json.loads(original_text)
    latencies: dict[str, list[float]] = {"in_process": [], "rpc": []}

    try:
        with (
            flagd_probe_client(
                docker_compose, FLAG_SET, resolver="in_process"
            ) as in_process,
            flagd_probe_client(docker_compose, FLAG_SET, resolver="rpc") as rpc,
        ):
            readers = {"in_process": in_process, "rpc": rpc}

            # Prime: the probe flag must exist and be served before timing starts,
            # so the first sample measures an edit, not a new-flag cold path.
            prime = uuid.uuid4().hex
            _write_in_place(path, _with_probe(base_doc, prime))
            deadline = time.monotonic() + 30.0
            while any(
                r.get_string_value(PROBE_FLAG, "", None) != prime
                for r in readers.values()
            ):
                assert time.monotonic() < deadline, (
                    "flagd never served the primed probe flag"
                )
                time.sleep(0.1)

            for _ in range(samples):
                time.sleep(random.uniform(1.1, 2.1))
                nonce = uuid.uuid4().hex
                written_at = _write_in_place(path, _with_probe(base_doc, nonce))
                pending = dict(readers)
                deadline = written_at + SAMPLE_TIMEOUT_SECONDS
                while pending:
                    now = time.monotonic()
                    assert now < deadline, (
                        f"readers {sorted(pending)} did not serve the edit within {SAMPLE_TIMEOUT_SECONDS}s"
                    )
                    for name, reader in list(pending.items()):
                        if reader.get_string_value(PROBE_FLAG, "", None) == nonce:
                            latencies[name].append(time.monotonic() - written_at)
                            del pending[name]
                    time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        _write_in_place(path, original_text)

    return latencies


@pytest.mark.nfr("NFR-014")
def test_flag_file_edit_reaches_readers_within_poll_bound(docker_compose):
    latencies = measure_flag_propagation(docker_compose)

    print(
        f"\n--- NFR-014 flag propagation, {SAMPLES} in-place edits of {FLAG_SET}.json ---"
    )
    for name, values in latencies.items():
        ordered = sorted(values)
        print(
            f"  {name:<10} min {ordered[0] * 1000:6.0f} ms  median {statistics.median(ordered) * 1000:6.0f} ms  "
            f"p90 {_percentile(ordered, 0.9) * 1000:6.0f} ms  max {ordered[-1] * 1000:6.0f} ms"
        )

    for name, values in latencies.items():
        assert len(values) == SAMPLES
        assert statistics.median(values) < MEDIAN_BOUND_SECONDS, (
            f"{name}: median {statistics.median(values):.3f}s"
        )
        assert max(values) < MAX_BOUND_SECONDS, f"{name}: max {max(values):.3f}s"
