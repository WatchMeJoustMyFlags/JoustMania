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

Dashboard (vitest) tests have no writing skill yet, deliberately: write them by hand with the
`describe('UC-040: ...')` convention; `coverage-check` already reads it.

There is no migration skill: JoustMania has no database (state lives in flagd JSON flag sets and
in-memory services).

## Tag conventions

| Where | Tag |
|---|---|
| pytest | `@pytest.mark.uc("UC-040", flow="A2", br=["BR-003"], steps=[9], tc="TC-001")` (`flow` = `main` or `A<n>`; other args optional). Registered in the root, `lib`, `services/pairing_daemon` and `tests/integration` `pyproject.toml` |
| Go | `// UC: UC-040 A2 BR-003` directly above `func Test...` and subtests `t.Run("UC-040/A2 ...")` |
| Dashboard (vitest) | `describe('UC-040: ...')` / `it('A2: ...')` (AIUP JS convention) |
| Code enforcing a rule | `# UC-040 BR-004: <one line>` / `// UC-040 BR-004: ...` above the enforcing code |
| NFR measurement / check (pytest) | `@pytest.mark.nfr("NFR-014")` (several ids allowed), on the test or class that measures or verifies the NFR; print the measured numbers. Registered next to `uc` |
| NFR measurement / check (Go) | `// NFR: NFR-012 NFR-013` directly above `func Test...` (next to a `// UC:` line if both apply) |

A use case may be `Tested` only when every main scenario and alternative flow has a tagged test.
An NFR may be `Verified` only when a test tagged with its id measures or checks the stated limit.
Find them with `grep -rn 'mark.nfr("NFR-014"' services lib tests` and `grep -rn '^// NFR: .*NFR-012' services/agent`.
