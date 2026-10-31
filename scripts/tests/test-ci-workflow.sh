#!/usr/bin/env bash
# Structural checks on .github/workflows/ci.yml that actionlint cannot express:
# job gates whose wrong value only shows up as a red or skipped job on main.
#
#   #1509: the Publish jobs (docker-build-merge, docker-build-extra-merge) run
#          only when their build job succeeded, never after a skipped build.
#   #1555: merge-queue support: the merge_group trigger, its paths-filter base,
#          concurrency per merge-group ref, every main-gating check runs in the
#          queue, CI Complete reports there, and nothing is published from it.
#   #1583: the e2e job runs only where `changes.e2e_required` says so
#          (merge_group, workflow_dispatch, ci-full, risky paths); a push to
#          main builds and publishes without them. Checked by simulating the
#          `changes/force` step and evaluating the job `if:`s per scenario.
#
#   #1422 S13: integration-test is one job: no matrix, no matrix.* in step ifs, the
#          agent is still built at HEAD on the fast path (#1254).
#   #1780: the prebuilt collector job (collector-image) runs exactly where the
#          e2e job runs, never fails or skips it, pushes only on
#          merge_group / push (never from a PR), and the e2e job takes its tag as
#          OTEL_COLLECTOR_TAG.
#   #1569: the gate cannot go silently non-blocking: no `continue-on-error` on any
#          CI Complete step, on integration-test (job or step), or at job level
#          on any job CI Complete needs (job-level COE reports `success`), and
#          CI Complete keeps `needs`-ing the required jobs. Allowed: the
#          informational sonarcloud job, and step-level COE in publish /
#          collector-image / retag jobs (#1795), which are not gating.
#   #1801: the validate job checks the committed agent Go stubs (services/agent/gen)
#          on every proto_pkg change, PR, push and dispatch; in the merge queue
#          only for an entry whose own diff touches proto_pkg (simulated).
#
# Needs python3 + PyYAML (as scripts/ci/check-retag-source.sh).
#   ./scripts/tests/test-ci-workflow.sh [path/to/ci.yml]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

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
# publish jobs block only on 'failure'. No integration-test: a push to main runs
# no e2e since #1583 (the queue ran it on that commit).
RETAG_IF = (
    "!cancelled() && github.event_name == 'push' && needs.changes.result == 'success'"
    " && needs.changes.outputs.retag_matrix != '[]'"
    " && needs.docker-build.result != 'failure' && needs.docker-build-extra.result != 'failure'"
    " && needs.docker-build-merge.result != 'failure' && needs.docker-build-extra-merge.result != 'failure'"
)
check(retag == RETAG_IF, f"retag-skipped: unexpected if: {retag}")
# #2120: the copy must be verified and must fail the job; it used to be
# continue-on-error + a warning, so a missing :<sha> image passed CI.
for st in jobs["retag-skipped"]["steps"]:
    if "retag-image.sh" in st.get("run", ""):
        check("continue-on-error" not in st, "retag-skipped: the retag-and-verify step must not be continue-on-error (#2120)")
        break
else:
    check(False, "retag-skipped: no step runs scripts/ci/retag-image.sh (#2120)")

# CI Complete's check step: fails on failure OR cancelled, exits 1, nothing
# softens it (`|| true`, success(), always(), a 'skipped' check). The second
# run step (#1583, required e2e) is checked below.
E2E_STEP = "Check required e2e ran"
complete_runs = [
    str(s.get("run", "")) for s in jobs["ci-complete"]["steps"] if s.get("run") and s.get("name") != E2E_STEP
]
check(len(complete_runs) == 1, "ci-complete: exactly one generic check step (plus the e2e step)")
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
# The docs-only queue skip relies on the ALLGREEN grouping strategy; keep the warning next to it.
check("ALLGREEN" in open(sys.argv[1]).read() and "HEADGREEN" in open(sys.argv[1]).read(),
      "ci.yml: the docs-only queue skip must document its ALLGREEN grouping assumption")
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
MATRIX_GATED = {"service-checks"}
TERM = re.compile(r"needs\.changes\.outputs\.\w+ == 'true'")
RUN_ALL = "needs.changes.outputs.run_all_checks == 'true'"

