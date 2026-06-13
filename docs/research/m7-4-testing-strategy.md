# Research: Testing & Mocking Strategy for the Agent Self-Improvement Pipeline (#931 / #935 / #936)

How to make the whole **propose → validate → promote** pipeline testable by
mocking every external/expensive surface, plus a decisive verdict on adopting
Microcks. One analysis for three issues because the mockable surfaces span the
whole pipeline:

| Stage | Issue | External/expensive surface to mock |
|---|---|---|
| **Propose** (M7-4) | #931 | LLM / coding-agent **inference** (Ollama / cloud chat) |
| **Validate** (M7-7) | #935 | **synthetic-game run** + the **fitness source** |
| **Promote** (M7-8) | #936 | **git / GitHub** side-effects (issue, PR, commit) |

This is a research doc — no features implemented. It validates the seams already
shipped/in-flight and prescribes the seams still to define.

> **Rescope note.** The track was rescoped 2026-06-12 from "remote coding agent
> edits source" to **shadow-scoped game-flag experiments**. The agent observes
> narratives → proposes a structured `{flag, value}` → the Writer/Gate apply it
> shadow-scoped to `game.json` → the Validator measures shadow fitness → #936
> promotes a winner. There is **no source-code diff** anywhere in this pipeline.
> The authoritative design lives in the "Refined design 2026-06-12" comments on
> #931/#932/#935 and is encoded in
> `services/agent/experiment/proposal.go` (package doc).

---

## 1. What exists today (ground truth)

### 1.1 The experiment package — Writer + Gate (shipped, #953/#954, M7-4/M7-5)

`services/agent/experiment/` is the foundation, built and proven in isolation:

- `proposal.go` — `Proposal{FlagKey, ExperimentalValue, Rationale}`. Deliberately
  tiny: the agent reasons about *which* flag and *what* value; the package turns
  that into a shadow-scoped flagd rule.
- `writer.go` — `Writer.Apply(Proposal)` does an order-preserving
  read-modify-write of `game.json` in place (`O_NOFOLLOW`, no temp+rename — EBUSY
  on the bind mount flagd watches). Hardcoded to the game flagset; `DefaultGamePath = /etc/flagd/game.json`, overridable via `GAME_FLAG_PATH` (the test seam — point at a `t.TempDir()` copy).
- `gate.go` — `Gate.Review(ctx, Proposal)` is the **single sanctioned entry
  point**: it validates THE INVARIANT (a write can never change what a
  `game_kind="real"` evaluation resolves) structurally **and** by a
  belt-and-suspenders JSONLogic eval, then applies via the Writer on accept.
  Emits `code_improvement.proposed` with `blocked`/`reason`.
- `resolve.go` — `resolveFlag(flagRaw, evalCtx)` mimics flagd resolution with
  `github.com/diegoholiveira/jsonlogic/v3` (the same engine the agent uses
  elsewhere). **Fails closed** on any operator it cannot evaluate.
- `experiment_test.go` + `testdata/` — the existing test convention for this
  package: temp-dir `game.json` copies, table-driven invariant cases.

**Testing seam already in place:** the Writer is path-injectable (`GAME_FLAG_PATH`
/ `NewWriter(path)`), and the Gate is constructed over a Writer + an injectable
`trace.Tracer`. Unit tests need no flagd, no docker — just a temp file.

### 1.2 The Validator — synthetic validation gate (in-flight, PR #956, M7-7 #935)

PR #956 (`feat/935-synthetic-validation`) adds `experiment/validate.go` with the
exact injectable-seam shape this analysis would have prescribed:

```go
type Baseline       interface { Fitness(ctx, Proposal) (float64, error) }       // last N real games (#731)
type SyntheticRunner interface { Run(ctx, Proposal, games int) (Run, error) }   // run N shadow games (M6)
type FitnessMeasurer interface { Measure(ctx, Run) (float64, error) }           // aggregate run fitness (#731)
type Reverter        interface { Revert(ctx, Proposal) error }                  // optional rollback
```

- `Validator.Validate(ctx, p, cfg) Decision` owns **only** the decision logic:
  baseline → run → measure → decide (`PROMOTE` if `after-before > threshold`
  strict, else `DISCARD`, `REVERT` instead of discard on degradation + flag).
- **Fails closed:** any seam error → `DISCARD` with the error on the span; never a
  promote on a broken fitness signal.
