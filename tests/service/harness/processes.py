"""Subprocess lifecycle for the service tier: free ports, ManagedProcess, PyService.

Every process the harness starts:
- runs in its own process group, so a stop reaches its children too;
- gets ``PR_SET_PDEATHSIG`` (Linux), so it dies with the pytest process even when
  pytest is killed and no fixture teardown runs. The kernel sends that signal
  when the *thread* that forked the child exits, so every spawn goes through
  one long-lived daemon spawner thread (``_spawn``): a start or restart from a
  worker thread (``asyncio.to_thread``) does not tie the child to that worker;
- is registered for an ``atexit`` sweep, the last line of defence on a normal exit;
- writes its output to a log file, whose tail a failed start or a failed test
  prints (``log_tail``).

Readiness is an observable condition (gRPC health SERVING, HTTP 200), never a
fixed sleep; the short poll interval is a wait on that condition.
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from pathlib import Path

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

REPO_ROOT = Path(__file__).resolve().parents[3]

START_TIMEOUT_SECONDS = 45.0
STOP_GRACE_SECONDS = 10.0
READY_POLL_SECONDS = 0.1

# Environment a child inherits from the test process; everything else is set
# explicitly so a developer shell (FLAGD_HOST, OTEL_*, ...) cannot leak in.
_INHERITED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "GOCACHE", "GOPATH", "GOMODCACHE")

_live: set[ManagedProcess] = set()

# Every port free_port() handed out in this process (see free_port).
_issued_ports: set[int] = set()
_issued_ports_lock = threading.Lock()
_FREE_PORT_ATTEMPTS = 100


def free_port() -> int:
    """An ephemeral TCP port on 127.0.0.1 that is free now and new to this session.

    Asking the kernel for port 0 and closing the socket does not reserve the
    port: the next ask can return the same number (about 1 in 200 batches of
    ten asks on Linux, #1612), and a stack allocates all its ports before any
    service binds one. Two services then get one port and the second fails to
    bind (CI: flag service ``grpc_port=56995 http_port=56995``). So a port is
    never handed out twice per pytest process. The kernel may still give a
    port to another process before the service binds it; that window is small,
    and a bind failure surfaces as a failed start with the log tail.
    """
    with _issued_ports_lock:
        for _ in range(_FREE_PORT_ATTEMPTS):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            if port not in _issued_ports:
                _issued_ports.add(port)
                return port
    raise RuntimeError(f"no unused ephemeral port after {_FREE_PORT_ATTEMPTS} attempts")


def base_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ}


def _load_prctl():
    """libc ``prctl``, resolved in the parent: the forked child only calls it."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        return ctypes.CDLL("libc.so.6", use_errno=True).prctl
    except (OSError, AttributeError):
        return None


_PRCTL = _load_prctl()
_PR_SET_PDEATHSIG = 1


def _die_with_parent() -> None:  # pragma: no cover - runs in the child
    """preexec_fn: own process group, and SIGTERM when the pytest process dies."""
    os.setsid()
    if _PRCTL is not None:
        _PRCTL(_PR_SET_PDEATHSIG, signal.SIGTERM)


class _Spawner:
    """One daemon thread that runs every ``Popen`` of the harness.

    ``PR_SET_PDEATHSIG`` fires when the forking thread exits. This thread lives
    as long as the pytest process, so a child dies with pytest, never with the
    (short-lived) thread that asked for the start.
    """

    def __init__(self):
        self._jobs: queue.SimpleQueue[tuple[Callable[[], subprocess.Popen], Future]] = queue.SimpleQueue()
        self._start_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def spawn(self, make: Callable[[], subprocess.Popen]) -> subprocess.Popen:
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="service-tier-spawner", daemon=True)
                self._thread.start()
        future: Future = Future()
        self._jobs.put((make, future))
        return future.result()

    def _run(self) -> None:
        while True:
            make, future = self._jobs.get()
            try:
                future.set_result(make())
            except BaseException as exc:  # re-raised in the caller's thread
                future.set_exception(exc)


_spawn = _Spawner().spawn


class ServiceStartError(RuntimeError):
    pass