# (#1583) Jobs gated by e2e_required, exact. integration-test always runs in the
# queue (fail closed even if e2e_required is empty); the builds wait for it only
# where it ran, and a required e2e job must have succeeded. (#1780) Its
# `if` ignores collector-image's result: !cancelled() + changes succeeded.
E2E_GATE = ("needs.changes.outputs.e2e_required == 'true' ||"
            " (github.event_name == 'merge_group' && needs.changes.outputs.non_docs != 'false')")
check(
    job_if("integration-test")
    == "!cancelled() && needs.changes.result == 'success' && (" + E2E_GATE + ")",
    f"integration-test: must gate on e2e_required and run in the merge queue unless non_docs is 'false': {job_if('integration-test')}",
)
for build, matrix in (("docker-build", "docker_build_matrix"), ("docker-build-extra", "docker_extra_matrix")):
    check(
        job_if(build)
        == f"needs.changes.outputs.{matrix} != '[]' && !cancelled() && needs.changes.result == 'success' &&"
        " (needs.integration-test.result == 'success' ||"
        " (needs.integration-test.result == 'skipped' && needs.changes.outputs.e2e_required != 'true'))",
        f"{build}: unexpected if: {job_if(build)}",
    )
E2E_GATED = {"integration-test", "docker-build", "docker-build-extra"}
e2e_steps = [s for s in jobs["ci-complete"]["steps"] if s.get("name") == E2E_STEP]
check(len(e2e_steps) == 1, f"ci-complete: must have the step {E2E_STEP!r}")
if e2e_steps:
    step = e2e_steps[0]
    check("if" not in step, f"ci-complete/{E2E_STEP}: must run unconditionally (fail closed)")
    check(
        step.get("env") == {
            "E2E_REQUIRED": "${{ needs.changes.outputs.e2e_required }}",
            "E2E_RESULT": "${{ needs.integration-test.result }}",
            "EVENT_NAME": "${{ github.event_name }}",
            "NON_DOCS": "${{ needs.changes.outputs.non_docs }}",
        },
        f"ci-complete/{E2E_STEP}: unexpected env: {step.get('env')}",
    )
    check("|| true" not in str(step["run"]), f"ci-complete/{E2E_STEP}: must not be softened with || true")
check("github.event_name == 'merge_group'" in job_if("integration-test"),
      "integration-test: must run in the merge queue unless docs-only (fail closed, #1583)")
for name in jobs["ci-complete"]["needs"]:
    if name in EXEMPT or name in E2E_GATED:
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

# 4. (#1583) e2e gating, simulated per scenario. The `force` step runs for real
#    (bash, a fake `gh` for the live label read); job `if:`s go through a small
#    evaluator for the GitHub expression subset ci.yml uses.
import json
import os
import subprocess
import tempfile

TOKEN = re.compile(r"\s*(?:(?P<str>'[^']*')|(?P<op>==|!=|&&|\|\||!|\(|\)|,)|(?P<id>[A-Za-z_][\w.*-]*))")


def to_python(expr):
    out, pos = [], 0
    expr = expr.strip()
    while pos < len(expr):
        m = TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot parse expression at {expr[pos:]!r}")
        pos = m.end()
        if m.group("str"):
            out.append(repr(m.group("str")[1:-1]))
        elif m.group("op"):
            out.append({"&&": " and ", "||": " or ", "!": " not "}.get(m.group("op"), m.group("op")))
        else:
            name = m.group("id")
            if expr[pos:].lstrip().startswith("("):
                out.append(f"F_{name}")
            elif name in ("true", "false"):
                out.append(name.capitalize())
            else:
                out.append(f"L({name!r})")
    return "".join(out)


def lookup(ctx, key):
    if key not in ctx:
        raise KeyError(f"simulator: unknown context key {key!r}; add it to the scenario context")
    return ctx[key]


# Fidelity limits (fine for the expressions ci.yml uses today):
#   - `==` / `!=` are case-sensitive here; GitHub compares strings ignoring case.
#   - `!x == y` binds as `not (x == y)` here; GitHub evaluates `(!x) == y`.
#   - an unknown context key raises (GitHub would yield ''), so a typo in
#     ci.yml or a missing scenario key cannot silently evaluate to ''.
def evaluate(expr, ctx):
    expr = expr.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2]
    return eval(  # noqa: S307 (expressions come from our own ci.yml)
        to_python(expr),
        {
            "L": lambda k: lookup(ctx, k),
            "F_cancelled": lambda: False,
            # The simulated steps never fail before a status check, so a
            # `failure()` step (the e2e container dump, #1649) is skipped.
            "F_failure": lambda: False,
            "F_contains": lambda hay, needle: needle in hay,
            "F_toJSON": lambda v: json.dumps(v),
        },
    )


