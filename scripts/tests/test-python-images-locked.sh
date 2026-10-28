#!/usr/bin/env bash
# C-046 (#2266): every Python service image installs its third-party
# dependencies from uv.lock, never from a fresh resolve, so the image runs the
# versions the unit tests ran. Structural checks, no docker:
#
#   1. each Python service Dockerfile (services/<svc>/ with a pyproject.toml)
#      COPYs uv.lock, exports it with `uv export --frozen`, and every
#      `uv pip install` that installs the workspace packages passes --no-deps
#      (an install without it would resolve fresh and override the lock);
#   2. a uv.lock change rebuilds the images: uv.lock is in the CI shared_python
#      anchor and in integration_deps, and scripts/ci/deps-fingerprint.sh hashes it;
#   3. the lock does not drift after a release: the root `joustmania` version in
#      uv.lock equals pyproject.toml, and release-please bumps it (toml updater);
#   4. no workspace pyproject asks for a grpcio / grpcio-health-checking floor
#      below the proto package's grpcio floor.
#
#   ./scripts/tests/test-python-images-locked.sh [repo root]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${1:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"

python3 - "$REPO_ROOT" <<'PY'
import json
import re
import sys
import tomllib
from pathlib import Path

import yaml

root = Path(sys.argv[1])
failures = []
passed = 0


def check(cond, msg):
    global passed
    if cond:
        passed += 1
    else:
        failures.append(msg)


# 1. Dockerfiles install from the lock.
services = sorted(
    p.parent for p in (root / "services").glob("*/pyproject.toml") if (p.parent / "Dockerfile").is_file()
)
check(len(services) >= 7, f"expected >= 7 Python service images, found {len(services)}")
for svc in services:
    name = svc.name
    text = (svc / "Dockerfile").read_text()
    # Join continuation lines so a RUN reads as one command.
    # Comment lines are not commands: drop them before matching.
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    joined = re.sub(r"\\\n", " ", text)
    check(re.search(r"^COPY\s+(\S+\s+)*uv\.lock\s", joined, re.M) is not None, f"{name}: Dockerfile does not COPY uv.lock")
    check("uv export --frozen" in joined, f"{name}: Dockerfile does not run `uv export --frozen`")
    check("pip install --no-cache-dir uv==0.5" not in joined, f"{name}: uv 0.5 cannot read the lockfile revision")
    for line in joined.splitlines():
        if "uv pip install" not in line:
            continue
        for cmd in re.findall(r"uv pip install[^&;]*", line):
            if " -e " in cmd or " -e\"" in cmd:
                check("--no-deps" in cmd, f"{name}: workspace install without --no-deps resolves fresh: {cmd.strip()}")
            else:
                check(re.search(r"(-r|--requirements)\s", cmd) is not None, f"{name}: uv pip install not from the exported lock: {cmd.strip()}")

# 2. A lock change rebuilds the images.
wf = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
filters = None
for step in wf["jobs"]["changes"]["steps"]:
    if step.get("id") == "f":
        filters = yaml.safe_load(step["with"]["filters"])
check(filters is not None, "ci.yml: paths-filter step `f` not found in the changes job")
if filters:
    check("uv.lock" in filters["shared_python"], "ci.yml: shared_python anchor lacks uv.lock")
    check("uv.lock" in filters["integration_deps"], "ci.yml: integration_deps lacks uv.lock")
fp = (root / "scripts/ci/deps-fingerprint.sh").read_text()
check(re.search(r"printf '%s\\0' uv\.lock", fp) is not None, "deps-fingerprint.sh does not hash uv.lock")

# 3. No release lock drift.
pyproject = tomllib.loads((root / "pyproject.toml").read_text())
lock = tomllib.loads((root / "uv.lock").read_text())
root_pkg = [p for p in lock["package"] if p["name"] == "joustmania"]
check(len(root_pkg) == 1, "uv.lock: no root joustmania package")
if root_pkg:
    check(
        root_pkg[0]["version"] == pyproject["project"]["version"],
        f"uv.lock joustmania {root_pkg[0]['version']} != pyproject {pyproject['project']['version']}",
    )
rp = json.loads((root / "release-please-config.json").read_text())
extra = rp["packages"]["."].get("extra-files", [])
check(
    any(
        isinstance(e, dict)
        and e.get("type") == "toml"
        and e.get("path") == "uv.lock"
        and e.get("jsonpath") == "$.package[?(@.name.value=='joustmania')].version"
        for e in extra
    ),
    "release-please-config.json: no toml extra-file updating the joustmania version in uv.lock",
)

# 4. grpcio floors follow the proto floor.
def floor(deps, pkg):
    for dep in deps:
        m = re.match(rf"^{re.escape(pkg)}\s*>=\s*([0-9.]+)", dep)
        if m:
            return tuple(int(x) for x in m.group(1).split("."))
    return None


proto_floor = floor(tomllib.loads((root / "proto/pyproject.toml").read_text())["project"]["dependencies"], "grpcio")
check(proto_floor is not None, "proto/pyproject.toml has no grpcio floor")
for member in pyproject["tool"]["uv"]["workspace"]["members"]:
    deps = tomllib.loads((root / member / "pyproject.toml").read_text())["project"].get("dependencies", [])
    for pkg in ("grpcio", "grpcio-health-checking"):
        f = floor(deps, pkg)
        if f is not None and proto_floor is not None:
            check(f >= proto_floor, f"{member}: {pkg} floor {'.'.join(map(str, f))} below proto grpcio floor")

if failures:
    print(f"FAIL: {len(failures)} check(s) failed, {passed} passed")
    for msg in failures:
        print(f"  - {msg}")
    sys.exit(1)
print(f"PASS: {passed} checks")
PY
