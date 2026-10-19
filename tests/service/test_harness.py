"""The service-tier harness itself (#1524, review of #1548): process lifetime,
host isolation of the service env, and the flag-service binary cache.

No stack is started here; these run without docker or go (except the one
docker test, which skips without it).
"""

import asyncio
import os
import shutil
import subprocess
import sys
import time

import pytest

from tests.service.harness import LOBBY, ServiceStack
from tests.service.harness.flag_plane import _docker, prune_binary_cache
from tests.service.harness.processes import ManagedProcess

pytestmark = pytest.mark.tier("service")

# PR_SET_PDEATHSIG is delivered at once when the forking thread exits; a child
# still running this long after the worker thread ended was not tied to it.
SURVIVAL_WINDOW_SECONDS = 1.0


def _sleeper(tmp_path) -> ManagedProcess:
    return ManagedProcess(
        "sleeper",
        [sys.executable, "-c", "import time; time.sleep(120)"],
        env={},
        log_path=tmp_path / "sleeper.log",
        ready=lambda: True,
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_restart_from_a_worker_thread_survives_the_thread(tmp_path):
    """A restart via ``asyncio.to_thread`` must not tie the new child to that worker thread."""
    proc = _sleeper(tmp_path)
    proc.start()
    first_pid = proc.pid
    try:
        # asyncio.run shuts its default executor down: the worker thread has
        # exited when it returns, as at the end of an async test.
        asyncio.run(asyncio.to_thread(proc.restart))
        assert proc.pid != first_pid

        # Absence assertion: a fixed window is the point (nothing to poll for).
        deadline = time.monotonic() + SURVIVAL_WINDOW_SECONDS
        while time.monotonic() < deadline:
            assert proc.running, f"restarted child died with the worker thread (exit {proc.proc.returncode})"
            time.sleep(0.05)
    finally:
        proc.stop(grace=2)


def test_service_env_never_reaches_host_hardware_or_a_collector(tmp_path):
    stack = ServiceStack(LOBBY, tmp_path)
    ports = {"controller-manager": 40001, "game-coordinator": 40002, "menu": 40003}
    envs = stack.service_envs(ports, mock_port=40004, audio_port=40005)

    assert set(envs) == set(LOBBY.services)
    for name, env in envs.items():
        assert env["OTEL_SDK_DISABLED"] == "true", name
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:1", name
        assert env["AUDIO_HOST"] == "127.0.0.1" and env["AUDIO_PORT"] == "40005", name
        assert env["CONTROLLER_MANAGER_PORT"] == "40001", name
        assert env["GAME_COORDINATOR_PORT"] == "40002", name

    controller_manager = envs["controller-manager"]
    assert controller_manager["RUST_HID_HOST"] == "127.0.0.1"
    assert controller_manager["RUST_HID_PORT"] == "1"
    assert controller_manager["PYTHON_HID_HOST"] == "127.0.0.1"
    assert controller_manager["PYTHON_HID_PORT"] == "1"
    assert controller_manager["DBUS_SYSTEM_BUS_ADDRESS"].startswith("unix:path=/nonexistent/")
    assert controller_manager["MOCK_CONTROL_PORT"] == "40004"


def test_binary_cache_keeps_the_newest(tmp_path):
    for age, name in enumerate(["flag-service-a", "flag-service-b", "flag-service-c", "flag-service-d"]):
        path = tmp_path / name
        path.write_bytes(b"")
        mtime = time.time() - 100 * age  # a newest, d oldest
        os.utime(path, (mtime, mtime))
    (tmp_path / "flag-service-e.tmp-123456").write_bytes(b"")  # a build in progress

    pruned = prune_binary_cache(tmp_path, keep=2)

    assert sorted(p.name for p in pruned) == ["flag-service-c", "flag-service-d"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "flag-service-a",
        "flag-service-b",
        "flag-service-e.tmp-123456",
    ]


@pytest.mark.skipif(
    not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0,
    reason="needs a docker daemon",
)
def test_docker_failure_carries_dockers_stderr():
    with pytest.raises(RuntimeError, match=r"docker restart failed .*joustmania-service-no-such-container"):
        _docker("restart", "joustmania-service-no-such-container")