def interpolate(text, ctx):
    def repl(m):
        v = evaluate(m.group(1), ctx)
        return ("true" if v else "false") if isinstance(v, bool) else str(v)

    return re.sub(r"\$\{\{(.*?)\}\}", repl, str(text))


def run_step(step, ctx, extra_path=None):
    env = {k: interpolate(v, ctx) for k, v in (step.get("env") or {}).items()}
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        open(out, "w").close()
        env.update(GITHUB_OUTPUT=out, PATH=f"{extra_path or tmp}:{os.environ['PATH']}")
        proc = subprocess.run(
            ["bash", "-eo", "pipefail", "-c", interpolate(step["run"], ctx)],
            env=env, capture_output=True, text=True,
        )
        outputs = dict(line.split("=", 1) for line in open(out).read().splitlines() if "=" in line)
    return proc.returncode, outputs


def simulate(event, e2e_paths, non_docs, ci_full=False, e2e_outcome="success",
             integration_deps=False, override=None, collector_outcome="success", proto_pkg=False):
    """Results of the e2e/build/publish/complete jobs for one event.

    `override` replaces `changes` outputs after the force step (e.g. an empty
    e2e_required, to prove the gates fail closed)."""
    ctx = {
        "github.event_name": event,
        "github.event.pull_request.number": "42" if event == "pull_request" else "",
        "github.event.pull_request.labels.*.name": ["ci-full"] if ci_full else [],
        "github.token": "x",
        "github.repository": "o/r",
        "steps.f.outputs.e2e_paths": str(e2e_paths).lower(),
        "steps.nondocs.outputs.non_docs": str(non_docs).lower(),
        "steps.f.outputs.integration_deps": str(integration_deps).lower(),
    }
    with tempfile.TemporaryDirectory() as bindir:
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as fh:
            fh.write("#!/bin/sh\n" + ("echo ci-full\n" if ci_full else "echo docs\n"))
        os.chmod(gh, 0o755)
        force = next(s for s in jobs["changes"]["steps"] if s.get("id") == "force")
        rc, out = run_step(force, ctx, extra_path=bindir)
    assert rc == 0, f"force step failed for {event}"
    for key, value in jobs["changes"]["outputs"].items():
        if "steps.force.outputs" in value:
            ctx[f"needs.changes.outputs.{key}"] = interpolate(value, {f"steps.force.outputs.{k}": v for k, v in out.items()})
    ctx["needs.changes.outputs.non_docs"] = str(non_docs).lower()
    ctx.update({
        "needs.changes.result": "success",
        "needs.changes.outputs.docker_build_matrix": '["menu"]',
        "needs.changes.outputs.docker_extra_matrix": '[{"service":"agent"}]',
        "needs.changes.outputs.retag_matrix": '["dashboard"]',
        "needs.changes.outputs.proto_pkg": str(proto_pkg).lower(),
    })
    for key, value in (override or {}).items():
        ctx[f"needs.changes.outputs.{key}"] = value
    results = {}

    def run_job(name, ok="success"):
        cond = str(jobs[name].get("if", "true"))
        # GitHub ANDs an implicit success() (every need succeeded) into an
        # `if:` that calls no status function.
        if not re.search(r"\b(success|failure|always|cancelled)\(\)", cond):
            need_list = jobs[name]["needs"]
            need_list = [need_list] if isinstance(need_list, str) else need_list
            implicit = all(ctx.get(f"needs.{n}.result") == "success" for n in need_list)
        else:
            implicit = True
        results[name] = ok if implicit and evaluate(cond, ctx) else "skipped"
        ctx[f"needs.{name}.result"] = results[name]

    run_job("collector-image", collector_outcome)
    run_job("integration-test", e2e_outcome)
    for name in ("docker-build", "docker-build-extra", "docker-build-merge", "docker-build-extra-merge", "retag-skipped"):
        run_job(name)
    needs = {n: {"result": results.get(n, "success")} for n in jobs["ci-complete"]["needs"]}
    for n, v in needs.items():
        ctx[f"needs.{n}.result"] = v["result"]
    ctx["needs.*.result"] = [v["result"] for v in needs.values()]
    ctx["needs"] = needs
    complete_rc = 0
    for step in jobs["ci-complete"]["steps"]:
        if "if" in step and not evaluate(str(step["if"]).lower() if isinstance(step["if"], bool) else step["if"], ctx):
            continue
        complete_rc, _ = run_step(step, ctx)
        if complete_rc:
            break
    results["ci-complete"] = "success" if complete_rc == 0 else "failure"
    results["e2e_required"] = ctx.get("needs.changes.outputs.e2e_required")
    results["e2e_steps"] = e2e_steps_run(ctx) if results["integration-test"] != "skipped" else []
    # (#1801) validate job and its agent Go stub step.
    results["validate"] = "success" if evaluate(job_if("validate"), ctx) else "skipped"
    agent = [st for st in jobs["validate"]["steps"] if st.get("name") == AGENT_STUB_STEP]
    results["agent_stubs"] = (results["validate"] == "success" and bool(agent)
                              and ("if" not in agent[0] or bool(evaluate(str(agent[0]["if"]), ctx))))
    return results


