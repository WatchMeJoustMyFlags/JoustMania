"""Services fail open when the OTel collector is down (#1365, UC-035 A3 / failure postcondition).

Runs the REAL lib.telemetry + lib.otel_metrics setup in a subprocess (the OTel
SDK providers are process-global and the test conftest disables them) against
an OTLP endpoint where nothing listens, then drives the hot recording path:
spans, counters, gauges and histograms must neither raise nor block while the
exporters keep failing in the background.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_SCRIPT = textwrap.dedent(
    """
    import json, logging, os, socket, time

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here: the "collector" is down
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{port}"
    os.environ["OTEL_SERVICE_NAME"] = "fail-open-test"

    import lib.telemetry as telemetry
    from lib.otel_metrics import Counter, Gauge, Histogram, init_metrics
    from opentelemetry import metrics as otel_metrics, trace as otel_trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

    class _Count(logging.Handler):
        failures = 0

        def emit(self, record):
            if record.levelno >= logging.WARNING:
                _Count.failures += 1

    logging.getLogger("opentelemetry.exporter.otlp").addHandler(_Count())

    # flagd is not reachable here either; skip its (separately fail-open) lookup
    telemetry._get_bsp_schedule_delay_ms = lambda service: 50
    init_metrics(export_interval_ms=50)
    tracer = telemetry.get_tracer("fail-open-test")
    counter = Counter("fail_open_test_total", "test", ["k"])
    gauge = Gauge("fail_open_test_gauge", "test")
    histogram = Histogram("fail_open_test_seconds", "test")

    # The real SDK must be live, or the timings below prove nothing (a NoOp
    # tracer/meter is always fast).
    sdk = {
        "tracer_provider": isinstance(otel_trace.get_tracer_provider(), TracerProvider),
        "meter_provider": isinstance(otel_metrics.get_meter_provider(), MeterProvider),
    }
    with tracer.start_as_current_span("fail_open.probe") as probe:
        sdk["span_recording"] = probe.is_recording()

    def burst(n):
        worst = 0.0
        start = time.monotonic()
        for i in range(n):
            t0 = time.monotonic()
            with tracer.start_as_current_span("fail_open.frame") as span:
                span.set_attribute("i", i)
                counter.labels(k="a").inc()
                gauge.set(i)
                histogram.observe(0.001)
            worst = max(worst, time.monotonic() - t0)
        return time.monotonic() - start, worst

    first = burst(2000)
    time.sleep(1.0)  # several export attempts fail in the background meanwhile
    second = burst(2000)
    print(json.dumps({"first": first, "second": second, "sdk": sdk, "export_failures": _Count.failures}), flush=True)
    os._exit(0)  # do not wait for the exporters' shutdown retries
    """
)


@pytest.mark.uc("UC-035", flow="A3", cond=["Post-F-1"])
def test_recording_spans_and_metrics_never_blocks_when_collector_is_down():
    """UC-035 A3: with no collector, the game's telemetry calls stay fast and never raise."""
    # Scrub inherited OTEL_* (other tests' disable_telemetry_for_tests() sets
    # OTEL_SDK_DISABLED / OTEL_*_EXPORTER=none, which would yield NoOp providers).
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    timings = json.loads(result.stdout.strip().splitlines()[-1])
    assert timings["sdk"] == {"tracer_provider": True, "meter_provider": True, "span_recording": True}, timings["sdk"]
    assert timings["export_failures"] > 0, "exporters never tried (and failed) to reach the dead collector"
    for phase in ("first", "second"):
        total, worst = timings[phase]
        # 2000 frames of span + 3 metric ops: well under a 60 Hz budget per frame even on CI
        assert total < 5.0, f"{phase}: 2000 frames took {total:.2f}s with the collector down"
        assert worst < 0.25, f"{phase}: one frame blocked for {worst:.3f}s with the collector down"
