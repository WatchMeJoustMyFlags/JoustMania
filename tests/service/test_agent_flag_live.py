"""The agent's flagd provider follows flag-service writes live (#1709, #1713).

The agent reads its flags through ``flags.SetupFlagd``: since #1713 the go-sdk-contrib
flagd in-process resolver on flagd's sync port 8015, behind ``syncedProvider``, which
waits for the first sync however late flagd is. Before, on the RPC resolver (8013)
with the resolver's default cache, a flag the agent
had evaluated once stayed at that value until the agent restarted: flagd served
the new variant, the agent kept answering ``CACHED``. The kill switch (``enabled``
off), the ``experiments_enabled`` gate and the live ``decision.backend`` switch were
ignored.

This module starts the flag plane (real flag service + real flagd, FLAG_PLANE
composition) and runs the Go tests in ``services/agent/flags/provider_live_test.go``
against it: each flag is evaluated (and would be cached), flipped through the flag
service, and must reach the agent's provider within the NFR-050 bound. Without the
two env vars the Go tests skip, so ``go test ./...`` stays hermetic. One test restarts
flagd (``docker restart``) mid-run: reads fail closed while the stream is lost, then
the stream reconnects and later flips are seen.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.service.compositions import FLAG_PLANE

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = FLAG_PLANE

AGENT_DIR = Path(__file__).resolve().parents[2] / "services" / "agent"
GO_TEST_TIMEOUT_SECONDS = 300


def _run_live_go_test(stack, name: str) -> None:
    go = shutil.which("go")
    assert go, "go is required for the service tier"
    flag_service = (
        f"http://{stack.get_service_host('flag-service', 8070)}:{stack.get_service_port('flag-service', 8070)}"
    )
    env = {
        **os.environ,
        "AGENT_LIVE_FLAGD_HOST": stack.get_service_host("flagd", 8015),
        "AGENT_LIVE_FLAGD_PORT": str(stack.get_service_port("flagd", 8015)),
        "AGENT_LIVE_FLAGD_CONTAINER": stack.flag_plane.container,
        "AGENT_LIVE_FLAG_SERVICE_URL": flag_service,
    }
    result = subprocess.run(
        [go, "test", "-count=1", "-v", "-run", f"^{name}$", "./flags/"],
        cwd=AGENT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=GO_TEST_TIMEOUT_SECONDS,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"go test {name} failed:\n{output[-6000:]}"
    # A skip would pass silently: the env above must have reached the test.
    assert "--- SKIP" not in output, f"go test {name} skipped:\n{output[-3000:]}"
    assert f"--- PASS: {name}" in output, output[-3000:]


@pytest.mark.uc("UC-043", flow="A1", cond=["Pre-1"])
@pytest.mark.uc("UC-044", flow="main", steps=[3, 4])
def test_agent_provider_sees_flips_of_already_evaluated_flags(service_stack):
    """experiments_enabled off->on->off and decision.backend rules->openai, each read first."""
    _run_live_go_test(service_stack, "TestLiveFlip_AlreadyEvaluatedFlagsFollowTheFlagService")


@pytest.mark.uc("UC-045", flow="main", steps=[1, 2, 3])
def test_agent_provider_sees_kill_switch_after_enabled_was_read_on(service_stack):
    """enabled read on, then off (overlay off, and overlay cleared to the off baseline)."""
    _run_live_go_test(service_stack, "TestLiveFlip_KillSwitch")


@pytest.mark.uc("UC-045", flow="main", steps=[1, 2, 3])
def test_agent_provider_sees_kill_switch_when_flagd_was_late_at_startup(service_stack):
    """The agent's provider started before flagd answered (compose: flagd only "started"):
    the kill switch still reaches it (the 2026-10-20 dry run, #1709)."""
    _run_live_go_test(service_stack, "TestLiveFlip_KillSwitchWhenFlagdWasLateAtStartup")


@pytest.mark.uc("UC-045", flow="A3")
@pytest.mark.uc("UC-045", flow="main", steps=[1, 2, 3])
def test_agent_provider_reconnects_after_flagd_restart(service_stack):
    """flagd restarts mid-run (#1713): enabled reads off while the sync stream is lost,
    the stream reconnects, and a later kill-switch flip is seen."""
    _run_live_go_test(service_stack, "TestLiveFlip_FlagdRestartMidRun")
