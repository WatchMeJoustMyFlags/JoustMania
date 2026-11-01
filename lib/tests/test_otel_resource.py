"""Tests for lib/otel_resource.py: one writer per series identity (#2543).

VictoriaMetrics keys a series on ``job`` (``service.namespace/service.name``) and
``instance`` (``service.instance.id``). Two processes that share an ``instance`` write
their cumulative counters into one series and every interleaved sample counts as a
counter reset. On the stage Pi two hosts booted from the same Docker state (same
container ids, so the same ``HOSTNAME``) did exactly that: 199 resets on
``game_player_deaths_total`` in four minutes.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import lib.otel_resource as otel_resource
from lib.otel_resource import get_otel_resource

REPO_ROOT = Path(__file__).resolve().parents[2]

_PRINT_INSTANCE = (
    "from lib.otel_resource import get_otel_resource;print(get_otel_resource().attributes['service.instance.id'])"
)


def _instance_id_in_new_process(hostname: str) -> str:
    """service.instance.id of a fresh Python process that sees the given HOSTNAME."""
    env = {**os.environ, "HOSTNAME": hostname, "PYTHONPATH": str(REPO_ROOT)}
    out = subprocess.run(
        [sys.executable, "-c", _PRINT_INSTANCE],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return out.stdout.strip()


@pytest.mark.uc("UC-035", br=["BR-014"])
def test_two_processes_with_one_hostname_get_distinct_instance_ids():
    """Two metric providers on one host (same container id) never share a series identity."""
    first = _instance_id_in_new_process("6f21301bee4e")
    second = _instance_id_in_new_process("6f21301bee4e")

    assert first != second
    # The container id stays readable as the prefix, so `docker ps` still maps it.
    assert first.startswith("6f21301bee4e-")
    assert second.startswith("6f21301bee4e-")


@pytest.mark.uc("UC-035", br=["BR-014"])
def test_one_process_uses_one_instance_id_for_every_signal():
    """Traces, metrics and logs of one process carry the same service.instance.id."""
    ids = {get_otel_resource().attributes["service.instance.id"] for _ in range(3)}

    assert len(ids) == 1


@pytest.mark.uc("UC-035", br=["BR-014"])
@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is POSIX only")
def test_forked_child_gets_its_own_instance_id():
    """A forked child that builds its own provider must not reuse the parent's id."""
    parent_id = get_otel_resource().attributes["service.instance.id"]
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        os.close(read_fd)
        child_id = otel_resource.get_otel_resource().attributes["service.instance.id"]
        os.write(write_fd, child_id.encode())
        os._exit(0)
    os.close(write_fd)
    child_id = os.read(read_fd, 256).decode()
    os.close(read_fd)
    os.waitpid(pid, 0)

    assert child_id
    assert child_id != parent_id


@pytest.mark.uc("UC-035", br=["BR-014"])
def test_instance_id_falls_back_to_node_name_without_hostname(monkeypatch):
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setattr(otel_resource.platform, "node", lambda: "pi-stage")
    monkeypatch.setattr(otel_resource, "_instance_id", None)

    assert get_otel_resource().attributes["service.instance.id"].startswith("pi-stage-")
