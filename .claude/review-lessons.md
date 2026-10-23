# Review lessons

Curated, durable lessons for the `pr-reviewer` agent and the `review-pr` skill, distilled from reviewer notes
(2026-10, #1236 to #2028). Each line is what to check and why. Verify against the code before relying on one: a
lesson that no longer holds is deleted, not kept. Rules already in `CLAUDE.md`, `.claude/rules/` and
`.claude/skills/review-pr/SKILL.md` are not repeated here; local flakes live in [known-flakes.md](known-flakes.md).

## Mutation recipes

- **Python service, no edit to the worktree.** `git archive HEAD services/<svc> lib proto | tar -x -C <scratch>`,
  copy the untracked `proto/*_pb2*.py`, then run the worktree's ROOT `.venv/bin/python -m pytest` with
  `PYTHONPATH=<scratch>[:<scratch>/services/<svc>]`. The venv is at the repo root, not `services/<svc>/.venv`.
- **Python, product edit denied.** Mutate with a Python `str.replace` (`assert old in text`), or A/B the same test
  on the fix and on `origin/main`. `sed` misses multi-line `>-` YAML values and `if:` expressions.
- **Go.** `go test -overlay <json>` mutates without touching tracked files, but `gate-run.sh` refuses `-overlay`
  (#1861): mutate in a separate detached worktree instead. Removing the only use of a variable gives "declared and
  not used", a build error that gate-runner reports as `CAUGHT? (non-test error)`: keep a reference (`_ = x`).
- **Go gate-run quirks.** A bare `.` package arg is silently dropped (`./promote/... .` ran only promote): run the
  root package as `./` in its own `run` line and read the package lines in the log. `-run 'A|B'` is refused (`|`):
  use a common substring.
- **connect-proxy** has no committed proto stubs, so its Go tests run only in the Docker builder stage:
  `docker build --target builder -f services/connect-proxy/Dockerfile .`, then mutate with
  `docker run --rm -v mutated.go:/app/<file> <img> sh -c 'go test -count=1 ./...'`. The runtime image has
  `ENTRYPOINT /connect-proxy`: use `--entrypoint sh` or the container starts the server and hangs (#1388).
- **Rust.** Tests are inline (`#[cfg(test)]`): mutate the source line and run `cargo test` in the crate; clippy
  must be clean with `-D warnings`. A new stable clippy lint firing inside `tonic::include_proto!` code is fixed
  with `#[allow(clippy::all)]` on the wrapper module (a named new lint is unknown to older toolchains), only when
  the module holds nothing hand-written (#1253).
- **Several mutations at once.** One detached worktree per mutation (`review-<N>-m1`, `-m2`) and one gate-runner
  each, with its own `logs <dir>`; two runs sharing a log dir clobber `run1-1.log`.
- **Shell tests** (`scripts/tests/test-*.sh`): a suite that stops at the first failure can hide which test killed
  a mutation. Build a temp script from the harness plus the one test block. gate-runner labels the shell `FAIL:`
  exit `CAUGHT? (non-test error)`: read the FAIL line, it can be a real assertion. To test workflow `run:` loops,
  extract the block with PyYAML and stub `gh`, `sleep` and `date` on `PATH` (sub-second, covers `set -e` paths).
- **Cross-PR semantic merge.** `T=$(git merge-tree --write-tree origin/main A); C=$(git commit-tree $T -p origin/main -m sim);
  git merge-tree --write-tree $C B`, then `git archive` the tree and run the suites. `--merge-base=origin/main`
  against an older PR branch gives false conflicts. Found real breaks no textual conflict showed (#1370 x #1379).
- **A test that cannot fail.** Before trusting a new test: does it pass with the gate off, with the producer
  mocked, or with the fix reverted? Typical vacuous shapes: the test hand-writes what the producer would write; a
  frozen clock plus a 0 s timer; a poll window longer than the reconcile period; asserting the client sees
  `CancelledError` (grpc raises it whatever the server does); a probe that never reaches the code (a MOCK serial
  skips targeting and rollout); a per-backend redirect test where the encode fails before the network. Add a
  positive control (a hit count) where the negative case is "nothing happened".
- **Overlapping safeguards mask each other** (#1351): the hold mutation survived because the reserve refused the
  same decision. Test each mechanism with an input the other one does not touch.
- `gate-run.sh` allow-lists tools that run the worktree's own code. It guards against agent mistakes, not a
  malicious PR; do not spend review time on option-injection payloads (#1570).
- `uv run --package <pyproject name>`, exact: `joustmania-game-coordinator`, `joustmania-pairing-daemon`,
  `joustmania-lib`, plus `--extra test`; the short name (`game-coordinator`) is refused. Use `in <dir>`
  (`in services/game_coordinator`, `in lib`) where the tests resolve relative paths.
- `make pr-gates` runs the git-status proto drift gate: run it on a committed tree or it false-fails (#1830).
  `SERVICE=` skips other gates (for example grafana-json), so a dashboard change needs its own run.
- A local PASS is not tier coverage: product paths need the CI "Service Tests" check (review-pr step 5).
- Never pipe test output where the exit code matters (`pytest | tail` masks rc; in zsh `${PIPESTATUS[0]}` is empty).
  Capture to a file first.
- A fresh brief can carry wrong SHAs or a stale head: compare with `gh pr view <n> --json headRefOid` (#1570).

## Flags and flagd

- **File watch.** flagd outside Kubernetes polls `os.Stat` mtime every 1 s (no inotify). Writing a flag file by
  temp + rename is safe on a DIRECTORY bind mount but silently freezes a SINGLE-FILE bind mount at the old inode,
  while the script prints success. Writers must truncate-and-write in place; grep the compose overlays for
  `.json:/` single-file mounts (#1237).
- **Sources clobber.** flagd keys its store by sync source URI: several gRPC sources with the same `host:port`
  overwrite each other. Edge intervention flags are read with `{gameId}` only (no targetingKey; the serial is in
  the payload), so a design that renders on targetingKey never matches.
- **In-process provider everywhere** (#1709, #1713). Python keeps serving cached flags while STALE; the Go
  `syncedProvider` errors while sync is lost (fails closed), so a Go flag drops to its env/default on every flap.
  A client change needs: leak per failed `Init` retry (flagd Go provider 0.6.x leaks a goroutine and ~300 KB per
  attempt), keepalive (#1775: grpc-go strikes pings sent faster than 5 min on an idle sync stream), and the
  FLAGD_PORT trap: editing a compose env breaks the `.env` release pin (old image, new env); change the binary
  default instead.
- **OpenFeature API traps.** `get_*_value` never raises (an error gives the default, a neutral baseline: fail-open
  if the default is permissive). `api.provider_registry.get_provider_status(x)` takes a provider object: a domain
  string always returns NOT_READY; use `api.get_client(domain=d).get_provider_status()`. `get_client()` returns a
  new client per call, so `remove_handler` on it leaks an entry.
- **Targeting context.** Flag reads inside coordinator intervention handlers run through
  `GameLoopDispatcher.run` (`run_coroutine_threadsafe`): the contextvars of the game transaction
  (`game_mode`, `targetingKey`) are NOT visible there, only the API-level attributes plus `gameId`. Any
  "agent evaluates with the coordinator's context" claim must model this (#1871). Conversely, per-game work that
  refreshes process-global config inside the session context leaks a shadow game's targeting into the real game.
- **game_kind has two vocabularies** (telemetry `primary`/`shadow`, flags `real`/`shadow`) and the mode names
  differ (`JoustFFA` enum name vs `FFA` span name): normalise at ingest and grep both spellings.
- **Safety gates fail closed, read at use time.** The wrong shape is a prime at init, or a "not found" mapped to
  permissive. Shipped defaults: `ci/*.json` may legally differ from base in DEFAULTS; a test requiring both to be
  equal tightens that policy silently (#1424). A flag-only PR must still run the agent and lib tests that read
  `services/flagd/*`.
- **Flag-service.** The agent is the sole writer of `interventions.json`; integration tests also write it outside
  the agent mutex (lost-update risk). `Clear` ignores the `writer` query for real scope (GET-then-DELETE window).
  A wrapper that reaches `c.Delete` escapes the C-034 source guard: probe it from a site the wire test does not
  drive (#1821). Removing a writable flag is safe for Pi state: persisted values for unknown flags are dropped
  with a warning (#1539).
- **Global state in tests.** `interventions_allowed` and the intervention rate limiter are GLOBAL: parallel
  intervention tests cross-talk (#893); a test that sets `api.set_transaction_context_propagator` must restore it.
- A flag removal: grep the keys AND their symbols (Python, Go struct fields, span attribute constants, Sound enums,
  shell fixtures, talk scripts). Leftovers are usually comments and fixtures, not readers.

## CI and merge queue

- **Job-level `continue-on-error`** makes `needs.<job>.result` read `success` while the job's own check stays red.
  An aggregator therefore ignores it; verify with the aggregator log's `toJSON(needs)` (#1236). A best-effort job
  inside another job's `needs` still needs `!cancelled()` on the dependent `if`, or its failure or timeout skips
  the required shards (#1785).
- **Matrix + `needs`.** `needs.build.result == 'success'` over a matrix: one failed leg skips ALL sibling
  publishes (#1495). `needs.X.result` of a matrix aggregates (any failure = failure).
- **Merge queue.** `merge_group.base_sha` is the PREVIOUS queue entry's head when queued behind another, so a
  paths-filter diff covers only that entry's own PR; safety rests on ruleset `grouping_strategy: ALLGREEN` (#1729).
  `merge_group` events have no PR number or labels: a label-gated step finds nothing. `gh pr merge` on a queued
  PR can enable auto-merge instead (check `autoMergeRequest`). Queue runs can exceed 25 min.
- **Labels and reruns.** Labelling starts no CI run but a late label can leave a suite without "CI Complete"
  (BLOCKED, #1527); the "Detect changes" step reads PR labels live, so a label added seconds after creation turns
  the only run into the FULL path (the fast path is then never exercised in CI). Re-running an OLD-sha run in the
  same concurrency group cancels the new head's run: `gh pr checks` shows all-fail with 0 s jobs: check
  `gh run view <id> --json headSha,attempt` before calling CI red. Two `gh run rerun` back to back fail on the
  in-progress run.
- **ci.yml-only edits** put no service in the docker matrices: zero image builds run. Exercise the build path with
  the `ci-full` label or `workflow_dispatch` on the branch.
- **Scripts run in odd cwds.** `ci.yml` manifest steps run in `runner.temp/digests`, outside the checkout: a bare
  `git` there is "not a repository" and an "unknown, so skip" policy freezes a tag forever (#1795). Ask for a
  non-git-cwd test case, and make "skip" mean a provable `is-ancestor`.
- **Spec Gate.** `scripts/aiup/check-spec-touched.sh` fails any `services/**`, `lib/**`, `proto/**` diff, even a
  dead-code deletion, without the `no-spec-change` label AND the body line `Spec changes: none: <reason>`. The lint exits non-zero on any NEW warning
  (a RULE_NUMBERING gap clears when the PR holding the missing BR merges, or on rebase).
A `git ls-tree`-hash tag reused by first writer: a same-repo PR run can plant a hash, so push only on
  `merge_group`/`push` (#1785). SonarCloud is informational.

## Spec and tag traps

- **Tag ids need the spec PR merged first.** A test tagged with a flow or BR id that exists only in an unmerged
  spec PR passes everywhere but dangles; check `git show origin/main:docs/use_cases/UC-XXX.md | grep '^### '`
  for every id. No CI linter validates pytest `uc` tags against docs, and the spec lint does not check flow ids.
- **Parallel tag PRs conflict on the same test functions** (both insert a `// UC:` line at the same spot).
  Resolve keep-both; picking a side silently drops coverage. Detect with
  `git merge-tree --write-tree --name-only A B`.
- **Vacuous BR tags.** A BR tag whose test restates the producer, uses a blanket `cond=["Pre-1"]`, or proves a
  minimum count with a lobby the all-ready rule blocks, over-claims. Read the assertions of every tagged test, and
  of every TC `Automated` path (helpers that accept any terminal event pass on error).
- **UC text vs tests.** New tests written against an unmerged spec can assert behaviour the UC text on main
  contradicts; diff the text, not only the heading ids. When a finding names one stale sentence, grep the whole UC
  and `docs/OWNERSHIP_MODEL.md` for the same claim.
- **"Every mode" claims** (BR-006 natural end) contradict per-mode Known gaps (UC-026 NonStop): check each mode.
- **Custom `run()` skips base hooks.** FightClub, Werewolf, Zombie, Tournament override `run()`. A hook added only
  to `BaseGameMode.run()` (outcome record, root span, early stream drain) silently skips them: grep
  `async def run(` in `games/`. A mode cloning another's `run()`: diff the two (#1926, #1941, #1996).
- **Latch first, then check.** Latch the outcome with `_latch_game_outcome()` BEFORE publishing the winner event,
  then re-check `force_ended`. `force_end` comes from the servicer thread, so it can land between the check and the
  latch (published `game_winner` plus `force_ended`). Probe: wrap `_latch_game_outcome` so it calls `force_end`
  first (#2002, #2005, #2007, #2008, #2014). Expect every new mode's latch PR to repeat this.
- **Kill gates.** The default `_accepts_kill` is "outcome not latched". Swapper, FightClub and Tournament override
  it without calling `super()`. A guard on the `_kill_player` override is not a guard on `_kill_player_impl`: a kill
  already past the guard (awaiting a shield discard or explosion) still lands after the latch. Base `_run_kill`
  counts the death (gauge, `deaths_total`, event) before the mode impl may veto it.
- **Guarded writes.** Raw `gameplay_stream.write` after the stream ends raises `InvalidStateError`/`AioRpcError`;
  a locally cancelled call raises a bare `CancelledError`. Use the base helpers (`_write_end_effect`, the guarded
  in-game variant). Census: grep `stream.write` in `games/` and check which sites run after the latch or inside
  background tasks (round timers, reveals, respawns). Each mode that opts into the early stream still has raw
  writes in its phase (#1924, #1926).
- **Resource acquire then await.** A new `await` between acquire (subscribe, stream open) and `try:` is a leak on
  client cancel: the `finally` is skipped (#1614). Probe with a subscribe that blocks the loop for 0.5 s while a
  client cancels at 0.2 s. A cleanup `finally` must run after tracked tasks are gathered (#1949).
- **EventBus and loops.** `asyncio.Lock` binds to the first loop that has to wait; the bus is shared by the gRPC
  loop and per-game thread loops. `game_ended` is published from the game thread before `game_force_ended` (up to
  a 2 s join later): consumers that take the first terminal event see a preempted game as completed (#1308).
- **Effects.** `_effect_rumble` pops `active_effects[serial]` without cancelling the task, orphaning long effects
  (shield, rainbow); `handle_game_effect` retypes the running effect, so a later `play_effect_with_restore`
  handler cancels it. A countdown that replaces the active effect without cancel interleaves (#1301, #1438).
- **Metrics from the coordinator.** `lib/otel_metrics` Gauge has `remove`/`clear`; Counter has none. A gauge with
  no `game_id` goes to the agent multiplexer's fallback partition, so labelling it turns a dead signal live for
  consumers: check them. `Gauge.set` no-ops until instruments are initialised (tests monkeypatch
  `_instrument_initialized`).

## Agent patterns (services/agent)

- **Charge ledger** (`decision/chargeledger.go`): refund deletes the newest equal-cost entry, not the dispatch's
  own; a release must check its generation (`e.gen == gen`); late spans must be dropped before they re-create an
  evicted partition (the multiplexer skips before `m.store`).
- **Rules golden.** `ObjectiveRules` is the non-LLM floor; its pipeline (cadence gate, sources re-read per eval,
  chaos roll, battery policy, variance cooldown, scoring, <=2 emitted) is load-bearing and the golden decision
  tests pin it. A rules change without a golden change is suspect; a golden change without a rules change is
  worse. Rules first, the LLM is an async length-gated upgrade (#1207).
- **Kill switch / promotion.** `killMu` serialises the revert with the write, with an epoch read BEFORE config
  resolve. A dedup marker placed in a shared helper (`retrack`) fires for every caller: a failed watch revert used
  up the kill switch's single failure report (#1880). Grep ALL callers of a helper that gains a side effect.
  The gamecontext timeline is a 64-entry ring: use `Session.StartedAt`, not a timeline "start" entry (#1749).
- **Dry-run vs real.** A rehearsed action must not commit reconcile state only an observed effect can clear
  (#1375). A stationary observed stage exposes it; an advancing stage in tests hides it.
- An OTel cumulative Counter has NO `remove`: a `game_id` (or any per-game id) label is one permanent series per
  game, forever. Per-game values belong on removable Gauges cleared at game end. Reviving a "dead" metric
  activates this: re-check the labels when a PR first calls `.inc()` on a never-written metric (#1376).
- A new metric, span or attribute needs its row in `docs/specs/telemetry.md` first; `make specs` regenerates the
  generated specs and `make specs-check` gates CI.
- Server span status: after `context.abort()` or `set_code`, aio `context.code()` returns the code; an
  `AioRpcError` that escapes a handler reaches the client as UNKNOWN, so never record `exc.code()` for it.
  `start_as_current_span(record_exception=False)` drops events `use_span` used to add. A handler that holds
  `start_as_current_span` across a `yield` loses it after the yield (the per-step attach/detach re-parents
  children). A test through a real aio loopback server is the only proof (#1928, #1794).
- SonarCloud quality gate (new coverage below 80 %): informational, not blocking. S2589 (tie detection in
  `ffa`/`teams_base`) and S7497 (`CancelledError` cleanup) are intentional.
- `limiter.admit()` under `r.mu` while the limiter holds its own mutex: strictly nested, one lock order, no
  deadlock (checked with `-race`). In-place `kept := cands[:0]` filters are standard Go compaction.
- `-race` does not catch a "write outside the mutex" mutation when ownership passes through that mutex (a benign
  happens-before edge remains): say so instead of calling it an escape.