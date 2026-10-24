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

import atexit
import functools
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from pathlib import Path

from .processes import REPO_ROOT, ManagedProcess, base_env, free_port

sys.path.insert(0, str(REPO_ROOT / "tests" / "contract"))

import flagd_harness as fh  # noqa: E402  (tests/contract, shared with the contract tier)
import infra_config as ic  # noqa: E402

FLAG_SERVICE_SRC = REPO_ROOT / "services" / "flag-service"
FLAG_SERVICE_SETS = "system,observability,interventions,rollout,agent,game,user,controller,operator"

# Service tier containers: own label prefix, so the contract tier's stale sweep
# and this one never touch each other's containers.
OWNERSHIP = fh.Ownership("joustmania.service")

FLAGD_EVAL_PORT = fh.EVAL_PORT  # 8013 in the container
FLAGD_SYNC_PORT = fh.SYNC_PORT  # 8015 in the container
FLAGD_READY_TIMEOUT_SECONDS = 30.0
# Flag-service binaries kept in the cache (newest by mtime); older ones are pruned.
BINARY_CACHE_KEEP = 5
READY_POLL_SECONDS = 0.1


def _cache_dir() -> Path:
    root = os.environ.get("JOUSTMANIA_SERVICE_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "joustmania-service-tier"
    )
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


@functools.cache
def _go_version() -> str:
    result = subprocess.run(["go", "env", "GOVERSION"], capture_output=True, text=True, check=False)
    return result.stdout.strip() or "unknown"


def flag_service_source_hash() -> str:
    """Hash of every file ``go build`` reads (sources, go.mod/go.sum, registry) and the Go toolchain."""
    digest = hashlib.sha256()
    digest.update(_go_version().encode())
    for path in sorted(FLAG_SERVICE_SRC.rglob("*")):
        if path.is_file() and (path.suffix in (".go", ".mod", ".sum") or path.name == "registry.yaml"):
            digest.update(str(path.relative_to(FLAG_SERVICE_SRC)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def build_flag_service() -> Path:
    """The flag-service binary for HEAD's sources; built once per source hash."""
    binary = _cache_dir() / f"flag-service-{flag_service_source_hash()}"
    if binary.exists():
        binary.touch()  # mtime = last use, for the pruning below
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
    prune_binary_cache(binary.parent)
    return binary


def prune_binary_cache(cache: Path, keep: int = BINARY_CACHE_KEEP, prefix: str = "flag-service-") -> list[Path]:
    """Delete all but the ``keep`` most recently used ``<prefix>*`` binaries; return the deleted ones.

    A parallel run may be using a pruned binary: on Linux its running process
    keeps the inode, and a later start rebuilds it.
    """
    binaries = sorted(
        (p for p in cache.glob(f"{prefix}*") if p.is_file() and ".tmp-" not in p.name),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    pruned = binaries[keep:]
    for path in pruned:
        path.unlink(missing_ok=True)
    return pruned


def _docker(*args: str) -> subprocess.CompletedProcess:
    """Run ``docker <args>``; a failure raises with docker's own stderr in the message."""
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"docker {args[0]} failed (exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


# flagd containers of this process that are still up: removed at exit when a
# fixture teardown did not run (SIGTERM via KeyboardInterrupt, an abort).
_live_containers: set[str] = set()


@atexit.register
def _remove_live_containers() -> None:
    for name in list(_live_containers):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        _live_containers.discard(name)


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

    def __init__(self, workdir: Path, variant_values: Mapping[tuple[str, str, str], object] | None = None):
        """``variant_values``: ``{(flag_set, flag, variant): value}`` rewritten in the
        temporary baseline copy before the flag service starts (e.g. the ``fake``
        variant of ``decision.openai.base_url`` -> the stack's fake). The variant must
        exist: a test-only value never adds a variant the registry does not know."""
        self.workdir = workdir
        self.variant_values = dict(variant_values or {})
        self.flag_dir = workdir / "flags"
        self.state_file = workdir / "flag-service-state" / "admin-ci.json"
        self.grpc_port = free_port()
        self.http_port = free_port()
        # Host ports for flagd: picked right before ``docker run`` (after the
        # possibly slow ``go build``), so the window for a clash stays small.
        self.flagd_eval_port = 0
        self.flagd_sync_port = 0
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
        self._rewrite_variants()
        self._agent_debug_logs()
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
                # No OTEL_EXPORTER_OTLP_ENDPOINT (the env starts empty, base_env
                # does not pass it through): tracing and metrics are no-ops.
            },
            log_path=self.workdir / "logs" / "flag-service.log",
            ready=lambda: _http_ok(f"{self.flag_service_url}/healthz"),
        )
        self.flag_service.start()
        self._start_flagd()

    def _agent_debug_logs(self) -> None:
        """The agent logs at debug in this tier (the shadow-experiment tests read its debug
        lines): the ``log_level`` flag wins over LOG_LEVEL since #1788, so target it here."""
        path = self.flag_dir / "observability.json"
        doc = json.loads(path.read_text())
        doc["flags"]["log_level"]["targeting"] = {"if": [{"==": [{"var": "service"}, "agent"]}, "debug", "info"]}
        path.write_text(json.dumps(doc, indent=2) + "\n")

    def _rewrite_variants(self) -> None:
        for (flag_set, flag, variant), value in self.variant_values.items():
            path = self.flag_dir / f"{flag_set}.json"
            doc = json.loads(path.read_text())
            variants = doc["flags"][flag]["variants"]
            if variant not in variants:
                raise KeyError(f"{flag_set}/{flag} has no variant {variant!r} (variants: {sorted(variants)})")
            variants[variant] = value
            path.write_text(json.dumps(doc, indent=2) + "\n")

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
        _docker("restart", "-t", "5", self.container)
        self._wait_flagd_ready()

    # ----------------------------------------------------------------- flagd
    def _start_flagd(self) -> None:
        fh.remove_stale_containers(OWNERSHIP)
        self.flagd_eval_port = free_port()
        self.flagd_sync_port = free_port()
        _live_containers.add(self.container)  # before run: a half-created container is swept too
        _docker(
            "run", "-d", "--name", self.container,
            *OWNERSHIP.docker_run_args(),
            "--add-host", "host.docker.internal:host-gateway",
            "-p", f"127.0.0.1:{self.flagd_eval_port}:{FLAGD_EVAL_PORT}",
            "-p", f"127.0.0.1:{self.flagd_sync_port}:{FLAGD_SYNC_PORT}",
            ic.compose_image("flagd"),
            "start", "--uri", f"grpc://host.docker.internal:{self.grpc_port}",
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
        if self._flagd_started or self.container in _live_containers:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, check=False)
            self._flagd_started = False
            _live_containers.discard(self.container)

    def log_tail(self) -> str:
        parts = [self.flagd_logs()] if self._flagd_started else []
        if self.flag_service is not None:
            parts.append(self.flag_service.log_tail())
        return "\n".join(parts)
