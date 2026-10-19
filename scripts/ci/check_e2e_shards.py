"""Check that every docker-compose e2e test lands in exactly one CI shard (#1422).

Collects tests/integration once per shard (E2E_SHARD=core, E2E_SHARD=agent) and
once unsharded, without starting docker, and fails unless the shards are
disjoint and together equal the full suite. Collection itself already fails on
a module missing from E2E_SHARD_BY_MODULE (tests/integration/conftest.py).

Also fails when a module mapped to `core` starts the agent container (its source
uses the agent compose profile): the core job skips the HEAD agent build, so
such a test would run against a stale pulled agent image.

Usage: make test-integration-shard-check
"""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

SHARDS = ("core", "agent")
INTEGRATION_DIR = Path("tests/integration")
# How the agent-shard modules start the agent: `"--profile", AGENT_SERVICE`
# (AGENT_SERVICE = "agent") or a literal `--profile agent`.
AGENT_PROFILE_RE = re.compile(r"""--profile["',\s]+(?:agent|AGENT_SERVICE)\b""")


def shard_by_module() -> dict[str, str]:
    tree = ast.parse((INTEGRATION_DIR / "conftest.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "E2E_SHARD_BY_MODULE"
            for t in node.targets
        ):
            return ast.literal_eval(node.value)
    sys.exit("E2E_SHARD_BY_MODULE not found in tests/integration/conftest.py")


def core_modules_starting_agent() -> list[str]:
    return sorted(
        module
        for module, shard in shard_by_module().items()
        if shard == "core"
        and (INTEGRATION_DIR / module).exists()
        and AGENT_PROFILE_RE.search((INTEGRATION_DIR / module).read_text())
    )


def collect(shard: str | None) -> set[str]:
    env = dict(os.environ)
    env.pop("E2E_SHARD", None)
    if shard:
        env["E2E_SHARD"] = shard
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration/",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stdout.write(result.stdout[-4000:])
        sys.stderr.write(result.stderr[-4000:])
        sys.exit(f"collection failed for shard={shard or 'all'}")
    return {line for line in result.stdout.splitlines() if "::" in line}


def main() -> int:
    everything = collect(None)
    by_shard = {shard: collect(shard) for shard in SHARDS}
    ok = True
    for shard, tests in by_shard.items():
        print(f"e2e-{shard}: {len(tests)} tests")
        if not tests:
            print(f"  FAIL: shard {shard} is empty")
            ok = False
    overlap = by_shard["core"] & by_shard["agent"]
    missing = everything - by_shard["core"] - by_shard["agent"]
    extra = (by_shard["core"] | by_shard["agent"]) - everything
    for label, ids in (
        ("in both shards", overlap),
        ("in no shard", missing),
        ("only in a shard", extra),
    ):
        if ids:
            ok = False
            print(f"  FAIL: {len(ids)} test(s) {label}:")
            for test_id in sorted(ids):
                print(f"    {test_id}")
    starts_agent = core_modules_starting_agent()
    if starts_agent:
        ok = False
        print(
            "  FAIL: core-shard module(s) start the agent container "
            "(move to 'agent' in E2E_SHARD_BY_MODULE):"
        )
        for module in starts_agent:
            print(f"    {module}")
    print(f"all: {len(everything)} tests -> {'OK' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
