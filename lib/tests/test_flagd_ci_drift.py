"""Drift guard for flagd CI override configs (issue #801).

`docker-compose.ci.yml` mounts a `<domain>.ci.json` over each base
`<domain>.json` for the domains it overrides. The CI variant is allowed to
change flag *defaults* (e.g. `play_audio=off`, `backend=mock`), but it must
define the *same set of flag keys* as its base file. If a base file gains a new
flag and the CI override is not updated, that flag resolves to FLAG_NOT_FOUND in
CI and integration tests silently fall back to the code default -- so a flag-key
typo or a non-default variant can never be exercised.

This test fails loudly whenever a base flagd config carries a flag key that its
corresponding `.ci.json` is missing.
"""

import json
from pathlib import Path

import pytest

# lib/tests/ -> lib/ -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FLAGD_DIR = REPO_ROOT / "services" / "flagd"


def _flag_keys(path: Path) -> set[str]:
    return set(json.loads(path.read_text())["flags"].keys())


def _ci_override_pairs() -> list[tuple[Path, Path]]:
    """Find every base flagd config that has a sibling `.ci.json` override.

    Discovery is automatic so new domains are guarded without editing this test.
    Partial/special overrides (e.g. `controller.ci-rust.json`, which is a
    single-flag canary override and is not mounted as a full base replacement)
    are intentionally excluded -- only the canonical `<domain>.ci.json` form is
    treated as a full override that must mirror its base key set.
    """
    pairs = []
    for ci_path in sorted(FLAGD_DIR.glob("*.ci.json")):
        base_path = FLAGD_DIR / f"{ci_path.name[: -len('.ci.json')]}.json"
        if base_path.exists():
            pairs.append((base_path, ci_path))
    return pairs


PAIRS = _ci_override_pairs()


def test_ci_override_pairs_discovered():
    """Sanity check: we actually found the expected CI override files."""
    ci_names = {ci.name for _, ci in PAIRS}
    expected = {
        "controller.ci.json",
        "game.ci.json",
        "observability.ci.json",
        "system.ci.json",
        "user.ci.json",
    }
    assert expected <= ci_names, f"Expected CI override files not discovered: {expected - ci_names}"


@pytest.mark.parametrize("base_path,ci_path", PAIRS, ids=[ci.name for _, ci in PAIRS])
def test_ci_config_has_no_missing_flags(base_path: Path, ci_path: Path):
    """Every flag key in the base config must exist in its CI override."""
    base_keys = _flag_keys(base_path)
    ci_keys = _flag_keys(ci_path)

    missing = sorted(base_keys - ci_keys)
    assert not missing, (
        f"{ci_path.name} is missing flag(s) present in {base_path.name}: "
        f"{missing}. Add the new flag(s) to {ci_path.name} "
        f"(defaults matching {base_path.name}); CI mounts {ci_path.name} over "
        f"{base_path.name}, so missing keys resolve to FLAG_NOT_FOUND in CI."
    )

    # A CI override should not invent flags that don't exist in the base config.
    unknown = sorted(ci_keys - base_keys)
    assert not unknown, (
        f"{ci_path.name} defines flag(s) not present in {base_path.name}: "
        f"{unknown}. Remove them or add them to {base_path.name}."
    )