def e2e_steps_run(ctx):
    """Names of the integration-test steps whose `if` holds."""
    step_ctx = dict(ctx)
    step_ctx.update({
        "steps.collector_image.outputs.build": "false",
    })
    return [
        st.get("name", "")
        for st in jobs["integration-test"]["steps"]
        if "if" not in st or evaluate(str(st["if"]), step_ctx)
    ]


AGENT_STUB_STEP = "Validate agent Go stubs"
check("e2e_required" in jobs["changes"]["outputs"], "changes: must output e2e_required (#1583)")
e2e_filter = next(s for s in jobs["changes"]["steps"] if s.get("id") == "f")["with"]["filters"]
e2e_path_list = yaml.safe_load(e2e_filter).get("e2e_paths") or []
for path in ("docker-compose*.yml", "**/Dockerfile*", "tests/integration/**", "services/otel-collector/**", "services/envoy/**",
             "services/flagd/**", ".github/workflows/ci.yml"):
    check(path in e2e_path_list, f"changes/f: e2e_paths must cover {path}")

SCENARIOS = [
    # (label, event, e2e_paths, non_docs, ci_full, expect_e2e)
    ("PR, plain code change", "pull_request", False, True, False, False),
    ("PR, risky path", "pull_request", True, True, False, True),
    ("PR, ci-full label", "pull_request", False, True, True, True),
    ("PR, docs-only under a risky path", "pull_request", True, False, False, False),
    ("merge_group", "merge_group", False, True, False, True),
    ("merge_group, docs only", "merge_group", False, False, False, False),
    ("workflow_dispatch", "workflow_dispatch", False, True, False, True),
    ("push to main", "push", True, True, False, False),
]
if "e2e_required" in jobs["changes"]["outputs"]:
    for label, event, paths, non_docs, ci_full, expect in SCENARIOS:
        r = simulate(event, paths, non_docs, ci_full)
        ran = r["integration-test"] != "skipped"
        check(ran == expect, f"{label}: e2e {'should' if expect else 'should not'} run, got {r['integration-test']}")
        check(r["ci-complete"] == "success", f"{label}: CI Complete should pass with every job green/skipped, got {r}")
        check(r["docker-build"] == "success", f"{label}: image builds should run, got {r['docker-build']}")
        if expect:
            # Required e2e that failed blocks the builds and CI Complete.
            bad = simulate(event, paths, non_docs, ci_full, e2e_outcome="failure")
            check(bad["docker-build"] == "skipped", f"{label}: builds must wait for e2e, got {bad['docker-build']}")
            check(bad["ci-complete"] == "failure", f"{label}: failed e2e must fail CI Complete")
            # A required e2e job that did not run (e.g. skipped) must not pass.
            gone = simulate(event, paths, non_docs, ci_full, e2e_outcome="skipped")
            check(gone["ci-complete"] == "failure", f"{label}: required e2e skipped must fail CI Complete, got {gone}")
            for build in ("docker-build", "docker-build-extra"):
                check(gone[build] == "skipped", f"{label}: {build} must not run when required e2e was skipped, got {gone[build]}")
    push = simulate("push", True, True)
    check(push["docker-build-merge"] == "success" and push["docker-build-extra-merge"] == "success",
          f"push to main: publish must run without e2e, got {push}")
    check(push["retag-skipped"] == "success", f"push to main: retag-skipped must run without e2e, got {push}")
    check("integration-test" not in jobs["retag-skipped"]["needs"], "retag-skipped: must not need integration-test")
    mq = simulate("merge_group", False, True)
    for name in ("docker-build-merge", "docker-build-extra-merge", "retag-skipped"):
        check(mq[name] == "skipped", f"merge_group: {name} must not run, got {mq[name]}")

    # Merge queue, docs-only group: e2e skipped, builds and CI Complete green.
    dq = simulate("merge_group", False, False)
    check(dq["integration-test"] == "skipped" and dq["ci-complete"] == "success" and dq["docker-build"] == "success"
          and dq["docker-build-extra"] == "success",
          f"merge_group docs-only: e2e skipped, builds and CI Complete green, got {dq}")
    # Fail closed: an empty/unknown non_docs in the queue still runs e2e and requires it.
    for value in ("", "maybe"):
        r = simulate("merge_group", False, False, override={"non_docs": value})
        check(r["integration-test"] == "success", f"merge_group, non_docs={value!r}: e2e must run, got {r['integration-test']}")
        r = simulate("merge_group", False, False, override={"non_docs": value}, e2e_outcome="skipped")
        check(r["ci-complete"] == "failure", f"merge_group, non_docs={value!r}, e2e skipped: CI Complete must fail, got {r}")
    # force step: empty/unknown NON_DOCS in the queue -> e2e_required true (non_docs feeds the step).
    for value in ("", "maybe"):
        r = simulate("merge_group", False, value)
        check(r["e2e_required"] == "true", f"force step, merge_group NON_DOCS={value!r}: e2e_required must be true, got {r['e2e_required']}")
    r = simulate("merge_group", False, True)
    check(r["e2e_required"] == "true", f"merge_group non-docs: e2e_required must be true, got {r['e2e_required']}")
    r = simulate("merge_group", False, False)
    check(r["e2e_required"] == "false", f"merge_group docs-only: e2e_required must be false, got {r['e2e_required']}")
    # A docs-only group that somehow reports e2e_required=false but a non-docs diff must still be required.
    r = simulate("merge_group", False, True, override={"e2e_required": "false"}, e2e_outcome="skipped")
    check(r["ci-complete"] == "failure", f"merge_group non-docs, e2e_required=false, e2e skipped: must fail, got {r}")

    # Fail closed (#1583 review): an empty/broken e2e_required must not let the
    # queue skip e2e, and CI Complete must reject it on any event.
    for event in ("merge_group", "pull_request"):
        for value in ("", "maybe"):
            r = simulate(event, False, True, override={"e2e_required": value})
            check(r["ci-complete"] == "failure", f"{event}, e2e_required={value!r}: CI Complete must fail, got {r}")
    r = simulate("merge_group", False, True, override={"e2e_required": ""})
    check(r["integration-test"] == "success", f"merge_group, empty e2e_required: e2e must still run, got {r['integration-test']}")
    r = simulate("merge_group", False, True, override={"e2e_required": ""}, e2e_outcome="skipped")
    check(r["ci-complete"] == "failure", f"merge_group, empty e2e_required, e2e skipped: CI Complete must fail, got {r}")
    r = simulate("merge_group", False, True, override={"e2e_required": "false"}, e2e_outcome="skipped")
    check(r["ci-complete"] == "failure", f"merge_group, e2e_required=false, e2e skipped: CI Complete must fail, got {r}")

    # Full path in the queue (#1583 review): every stack image built at HEAD,
    # never the fast path that pulls :latest for connect-proxy/rust-hid/dashboard.
    FULL, FAST = "Run integration tests (full build)", "Run integration tests (fast - source only)"
    PATHS = [
        # (label, kwargs, expect_full)
        ("merge_group", dict(event="merge_group", e2e_paths=False, non_docs=True), True),
        ("workflow_dispatch", dict(event="workflow_dispatch", e2e_paths=False, non_docs=True), True),
        ("PR, ci-full label", dict(event="pull_request", e2e_paths=False, non_docs=True, ci_full=True), True),
        ("PR, risky path", dict(event="pull_request", e2e_paths=True, non_docs=True), False),
        ("PR, risky path + deps", dict(event="pull_request", e2e_paths=True, non_docs=True, integration_deps=True), True),
        ("merge_group, empty e2e_full_build", dict(event="merge_group", e2e_paths=False, non_docs=True,
                                                   override={"e2e_full_build": ""}), True),
    ]
    for label, kwargs, expect_full in PATHS:
        steps = simulate(**kwargs)["e2e_steps"]
        check((FULL in steps) == expect_full and (FAST in steps) != expect_full,
              f"{label}: e2e must take the {'full' if expect_full else 'fast'} path, ran {steps}")
        if expect_full:
            fast_only = [n for n in steps if "fast path" in n]
            check(not fast_only, f"{label}: no fast-path step may run on the full path, ran {fast_only}")

    # (#1422 S13) One e2e job: no matrix, no matrix.* anywhere in it, no E2E_SHARD.
    it = jobs["integration-test"]
    check("strategy" not in it, "integration-test: must be one job (no strategy/matrix, #1422 S13)")
    check(it.get("name") == "Integration Tests",
          f"integration-test: name must be 'Integration Tests', got {it.get('name')!r}")
    for st in it["steps"]:
        check("matrix." not in str(st.get("if", "")),
              f"integration-test/{st.get('name')}: `if` must not use matrix.*")
    check("matrix." not in yaml.safe_dump(it), "integration-test: no matrix.* reference (name, env, steps)")
    check("E2E_SHARD" not in open(sys.argv[1]).read(), "ci.yml: E2E_SHARD is gone (#1422 S13)")

    # (#1254) A risky-path PR on the fast path still builds the agent at HEAD before the tests.
    AGENT_STEPS = ("Resolve agent image ref (fast path)", "Build agent at HEAD (fast path)")
    steps = simulate("pull_request", True, True)["e2e_steps"]
    for name in AGENT_STEPS:
        check(name in steps, f"PR, risky path: {name!r} must run (#1254), ran {steps}")
    if all(n in steps for n in AGENT_STEPS) and FAST in steps:
        check(steps.index("Build agent at HEAD (fast path)") < steps.index(FAST),
              "PR, risky path: the agent must be built before the fast e2e run")

    # (#1780) Prebuilt collector: same gate as the e2e job, so it is never
    # skipped by it and it never runs where they do not.
    check(job_if("collector-image") == E2E_GATE,
          "collector-image: must have the same e2e gate as integration-test (#1780)")
    check("collector-image" not in job_if("integration-test"),
          "integration-test: `if` must not depend on collector-image (#1780)")
    # Any collector-image failure (timeout, setup step, Publish result) must
    # leave the e2e job running with an empty tag, never skipped.
    for outcome in ("failure", "cancelled"):
        for label, kwargs in (
            ("merge_group", dict(event="merge_group", e2e_paths=False, non_docs=True)),
            ("PR, risky path", dict(event="pull_request", e2e_paths=True, non_docs=True)),
        ):
            r = simulate(collector_outcome=outcome, **kwargs)
            check(r["integration-test"] == "success",
                  f"{label}: collector-image {outcome} must not skip integration-test, got {r['integration-test']}")
            check(r["ci-complete"] == "success",
                  f"{label}: collector-image {outcome} must not fail ci-complete, got {r['ci-complete']}")
    check("collector-image" in jobs["integration-test"]["needs"],
          "integration-test: must need collector-image (#1780)")
    check("collector-image" not in jobs["ci-complete"]["needs"],
          "ci-complete: must not need collector-image; it is best effort (#1780)")
    for label, kwargs, want in (
        ("merge_group", dict(event="merge_group", e2e_paths=False, non_docs=True), "success"),
        ("PR, risky path", dict(event="pull_request", e2e_paths=True, non_docs=True), "success"),
        ("PR, docs only", dict(event="pull_request", e2e_paths=False, non_docs=False), "skipped"),
    ):
        r = simulate(**kwargs)
        check(r["collector-image"] == want, f"{label}: collector-image should be {want}, got {r['collector-image']}")
        check((r["collector-image"] == "skipped") == (r["integration-test"] == "skipped"),
              f"{label}: collector-image and integration-test must run together")
    ci_steps = jobs["collector-image"]["steps"]
    push = [st for st in ci_steps if "build-push-action" in str(st.get("uses", ""))]
    check(len(push) == 1, "collector-image: exactly one build-push step")
    if push:
        st = push[0]
        check(st.get("continue-on-error") is True, "collector-image: the push step must be continue-on-error")
        check(st.get("timeout-minutes") and int(st["timeout-minutes"]) <= 12,
              "collector-image: the push step needs a step timeout <= 12 min")
        for event, want in (("merge_group", True), ("push", True), ("pull_request", False),
                            ("workflow_dispatch", False)):
            ctx = {"github.event_name": event, "steps.exists.outputs.exists": "false"}
            check(bool(evaluate(str(st.get("if", "")), ctx)) is want,
                  f"collector-image: push step on {event} should be {want} (only reviewed code pushes)")
        check("steps.exists.outputs.exists != 'true'" in str(st.get("if", "")),
              "collector-image: must build only when the hash tag is missing")
        check(st["with"].get("push") is True and "${{ steps.ref.outputs.ref }}" in st["with"].get("tags", ""),
              "collector-image: must push the hash tag only")
    check(all(st.get("continue-on-error") is True for st in ci_steps
              if "login-action" in str(st.get("uses", "")) or st.get("id") == "exists"),
          "collector-image: login and exists check must be continue-on-error")
    check(all(st.get("continue-on-error") is True for st in ci_steps
              if "setup-buildx-action" in str(st.get("uses", ""))),
          "collector-image: setup-buildx must be continue-on-error")
    check(jobs["integration-test"].get("env", {}).get("OTEL_COLLECTOR_TAG")
          == "${{ needs.collector-image.outputs.tag }}",
          "integration-test: OTEL_COLLECTOR_TAG must be exactly collector-image's tag output")
    # Publish result: the tag only when the image is available, else empty.
    publish = next((st for st in ci_steps if st.get("id") == "result"), None)
    check(publish is not None, "collector-image: needs a `result` step")
    if publish:
        for available, want in ((True, "abc123"), (False, "")):
            with tempfile.TemporaryDirectory() as fake:
                with open(os.path.join(fake, "docker"), "w") as fh:
                    fh.write("#!/bin/sh\nexit %d\n" % (0 if available else 1))
                os.chmod(os.path.join(fake, "docker"), 0o755)
                rc, out = run_step(publish, {"steps.ref.outputs.ref": "ghcr.io/o/r/otel-collector:abc123",
                                             "steps.hash.outputs.hash": "abc123"}, extra_path=fake)
            check(rc == 0 and out.get("tag") == want,
                  f"collector-image: Publish result with image available={available} should emit tag={want!r}, got rc={rc} {out}")

