#!/usr/bin/env bash
# Structural checks on .github/workflows/ci.yml that actionlint cannot express:
# job gates whose wrong value only shows up as a red or skipped job on main.
#
#   #1509: the Publish jobs (docker-build-merge, docker-build-extra-merge) run
#          only when their build job succeeded, never after a skipped build.
#   #1555: merge-queue support: the merge_group trigger, its paths-filter base,
#          concurrency per merge-group ref, every main-gating check runs in the
#          queue, CI Complete reports there, and nothing is published from it.
#
# Needs python3 + PyYAML (as scripts/ci/check-retag-source.sh).
#   ./scripts/tests/test-ci-workflow.sh [path/to/ci.yml]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
WORKFLOW="${1:-${REPO_ROOT}/.github/workflows/ci.yml}"

python3 - "$WORKFLOW" <<'PY'
import re
import sys

import yaml

with open(sys.argv[1]) as fh:
    wf = yaml.safe_load(fh)
jobs = wf["jobs"]
failures = []
passed = 0


def check(cond, msg):
    global passed
    if cond:
        passed += 1
    else:
        failures.append(msg)


def job_if(name):
    return " ".join(str(jobs[name].get("if", "")).split())


PUBLISH_EVENTS = "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')"


# 1. (#1509) Publish jobs gate on their build's result == 'success'.
for publish, build in (
    ("docker-build-merge", "docker-build"),
    ("docker-build-extra-merge", "docker-build-extra"),
):
    cond = job_if(publish)
    matrix = "docker_build_matrix" if build == "docker-build" else "docker_extra_matrix"
    check(build in jobs[publish]["needs"], f"{publish}: must need {build}")
    # Exact: !cancelled(), publishing events only (never PRs or the merge
    # queue), and the build's result == 'success' (#1509). No `|| always()`.
    check(
        cond
        == f"!cancelled() && {PUBLISH_EVENTS} && needs.changes.outputs.{matrix} != '[]'"
        f" && needs.{build}.result == 'success'",
        f"{publish}: unexpected if: {cond}",
    )

# 2. A skipped publish must not block retag-skipped or fail CI Complete:
#    retag-skipped treats only 'failure' of the publish jobs as blocking, and
#    ci-complete fails only on failure/cancelled.
retag = job_if("retag-skipped")
for publish in ("docker-build-merge", "docker-build-extra-merge"):
    check(
        f"needs.{publish}.result != 'failure'" in retag,
        f"retag-skipped: must accept a skipped {publish}",
    )
    check(
        f"needs.{publish}.result == 'success'" not in retag,
        f"retag-skipped: must not require {publish} success (it skips when nothing was built)",
    )
# Exact: push only (never merge_group: the queue publishes nothing), and the
# publish jobs block only on 'failure'.
RETAG_IF = (
    "!cancelled() && github.event_name == 'push' && needs.changes.outputs.retag_matrix != '[]'"
    " && needs.integration-test.result == 'success'"
    " && needs.docker-build.result != 'failure' && needs.docker-build-extra.result != 'failure'"
    " && needs.docker-build-merge.result != 'failure' && needs.docker-build-extra-merge.result != 'failure'"
)
check(retag == RETAG_IF, f"retag-skipped: unexpected if: {retag}")

# CI Complete's check step: fails on failure OR cancelled, exits 1, nothing
# softens it (`|| true`, success(), always(), a 'skipped' check).
complete_runs = [str(s.get("run", "")) for s in jobs["ci-complete"]["steps"] if s.get("run")]
check(len(complete_runs) == 1, "ci-complete: exactly one run step")
lines = [ln.strip() for ln in (complete_runs[0] if complete_runs else "").splitlines() if ln.strip()]
for required in (
    "FAILED=\"${{ contains(needs.*.result, 'failure') }}\"",
    "CANCELLED=\"${{ contains(needs.*.result, 'cancelled') }}\"",
    'if [[ "$FAILED" == "true" || "$CANCELLED" == "true" ]]; then',
    "exit 1",
):
    check(required in lines, f"ci-complete: step must contain the line: {required}")
if "exit 1" in lines and lines[0].startswith("FAILED="):
    if_at = next((i for i, ln in enumerate(lines) if ln.startswith("if ")), -1)
    fi_at = lines.index("fi") if "fi" in lines else -1
    check(if_at < lines.index("exit 1") < fi_at, "ci-complete: exit 1 must sit inside the failure branch")
body = " ".join(lines)
for banned in ("'skipped'", "success()", "always()"):
    check(banned not in body, f"ci-complete: step must not use {banned}")
check(
    not any(ln.startswith(("FAILED=", "CANCELLED=", "if ", "exit")) and "|| true" in ln for ln in lines),
    "ci-complete: failure detection must not be softened with || true",
)

