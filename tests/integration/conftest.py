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
import os
import sys
import time

import grpc
import pytest
from testcontainers.compose import DockerCompose

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import (
    game_coordinator_pb2,
    game_coordinator_pb2_grpc,
)

_COMPOSE_FILES = [
    "docker-compose.yml",
    "docker-compose.override.yml",
    "docker-compose.ci.yml",
]


@pytest.fixture(scope="session")
def docker_compose(request):
    """Fixture to start docker-compose mock environment.

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
        print(f"\nUsing prebuilt images from GHCR (tag: {image_tag})")
    elif use_dev_mounts:
        print("\nUsing dev volume mounts (no build)")
    else:
        print("\nBuilding images locally")

    compose.start()

    # Wait for services to be ready
    # Docker Compose --wait already waits for health checks, so minimal wait needed
    time.sleep(2)

    print("\n" + "=" * 80)
    print("Mock environment is running!")
    print("=" * 80)
    print("Jaeger UI: http://localhost:16686")
    print("WebUI: http://localhost:80")
    print("Mock Control API: localhost:50062")
    print("=" * 80)

    yield compose

    # Skip teardown in CI mode for faster test completion
    # Containers will be cleaned up by the CI runner
    if os.getenv("CI") or os.getenv("SKIP_TEARDOWN"):
        print("\nSkipping teardown (CI mode)")
        return

    # Optional pause before teardown (set PAUSE_BEFORE_TEARDOWN=1 to inspect Jaeger)
    if os.getenv("PAUSE_BEFORE_TEARDOWN"):
        print("\n" + "=" * 80)
        print("PAUSED - Inspect Jaeger at http://localhost:16686")
        print("=" * 80)
        print("Press ENTER to tear down the environment...")
        input()

    compose.stop()


@pytest.fixture(scope="session", autouse=True)
def warmup_game_path(docker_compose):
    """Exercise the menu->coordinator game-start path once before any test runs.

    Docker Compose health checks confirm each service's port is open, but the
    very first game start after ``compose up`` still races service warmup: the
    menu has to receive its initial controller-connection events, the
    coordinator has to lazily spin up its game machinery, and flagd has to serve
    its first resolution. The first test to call ``start_game_via_menu`` pays
    that cold-start cost and can exceed the per-call timeout (the original F6
    failure: a 20s timeout on the first-positioned test, while the same call
    succeeded in ~6s once services were warm).

    Running one full start/force-end cycle here, with retries, absorbs that
    cold-start latency at session scope so EVERY test sees a warm path -- this
    protects whichever test happens to sort first, not just today's. The warmup
    is best-effort: if it can't complete (e.g. flag state differs), tests still
    run and fail loudly on their own, so this never masks a real regression.
    """
    # Imported lazily so collection of unit-only runs doesn't require helpers.
    from tests.integration.helpers import (
        GameEventCollector,
        get_game_client,
        setup_mock_controllers,
        start_game_via_menu,
    )
    from proto import game_coordinator_pb2 as _gc_pb2

    async def _warmup() -> None:
        await setup_mock_controllers(docker_compose, count=4)
        game_client, channel = await get_game_client(docker_compose)
        try:
            # Generous timeout: this call deliberately absorbs the cold start
            # so the first real test does not have to.
            collector = GameEventCollector(game_client)
            async with collector:
                await start_game_via_menu(
                    docker_compose, game_mode="JoustFFA", timeout=45.0,
                    event_collector=collector,
                )
            # Leave no game running for the first real test.
            await game_client.ForceEndGame(
                _gc_pb2.ForceEndGameRequest(reason="warmup_cleanup")
            )
            await asyncio.sleep(0.5)
        finally:
            await channel.close()

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            asyncio.run(_warmup())
            print(f"\nWarmup game-start path succeeded (attempt {attempt})")
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 - best-effort warmup
            last_error = exc
            print(f"\nWarmup attempt {attempt} failed: {exc!r}")

    if last_error is not None:
        print(
            "\nWarmup never completed; proceeding anyway "
            "(tests will surface any real failure)."
        )

    yield


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


@pytest.fixture
async def headless_cleanup(docker_compose):
    """Per-test cleanup for headless (shadow) games.

    Menu-flow tests use ``ensure_game_stopped`` (force-ends the single primary
    game). Headless tests instead own specific game_ids and reserved-controller
    tags, so cleanup must be targeted: this fixture returns a registry the test
    populates, then force-ends each registered game_id and removes each reserved
    controller tag on teardown — even if the test body raised.

    Usage:
        async def test_x(docker_compose, headless_cleanup):
            game_id, collector = await start_game_headless(...)
            headless_cleanup.add_game(game_id)
            headless_cleanup.add_tag("shadow-x")
            ...
    """
    from tests.integration.helpers import (
        force_end_game_by_id,
        remove_reserved_controllers,
    )

    class _Registry:
        def __init__(self):
            self.game_ids: list[str] = []
            self.tags: list[str] = []

        def add_game(self, game_id: str) -> str:
            if game_id and game_id not in self.game_ids:
                self.game_ids.append(game_id)
            return game_id

        def add_tag(self, tag: str) -> str:
            if tag and tag not in self.tags:
                self.tags.append(tag)
            return tag

    registry = _Registry()

    yield registry

    # Teardown: best-effort force-end every registered game and sweep tags.
    host = docker_compose.get_service_host("game-coordinator", 50053)
    port = docker_compose.get_service_port("game-coordinator", 50053)
    channel = grpc.aio.insecure_channel(f"{host}:{port}")
    game_client = game_coordinator_pb2_grpc.GameCoordinatorServiceStub(channel)
    try:
        for game_id in registry.game_ids:
            await force_end_game_by_id(game_client, game_id, reason="headless_cleanup")
        for tag in registry.tags:
            try:
                await remove_reserved_controllers(docker_compose, tag)
            except Exception:
                pass
        await asyncio.sleep(0.3)
    finally:
        await channel.close()