# 8. (#1793) Both publish jobs run the :main ordering guard on main and keep :<sha>
# unconditional; :main/:latest are only added behind the guard.
for publish in ("docker-build-merge", "docker-build-extra-merge"):
    steps = jobs[publish]["steps"]
    checkout = next((st for st in steps if "actions/checkout" in str(st.get("uses", ""))), {})
    check(checkout.get("with", {}).get("fetch-depth") == 0,
          f"{publish}: needs a full-history checkout for the :main ordering guard")
    check(checkout.get("with", {}).get("filter") == "blob:none"
          and "scripts/ci" in str(checkout.get("with", {}).get("sparse-checkout", "")),
          f"{publish}: guard checkout must be a blobless partial clone with scripts/ci sparse-checked-out")
    manifest = next((st for st in steps if st.get("name") == "Create multi-arch manifest"), {})
    run = manifest.get("run", "")
    check("scripts/ci/check-main-tag-order.sh" in run, f"{publish}: manifest step must run check-main-tag-order.sh")
    check('TAGS=(--tag "${IMAGE}:${GITHUB_SHA}")' in run, f"{publish}: :<sha> must be tagged unconditionally")
    check("MOVE_TAGS" in run and '--tag "${IMAGE}:latest"' in run
          and run.index("MOVE_TAGS=false") < run.index('--tag "${IMAGE}:latest"'),
          f"{publish}: :main/:latest must be added only after the guard")
    check('[[ "$rc" == 0 || "$rc" == 3 ]] || exit "$rc"' in run, f"{publish}: guard skip (3) must not fail the run")

