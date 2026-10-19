# JoustMania construction skills (AIUP stack plugin)

Project skills that implement the AIUP [stack plugin contract](https://unifiedprocess.ai/stack-guide.html)
for our stack (Python services on `lib/`, the Go agent, the TS dashboard, docker-compose e2e on mock
controllers). The spec skills (`requirements`, `entity-model`, `use-case-diagram`, `use-case-spec`) come
from the `aiup-core` plugin. `spec-review` and `test-case` require the switch to the
[AI-Unified-Process/marketplace](https://github.com/AI-Unified-Process/marketplace) (PR #1290); until that
lands, skip step 1 of the loop.

## The loop, one use case at a time

```
/spec-review UC-040        # aiup-core: is the spec complete and consistent? (fix the spec first)
/implement UC-040          # code follows the spec; reports which spec change drove each file
/pytest-test UC-040        # Python unit tests, tagged @pytest.mark.uc(...)
/go-test UC-040            # Go agent tests, tagged // UC: ... + "UC-040/A2 ..." subtests
/integration-test UC-040   # docker-compose e2e on mock controllers (or a TC-XXX journey)
/coverage-check UC-040     # read-only: flows/BRs -> tagged tests -> gaps, allowed status, drift
```

Run only the test skills whose side the use case touches. Skills never edit `docs/` or a `**Status:**`
line; when code and spec disagree they stop and propose a spec change.

| Skill | Writes | Contract role |
|---|---|---|
| `implement` | production code | Implement |
| `pytest-test` | `services/*/tests`, `lib/tests` | Unit tests |
| `go-test` | `services/agent/**/*_test.go` | Unit tests |
| `integration-test` | `tests/integration` | End-to-end (our `playwright-test`) |
| `coverage-check` | nothing (delegates to the read-only `uc-coverage` agent in `.claude/agents/`) | Coverage audit |
| `implement-issue` | one issue end to end: worktree, spec, code, tests, PR (then stops) | Orchestration |
| `review-pr` | nothing (review worktree removed at the end) | Review |

Agents in `.claude/agents/` that run these workflows:

| Agent | Model | Role |
|---|---|---|
| `implementer` | sonnet (opus for hot path, concurrency, safety) | runs `implement-issue` (and its fixer mode); delegates gates and test runs to `gate-runner`, broad searches to `Explore` |
| `gate-runner` | haiku | runs only `scripts/ci/gate-run.sh` (allow-list enforced in code) for `make pr-gates` or test commands (repeat counts, mutation checks) in one worktree, in the foreground under ~8 min total, in the background for integration/e2e/full `pr-gates`; logs to files, relays the script's verdict and at most 20 failing lines; never edits |
| `pr-merger` | haiku | merge mechanics after a clean review (below) |
| `uc-coverage` | inherit | read-only coverage audit behind `coverage-check` |

After a clean review, hand the merge to the Haiku `pr-merger` agent (`.claude/agents/pr-merger.md`) with
`<PR> <HEAD_PREFIX>` and any `--close` / `--note-issue` options: it runs `scripts/ci/safe-merge.sh
--rerun-failed-once`, cleans up the worktree and branch on MERGED, and escalates everything else with a
fixed report (it never edits code, rebases, or changes labels).

Dashboard (vitest) tests have no writing skill yet, deliberately: write them by hand with the
`describe('UC-040: ...')` convention; `coverage-check` already reads it.

There is no migration skill: JoustMania has no database (state lives in flagd JSON flag sets and
in-memory services).

## Tag conventions

| Where | Tag |
|---|---|
| pytest | `@pytest.mark.uc("UC-040", flow="A2", br=["BR-003"], steps=[9], tc="TC-001")` (`flow` = `main` or `A<n>`; other args optional). A test whose purpose is a pre- or postcondition uses `cond=["Pre-1"]` instead of a flow, see "Condition tag" below. Registered in the root, `lib`, `services/pairing_daemon`, `tests/integration` and `tests/service` `pyproject.toml` |
| Test tier (pytest) | `pytestmark = pytest.mark.tier("service")` in every `tests/service` module (T2 service tier: services from source on a fresh flag plane, #1422); collection fails without it. The TC's Automation section names the tier in a `**Tier:**` line |
| Go | `// UC: UC-040 A2 BR-003` directly above `func Test...` and subtests `t.Run("UC-040/A2 ...")`; a condition is another unit token: `// UC: UC-040 Pre-1`, `t.Run("UC-040/Post-F-1 ...")` |
| Dashboard (vitest) | `describe('UC-040: ...')` / `it('A2: ...')` (AIUP JS convention); a condition test: `it('Pre-1: ...')` |
| Shell test (`scripts/tests/*.sh`) | a `# UC: UC-070 A8 BR-003` line in the header comment (one line per use case; conditions are unit tokens too: `# UC: UC-070 Pre-1`) |
| Code enforcing a rule | `# UC-040 BR-004: <one line>` / `// UC-040 BR-004: ...` above the enforcing code |
| NFR measurement / check (pytest) | `@pytest.mark.nfr("NFR-014")` (several ids allowed), on the test or class that measures or verifies the NFR; print the measured numbers. Registered next to `uc` |
| NFR measurement / check (Go) | `// NFR: NFR-012 NFR-013` directly above `func Test...` (next to a `// UC:` line if both apply) |
| Rust (`cargo test`) | `// UC: UC-030 main step 2` and, for an NFR, `// NFR: NFR-001` directly above `#[test] fn ...`: the flow token is `main` / `A<n>` as in Go (conditions and `BR-xxx` are unit tokens too), plus an optional `step N`, the analogue of pytest `steps=`. Tests live in the `#[cfg(test)] mod tests` of the file, so the tag line is indented |

### Condition tag (`cond=`)

`cond=` is an optional marker keyword, analogous to `br=`, for a test whose purpose is a precondition or
postcondition of the use case: `Pre-<n>`, `Post-S-<n>`, `Post-F-<n>`, numbered in order of appearance under the UC spec's Preconditions and Success/Failure Postconditions headings
(`@pytest.mark.uc("UC-020", cond=["Pre-1"])`, `@pytest.mark.uc("UC-020", cond=["Post-F-1"])`).

- Use it only when the test is about the condition. Many preconditions are setup-only and need no test of their
  own: do not write padding tests.
- A condition-only marker has no `flow=`. Do not tag such a test `flow="main"`: that counts as main-flow evidence
  and overstates coverage. A test that drives a flow and also asserts a condition keeps its `flow=` and adds `cond=`.
- It is informational: `uc-coverage` reports Pre/Post coverage from it, the `Tested` rule below is unchanged.

A use case may be `Tested` only when every main scenario and alternative flow has a tagged test, it carries no open
`Known gap` line, and every FR on its `Requirements:` line can be `Verified` (every other use case listing that FR is
`Tested`): a use case is never above the weakest FR on its `Requirements:` line (docs/CONTRIBUTING.md "Status
vocabularies"; same rule as `.claude/agents/uc-coverage.md` Step 5).
An NFR may be `Verified` only when a test tagged with its id measures or checks the stated limit.
Find them with `grep -rn 'mark.nfr("NFR-014"' services lib tests` and `grep -rn '^// NFR: .*NFR-012' services`.
