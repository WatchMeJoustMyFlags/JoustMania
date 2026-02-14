"""
Shared pytest fixtures for JoustMania integration tests.

These fixtures provide:
- Docker compose environment management
- Automatic cleanup between tests
- Pre-connected gRPC clients

Usage:
    Fixtures are auto-discovered by pytest from this conftest.py file.
    Import helpers from helpers.py for shared utility functions.

Environment Variables:
    USE_PREBUILT_IMAGES: Set to "true" to pull images from GHCR instead of building
    USE_DEV_MOUNTS: Set to "true" to overlay source via docker-compose.dev.yml
    IMAGE_TAG: Specify image tag to pull (default: latest)
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import grpc
import pytest
from testcontainers.compose import DockerCompose

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import (
    game_coordinator_pb2,
    game_coordinator_pb2_grpc,
)

# Compose files used for both legacy and multiplexer parametrizations
_COMPOSE_FILES = [
    "docker-compose.yml",
    "docker-compose.override.yml",
    "docker-compose.ci.yml",
]


def _set_multiplexer_flag(enabled: bool):
    """Toggle multiplexer_backend_enabled in the CI flagd config.

    flagd watches the file via inotify and picks up changes within ~100ms.
    """
    config_path = Path("services/flagd/performance.ci.json")
    config = json.loads(config_path.read_text())
    config["flags"]["multiplexer_backend_enabled"]["defaultVariant"] = "on" if enabled else "off"
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def _restart_service(compose_files: list[str], service: str):
    """Restart a single service via docker compose."""
    cmd = ["docker", "compose"]
    for f in compose_files:
        cmd.extend(["-f", f])
    cmd.extend(["restart", service])
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture(scope="session", params=["legacy", "multiplexer"])
def docker_compose(request):
    """Fixture to start docker-compose mock environment.

    Parametrized to run the entire test suite twice:
    - "legacy": default single-backend code path (multiplexer flag OFF)
    - "multiplexer": MultiplexerBackend wrapping the backend (multiplexer flag ON)

    Uses docker-compose.yml with overrides for testing:
    - docker-compose.override.yml: port exposures for testing
    - docker-compose.ci.yml: mock mode for audio/controllers (no hardware)

    By default, builds images locally. Set USE_PREBUILT_IMAGES=true to pull from GHCR.
    """
    # Check if we should use prebuilt images or dev volume mounts
    use_prebuilt = os.getenv("USE_PREBUILT_IMAGES", "false").lower() == "true"
    use_dev_mounts = os.getenv("USE_DEV_MOUNTS", "false").lower() == "true"
    image_tag = os.getenv("IMAGE_TAG", "latest")

    compose_files = list(_COMPOSE_FILES)
    if use_dev_mounts:
        compose_files.append("docker-compose.dev.yml")

    compose = DockerCompose(
        context=".",
        compose_file_name=compose_files,
        pull=use_prebuilt,
        build=not use_prebuilt and not use_dev_mounts,
        env_file=None,  # Avoid conflicts with .env (e.g., IMAGE_TAG from development)
    )

    # Set IMAGE_TAG if using prebuilt images
    # Note: This modifies the environment for docker-compose but is session-scoped
    if use_prebuilt:
        os.environ["IMAGE_TAG"] = image_tag
        print(f"\n[{request.param}] Using prebuilt images from GHCR (tag: {image_tag})")
    elif use_dev_mounts:
        print(f"\n[{request.param}] Using dev volume mounts (no build)")
    else:
        print(f"\n[{request.param}] Building images locally")

    compose.start()

    # Wait for services to be ready
    # Docker Compose --wait already waits for health checks, so minimal wait needed
    time.sleep(2)

    if request.param == "multiplexer":
        print(f"\n[{request.param}] Enabling multiplexer_backend_enabled flag")
        _set_multiplexer_flag(enabled=True)
        # flagd picks up inotify change within ~100ms; give it a moment
        time.sleep(1)
        # Restart controller-manager so it re-reads the flag at startup
        _restart_service(compose_files, "controller-manager")
        time.sleep(2)

    print("\n" + "=" * 80)
    print(f"Mock environment is running! [backend_mode={request.param}]")
    print("=" * 80)
    print("Jaeger UI: http://localhost:16686")
    print("WebUI: http://localhost:80")
    print("Mock Control API: localhost:50062")
    print("=" * 80)

    yield compose

    # Restore flag to default (off) if we toggled it
    if request.param == "multiplexer":
        _set_multiplexer_flag(enabled=False)

    # Skip teardown in CI mode for faster test completion
    # Containers will be cleaned up by the CI runner
    if os.getenv("CI") or os.getenv("SKIP_TEARDOWN"):
        print(f"\n[{request.param}] Skipping teardown (CI mode)")
        return

    # Optional pause before teardown (set PAUSE_BEFORE_TEARDOWN=1 to inspect Jaeger)
    if os.getenv("PAUSE_BEFORE_TEARDOWN"):
        print("\n" + "=" * 80)
        print("PAUSED - Inspect Jaeger at http://localhost:16686")
        print("=" * 80)
        print("Press ENTER to tear down the environment...")
        input()

    compose.stop()


@pytest.fixture
async def ensure_game_stopped(docker_compose):
    """Ensure no game is running before and after each test.

    Use this fixture explicitly in tests that start games to prevent
    'Game already in progress' errors between tests.
    """
    async def force_end_game():
        """Force end any running game."""
        try:
            host = docker_compose.get_service_host("game-coordinator", 50053)
            port = docker_compose.get_service_port("game-coordinator", 50053)
            channel = grpc.aio.insecure_channel(f"{host}:{port}")
            client = game_coordinator_pb2_grpc.GameCoordinatorServiceStub(channel)

            # Try to force end any running game
            await client.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest())

            # Brief wait for cleanup
            await asyncio.sleep(0.5)

            await channel.close()
        except Exception:
            pass  # Ignore errors (no game running, service not ready, etc.)

    # Before test: ensure no game is running
    await force_end_game()

    yield

    # After test: cleanup any game that was started
    await force_end_game()
