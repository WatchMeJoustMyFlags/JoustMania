"""The service-tier harness itself (#1524, review of #1548): process lifetime,
host isolation of the service env, and the flag-service binary cache.

No stack is started here; these run without docker or go (except the one
docker test, which skips without it).
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from tests.service.harness import AGENT, LOBBY, Composition, FlagPlane, ServiceStack, processes
from tests.service.harness.flag_plane import _docker, fh, prune_binary_cache
from tests.service.harness.processes import ManagedProcess, free_port

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


class _KernelSocket:
    """A socket whose bind to port 0 returns the next port of a fixed sequence."""

    def __init__(self, ports):
        self._ports = ports

    def __call__(self, *_args):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def bind(self, _address):
        self._port = next(self._ports)

    def getsockname(self):
        return ("127.0.0.1", self._port)


def test_free_port_never_hands_out_a_port_twice(monkeypatch):
    """#1612: the kernel can return a just-closed port again; a stack allocates all
    its ports before any bind, so two services got the same port in CI."""
    monkeypatch.setattr(processes, "_issued_ports", set(), raising=False)
    monkeypatch.setattr(processes.socket, "socket", _KernelSocket(iter([56995, 56995, 56996, 56995, 56997])))

    assert [free_port(), free_port(), free_port()] == [56995, 56996, 56997]


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


def test_binary_cache_prunes_per_binary(tmp_path):
    """The agent's binaries and the flag service's share the cache but not the keep budget."""
    for age, name in enumerate(["agent-a", "agent-b", "flag-service-a"]):
        path = tmp_path / name
        path.write_bytes(b"")
        mtime = time.time() - 100 * age
        os.utime(path, (mtime, mtime))

    pruned = prune_binary_cache(tmp_path, keep=1, prefix="agent-")

    assert [p.name for p in pruned] == ["agent-b"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["agent-a", "flag-service-a"]


def test_agent_composition_wires_sink_agent_and_fake(tmp_path):
    """No start: the sink tees to the agent's port, and the fake OpenAI URL lands in the baseline variant."""
    stack = ServiceStack(AGENT, tmp_path)
    assert stack.otlp_sink is not None and stack.fake_openai is not None
    assert stack.otlp_sink.forward_to == f"127.0.0.1:{stack.agent_port}"
    assert stack.flag_plane.variant_values == {
        ("agent", "decision.openai.base_url", "fake"): f"http://127.0.0.1:{stack.fake_openai.port}/v1"
    }
    assert ServiceStack(LOBBY, tmp_path).fake_openai is None
    with pytest.raises(ValueError, match="otlp=True"):
        Composition(("controller-manager", "game-coordinator"), agent=True)


def test_flag_plane_rewrites_only_declared_variants(tmp_path):
    variant = ("agent", "decision.openai.base_url", "fake")
    plane = FlagPlane(tmp_path / "a", variant_values={variant: "http://127.0.0.1:9/v1"})
    fh.copy_ci_flag_dir(plane.flag_dir)
    plane._rewrite_variants()
    flags = json.loads((plane.flag_dir / "agent.json").read_text())["flags"]
    assert flags["decision.openai.base_url"]["variants"]["fake"] == "http://127.0.0.1:9/v1"

    unknown = FlagPlane(tmp_path / "b", variant_values={("agent", "decision.openai.base_url", "nope"): "x"})
    fh.copy_ci_flag_dir(unknown.flag_dir)
    with pytest.raises(KeyError, match="no variant 'nope'"):
        unknown._rewrite_variants()


@pytest.mark.skipif(
    not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0,
    reason="needs a docker daemon",
)
def test_docker_failure_carries_dockers_stderr():
    with pytest.raises(RuntimeError, match=r"docker restart failed .*joustmania-service-no-such-container"):
        _docker("restart", "joustmania-service-no-such-container")


class _FakeStack:
    """ServiceStack stand-in: start() fails with the given messages in turn."""

    outcomes: list[str | None] = []
    started = 0

    def __init__(self, _composition, workdir):
        self.workdir = workdir

    def start(self):
        outcome = type(self).outcomes[type(self).started]
        type(self).started += 1
        if outcome is not None:
            raise processes.ServiceStartError(outcome)
        return self


