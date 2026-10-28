"""The talk runbook is rehearsable and names only things that exist (FR-094).

These are doc-existence checks for FR-094, not UC-070 scenario tests, so they carry no
``uc`` marker (no flow, step or br fits; the repo has no FR-only tag). The behaviour of
the reset and the screen-only fallback is covered by scripts/tests/test-talk-stage.sh
and the dashboard follow tests.

Static checks over ``docs/talk/two-standards-runbook.md``: the beat table, the timing
table, the fallbacks and the reset section exist, and every script, make target and repo path
the runbook names, plus every flag key in the beat table and in a setflag / flags.sh
command, resolves in the repo (flags named only in prose are not checked). A renamed script or flag breaks the
runbook silently otherwise; the hardware rehearsal (#743) is an operational readiness
check, not what verifies this file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "docs" / "talk" / "two-standards-runbook.md"
FLAGD_DIR = REPO_ROOT / "services" / "flagd"
FLAGS_SPEC = REPO_ROOT / "docs" / "specs" / "flags.md"

# Beats the talk cannot run without; extra beats (6c, ...) are allowed.
REQUIRED_BEAT_IDS = {"1", "2", "3a", "3b", "4", "5", "6", "7", "8"}
REQUIRED_SECTIONS = [
    "Setup",
    "Talk timing",
    "Beat table",
    "Pre-flight checklist",
    "Reset to baseline",
    "Failure modes and recovery",
]


def _text() -> str:
    return RUNBOOK.read_text()


def _section(title_part: str) -> str:
    """Body of the ``## N. <title_part>...`` section, up to the next ``## `` heading."""
    match = re.search(
        rf"^## \d+\. {re.escape(title_part)}.*?$(.*?)(?=^## |\Z)",
        _text(),
        re.DOTALL | re.MULTILINE,
    )
    assert match, f"runbook has no section '{title_part}'"
    return match.group(1)


def _table_rows(body: str) -> list[list[str]]:
    """Data rows of the first markdown table in ``body`` (header and separator dropped)."""
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in body.splitlines()
        if line.startswith("|")
    ]
    return [row for row in rows[2:] if row]


def _flag_keys() -> set[str]:
    keys: set[str] = set()
    for path in FLAGD_DIR.rglob("*.json"):
        keys.update(json.loads(path.read_text()).get("flags", {}))
    return keys


# --------------------------------------------------------------------------- #
# Structure: beat table, timings, fallbacks, reset
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title", REQUIRED_SECTIONS)
def test_runbook_has_required_section(title):
    assert _section(title).strip(), f"runbook section '{title}' is empty"


def test_beat_table_has_every_beat_with_command_and_fallback():
    rows = _table_rows(_section("Beat table"))
    by_id = {row[0]: row for row in rows}
    assert len(by_id) == len(rows), "duplicate beat id in the beat table"
    assert REQUIRED_BEAT_IDS <= set(by_id), (
        f"beat table misses {sorted(REQUIRED_BEAT_IDS - set(by_id))}"
    )
    for beat_id, row in by_id.items():
        assert len(row) == 7, f"beat {beat_id}: expected 7 columns, got {len(row)}"
        command, fallback = row[2], row[6]
        assert command, f"beat {beat_id} has no command"
        assert fallback, f"beat {beat_id} has no fallback"


def test_timing_table_lists_every_live_beat_with_a_clock_time():
    rows = _table_rows(_section("Talk timing"))
    assert re.fullmatch(r"\d+:\d{2}", rows[0][0])
    assert all(re.fullmatch(r"\d+:\d{2}", row[0]) for row in rows)
    segments = " ".join(row[1] for row in rows)
    for beat_id in REQUIRED_BEAT_IDS:
        assert f"Beat {beat_id}:" in segments, f"timing table misses beat {beat_id}"


def test_fallbacks_cover_bluetooth_down():
    assert re.search(r"^### Fallback: Bluetooth down", _text(), re.MULTILINE)


def test_reset_section_runs_the_stage_reset():
    assert "talk-stage.sh reset" in _section("Reset to baseline")


# --------------------------------------------------------------------------- #
# Everything the runbook names exists
# --------------------------------------------------------------------------- #
def test_every_named_script_exists():
    names = set(re.findall(r"scripts/[A-Za-z0-9_./-]*[A-Za-z0-9_]", _text()))
    names = {n for n in names if "." in Path(n).name}  # files, not the scripts/ dir
    assert {"scripts/agent-backend.sh", "scripts/talk-stage.sh"} <= names
    missing = sorted(n for n in names if not (REPO_ROOT / n).is_file())
    assert not missing, f"runbook names scripts that do not exist: {missing}"


def test_every_named_make_target_exists():
    targets = set(re.findall(r"`make ([a-z][a-z0-9_-]*)`", _text()))
    assert "dry-run" in targets
    makefile = (REPO_ROOT / "Makefile").read_text()
    missing = sorted(
        t
        for t in targets
        if not re.search(rf"^{re.escape(t)}:", makefile, re.MULTILINE)
    )
    assert not missing, f"runbook names make targets that do not exist: {missing}"


def test_every_named_repo_path_exists():
    paths = {
        p.rstrip(".")
        for p in re.findall(
            r"\b(?:docs|services|tools|lib|tests|proto)/[A-Za-z0-9_./-]+", _text()
        )
    }
    assert "tools/demo/demo_driver.py" in paths
    missing = sorted(p for p in paths if not (REPO_ROOT / p).exists())
    assert not missing, f"runbook names paths that do not exist: {missing}"


def test_every_flag_key_in_the_beat_table_exists():
    known = _flag_keys()
    spec = FLAGS_SPEC.read_text()
    named: set[str] = set()
    for row in _table_rows(_section("Beat table")):
        for token in re.findall(r"`([^`]+)`", row[3]):
            key = token.split("=")[0].strip()
            if re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)*", key):
                named.add(key)
    assert {"decision.backend", "decision.jev.act_confidence_threshold"} <= named
    missing = sorted(k for k in named if k not in known and f"`{k}`" not in spec)
    assert not missing, f"runbook names flags that do not exist: {missing}"


def test_every_flag_set_in_a_command_exists():
    known = _flag_keys()
    keys = set(
        re.findall(r"setflag \w+ ([a-z][a-z0-9_.]*) ", _text())
        + re.findall(r"flags\.sh (?:get|set) \w+ ([a-z][a-z0-9_.]*)", _text())
    )
    assert "decision.jev.base_url" in keys
    missing = sorted(k for k in keys if k not in known)
    assert not missing, f"runbook sets flags that do not exist: {missing}"
