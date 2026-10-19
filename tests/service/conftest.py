"""Service tier (T2) fixtures (#1422, #1524). See README.md.

This is a sibling of tests/integration, not a child: none of the integration
autouse fixtures (compose start, warmup, coordinator quiesce) apply here.

- ``service_stack`` (module scope): one stack for every test of the module. The
  module picks its composition with ``SERVICE_COMPOSITION = <Composition>``
  (default ``GAME``).
- ``fresh_stack`` (function scope): a stack of its own for one test, for tests
  that change process-global state (stack-wide flags, the rate limiter, a
  restart). Same composition rule.
- ``flag_service``: the integration suite's FlagService client bound to the
  test's stack (``fresh_stack`` when the test uses it, else ``service_stack``);
  stack-wide overlays it set are DELETEd at teardown.
- ``headless_cleanup``: force-ends the headless games and removes the reserved
  mock controllers a test registered, even when it failed.

Every test here carries ``@pytest.mark.tier("service")`` (module ``pytestmark``);
collection fails without it.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tests.service.harness import GAME, ServiceStack  # noqa: E402
from tests.service.harness.processes import REPO_ROOT as _HARNESS_ROOT  # noqa: E402

assert _HARNESS_ROOT == REPO_ROOT

TIER = "service"


# --------------------------------------------------------------------------- #
# Tier marker: every module must declare it
# --------------------------------------------------------------------------- #
def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    untagged = sorted(
        {item.path.name for item in items if not any(m.args[:1] == (TIER,) for m in item.iter_markers("tier"))}
    )
    if untagged:
        raise pytest.UsageError(
            "tests/service modules without the tier marker: "
            + ", ".join(untagged)
            + '. Add `pytestmark = pytest.mark.tier("service")` to each.'
        )


# --------------------------------------------------------------------------- #
# Prerequisites: docker (flagd) and go (flag service)
# --------------------------------------------------------------------------- #
def _missing_prerequisites() -> list[str]:
    import shutil
    import subprocess

    missing = []
    if not shutil.which("go"):
        missing.append("go (builds services/flag-service)")
    if not shutil.which("docker") or (
        subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0
    ):
        missing.append("a running docker daemon (flagd)")
    return missing


@pytest.fixture(scope="session")
def _prerequisites():
    missing = _missing_prerequisites()
    if missing:
        message = "service tier needs " + " and ".join(missing)
        if os.getenv("CI"):
            pytest.fail(message)
        pytest.skip(message)


# --------------------------------------------------------------------------- #
# Stacks
# --------------------------------------------------------------------------- #
def _composition(request):
    return getattr(request.module, "SERVICE_COMPOSITION", GAME)


def _run_stack(request, tmp_path_factory, label: str):
    workdir = tmp_path_factory.mktemp(label)
    stack = ServiceStack(_composition(request), workdir)
    stack.start()
    try:
        yield stack
    finally:
        stack.stop()


@pytest.fixture(scope="module")
def service_stack(request, tmp_path_factory, _prerequisites):
    yield from _run_stack(request, tmp_path_factory, f"stack-{request.module.__name__.rsplit('.', 1)[-1]}")


@pytest.fixture
def fresh_stack(request, tmp_path_factory, _prerequisites):
    yield from _run_stack(request, tmp_path_factory, f"fresh-{request.node.name[:40]}")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):  # noqa: ARG001
    """Attach the stack's log tails to a failed test's report."""
    report = yield
    if report.failed:
        for name in ("stack", "fresh_stack", "service_stack"):
            stack = getattr(item, "funcargs", {}).get(name)
            if stack is not None:
                report.sections.append((f"service tier logs ({name})", stack.log_tail()))
                break
    return report


@pytest.fixture
def stack(request):
    """The test's stack: ``fresh_stack`` when it asks for one, else ``service_stack``."""
    if "fresh_stack" in request.fixturenames:
        return request.getfixturevalue("fresh_stack")
    return request.getfixturevalue("service_stack")


@pytest.fixture
def flag_service(stack):
    from tests.integration import flag_service as flag_service_module
    from tests.integration.flag_service import FlagService

    client = FlagService.from_compose(stack)
    flag_service_module.bind(client)
    try:
        yield client
    finally:
        flag_service_module.bind(None)
        client.cleanup()


@pytest.fixture
async def headless_cleanup(stack):
    """Registry of headless game ids and reserved-controller tags to clean up."""
    from tests.integration.helpers import (
        force_end_game_by_id,
        get_game_client,
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

    game_client, channel = await get_game_client(stack)
    try:
        for game_id in registry.game_ids:
            await force_end_game_by_id(game_client, game_id, reason="headless_cleanup")
        for tag in registry.tags:
            with contextlib.suppress(Exception):  # best effort, the stack may be gone
                await remove_reserved_controllers(stack, tag)
    finally:
        await channel.close()
