"""
Benchmark test fixtures.

Starts the full JoustMania stack with the benchmark overlay that enables
Prometheus as a remote_write receiver alongside VictoriaMetrics.
"""

import os
import sys
import time

import pytest
from testcontainers.compose import DockerCompose

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

_COMPOSE_FILES = [
    "docker-compose.yml",
    "docker-compose.override.yml",
    "docker-compose.ci.yml",
    "docker-compose.benchmark.yml",
]


@pytest.fixture(scope="session")
def docker_compose(request):
    """Start docker-compose with benchmark overlay."""
    use_prebuilt = os.getenv("USE_PREBUILT_IMAGES", "false").lower() == "true"
    image_tag = os.getenv("IMAGE_TAG", "latest")

    compose = DockerCompose(
        context=".",
        compose_file_name=list(_COMPOSE_FILES),
        pull=use_prebuilt,
        build=not use_prebuilt,
        env_file=None,
    )

    if use_prebuilt:
        os.environ["IMAGE_TAG"] = image_tag

    compose.start()
    time.sleep(5)

    print("\n" + "=" * 80)
    print("Benchmark environment running")
    print("  Prometheus:       http://localhost:9090")
    print("  VictoriaMetrics:  http://localhost:8428")
    print("=" * 80)

    yield compose

    if os.getenv("CI") or os.getenv("SKIP_TEARDOWN"):
        return
    if os.getenv("PAUSE_BEFORE_TEARDOWN"):
        input("Press ENTER to tear down...")
    compose.stop()