# 9. (#1569) The CI Complete gate stays blocking.
REQUIRED_NEEDS = {
    "changes", "dashboard-tests", "connect-proxy-tests", "lib-tests", "service-checks",
    "rust-checks", "rust-deny", "agent-tests", "flag-service-tests", "registry-pinned", "script-tests", "telemetry-contract",
    "flagd-contract", "service-tests", "lint-dockerfiles", "spec-gate", "validate",
    "integration-test", "docker-build", "docker-build-extra", "docker-build-merge",
    "docker-build-extra-merge", "retag-skipped",
}
INFORMATIONAL_JOBS = {"sonarcloud"}  # job-level continue-on-error is deliberate (see its comment)
needs = jobs["ci-complete"].get("needs", [])
needs = [needs] if isinstance(needs, str) else list(needs)
missing = sorted(REQUIRED_NEEDS - set(needs))
check(not missing, f"ci-complete: must keep needing {missing}")
unlisted = sorted(set(needs) - REQUIRED_NEEDS - INFORMATIONAL_JOBS)
check(not unlisted, f"ci-complete: new needs {unlisted} must be added to REQUIRED_NEEDS or INFORMATIONAL_JOBS")
# Step-level continue-on-error in other jobs (e.g. service-checks' ty step, publish/retag #1795) is deliberate.
for name in needs:
    if name in INFORMATIONAL_JOBS or name not in jobs:
        continue
    check("continue-on-error" not in jobs[name],
          f"{name}: job-level continue-on-error makes needs.result 'success'; a failure would pass CI Complete")
