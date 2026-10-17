"""A single real flagd for the flagd contract tier (#1422).

Starts ONLY flagd, with the image docker-compose.yml runs, on a temporary copy of
``services/flagd/ci`` (the flag directory the integration stack serves). The copy
is bind-mounted as a DIRECTORY and every write is in place: docker flagd has no
inotify, it stat-polls each file's mtime about once a second, and a temp+rename
write breaks single-file bind mounts.

No game services, no build: the tier proves what only real flagd can show
(server-side targeting evaluation, typed resolution, file-watch reload), in
seconds instead of a full docker-compose session.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import infra_config as ic

CI_FLAG_DIR = ic.REPO_ROOT / "services" / "flagd" / "ci"

EVAL_PORT = 8013  # RPC evaluation API
SYNC_PORT = 8015  # sync stream for the in-process resolver (lib/feature_flags)

# Worst-case bound for a write to be served; a deadline, not a paid wait.
RESOLVE_TIMEOUT_SECONDS = 30.0
# Re-apply an idempotent write this often while waiting: a fresh mtime makes
# flagd reload, which self-heals a missed or coalesced file-watch poll.
REWRITE_INTERVAL_SECONDS = 2.0

# Flag added to a restored file so a fixture can see when flagd serves the
# restored content (flagd reloads a whole file at once).
RESTORE_PROBE_FLAG = "contract_restore_epoch"

# Labels on every contract flagd container, so a run can remove containers a
# killed earlier run left behind (``--rm`` only fires on a clean stop) without
# touching a parallel run's container (another worktree, same docker daemon).
CONTAINER_LABEL = "joustmania.contract=flagd"
OWNER_PID_LABEL = "joustmania.contract.owner_pid"
OWNER_HOST_LABEL = "joustmania.contract.owner_host"
STARTED_LABEL = "joustmania.contract.started"
# Worst-case lifetime of one module run (startup + every test's deadlines, incl.
# NFR-014's 12 samples). An older container is orphaned whoever owns it; this
# also covers owners on another host, whose pid cannot be checked here.
MAX_CONTAINER_AGE_SECONDS = 30 * 60


def flagd_command() -> list[str]:
    """flagd's ``start`` args from docker-compose.yml, minus the OTel exporter.

    Keeps the ``--uri`` list (one file per flag set) in step with the stack.
    """
    command = ic.compose_services()["flagd"]["command"]
    args = ["start"]
    for i, arg in enumerate(command):
        if arg == "--uri":
            args += ["--uri", command[i + 1]]
    return args


def write_in_place(path: Path, text: str) -> float:
    """Write ``text`` without replacing the inode; return the monotonic end time."""
    with open(path, "w") as fh:
        fh.write(text)
        fh.flush()
    return time.monotonic()


def dump_in_place(path: Path, doc: dict) -> None:
    """In-place JSON write, the shape every flag writer in the repo uses."""
    write_in_place(path, json.dumps(doc, indent=2) + "\n")


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def poll_until(
    predicate: Callable[[], bool],
    timeout: float = RESOLVE_TIMEOUT_SECONDS,
    *,
    interval: float = 0.2,
    rewrite: Callable[[], None] | None = None,
) -> bool:
    """Poll ``predicate()`` until true or ``timeout``; return the final value.

    ``rewrite`` is an IDEMPOTENT re-application of the write being waited on,
    re-run every REWRITE_INTERVAL_SECONDS.
    """
    deadline = time.monotonic() + timeout
    last_rewrite = time.monotonic()
    while time.monotonic() < deadline:
        if predicate():
            return True
        if (
            rewrite is not None
            and time.monotonic() - last_rewrite >= REWRITE_INTERVAL_SECONDS
        ):
            rewrite()
            last_rewrite = time.monotonic()
        time.sleep(interval)
    return predicate()


class FlagdContainer:
    """One flagd container serving a writable temp copy of services/flagd/ci."""

    def __init__(self, flag_dir: Path):
        self.flag_dir = flag_dir
        self.name = f"joustmania-contract-flagd-{uuid.uuid4().hex[:8]}"
        self.host = "127.0.0.1"
        self.ports: dict[int, int] = {}

    def file(self, flag_set: str) -> Path:
        """Host path of the file flagd serves for ``flag_set``."""
        return self.flag_dir / f"{flag_set}.json"

    def start(self) -> None:
        remove_stale_containers()
        subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", self.name,
                "--label", CONTAINER_LABEL,
                "--label", f"{OWNER_PID_LABEL}={os.getpid()}",
                "--label", f"{OWNER_HOST_LABEL}={socket.gethostname()}",
                "--label", f"{STARTED_LABEL}={int(time.time())}",
                "-v", f"{self.flag_dir}:/etc/flagd:ro",
                "-p", f"{self.host}::{EVAL_PORT}", "-p", f"{self.host}::{SYNC_PORT}",
                ic.compose_image("flagd"), *flagd_command(),
            ],
            check=True, capture_output=True, text=True,
        )  # fmt: skip
        for port in (EVAL_PORT, SYNC_PORT):
            out = subprocess.run(
                ["docker", "port", self.name, f"{port}/tcp"],
                check=True, capture_output=True, text=True,
            ).stdout  # fmt: skip
            # "127.0.0.1:49153" (one line per binding)
            self.ports[port] = int(out.strip().splitlines()[0].rsplit(":", 1)[1])

    def logs(self) -> str:
        result = subprocess.run(
            ["docker", "logs", self.name], capture_output=True, text=True, check=False
        )
        return (result.stdout + result.stderr)[-4000:]

    def stop(self) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.name], capture_output=True, check=False
        )

    @contextlib.contextmanager
    def client(self, flag_set: str, *, resolver: str = "rpc") -> Iterator:
        """OpenFeature client bound to ``flagSetId=<flag_set>``.

        ``resolver``:
        - ``"rpc"``: flagd's server-side evaluation (port 8013), cache off so
          each read re-resolves (the RPC LRU can serve a first resolution
          forever when a change event is missed, CI run 27029212634);
        - ``"in_process"``: the sync stream (port 8015) every Python service
          reads flags through (lib/feature_flags), evaluated client-side.
        """
        from openfeature import api
        from openfeature.contrib.provider.flagd import FlagdProvider
        from openfeature.contrib.provider.flagd.config import CacheType, ResolverType

        if resolver == "in_process":
            provider = FlagdProvider(
                host=self.host,
                port=self.ports[SYNC_PORT],
                resolver_type=ResolverType.IN_PROCESS,
                selector=f"flagSetId={flag_set}",
            )
        else:
            provider = FlagdProvider(
                host=self.host,
                port=self.ports[EVAL_PORT],
                resolver_type=ResolverType.RPC,
                selector=f"flagSetId={flag_set}",
                cache=CacheType.DISABLED,
            )
        domain = f"contract_{flag_set}_{resolver}_{uuid.uuid4().hex[:6]}"
        api.set_provider(provider, domain=domain)
        try:
            yield api.get_client(domain)
        finally:
            provider.shutdown()

    def wait_served(
        self, flag_set: str, flag_key: str, value: str, *, rewrite=None
    ) -> bool:
        """True once BOTH resolvers serve string ``flag_key`` == ``value``."""
        with (
            self.client(flag_set, resolver="rpc") as rpc,
            self.client(flag_set, resolver="in_process") as in_proc,
        ):
            return poll_until(
                lambda: all(
                    c.get_string_value(flag_key, "", None) == value
                    for c in (rpc, in_proc)
                ),
                rewrite=rewrite,
            )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


def is_stale(labels: dict[str, str], *, host: str, now: float) -> bool:
    """True when a contract container's owning run is gone.

    Stale = older than MAX_CONTAINER_AGE_SECONDS, or started on THIS host by a
    process that no longer exists. A container from a live run (any worktree)
    or from another host within the age bound is left alone.
    """
    try:
        started = float(labels.get(STARTED_LABEL, ""))
    except ValueError:
        started = 0.0  # unlabelled (pre-owner-label) container: treat as old
    if now - started > MAX_CONTAINER_AGE_SECONDS:
        return True
    if labels.get(OWNER_HOST_LABEL) != host:
        return False
    try:
        pid = int(labels.get(OWNER_PID_LABEL, ""))
    except ValueError:
        return True
    return not _pid_alive(pid)


def remove_stale_containers() -> None:
    """Remove contract flagd containers whose owning run is gone (see is_stale)."""
    ids = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={CONTAINER_LABEL}"],
        capture_output=True, text=True, check=False,
    ).stdout.split()  # fmt: skip
    host, now = socket.gethostname(), time.time()
    stale = []
    for container_id in ids:
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Labels}}", container_id],
            capture_output=True, text=True, check=False,
        ).stdout  # fmt: skip
        try:
            labels = json.loads(out) or {}
        except json.JSONDecodeError:
            continue  # gone meanwhile
        if is_stale(labels, host=host, now=now):
            stale.append(container_id)
    if stale:
        subprocess.run(["docker", "rm", "-f", *stale], capture_output=True, check=False)


def copy_ci_flag_dir(dest: Path) -> Path:
    """Copy services/flagd/ci to ``dest`` with permissions flagd (nonroot) can read."""
    shutil.copytree(CI_FLAG_DIR, dest)
    dest.chmod(0o755)
    for path in dest.iterdir():
        path.chmod(0o644)
    return dest