- One trace tree: `code_improvement.synthetic_validation → apply | discard`
  (`validate_telemetry.go`), carrying `fitness_before/after/delta`.
- `validate_test.go` already supplies `fakeBaseline`/`fakeRunner`/`fakeMeasurer`/
  `fakeReverter` + a `tracetest.SpanRecorder` — **no clock, network, or stack**.
- Flags `code_improvement.validation_games` / `fitness_improvement_threshold` /
  `revert_on_degradation` are read live via `flags.Validation(ctx)` and passed in
  as a `Config` (the package stays free of an OpenFeature dependency).

**The three seams are well-designed and correct. §6 refines them.**

### 1.3 The LLM inference surface (in worktrees, #739/#741, NOT on main)

The `Backend` / `Infer` / `resolver.go` / `llm_decide.go` named in the brief
exist only in worktrees (`.worktrees/739-llm-decide`, `.worktrees/928`), and they
serve the **intervention** decision path, not the M7-4 flag-proposal path. But
they are the canonical seam to mirror:

- `decision/resolver.go` — `type Backend interface { Name(); Available(ctx) bool; Infer(ctx, llm.Prompt) (string, error) }`.
- `decision/probe_backend.go` — the only network-touching `Backend`:
  `endpointBackend.Available` is a **bounded TCP dial** (2 s) of a tier endpoint
  (`AGENT_JETSON_ENDPOINT=jetson:11434`, `AGENT_LOCALHOST_ENDPOINT=localhost:11434`,
  `AGENT_CLOUD_ENDPOINT`); `Infer` is a **sentinel `errInferNotImplemented`** —
  the real Ollama `/api/chat` / cloud chat transport is hardware-/credential-
  blocked (#738/#742). There is **no real transport code yet**.
- `decision/resolver_test.go` — `fakeBackend` implements the same interface with a
  canned JSON response and a flippable `Available`, no I/O.
- `llm/prompt.go` — `Prompt{System, User, Variant, Model}`; `llm/decode.go` parses
  raw model text **defensively** (unparseable/out-of-vocab never dispatches).
- `llm/testdata/*.golden` — prompt rendering already uses **golden files**.

**Key finding:** in dev every endpoint is unreachable, so the chain degrades to
rules. The inference wire path is a stub today; the seam is `Backend.Infer`
returning a raw string that a defensive decoder parses.

### 1.4 Fitness, GameContext, and shadow games

- **Fitness is computed in-process** (`decision/fitness.go`, #731):
  `EvaluateFitness(GameContext, FitnessThresholds) → FitnessEvaluation`. Stateless
  pure function over a `GameContext` snapshot and flag-resolved thresholds.
- **GameContext arrives over OTLP gRPC** (`receiver.go`, `gamecontext/store.go`):
  the agent ingests **metrics** (~100 ms–1 s, the timely signal) and **spans**
  (~10 s/game-end) fanned out by the OTel Collector; `Store.ApplyMetrics/ApplySpans`
  build the snapshot. The agent **self-skips** its own `service.name` to avoid a
  feedback loop.
- **Shadow games (#774–#778) are not yet runtime-triggerable by the agent.** The
  M6 design (`docs/research/774-shadow-games.md`) is: multi-session coordinator
  (#775), `game_id`/`game_kind` on the RPC surface (#776), controller reservation
  (#777), then an agent-side `ShadowGameRunner` (#778). Today shadow/headless games
  are driven **only by test-harness code** (`tests/integration/`, `StreamGameEvents(start_config)`),
  and #954 threads `game_kind` into the eval context with a fail-safe `real`
  default. So `SyntheticRunner.Run` has **no production implementation to call
  yet** — its live wiring is correctly a documented follow-up.
- **There is no "baseline from last N real games" store** today. The agent has no
  persistence of completed-game fitness. `Baseline.Fitness` must be backed by
  either a small in-memory rolling window the decision loop maintains, or a
  telemetry query (§6.2).

### 1.5 git / GitHub surface (#936)

**Greenfield.** There is **zero** GitHub-API or git-automation code in any
service: no `go-github`, no `gh` invocation, no `git` shell-out in
`services/**`. `services/agent/go.mod` has no GitHub client. The only `gh`/git
usage in the repo is dev/CI tooling. #936 is a clean slate — define the seam from
scratch (§6.3).

### 1.6 Test infrastructure & conventions

- **Go unit:** interface fakes + `go.opentelemetry.io/otel/sdk/trace/tracetest`
  `SpanRecorder` (`decision/loop_test.go`, `attribution_test.go`); the OpenFeature
  **in-memory provider** (`memprovider.NewInMemoryProvider`, `flags/provider_test.go`);
  a `settableFlags` fake; golden files (`llm/testdata/*.golden`). House style is
  emphatically **interface fakes, no network, `t.TempDir()` for files**.
- **Integration:** Python `testcontainers` driving docker-compose
  (`tests/integration/conftest.py`: `docker-compose.yml` + `.override.yml` +
  `.ci.yml`, mock audio/controllers, `flagd` v0.16.0 container, an `agent`
  service). `flag_files` fixture (`tests/integration/test_intervention_flow.py`)
  writes flag JSON the running flagd hot-reloads. `make test` is the gate.
- **Microcks:** **not present anywhere.** `grep -ri microcks` over compose, docs,
  Makefile, CI, go.mod, research docs returns nothing. The memory note "Microcks
  deferred to #741" reflects an intent that was never acted on; #741 instead chose
  a TCP-dial probe + sentinel `Infer`.

---

## 2. Layered strategy

Three layers, matching the repo's existing habit (interface fakes for logic,
docker-compose for integration, real-thing tests gated off by default):

| Layer | What runs | What's mocked | Gate |
|---|---|---|---|
| **UNIT** | pure decision logic in-process (`go test`) | every external via interface fakes; OpenFeature via `memprovider`; spans via `tracetest` | every PR, fast |
| **INTEGRATION** | the agent + a small real surround in docker-compose | the *wire boundary only* (inference endpoint, GitHub API, the synthetic-run trigger) | `make test`, per PR |
| **E2E** | real shadow games, real flagd, optionally a real local model | nothing (or only credentials) | **default-OFF, explicitly gated** |

The whole pipeline must be **fully unit-testable in-process from day one**; mock
servers appear only at the wire boundary; real externals only behind an explicit
e2e gate.

---

## 3. Per-surface seam + mocking plan

### 3.1 LLM / coding-agent inference (M7-4 #931)

**Reality check first:** for *flag deltas*, the "remote coding agent backed by
side-laptop Ollama" is **overkill** (the #931 refined design says so explicitly).
The agent only needs to emit a structured `{flag, value, rationale}` from a
narrative — a single constrained-JSON inference, not a code-editing agent loop.
The existing `Backend.Infer(ctx, prompt) (string, error)` + defensive
`llm.Decode` pipeline (#739) is exactly the right shape; M7-4 should **reuse it**,
not invent a coding-agent RPC. `agent.code_improvement.engine` selects the backend
when a generative one is wired; the default/dev backend emits the proposal from
the rules surface or a stub.

**Seam (define now):** a `Proposer` that turns a narrative into a `Proposal`,
backed by the same `Backend` interface:

```go
// services/agent/experiment (or a sibling): the propose-side seam.
type Proposer interface {
    // Propose turns a game narrative/snapshot into a structured experiment, or
    // (nil, nil) for "no proposal this cycle". Backed by Backend.Infer + a
    // defensive decoder that REJECTS anything that isn't a {known-flag, value}.
    Propose(ctx context.Context, narrative Narrative) (*Proposal, error)
}
```

| Layer | Mock |
|---|---|
| **UNIT** | a `fakeProposer` returning canned `Proposal`s; for the backend itself, the existing `fakeBackend` (canned raw JSON) + golden prompt files. Assert: the decoder rejects out-of-vocab flags, unparseable JSON, and a value whose JSON type mismatches the flag (this feeds straight into the Gate's `ReasonUnknownFlag` / #955 type guard). |
| **INTEGRATION** | the real `endpointBackend` pointed at a **mock inference server** serving an Ollama-shaped `/api/chat` (or OpenAI-compatible) response. Spin it up in docker-compose; assert the agent produces a Gate-accepted proposal. **Recorded cassettes / golden responses** are the cheapest form and match the repo's `llm/testdata/*.golden` habit. |
| **E2E** | real local Ollama (`localhost:11434`), default-off (endpoint unreachable in CI → chain degrades to rules, which is the existing safe behavior). |

**Recommendation:** httptest/cassette for unit; a single small mock HTTP container
for integration. The inference contract is *one* simple JSON-in/JSON-out call —
it does not justify heavy contract tooling on its own (see §4).

### 3.2 Synthetic run + fitness (M7-7 #935) — the flakiness risk

This is the surface most at risk of becoming a slow/flaky integration test,
because a "real" implementation runs N games through the coordinator.

**The PR #956 seams are correct.** Keep `Baseline` / `SyntheticRunner` /
`FitnessMeasurer` / `Reverter`. Refinements in §6.

| Layer | Mock / approach |
|---|---|
| **UNIT** | PR #956's `fakeRunner`/`fakeMeasurer`/`fakeBaseline` over `tracetest`. This already covers promote/discard/revert/threshold-boundary/fail-closed. Nothing to add structurally. |
| **INTEGRATION** | **Do NOT run real shadow games in the default integration suite.** Instead test the two halves independently: **(a)** a `SyntheticRunner` whose `Run` triggers a **tiny** headless batch (`validation_games=1`) against the existing mock-controller coordinator from the M6 harness, asserting only that shadow games *start and complete with `game_kind != "real"`* — i.e. a *smoke* test of the trigger, not a fitness assertion; **(b)** a `FitnessMeasurer` test that feeds **pre-recorded GameContext snapshots / OTLP fixtures** through the real `EvaluateFitness` path and asserts deterministic numbers. Splitting trigger-correctness from fitness-determinism keeps both fast and stable. |
| **E2E** | full `validation_games=5` real shadow batch + real telemetry read, default-off. |

**Keeping it fast & deterministic — concrete rules:**

1. **Fitness must be read deterministically, not by polling telemetry timing.**
   `EvaluateFitness` is a pure function; the integration `FitnessMeasurer` should
   read **completed-game** fitness keyed by `game_id` (not "whatever the live
   gauge says now"), so there is no race against the ~10 s span flush. Prefer a
   coordinator/agent endpoint that returns a *finalized* per-game fitness over
   scraping the live metric.
2. **Cap `validation_games` in the integration suite to 1–2** via the flag. The
   `Config.ValidationGames` clamp (`>=1`) already guards the floor.
3. **The experiment is shadow-scoped by construction**, so a concurrent "real"
   game in the same compose stack is provably untouched — integration tests can
   run a real-context assertion in parallel without contaminating the shadow run
   (mirrors the #893 parallel-intervention precedent, but note the global
   rate-limiter / single-coordinator caveats there).
4. **Avoid the full docker stack for the fitness half** — drive `EvaluateFitness`
   from fixtures in a Go test, no containers.

### 3.3 git / GitHub promotion (M7-8 #936) — and the SAFETY rail

Greenfield. Define a `Promoter` seam that abstracts the three modes and two
targets behind one interface:

```go
// services/agent/promote: the promote-side seam.
type Promoter interface {
    // Promote acts per the resolved mode (issue|pr|autonomous) and target
    // (local|github). Returns what it DID (for the span/outcome), never panics on
    // a disabled/offline target — it degrades or no-ops.
    Promote(ctx context.Context, p Proposal, ev Evidence) (PromoteResult, error)
}

type GitHubClient interface {                       // the wire boundary
    OpenIssue(ctx, IssueReq) (URL, error)
    OpenPR(ctx, PRReq) (URL, error)
}
type GitClient interface { Commit(ctx, CommitReq) (SHA, error) }  // local target
```

| Layer | Mock |
|---|---|
| **UNIT** | in-process `fakeGitHub`/`fakeGit` that **record** calls; assert "mode=issue → OpenIssue called with hypothesis body X, no PR, no commit", "mode=pr → OpenPR with structured body (fitness before/after, synthetic results, reasoning, flag state)", "mode=autonomous → local apply, no GitHub call". Assert `code_improvement.outcome = applied\|discarded\|reverted` on the span. |
| **INTEGRATION** | **local target:** a throwaway `t.TempDir()` git repo (`git init`), assert a commit lands — no network. **github target:** a **mock GitHub API server** in docker-compose serving the handful of endpoints used (`POST /repos/.../issues`, `POST /repos/.../pulls`); assert the agent posts the right payload. |
| **E2E** | real GitHub against a sandbox repo, default-off, credential-gated. |

**SAFETY (non-negotiable, fail-closed like the rest of the agent):**

- The `github` target must be **default-off and require an explicit, separate
  opt-in** beyond the kill-switch — e.g. `AGENT_GITHUB_ENABLED=true` *and* a
  present `GITHUB_TOKEN` *and* `agent.code_improvement.target=github`. Absent any
  one → the `GitHubClient` is the **no-op recording fake**, never a real client.
- In **all unit and default integration tests**, the constructor must wire the
  fake client; there must be **no code path** by which a test instantiates a real
  GitHub client or runs `git push`. Make this structural: the real client is only
  built in `main.go` behind the env gate, never in test-reachable constructors —
  mirror how the Writer is hardcoded to `game.json` and the LLM endpoints are
  empty/unreachable in dev.
- `autonomous` mode applies **locally** (the flag write the Writer already does) +
  watches; it must **not** imply a GitHub push.
- Add a guard test asserting that with no token / gate off, `Promote` with
  `target=github` **degrades to local or no-ops and records the reason**, never
  dials github.com.

---

## 4. MICROCKS VERDICT

**Skip Microcks. Use per-surface httptest fakes + recorded cassettes + a
throwaway git repo, consistent with the repo's existing "interface fakes +
docker-compose" habit.**

**What Microcks is / buys:** a container that serves **contract-driven,
multi-protocol mocks** (REST/gRPC/GraphQL/AsyncAPI/Kafka) imported from OpenAPI /
Postman / AsyncAPI artifacts, with dynamic/example-based responses and
contract-testing. Genuinely strong when you have **several real, evolving,
contract-defined APIs** to mock uniformly and want one tool for all of them.

**Why it loses here — decisively:**

1. **Only two of the "three externals" are actually network APIs, and both are
   trivial.** The inference call is *one* JSON-in/JSON-out endpoint; the GitHub use
   is *two* endpoints (issue, PR). The synthetic-run boundary is **not an HTTP API
   to mock at all** — it's an in-process interface (`SyntheticRunner`) whose live
   form drives the coordinator over gRPC; you mock it with a Go fake, not a
   contract server. So Microcks would cover at most ~3 endpoints across two
   services.
2. **No contracts exist to import.** There is no Ollama OpenAPI artifact in-repo;
   the GitHub surface is greenfield. The value of Microcks (drive mocks from the
   real contract) requires maintaining those artifacts — pure new cost, no
   existing asset to leverage.
3. **It fights the house style.** Every existing agent test uses Go interface
   fakes + `memprovider` + `tracetest` + golden files; the integration suite is
   `testcontainers` + docker-compose. A Microcks container + contract import +
   query-template learning curve is a new operational and cognitive dependency for
   a payoff (3 simple endpoints) that `httptest`/a 30-line mock container already
   delivers.
4. **The hard problems Microcks doesn't solve.** The real testing risk here is
   *fitness determinism* and the *GitHub safety rail* — neither is an API-mocking
   problem. Microcks would add surface area without touching the actual risk.

**When to revisit:** if the inference backend grows into a real multi-endpoint
coding-agent protocol *and* a second contract-defined external lands *and* the team
wants contract-verification against upstream specs — then a single Microcks
container could consolidate. That is not this milestone. **Adopt nothing now;
re-evaluate only if/when a generative coding-agent backend with a real OpenAPI
contract is wired (post-#738/#742).**

---

## 5. Tests-first seam plan (define these NOW)

So propose → validate → promote is fully unit-testable in-process from day one,
with mock servers only at the wire boundary:

| Seam | Where | Status | Unit fake | Wire-boundary mock |
|---|---|---|---|---|
| `Proposer` (narrative → `Proposal`) | new, propose side | **define now** | `fakeProposer` + existing `fakeBackend`/golden prompts | mock `/api/chat` cassette (integration) |
| `Backend.Infer` | `decision/resolver.go` (#739, worktree) | **exists** — reuse, don't reinvent | `fakeBackend` (canned JSON) | httptest Ollama-shaped server |
| `experiment.Gate.Review` / `Writer` | `experiment/gate.go`, `writer.go` | **shipped** | `t.TempDir()` `game.json` | real flagd hot-reload (integration) |
| `Baseline.Fitness` | `experiment/validate.go` (#956) | **in-flight** — keep | `fakeBaseline` | recorded GameContext fixtures → `EvaluateFitness` |
| `SyntheticRunner.Run` | `experiment/validate.go` (#956) | **in-flight** — keep | `fakeRunner` | tiny `validation_games=1` headless batch, smoke only |
| `FitnessMeasurer.Measure` | `experiment/validate.go` (#956) | **in-flight** — keep | `fakeMeasurer` | finalized-per-game fitness read (deterministic) |
| `Reverter.Revert` | `experiment/validate.go` (#956) | **in-flight, optional** | `fakeReverter` | route through Gate/Writer (one write path) |
| `Promoter` + `GitHubClient` + `GitClient` | new, promote side (#936) | **define now** | recording fakes asserting payloads | mock GitHub API container; `git init` temp repo |

All of these tie to the existing `experiment.Proposal` carrier and the Gate's
single-entry-point discipline: the Proposer's output is a `Proposal`, the Gate
gates the write, the Validator decides on it, the Promoter acts on a validated
`Proposal` + `Evidence`. One value object threads the whole pipeline.

---

## 6. M7-7-specific guidance (validate/refine PR #956's seams)

The seams are right. Refinements:

1. **`Baseline` needs a concrete production plan — there is no real-game fitness
   store today (§1.4).** Recommend the cheapest correct source: a **rolling
   in-memory window** of the last N *finalized real-game* fitness values the
   decision loop already computes (it runs `EvaluateFitness` every cycle; capture
   the final value per real `game_id` into a ring buffer). This avoids a telemetry
   round-trip and is trivially fakeable. Document this as the `Baseline` follow-up
   instead of the vaguer "read from #731 / gamesummary window."
2. **`FitnessMeasurer` must read FINALIZED, not live, fitness.** As written, the
   doc-comment says "read shadow-game fitness from telemetry." Pin it to a
   per-`game_id` finalized value (the same ring-buffer mechanism, keyed by the
   shadow `game_id`s in `Run.ID`) so integration tests aren't racing the span
   flush. This is the single most important change to keep the integration layer
   non-flaky.
3. **`Run.ID` should carry the shadow `game_id`s explicitly**, not an opaque UUID,
   so `FitnessMeasurer` can key finalized fitness without a side lookup. Minor
   field-shape tweak; the interface stays.
4. **Keep the fail-closed contract exactly as PR #956 has it** — it already never
   promotes on a seam error. Add one unit test that a *partial* run
   (`Run.Games < requested`) still measures and decides (the PR already trusts
   `run.Games`; assert it).
5. **The `SyntheticRunner` live wiring is correctly deferred** (blocked on the
   #778 `ShadowGameRunner`, which itself depends on #775–#777 multi-session
   coordinator). The integration test for it should be a **smoke test of the
   trigger** (one shadow game starts/completes with `game_kind != "real"`), not a
   fitness assertion — fitness determinism is tested separately via fixtures.

---

## 7. Summary of recommendations

1. **Reuse the `Backend.Infer` + defensive-decode seam (#739) for M7-4**; a full
   coding-agent is overkill for flag deltas. Add a thin `Proposer` seam over it.
2. **Keep PR #956's `Baseline`/`SyntheticRunner`/`FitnessMeasurer`/`Reverter`
   seams**; pin `Baseline`/`FitnessMeasurer` to **finalized per-game** fitness
   (rolling in-memory window keyed by `game_id`) so the integration layer is
   deterministic, and make `SyntheticRunner`'s integration test a **trigger smoke
   test**, not a fitness assertion.
3. **Define a `Promoter` + `GitHubClient`/`GitClient` seam now** with recording
   unit fakes; **hard safety rail**: the real GitHub client is built only in
   `main.go` behind `AGENT_GITHUB_ENABLED` + token + `target=github`, never
   reachable from a test constructor; default integration uses a mock GitHub
   container + a `git init` temp repo; real GitHub is e2e-only, default-off.
4. **Skip Microcks.** Two trivial endpoints + one in-process interface don't
   justify a contract-mock platform; per-surface httptest/cassette + temp git repo
   match the repo's interface-fake + docker-compose habit. Revisit only if a real
   contract-defined coding-agent backend lands.
5. **Layering:** UNIT (in-process fakes, every PR) → INTEGRATION (mock servers at
   the wire boundary, `make test`) → E2E (real shadow games / real model / real
   GitHub, default-off, explicitly gated).