for name in ("ci-complete", "integration-test", "validate"):
    for st in jobs[name].get("steps", []):
        check("continue-on-error" not in st,
              f"{name}: step {st.get('name', st.get('uses', '?'))!r} must not use continue-on-error")

# 10. (#1801) Drift check for the committed agent Go stubs (services/agent/gen).
check(
    job_if("validate") == "needs.changes.outputs.proto_pkg == 'true' || needs.changes.outputs.run_all_checks == 'true'",
    f"validate: unexpected if: {job_if('validate')}",
)
proto_pkg_paths = yaml.safe_load(e2e_filter).get("proto_pkg") or []
for path in ("proto/**", "services/agent/gen/**", "scripts/ci/**"):
    check(path in proto_pkg_paths, f"changes/f: proto_pkg must cover {path}")
v_steps = jobs["validate"]["steps"]
v_names = [st.get("name") for st in v_steps]
agent_steps = [st for st in v_steps if st.get("name") == AGENT_STUB_STEP]
check(len(agent_steps) == 1, f"validate: needs exactly one {AGENT_STUB_STEP!r} step")
if agent_steps:
    st = agent_steps[0]
    check(str(st.get("run", "")).strip() == "make ci-validate-agent-protos",
          f"validate/{AGENT_STUB_STEP}: must run exactly `make ci-validate-agent-protos`, got {st.get('run')!r}")
    check(" ".join(str(st.get("if", "")).split())
          == "needs.changes.outputs.proto_pkg == 'true' || github.event_name != 'merge_group'",
          f"validate/{AGENT_STUB_STEP}: unexpected if: {st.get('if')}")
    check("Validate protos and packages" in v_names
          and v_names.index("Validate protos and packages") < v_names.index(AGENT_STUB_STEP),
          f"validate: {AGENT_STUB_STEP!r} must be its own step after the Python check")