# 3. (#1555) Merge queue.
triggers = wf.get("on", wf.get(True))  # PyYAML reads the bare key `on` as True
check("merge_group" in triggers, "on: must include merge_group")
check(
    (triggers.get("merge_group") or {}).get("types") == ["checks_requested"],
    "on.merge_group.types must be [checks_requested]",
)
for event in ("pull_request", "push"):
    check(event in triggers, f"on: must keep {event}")
check("labeled" not in str(triggers.get("pull_request")), "on.pull_request must not trigger on labeled (#1527)")

group = str(wf["concurrency"]["group"])
check("github.event.merge_group.head_ref" in group, f"concurrency group must key merge-queue runs by their ref: {group}")
check(
    str(wf["concurrency"]["cancel-in-progress"]) == "${{ github.event_name == 'pull_request' }}",
    "concurrency must cancel only superseded PR runs (never queue or main runs)",
)

for step in jobs["changes"]["steps"]:
    if "paths-filter" in str(step.get("uses", "")):
        w = step.get("with", {})
        check(
            w.get("base") == "${{ github.event.merge_group.base_sha }}",
            f"changes/{step.get('id')}: paths-filter base must be merge_group.base_sha",
        )
        check(
            w.get("ref") == "${{ github.event.merge_group.head_sha }}",
            f"changes/{step.get('id')}: paths-filter ref must be merge_group.head_sha",
        )
force = next(s for s in jobs["changes"]["steps"] if s.get("id") == "force")["run"]
check(
    re.search(r"checks=.*github\.event_name == 'merge_group'", force) is not None,
    "changes/force: merge_group must set run_all_checks (queue gates main like a push)",
)
check(
    re.search(r"full=.*merge_group", force) is None,
    "changes/force: merge_group must not force all image builds",
)

# Every job CI Complete needs runs in the queue when its check runs on a main
# push; SonarCloud (informational) and the publishing jobs are the exceptions.
check(job_if("ci-complete") == "${{ !cancelled() }}", "ci-complete: if must be exactly ${{ !cancelled() }}")
check(
    job_if("sonarcloud").startswith(
        "!cancelled() && !contains(needs.*.result, 'failure') && github.event_name != 'merge_group' && "
    ),
    f"sonarcloud: must skip in the merge queue: {job_if('sonarcloud')}",
)
# Every other job CI Complete needs is gated ONLY by `changes` outputs: an OR of
# `needs.changes.outputs.X == 'true'` terms that includes run_all_checks, or a
# non-empty matrix (the matrices are full on run_all_checks). No event_name
# restriction, no always()/success()/true, no &&.
EXEMPT = {"changes", "sonarcloud", "docker-build-merge", "docker-build-extra-merge", "retag-skipped"}
MATRIX_GATED = {"service-checks", "docker-build", "docker-build-extra"}
TERM = re.compile(r"needs\.changes\.outputs\.\w+ == 'true'")
RUN_ALL = "needs.changes.outputs.run_all_checks == 'true'"
for name in jobs["ci-complete"]["needs"]:
    if name in EXEMPT:
        continue
    cond = job_if(name)
    check("github.event_name" not in cond, f"{name}: must not restrict github.event_name: {cond}")
    if name in MATRIX_GATED:
        check(
            re.fullmatch(r"needs\.changes\.outputs\.\w+_matrix != '\[\]'", cond) is not None,
            f"{name}: if must be exactly a non-empty-matrix gate: {cond}",
        )
        continue
    terms = cond.split(" || ")
    check(all(TERM.fullmatch(t) for t in terms), f"{name}: if must be an OR of changes outputs only: {cond}")
    check(RUN_ALL in terms, f"{name}: must run on run_all_checks (queue and main runs): {cond}")
check(not job_if("changes"), "changes: must run unconditionally")

# Image builds in the queue: amd64 only, push nothing (only push / dispatch publish).
for build in ("docker-build", "docker-build-extra"):
    platform = str(jobs[build]["strategy"]["matrix"]["platform"])
    check(PUBLISH_EVENTS in platform, f"{build}: arm64 only on publishing events: {platform}")
    for step in jobs[build]["steps"]:
        outputs = str((step.get("with") or {}).get("outputs", ""))
        if "push=true" in outputs:
            check(outputs.startswith("${{ " + PUBLISH_EVENTS), f"{build}: pushes only on publishing events: {outputs}")
        if step.get("name") in ("Export digest", "Upload digest"):
            check(
                step.get("if") == "github.event_name == 'push' || github.event_name == 'workflow_dispatch'",
                f"{build}/{step.get('name')}: only on publishing events",
            )

if failures:
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    sys.exit(1)
print(f"PASS: {passed} ci.yml checks")
PY