class ManagedProcess:
    """One long-running child process with a readiness probe."""

    def __init__(
        self,
        name: str,
        argv: list[str],
        *,
        env: Mapping[str, str],
        log_path: Path,
        ready: Callable[[], bool],
        cwd: Path = REPO_ROOT,
        ready_log_marker: str | None = None,
    ):
        self.name = name
        self.argv = argv
        self.env = dict(env)
        self.log_path = log_path
        self.ready = ready
        self.cwd = cwd
        # Ready only once this line is in the log written by THIS start (not an earlier
        # one of a restart): for a service that reports healthy before its boot work ends.
        self.ready_log_marker = ready_log_marker
        self._log_offset = 0
        self.proc: subprocess.Popen | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self, timeout: float = START_TIMEOUT_SECONDS) -> None:
        if self.running:
            raise RuntimeError(f"{self.name} already running")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "ab") as log:
            log.write(f"\n==== start {self.name}: {' '.join(self.argv)}\n".encode())
            log.flush()
            self._log_offset = log.tell()
            self.proc = _spawn(
                lambda: subprocess.Popen(
                    self.argv,
                    cwd=self.cwd,
                    env=self.env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    preexec_fn=_die_with_parent,  # setsid + pdeathsig only
                )
            )
        _live.add(self)
        deadline = time.monotonic() + timeout
        while True:
            if self.proc.poll() is not None:
                raise ServiceStartError(
                    f"{self.name} exited with {self.proc.returncode} during start\n{self.log_tail()}"
                )
            healthy = self.ready()
            if healthy and self._marker_logged():
                return
            if time.monotonic() > deadline:
                self.stop()
                if healthy:
                    reason = (
                        f"health SERVING but log marker {self.ready_log_marker!r} not seen "
                        "(auto-start off or log level above INFO?)"
                    )
                else:
                    reason = "health not SERVING"
                raise ServiceStartError(f"{self.name} not ready within {timeout}s: {reason}\n{self.log_tail()}")
            time.sleep(READY_POLL_SECONDS)

    def _marker_logged(self) -> bool:
        if self.ready_log_marker is None:
            return True
        with open(self.log_path, "rb") as log:
            log.seek(self._log_offset)
            return self.ready_log_marker.encode() in log.read()

    def stop(self, grace: float = STOP_GRACE_SECONDS) -> int | None:
        """SIGTERM the process group, SIGKILL after ``grace``; return the exit code."""
        proc, self.proc = self.proc, None
        _live.discard(self)
        if proc is None:
            return None
        if proc.poll() is None:
            self._signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self._signal_group(proc, signal.SIGKILL)
                proc.wait(timeout=5)
        return proc.returncode

    def restart(self, timeout: float = START_TIMEOUT_SECONDS) -> None:
        """Stop and start again on the same ports and state."""
        self.stop()
        self.start(timeout=timeout)

    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)

    # ---------------------------------------------------------------- status
    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def log_tail(self, lines: int = 60) -> str:
        try:
            text = self.log_path.read_text(errors="replace")
        except FileNotFoundError:
            return f"[{self.name}: no log]"
        tail = "\n".join(text.splitlines()[-lines:])
        return f"---- {self.name} log tail ({self.log_path}) ----\n{tail}"


def grpc_health_ready(port: int, service: str = "") -> Callable[[], bool]:
    """Readiness probe: gRPC health ``service`` is SERVING on 127.0.0.1:port."""

    def probe() -> bool:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            try:
                resp = health_pb2_grpc.HealthStub(channel).Check(
                    health_pb2.HealthCheckRequest(service=service), timeout=1.0
                )
            except grpc.RpcError:
                return False
            return resp.status == health_pb2.HealthCheckResponse.SERVING

    return probe


class PyService(ManagedProcess):
    """A Python service from source at HEAD: ``python services/<dir>/server.py``.

    Started exactly as its image starts it (WORKDIR = repo root, PYTHONPATH =
    repo root), with this interpreter (the service-tests venv holds every
    service's dependencies). Ready when its gRPC health reports SERVING.
    """

    def __init__(
        self,
        name: str,
        source_dir: str,
        *,
        grpc_port: int,
        env: Mapping[str, str],
        log_path: Path,
        ready_log_marker: str | None = None,
    ):
        full_env = {
            **base_env(),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUNBUFFERED": "1",
            "GRPC_PORT": str(grpc_port),
            **env,
        }
        super().__init__(
            name,
            [sys.executable, f"services/{source_dir}/server.py"],
            env=full_env,
            log_path=log_path,
            ready=grpc_health_ready(grpc_port),
            ready_log_marker=ready_log_marker,
        )
        self.grpc_port = grpc_port


@atexit.register
def _stop_all() -> None:
    for proc in list(_live):
        proc.stop(grace=3)