AGENT_SCENARIOS = [
    # (label, kwargs, expect_step)
    ("PR touching proto", dict(event="pull_request", e2e_paths=False, non_docs=True, proto_pkg=True), True),
    ("PR not touching proto", dict(event="pull_request", e2e_paths=False, non_docs=True), False),
    ("merge_group touching proto", dict(event="merge_group", e2e_paths=False, non_docs=True, proto_pkg=True), True),
    ("merge_group, code change without proto", dict(event="merge_group", e2e_paths=False, non_docs=True), False),
    ("merge_group, docs only", dict(event="merge_group", e2e_paths=False, non_docs=False), False),
    ("push to main", dict(event="push", e2e_paths=False, non_docs=True), True),
    ("workflow_dispatch", dict(event="workflow_dispatch", e2e_paths=False, non_docs=True), True),
]
for label, kwargs, expect in AGENT_SCENARIOS:
    r = simulate(**kwargs)
    check(r["agent_stubs"] is expect,
          f"{label}: {AGENT_STUB_STEP!r} {'should' if expect else 'should not'} run, got {r['agent_stubs']}")
    if kwargs["event"] == "merge_group":
        check(r["validate"] == "success", f"{label}: validate (Python stubs) must still run in the queue, got {r['validate']}")
        check(r["ci-complete"] == "success", f"{label}: CI Complete should pass, got {r['ci-complete']}")

if failures:
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    sys.exit(1)
print(f"PASS: {passed} ci.yml checks")
PY
