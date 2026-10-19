"""FlagPlane: a fresh flag plane per stack (flag service from source + one flagd).

- flag service: ``go build`` of ``services/flag-service`` at HEAD, cached by a
  hash of its sources, run as a subprocess on ephemeral ports with a temporary
  state file and a temporary copy of ``services/flagd/ci`` as its baseline (the
  directory the compose CI stack mounts);
- flagd: the compose image (``docker-compose.yml`` tag), whose only source is the
  flag service's sync stream (``--uri grpc://...``), as in the stack. It reaches
  the host process through ``host.docker.internal`` (``host-gateway``) and
  publishes its RPC (8013) and sync (8015) ports on fixed ephemeral host ports,
  so a ``restart_flagd()`` keeps the addresses every service already uses.

Containers carry the ``joustmania.service`` ownership labels
(tests/contract/flagd_harness.py ``Ownership``): a start first removes the
containers of runs that are gone, never a live parallel run's.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .processes import REPO_ROOT, ManagedProcess, base_env, free_port

sys.path.insert(0, str(REPO_ROOT / "tests" / "contract"))

import flagd_harness as fh  # noqa: E402  (tests/contract, shared with the contract tier)
import infra_config as ic  # noqa: E402

FLAG_SERVICE_SRC = REPO_ROOT / "services" / "flag-service"
FLAG_SERVICE_SETS = "system,observability,interventions,rollout,agent,game,user,controller"

# Service tier containers: own label prefix, so the contract tier's stale sweep
# and this one never touch each other's containers.
OWNERSHIP = fh.Ownership("joustmania.service")

FLAGD_EVAL_PORT = fh.EVAL_PORT  # 8013 in the container
FLAGD_SYNC_PORT = fh.SYNC_PORT  # 8015 in the container
FLAGD_READY_TIMEOUT_SECONDS = 30.0
READY_POLL_SECONDS = 0.1


def _cache_dir() -> Path:
    root = os.environ.get("JOUSTMANIA_SERVICE_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "joustmania-service-tier"
    )
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def flag_service_source_hash() -> str:
    """Hash of every file ``go build`` reads (sources, go.mod/go.sum, registry)."""
    digest = hashlib.sha256()
    for path in sorted(FLAG_SERVICE_SRC.rglob("*")):
        if path.is_file() and (path.suffix in (".go", ".mod", ".sum") or path.name == "registry.yaml"):
            digest.update(str(path.relative_to(FLAG_SERVICE_SRC)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def build_flag_service() -> Path:
    """The flag-service binary for HEAD's sources; built once per source hash."""
    binary = _cache_dir() / f"flag-service-{flag_service_source_hash()}"
    if binary.exists():
        return binary
    tmp = binary.with_suffix(f".tmp-{uuid.uuid4().hex[:6]}")
    result = subprocess.run(
        ["go", "build", "-o", str(tmp), "."],
        cwd=FLAG_SERVICE_SRC,
        env={**os.environ, "CGO_ENABLED": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"go build services/flag-service failed:\n{result.stdout}{result.stderr}")
    tmp.replace(binary)  # atomic: a parallel run sees a whole binary or none
    return binary


def _http_ok(url: str, body: bytes | None = None) -> bool:
    req = urllib.request.Request(
        url, data=body, method="POST" if body is not None else "GET", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False


class FlagPlane:
    """One flag service + one flagd, both fresh, on ephemeral ports."""

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.flag_dir = workdir / "flags"
        self.state_file = workdir / "flag-service-state" / "admin-ci.json"
        self.grpc_port = free_port()
        self.http_port = free_port()
        self.flagd_eval_port = free_port()
        self.flagd_sync_port = free_port()
        self.container = f"joustmania-service-flagd-{uuid.uuid4().hex[:8]}"
        self.flag_service: ManagedProcess | None = None
        self._flagd_started = False

    # -------------------------------------------------------------- addresses
    @property
    def flag_service_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        fh.copy_ci_flag_dir(self.flag_dir)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        binary = build_flag_service()
        self.flag_service = ManagedProcess(
            "flag-service",
            [str(binary)],
            env={
                **base_env(),
                "FLAG_SERVICE_SETS": FLAG_SERVICE_SETS,
                "FLAG_SERVICE_BASELINE_DIR": str(self.flag_dir),
                "FLAG_SERVICE_STATE_FILE": str(self.state_file),
                "FLAG_SERVICE_REGISTRY": str(FLAG_SERVICE_SRC / "registry.yaml"),
                "FLAG_SERVICE_GRPC_PORT": str(self.grpc_port),
                "FLAG_SERVICE_HTTP_PORT": str(self.http_port),
                # No OTEL_EXPORTER_OTLP_ENDPOINT: tracing and metrics are no-ops.
            },
            log_path=self.workdir / "logs" / "flag-service.log",
            ready=lambda: _http_ok(f"{self.flag_service_url}/healthz"),
        )
        self.flag_service.start()
        self._start_flagd()

    def stop(self) -> None:
        self._stop_flagd()
        if self.flag_service is not None:
            self.flag_service.stop()

    def restart_flag_service(self) -> None:
        """Stop and start the flag service on the same ports and state file.

        In-memory runtime values are gone afterwards; persisted admin values
        come back from the state file (C-030). flagd reconnects on its own.
        """
        assert self.flag_service is not None
        self.flag_service.restart()

    def restart_flagd(self) -> None:
        """``docker restart`` flagd (same host ports); wait until it serves again."""
        subprocess.run(["docker", "restart", "-t", "5", self.container], check=True, capture_output=True)
        self._wait_flagd_ready()

    # ----------------------------------------------------------------- flagd
    def _start_flagd(self) -> None:
        fh.remove_stale_containers(OWNERSHIP)
        subprocess.run(
            [
                "docker", "run", "-d", "--name", self.container,
                *OWNERSHIP.docker_run_args(),
                "--add-host", "host.docker.internal:host-gateway",
                "-p", f"127.0.0.1:{self.flagd_eval_port}:{FLAGD_EVAL_PORT}",
                "-p", f"127.0.0.1:{self.flagd_sync_port}:{FLAGD_SYNC_PORT}",
                ic.compose_image("flagd"),
                "start", "--uri", f"grpc://host.docker.internal:{self.grpc_port}",
            ],
            check=True, capture_output=True, text=True,
        )  # fmt: skip
        self._flagd_started = True
        self._wait_flagd_ready()

    def _wait_flagd_ready(self) -> None:
        """Ready = flagd's RPC API resolves the flag service's configuration."""
        url = f"http://127.0.0.1:{self.flagd_eval_port}/flagd.evaluation.v1.Service/ResolveAll"
        deadline = time.monotonic() + FLAGD_READY_TIMEOUT_SECONDS
        while not self._resolves_flags(url):
            if time.monotonic() > deadline:
                raise RuntimeError(f"flagd did not serve the flag service's flags\n{self.flagd_logs()}")
            time.sleep(READY_POLL_SECONDS)

    @staticmethod
    def _resolves_flags(url: str) -> bool:
        req = urllib.request.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                return bool(json.loads(resp.read() or b"{}").get("flags"))
        except (urllib.error.URLError, ConnectionError, TimeoutError, ValueError):
            return False

    def flagd_logs(self, chars: int = 4000) -> str:
        result = subprocess.run(["docker", "logs", self.container], capture_output=True, text=True, check=False)
        return f"---- flagd ({self.container}) ----\n{(result.stdout + result.stderr)[-chars:]}"

    def _stop_flagd(self) -> None:
        if self._flagd_started:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, check=False)
            self._flagd_started = False

    def log_tail(self) -> str:
        parts = [self.flagd_logs()] if self._flagd_started else []
        if self.flag_service is not None:
            parts.append(self.flag_service.log_tail())
        return "\n".join(parts)


def remove_workdir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
