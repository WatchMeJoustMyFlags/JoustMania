"""Guard for the partial_shield -> toughness_boost rename (#1838).

The intervention id, its flag, the game effect and every telemetry value are
``toughness_boost``. The retired name may only survive where it is documented as
retired or pinned by a test. This test walks the tracked files, so a later change
that reintroduces the old name fails here. Every allowlisted file must still
contain a match, so a stale allowlist entry fails too.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PATTERN = re.compile(r"partial[ _-]?shield", re.IGNORECASE)

# file -> maximum number of matching lines. docs/research/** is historical.
ALLOWED_MAX_LINES = {
    "docs/glossary.md": 1,
    "docs/specs/decide-contract.md": 1,
    "docs/specs/flags.md": 1,
    "docs/specs/telemetry.md": 1,
    "services/agent/decide/decide_test.go": 3,
    "tests/contract/test_toughness_boost_rename.py": 2,
}
ALLOWED_PREFIXES = ("docs/research/",)
ALLOWED_FILES_NAMES = ("CHANGELOG.md",)


def _matches() -> Counter:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.split(b"\0")
    hits: Counter = Counter()
    for raw in tracked:
        if not raw:
            continue
        rel = raw.decode()
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError, OSError:
            continue
        count = sum(1 for line in text.splitlines() if PATTERN.search(line))
        if count:
            hits[rel] = count
    return hits


@pytest.fixture(scope="module")
def hits() -> Counter:
    return _matches()


def _is_exempt(rel: str) -> bool:
    return rel.startswith(ALLOWED_PREFIXES) or Path(rel).name in ALLOWED_FILES_NAMES


@pytest.mark.uc("UC-040", flow="main", steps=[4])
def test_retired_name_only_in_allowlisted_files(hits):
    stray = {
        f: n
        for f, n in hits.items()
        if not _is_exempt(f) and f not in ALLOWED_MAX_LINES
    }
    assert not stray, f"the retired name partial_shield is back in: {stray}"


@pytest.mark.uc("UC-040", flow="main", steps=[4])
def test_allowlisted_files_stay_within_their_limit(hits):
    over = {
        f: (hits[f], cap)
        for f, cap in ALLOWED_MAX_LINES.items()
        if hits.get(f, 0) > cap
    }
    assert not over, (
        f"more mentions of the retired name than allowed (found, max): {over}"
    )


@pytest.mark.uc("UC-040", flow="main", steps=[4])
def test_allowlist_has_no_stale_entries(hits):
    stale = [f for f in ALLOWED_MAX_LINES if hits.get(f, 0) == 0]
    assert not stale, (
        f"allowlisted files without a match, drop them from the allowlist: {stale}"
    )
