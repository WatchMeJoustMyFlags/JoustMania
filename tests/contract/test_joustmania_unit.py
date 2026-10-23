"""The autostart unit keeps the stack up across boots and failures (UC-060, #2027).

Static checks over ``scripts/setup/joustmania.service``: nothing in it removes or tears
down containers (a reboot with unchanged images recreates 0, NFR-026), a stop is a plain
compose ``stop``, retries are bounded (3 in 15 minutes, A5), the unit requires Docker and
orders after Docker and Bluetooth (Pre-1), and it declares the revision the start script
checks. ``Wants=network-online.target`` stays until the maintainer decides #2027 Decision 10;
it is not asserted either way (docker.service wants network-online.target anyway).
"""

from __future__ import annotations

import configparser
import re
import shutil
import subprocess

import infra_config as ic
import pytest

UNIT = ic.REPO_ROOT / "scripts" / "setup" / "joustmania.service"


def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(UNIT.read_text())
    return parser


def _lines(section: str, key: str) -> list[str]:
    # configparser keeps only the last duplicate key; systemd keys such as After= repeat
    out, current = [], None
    for raw in UNIT.read_text().splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
        elif current == section and line.startswith(f"{key}="):
            out.append(line.split("=", 1)[1])
    return out


@pytest.mark.uc("UC-060", flow="A5")
def test_start_attempts_are_bounded_to_3_in_15_minutes():
    assert _lines("Unit", "StartLimitIntervalSec") == ["900"]
    assert _lines("Unit", "StartLimitBurst") == ["3"]


@pytest.mark.uc("UC-060", cond=["Pre-1"])
def test_unit_requires_docker_and_orders_after_docker_and_bluetooth():
    after = " ".join(_lines("Unit", "After")).split()
    wants = " ".join(_lines("Unit", "Wants")).split()
    assert "bluetooth.service" in after
    assert "bluetooth.service" in wants
    assert "docker.service" in after
    assert "docker.service" in " ".join(_lines("Unit", "Requires")).split()


@pytest.mark.uc("UC-060", cond=["Post-F-1"])
def test_unit_removes_nothing_before_start():
    exec_start_pre = _lines("Service", "ExecStartPre")
    text = "\n".join(exec_start_pre)
    assert not re.search(r"\brm\b|prune|\bdown\b", text), (
        f"ExecStartPre tears containers down: {exec_start_pre}"
    )
    # and nowhere in the unit file
    body = "\n".join(
        line
        for line in UNIT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert not re.search(r"docker (container )?prune|\brm -f\b", body)


@pytest.mark.uc("UC-060", cond=["Post-F-1"])
def test_stop_is_a_plain_compose_stop_not_down():
    (stop,) = _lines("Service", "ExecStop")
    assert stop.endswith("joustmania-compose.sh stop --timeout 20")
    assert " down" not in stop


@pytest.mark.uc("UC-060")
def test_unit_declares_revision_runtime_directory_and_timeouts():
    assert "JOUSTMANIA_UNIT_REV=2" in " ".join(_lines("Service", "Environment"))
    assert _lines("Service", "RuntimeDirectory") == ["joustmania"]
    assert _lines("Service", "TimeoutStartSec") == ["900"]
    assert _lines("Service", "Type") == ["oneshot"]
    assert _lines("Service", "RemainAfterExit") == ["yes"]


@pytest.mark.uc("UC-060")
def test_unit_parses_and_verifies_when_systemd_analyze_is_available(tmp_path):
    assert _unit().has_section("Service")
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not installed")
    copy = tmp_path / "joustmania.service"
    # verify checks that ExecStart exists: point it at the repo's own scripts
    copy.write_text(UNIT.read_text().replace("/home/pi/JoustMania", str(ic.REPO_ROOT)))
    result = subprocess.run(
        ["systemd-analyze", "verify", str(copy)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    problems = [
        line
        for line in result.stderr.splitlines()
        if "joustmania.service" in line
        and "User" not in line
        and "docker.service" not in line
    ]
    assert not problems, "\n".join(problems)
