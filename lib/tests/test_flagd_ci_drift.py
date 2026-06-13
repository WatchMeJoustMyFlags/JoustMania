"""Drift guard for the flagd CI flag directory (issue #801, #959).

`docker-compose.ci.yml` mounts the whole `services/flagd/ci/` directory in
place of base `services/flagd/` (single source of truth, #959). Each
`services/flagd/ci/<domain>.json` is the CI variant of the base
`services/flagd/<domain>.json`. The CI variant is allowed to change flag
*defaults* (e.g. `play_audio=off`, `backend=mock`), but it must define the
*same set of flag keys* as its base file. If a base file gains a new flag and
the CI copy is not updated, that flag resolves to FLAG_NOT_FOUND in CI and
integration tests silently fall back to the code default -- so a flag-key typo
or a non-default variant can never be exercised.

This test fails loudly whenever a base flagd config carries a flag key that its
`ci/<domain>.json` counterpart is missing (or vice versa). It covers every
domain present in `ci/`, including ones that are verbatim copies of base
(e.g. `agent.json`, `interventions.json`, `rollout.json`) -- those must still
track base key-for-key.
"""

import json
from pathlib import Path

import pytest

# lib/tests/ -> lib/ -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FLAGD_DIR = REPO_ROOT / "services" / "flagd"
CI_DIR = FLAGD_DIR / "ci"


def _flag_keys(path: Path) -> set[str]:
    return set(json.loads(path.read_text())["flags"].keys())


def _ci_pairs() -> list[tuple[Path, Path]]:
    """Pair every `ci/<domain>.json` with its base `services/flagd/<domain>.json`.

    Discovery is automatic so new domains are guarded without editing this test.
    A CI flag file with no base sibling is reported by
    `test_every_ci_file_has_base`; here we only build the pairs that exist.
    """
    pairs = []
    for ci_path in sorted(CI_DIR.glob("*.json")):
        base_path = FLAGD_DIR / ci_path.name
        if base_path.exists():
            pairs.append((base_path, ci_path))
    return pairs


PAIRS = _ci_pairs()


def test_ci_dir_discovered():
    """Sanity check: the CI flag directory exists and is non-empty."""
    assert CI_DIR.is_dir(), f"CI flag directory missing: {CI_DIR}"
    ci_names = {ci.name for _, ci in PAIRS}
    expected = {
        "controller.json",
        "game.json",
        "observability.json",
        "system.json",
        "user.json",
    }
    assert expected <= ci_names, f"Expected CI flag files not discovered: {expected - ci_names}"


def test_every_ci_file_has_base():
    """Every `ci/<domain>.json` must correspond to a base `<domain>.json`."""
    orphans = sorted(p.name for p in CI_DIR.glob("*.json") if not (FLAGD_DIR / p.name).exists())
    assert not orphans, (
        f"CI flag file(s) with no base sibling in {FLAGD_DIR}: {orphans}. "
        f"flagd serves {CI_DIR} in CI; a CI-only flag file can never be exercised "
        f"against a base config and is almost certainly stale."
    )


@pytest.mark.parametrize("base_path,ci_path", PAIRS, ids=[ci.name for _, ci in PAIRS])
def test_ci_config_has_same_flag_keys(base_path: Path, ci_path: Path):
    """Every flag key in the base config must exist in its CI copy, and vice versa."""
    base_keys = _flag_keys(base_path)
    ci_keys = _flag_keys(ci_path)

    missing = sorted(base_keys - ci_keys)
    assert not missing, (
        f"ci/{ci_path.name} is missing flag(s) present in {base_path.name}: "
        f"{missing}. Add the new flag(s) to ci/{ci_path.name} "
        f"(defaults may differ from {base_path.name}, but the key must exist); "
        f"CI serves the ci/ directory, so missing keys resolve to FLAG_NOT_FOUND in CI."
    )

    # A CI copy should not invent flags that don't exist in the base config.
    unknown = sorted(ci_keys - base_keys)
    assert not unknown, (
        f"ci/{ci_path.name} defines flag(s) not present in {base_path.name}: "
        f"{unknown}. Remove them or add them to {base_path.name}."
    )
