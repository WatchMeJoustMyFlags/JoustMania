"""promtool unit tests for the key Prometheus alerts (#1365, UC-035 A2).

The scenarios live in tests/contract/prometheus/alerts_test.yml; this wrapper
runs them with the Prometheus image docker-compose.yml uses.
"""

from __future__ import annotations

import subprocess

import infra_config as ic
import pytest

ALERTS_DIR = ic.REPO_ROOT / "services" / "prometheus"
ALERT_TESTS_DIR = ic.REPO_ROOT / "tests" / "contract" / "prometheus"


@pytest.mark.uc("UC-035", flow="A2")
def test_key_alerts_fire_and_stay_quiet_as_specified():
    """UC-035 A2: controller disconnected / low battery, frame consistency, gRPC errors,
    intervention handler errors, flag fallbacks and agent write failures / file corruption
    fire on their condition and stay quiet otherwise."""
    if not ic.docker_available():
        pytest.skip("docker not available")
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--entrypoint", "promtool",
            "-v", f"{ALERTS_DIR}:/rules:ro", "-v", f"{ALERT_TESTS_DIR}:/tests:ro",
            ic.compose_image("prometheus"), "test", "rules", "/tests/alerts_test.yml",
        ],
        capture_output=True, text=True, check=False, timeout=300,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