def _start_with(monkeypatch, tmp_path, outcomes):
    from tests.service.harness import stack as stack_module

    fake = type("Fake", (_FakeStack,), {"outcomes": outcomes, "started": 0})
    monkeypatch.setattr(stack_module, "ServiceStack", fake)
    workdirs = []

    def make_workdir():
        workdirs.append(tmp_path / f"w{len(workdirs)}")
        return workdirs[-1]

    return stack_module.start_stack(LOBBY, make_workdir), fake, workdirs


def test_start_stack_retries_on_fresh_ports_when_a_port_is_in_use(monkeypatch, tmp_path):
    """#1763: a service lost the bind race; the stack is rebuilt (new workdir, new ports)."""
    in_use = "agent exited with 1 during start\nlisten tcp 127.0.0.1:36905: bind: address already in use"
    started, fake, workdirs = _start_with(monkeypatch, tmp_path, [in_use, in_use, None])

    assert fake.started == 3
    assert started.workdir == workdirs[-1] and len(set(workdirs)) == 3


def test_start_stack_gives_up_after_three_port_conflicts(monkeypatch, tmp_path):
    in_use = "bind: address already in use"
    with pytest.raises(processes.ServiceStartError, match="already in use"):
        _start_with(monkeypatch, tmp_path, [in_use] * 3)


def test_start_stack_does_not_retry_other_start_failures(monkeypatch, tmp_path):
    from tests.service.harness import stack as stack_module

    fake = type("Fake", (_FakeStack,), {"outcomes": ["menu not ready within 45s"], "started": 0})
    monkeypatch.setattr(stack_module, "ServiceStack", fake)
    with pytest.raises(processes.ServiceStartError, match="not ready"):
        stack_module.start_stack(LOBBY, lambda: tmp_path)
    assert fake.started == 1


def test_start_stack_retries_on_grpc_failed_to_bind_and_warns(monkeypatch, tmp_path, caplog):
    """Python gRPC's RuntimeError text; the C++ "address already in use" line may be hidden."""
    bind_error = "game-coordinator exited with 1 during start\nRuntimeError: Failed to bind to address 127.0.0.1:1"
    with caplog.at_level("WARNING", logger="tests.service.harness.stack"):
        _, fake, _ = _start_with(monkeypatch, tmp_path, [bind_error, None])

    assert fake.started == 2
    assert any("1/3" in r.getMessage() and "game-coordinator exited with 1" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# #1798: a service healthy before its boot work ends is not ready yet
# --------------------------------------------------------------------------- #
BOOT_DELAY_SECONDS = 1.0
_PRINTS_MARKER_LATE = (
    f"import time; time.sleep({BOOT_DELAY_SECONDS}); print('Menu auto-started', flush=True); time.sleep(120)"
)


def _healthy_early(tmp_path, code: str) -> ManagedProcess:
    return ManagedProcess(
        "late-boot",
        [sys.executable, "-c", code],
        env={},
        log_path=tmp_path / "late-boot.log",
        ready=lambda: True,  # the health probe: SERVING from the first poll
        ready_log_marker="Menu auto-started",
    )


@pytest.mark.uc("UC-010", flow="BR-003")
def test_start_waits_for_the_boot_marker_after_health_is_serving(tmp_path):
    """Health SERVING is not enough: start() returns only once the boot lobby entry is logged."""
    proc = _healthy_early(tmp_path, _PRINTS_MARKER_LATE)
    began = time.monotonic()
    try:
        proc.start()
        assert time.monotonic() - began >= BOOT_DELAY_SECONDS
        assert "Menu auto-started" in proc.log_path.read_text()
    finally:
        proc.stop(grace=2)


@pytest.mark.uc("UC-010", flow="BR-003")
def test_restart_ignores_the_marker_of_the_previous_start(tmp_path):
    """The log is appended across restarts: the first start's marker must not satisfy the second."""
    proc = _healthy_early(tmp_path, _PRINTS_MARKER_LATE)
    try:
        proc.start()
        proc.stop(grace=2)
        began = time.monotonic()
        proc.start()
        assert time.monotonic() - began >= BOOT_DELAY_SECONDS
    finally:
        proc.stop(grace=2)
