# Specs inventory

> Snapshot of every Markdown document on `main` @ 2d162b33 (2026-10-16) when the AIUP spec tree was
> introduced (epic #1260), updated for PRs #1256, #1257, #1259, #1261 and #1265 (merged the same day, `main` @ 81c9ecea), for the spec consistency pass on `main` @ 2fdfbfd1 (PRs #1266, #1282, #1284, #1286, #1288), and for the AIUP compliance audit consistency pass on `main` @ 84c3ba15 (2026-10-16, epic #1369; PRs #1291 to #1315, #1324, #1308; new rows D-T10 to D-T12, D-A15 to D-A19, D-M10 to D-M14, D-G12 to D-G20, D-C8, D-AU3 for the bugs filed by the audit): what it covers, whether it is still true, and where its content now lives.
> Followed by the **discrepancies found** between docs and code. Index: [README.md](README.md).

**Status legend.** *current*: matches the code (minor drift at most). *stale*: contains claims the code on
`main` contradicts (evidence given). *historical*: research, designs or plans for work that has shipped or
been retired; kept for the reasoning, not as a description of the system. Superseded docs carry a one-line
banner pointing at their replacement; nothing was deleted.

**Target** names the spec-tree location: [vision](vision.md), [requirements](requirements.md) (REQ),
[entity model](entity_model.md) (ENT), [use cases](use_cases.puml) (UC-xxx),
[services](specs/services.md), [flags](specs/flags.md), [telemetry](specs/telemetry.md),
[decide contract](specs/decide-contract.md), or "guide" (stays as how-to / runbook) / "history".

## 1. Root, process and templates

| Doc | Lines | Covers | Status | Evidence | Target |
|---|---|---|---|---|---|
| `README.md` | 150 | Pitch, quick start, game modes, service table | stale | Dashboard at `localhost:8080` but envoy publishes only `80:80` (`docker-compose.yml:855`); "Python 3.11+" vs `requires-python >=3.14` (`pyproject.toml:7`); agent missing from service table. 10-mode list is correct. | vision, services; quick start stays as guide |
| `CLAUDE.md` | 76 | Contributor / agent workflow | current | `make lint/protos/test` exist (`Makefile:144,165,228`). | gains the AIUP section (this PR) |
| `CHANGELOG.md` | 802 | release-please changelog to 0.7.0 | historical | Last release 0.7.0; agent work since is unreleased. | history |
| `.claude/rules/architecture.md` | 16 | Ports, streams, state machines, motion rates | stale | "60 Hz active / 10 Hz idle stream": the CM loop is a fixed 100 Hz and there is no idle stream (D-C1); no DISCONNECTED menu state (D-M7). | services, ENT |
| `.claude/rules/development.md` | 26 | Test / metric / game-mode gotchas | stale (minor) | Stack at `localhost:8080` (D-E1); `_get_death_thresholds()` does not exist (D-G7); span examples `game.process_death`, `controller.poll_batch` do not exist (D-T6). | guide; game-mode steps → UC-020..026 |
| `.github/PULL_REQUEST_TEMPLATE.md` | 17 | PR checklist | current | Gains "Spec changes" (this PR). Related issues use "Part of #N", aligned with the repo convention. | process |
| `.github/ISSUE_TEMPLATE/*.md` | 59 | Bug / feature templates | current | `backend: bluetooth` is not a variant of the `backend` flag. | process |

## 2. Core docs (`docs/`)

| Doc | Lines | Covers | Status | Evidence | Target |
|---|---|---|---|---|---|
| `NORTHSTAR.md` | 75 | Dark-factory goal, tiers, hard constraints, today's gap | current | `decision.backend` variants, `code_improvement.mode`, no gateway all match code. | **kept as authoritative northstar**, summarised in vision (C-001..C-007) |
| `ARCHITECTURE.md` | 500 | Services, RPCs, data flows, config, observability | stale | `PlayControllerEffect` RPC absent (D-S1); "13 game modes" (D-G1); Caddy / `:8080` (D-E1, D-E2); `docker-compose.test.yml` (D-E4); metrics at `/metrics` (D-T1); admin controls (D-M3); agent, pairing daemon, connect-proxy, envoy, VictoriaMetrics missing (D-S3); `game_end` vs `game_ended` (D-G5). | services, ENT, UC-010..027, telemetry |
| `OWNERSHIP_MODEL.md` | 380 | Human vs agent ownership, composition rules | current spec with stale "Today" notes | §4.1 and §4.2 describe bugs fixed by #816 / #817 (`services/game_coordinator/difficulty_handlers.py` `handle_global_sensitivity_override`, `games/base.py` `effective_grace_until`); invariants 1 and 2 amended for the agent's gated `game.json` / `rollout.json` writes and the missing `game.json` lock (D-A3, resolved); `_effective_variant` (#818) never called (D-M6); line refs drifted. | **kept as authoritative ownership contract**; C-008, flags §2, UC-012, UC-040 |
| `CONTRIBUTING.md` | 321 | Setup, CI, style | stale | `make ci-quick`, `make lint-dockerfiles` do not exist (D-E5); line length 100 vs 120; `services/settings/` does not exist. Gains the AIUP workflow (this PR). Branch convention and Python 3.14 aligned with `CLAUDE.md` / `pyproject.toml` in the consistency pass. | guide + process |
| `DEVELOPMENT.md` | 836 | Build, run, debug | stale | `localhost:8080`; `docker-compose.test.yml`; `python:3.11-slim` examples; `--scale game-coordinator=3` impossible with fixed container names; network name `joustmania_default` vs `joustmania-network`. | guide (needs rewrite) |
| `DOCKER_WORKFLOW.md` | 208 | Compose and image workflow | stale | Images `settings-service`, `supervisor-service`, `webui-service` do not exist; `make test-unit` absent. | guide (needs rewrite) |
| `CADDY_PROXY.md` | 186 | Caddy reverse proxy | stale (obsolete) | No Caddyfile exists; the proxy is envoy (`services/envoy/envoy.yaml`). | **superseded** by services §Envoy |
| `feature-flags.md` | 734 | flagd architecture, naming, per-domain catalog | stale (core current) | ~30 flags missing from the catalog incl. `interventions_enabled`, `rollout_enabled`, `experiments_enabled`, `code_improvement.*`, `verdict_*`; lifecycle flags documented as restart-only although they hot-reload since #927 (D-F2). | **catalog superseded** by flags.md; architecture prose stays as guide |
| `AUDIO.md` | 197 | Audio service | stale (minor) | pygame / scipy / WAV vs miniaudio / soxr / OGG (D-AU1). RPCs and 8-channel limit correct. | services §audio, UC-031 |
| `25-controller-setup.md` | 291 | 25 controllers on a Pi 5 | stale | `UPDATE_FREQUENCY = 30` vs 60 (`games/base.py:53`, flag default 60). | NFR-025, guide |
| `hardware-setup-guide.md` | 582 | Pi 5 hardware, adapters, power | current (minor drift) | Daemon poll "30 s" vs flag default 10 s; `:8002/metrics` serves `/healthz` only. | guide; C-010, C-011 |
| `CONTROLLER_CONNECTIVITY.md` | 349 | RSSI troubleshooting | stale | `controller_rssi_dbm` does not exist (real: `bluetooth_device_rssi_dbm` from the pairing daemon); no weak-signal LED; `get_all_device_rssi_values` absent (D-C5). | guide (needs rewrite); telemetry |
| `controller-guide.md` | 115 | Player button reference | stale | 4 admin options incl. `agent_control`, 5 sensitivity levels (D-M3); `localhost:8080`. | UC-010..013 |
| `controller-feedback.md` | 258 | LED colour reference | stale | Wrong RGB values, 50 % dim vs 0.3, orange vs white warning, wrong timings (D-C3); broken `planning/` links. | **superseded** by UC-010, UC-030 + ENT |
| `led-feedback.md` | 249 | Lobby / admin LED states | stale | Duplicates controller-feedback.md with the same errors (D-C3, D-M3). | **superseded** by UC-010..013 |
| `observability-quickstart.md` | 373 | Performance tooling | stale | Four metrics that do not exist, `StartGame` RPC, port 50051, `localhost:9090/metrics` (D-T5). | guide; telemetry |
| `CANARY_RUNBOOK.md` | 513 | Manual Python→Rust canary | stale | `/canary/*` not routed by envoy (D-B3); `localhost:8080`; obsolete #612 stub note; ignores the agent rollout. | guide; UC-052 |
| `architecture/controller-backends.md` | 203 | CM backend layering | current (minor drift) | Invalid example `defaultVariant: "mock,bluetooth"`; misses UnstableAdapter, rollout router. | services §controller-manager |
| `agent-act-runbook.md` | 406 | Taking the agent live | stale (minor) | Greps for a log line that no longer exists (D-A7); `localhost:8080`; describes the startup self-heal that never runs (D-A1). | guide; UC-040, UC-045 |
| `agent-demo-runbook.md` | 482 | Three-act demo operator playbook | current (one stale line) | Same stale log grep (D-A7); tells you to edit base `agent.json` while the demo serves `ci/`. | guide; UC-070 |
| `agent-dry-run-runbook.md` | 159 | Experiment loop dry run | stale (minor) | Seeded flag is `windows`, not `death_grace_period_seconds`; omits the env gates on promotion (D-A6). Kill-switch span name corrected to `agent.disabled` in the consistency pass. | guide; UC-043 |
| `agent-inference.md` | 95 | Who calls which model | current | Matches #1234 (PR #1252) / #1244; does not say the #1207 length gate is retired, nor the backend role split (C-018). | decide contract; UC-042, UC-044 |
| `talk/two-standards-runbook.md` | n/a (PR #1256, merged after the snapshot) | Talk operator runbook: setup, timing, beat table, pre-flight, reset, failure modes | current | Added by PR #1256; rewritten for the C-018 role split (#743): Jev-first model beat with act / noop / held on Jev's own threshold (#1288), Claude as the manual backup and the closing post-game summary, `stage` budget, kill-switch scope (#1284). Beats map to UC-070; rehearsal record still open. | guide; UC-070 |
| `agent-trace-viz.md` | 307 | Agent trace on stage | stale (minor) | `agent.span_received` retired (#1053); `agent.signal_received` is a remote child of `game_cycle` (#1325), not a root (D-T2). | telemetry §agent; guide |
| `design/agent-experiment-cohorts.md` | 389 | Cohort framework design (#982) | historical | Says "no implementation" but shipped (`services/agent/experiment/`). | ENT (Experiment, Arm), UC-043; history |
| `research/722-intervention-surface.md` | 576 | Intervention vocabulary research | historical | Allow-list format and limiter location changed (#1127, #919). | ENT (Intervention), flags; history |
| `research/739-prompt-capture.md` | 146 | In-game LLM prompt spike | historical | Already bannered; path retired by PR #1252 (issue #1234). | history |
| `research/774-shadow-games.md` | 267 | Shadow game analysis | historical | `game_kind` vocabulary changed (#1249); broken link to `controller.ci.json`. | ENT (GameSession), UC-043; history |
| `research/844-post-game-retro.md` | 168 | Retro capture | historical | Already bannered. | UC-042; history |
| `research/m7-4-testing-strategy.md` | 412 | Test strategy for propose / validate / promote | historical | Already bannered; pre-#1234. | history |
| `research/spike-survival-calibration-1125.md` | 153 | Offline fitness calibration | historical | Self-declared offline evidence (#1125 open). | history |

## 3. Service, tool and test docs

`scripts/talk-preflight.sh` (PR #1256) is a read-only pre-flight check, not a doc; it is FR-093 and is consumed by UC-070. It checks the runbook's stage baseline, including Jev's threshold, both backend keys (presence only), `AGENT_INFERENCE_BACKEND=claude` and a budget of at least 30 per minute (#743).

| Doc | Lines | Covers | Status | Evidence | Target |
|---|---|---|---|---|---|
| `services/agent/README.md` | 1260 | Agent reference | stale (several sections) | "Stub decision hooks" intro; removed `mode` flag still listed; `AGENT_EXPERIMENTS_ENABLED` called the gate (it is a fallback, D-A4); promotion env gates undocumented (D-A6); Files tree outdated. The write-surface paragraph ("only flagd write", `:ro` mount) and the kill-switch span name were corrected in the consistency pass. | services §agent, flags, telemetry, decide, UC-040..051; keeps code-level detail |
| `services/agent/decide/backends/jev/README.md` | 63 (on 2d162b33) | Jev backend: wire format, confidence, errors | current (rewritten by PR #1261 for the real client) | Stale `unavailable` value fixed (D-K2). | decide contract |
| `services/audio/README.md` | 101 | Audio API | current (minor drift) | `AUDIO_ASSETS_DIR` default; WAV examples (D-AU1). | services §audio |
| `services/connect-proxy/README.md` | 59 | Connect bridge | stale | Omits `GetConnectedControllers`, `ListGames`, `/chaos/*`, `/canary/*`. | services §connect-proxy |
| `services/dashboard/README.md` | 65 | SPA | current (file tree stale) | Experiments tab undocumented. | services §dashboard, UC-035 |
| `services/controller_manager/README.md` | 247 | CM architecture | stale | `PlayControllerEffect` (D-S1); flag `controller_backend` in `performance.json` (real: `backend` in `controller.json`); metrics port 8001 vs 8000. | services §controller-manager |
| `services/controller_manager/MOCK_ENVIRONMENT.md` | 532 | Mock hardware | stale | `scripts/testing/` absent; serials `MOCK%04d` not `mock_controller_N`; `MOCK_CONTROLLER_COUNT` unused; `StartGame` RPC. | guide (needs rewrite); UC-003 |
| `services/game_coordinator/README.md` | 120 | GC overview | stale | `StartGame` / `GetGameStatus` RPCs do not exist (D-S2); mode list wrong (D-G1). | services §game-coordinator, UC-020..027 |
| `services/game_coordinator/DISTRIBUTED_TRACING.md` | 464 | GC tracing (Phase 36) | stale / historical | `GrpcInstrumentor*` unused; span names `player_{serial}_lifecycle` vs `player_lifecycle` (D-T4). | **superseded** by telemetry §game-coordinator |
| `services/game_coordinator/games/PER_PLAYER_TRACING.md` | 370 | Early per-player span design | historical | Span names superseded. | **superseded** by telemetry; history |
| `services/game_coordinator/games/README.md` | 516 | Game modes dir | stale (span section current) | Says only 3 modes implemented; `legacy_archived/` absent; wrong registration file (D-G7). | UC-020..026; guide for adding a mode |
| `services/menu/README.md` | 406 | Menu RPCs, LEDs, admin | stale (admin section partly current) | Mode list / colours / dim wrong (D-C3); `GetMenuStatus` absent; `admin_action` event never emitted (D-M5). | services §menu, UC-010..013 |
| `services/pairing_daemon/README.md` | 178 | Pairing daemon | stale | Describes a host systemd install; it is now a compose container with flag-driven intervals (D-C4). | services §pairing-daemon, UC-001 |
| `proto/README.md` | 375 | Proto layout, RPC summary | stale | `PlayControllerEffect`, `StartGame`, mock RPC names, "13 modes" (D-S1, D-S2, D-G1). | services (RPC tables generated from `.proto`) |
| `scripts/README.md` | 174 | Script layout | stale | `docker/`, `planning/`, `testing/` dirs absent; six make targets absent (D-E5); agent scripts undocumented. | guide |
| `scripts/ci/README.md` | 100 | CI scripts | current (minor) | `make ci-build-service` absent. | guide |
| `tests/integration/README.md` | 444 | Integration tests | stale (agent sections current) | Lists `test_mock_environment.py` (absent); `StartGame` RPC example. | guide |
| `tools/ci-proto/README.md` | 40 | ci-proto image | stale (minor) | Python 3.11 vs `python:3.14-slim`. | guide |
| `tools/demo/README.md` | 173 | Demo driver | current (URL drift) | `localhost:8080`; edits base `agent.json` while `make dry-run` serves `ci/` (D-A8). | UC-070; guide |

## 4. Discrepancies found

Where docs and code disagree, the specs state what the **code** does. Each row is either a stale doc
(fix the doc) or a likely bug (fix the code); "Kind" says which we believe it is. None were fixed in the
spec PRs. Verified against `main` @ 2d162b33; rows added or updated by the audit consistency pass (epic #1369) are verified against `main` @ 84c3ba15. "verified by reading" means not run. Rows marked "backlog, not scheduled (FFA focus)" concern modes other than Free-for-All: the bug is filed but not planned while the focus is FFA.

### 4.1 Entry points, environment, build (D-E)

| ID | Doc claim | Code | Kind |
|---|---|---|---|
| D-E1 | UIs at `http://localhost:8080` (README, ARCHITECTURE, DEVELOPMENT, CANARY_RUNBOOK, observability-quickstart, agent-act-runbook, agent-trace-viz, controller-guide, agent README, tools/demo README and `tools/demo/validation_harness.py`, `.claude/rules/development.md`) | Only envoy is published, on host port 80 (`docker-compose.yml:855`, `services/envoy/envoy.yaml:20`); 8080 is container-internal (connect-proxy, dashboard). `make up-mock` prints `http://localhost/` (`Makefile:76-79`). | stale doc (+ harness default); **partly resolved**: `.claude/rules/development.md` now names envoy on port 80 (`http://localhost/`); README, agent-act-runbook, CANARY_RUNBOOK and the other docs listed still say `localhost:8080` |
| D-E2 | Caddy reverse proxy (`ARCHITECTURE.md:248,413`, `CADDY_PROXY.md`) | Envoy; dashboard served by static-web-server (`services/dashboard/Dockerfile:25`). | stale doc; **superseded**: both docs carry a "Superseded" banner pointing at services.md (fix-or-banner rule) |
| D-E3 | Python 3.11+ (README, CONTRIBUTING, DEVELOPMENT, several READMEs) | `requires-python >=3.14,<3.15` (`pyproject.toml:7`), `python:3.14-slim` / Chainguard 3.14 images. `lib/`, `proto/`, `tests/integration` still declare `>=3.11`. | stale doc |
| D-E4 | `docker-compose.test.yml` (ARCHITECTURE:390, DEVELOPMENT:305, feature-flags:694) | Does not exist; `make test` runs `uv run --all-packages pytest` (`Makefile:228-229`). | stale doc |
| D-E5 | Make targets `images`, `up`, `ci-quick`, `lint-dockerfiles`, `test-unit`, `image-dashboard`, `build-all-services`, `docker-start`, `docker-stop`, `ci-all`, `validate-protos`, `validate-packages`, `ci-build-service` | Not in the Makefile. | stale doc |
| D-E6 | `docker-compose.override.yml:6` "base config already defaults to mock mode" | Base flagd serves `controller.json` with `backend=python_rust`; only `ci/controller.json` is mock. | stale comment; **resolved by #1371** (base default is now `rust`; the override comment says so) |
| D-E7 | README Grafana credentials `admin/joustmania` | Not configured (`services/grafana/grafana.ini:247-250` commented out). | stale doc |

### 4.2 Services and RPCs (D-S)

| ID | Doc claim | Code | Kind |
|---|---|---|---|
| D-S1 | `PlayControllerEffect` RPC (ARCHITECTURE:80, CM README, proto README) | Not in `proto/controller_manager.proto` (RPCs: StreamButtonEvents, StreamGameplayData, RenameController, GetConnectedControllers). Effects ride on the streams. | stale doc |
| D-S2 | `StartGame` / `GetGameStatus` (GC README, DISTRIBUTED_TRACING, MOCK_ENVIRONMENT, proto README, tests README, observability-quickstart); `GetMenuStatus` (menu README) | Games start through `StreamGameEvents(start_config)`; RPCs are ForceEndGame, StreamGameEvents, GetGameState, ListGames (`proto/game_coordinator.proto:10-22`). | stale doc |
| D-S3 | ARCHITECTURE lists 6 app services | Agent, pairing daemon, connect-proxy, envoy, VictoriaMetrics, Loki, collector, flagd all run (`docker-compose.yml`). | stale doc; **superseded**: `docs/ARCHITECTURE.md` carries a "Superseded" banner pointing at services.md |
| D-S4 | Mock RPC names `AddMockController` etc. (proto README) | `AddController`, `RemoveController`, `AddControllers`, `Simulate*`, ... (`proto/controller_manager_mock.proto`). | stale doc |
| D-S5 | `proto/controller_manager.proto:160` gameplay default 60 Hz | Servicer fallback 30 Hz when the client sends 0 (`services/controller_manager/servicer.py` `_process_gameplay_config`). | stale comment |

### 4.3 Game modes and lifecycle (D-G)

| ID | Doc claim | Code | Kind |
|---|---|---|---|
| D-G1 | "13 game modes" incl. Commander, Ninja, Random (ARCHITECTURE, proto README, GC README) | 10 registered in `_GAME_CLASSES` (`services/game_coordinator/game_factory.py:56-67`); the three are enum names only. A raw start of one publishes `game_start` then `game_error "not implemented"`. | stale doc; **superseded** in `docs/ARCHITECTURE.md` (banner); `services/game_coordinator/README.md` still lists Commander and Ninja |
| D-G2 | `lib/types.py` `minimum_players` per mode (Traitor/Zombies 4, Teams 3, ...) | Never enforced; the only gate is ≥ 2 players (`services/game_coordinator/servicer.py` `_start_game_from_config`, `games/base.py` `run` (the `< 2` players check), `services/menu/state_manager.py` `all_ready`, `services/menu/servicer.py` `_run_game_lifecycle`). | likely bug (issue #1277) |
| D-G3 | Werewolf, Zombie, Fight Club and Tournament behave like other modes for music tempo | They override `run()` and never start `_music_loop`, `_warmup_ema` or `set_game_transaction_context`; tempo stays at 1.0 and the slow threshold table applies all game (`werewolf.py:512`, `zombie.py:582`, `fight_club.py:571`, `tournament.py:784`). Verified by reading. | likely bug (issue #1273; audit A24 adds the missing BR-003 EMA warm-up, commented on #1273); backlog, not scheduled (FFA focus) |
| D-G4 | Proto `GameState.ENDING`, "STARTING = countdown in progress" | Session state is never set to ENDING; it becomes RUNNING on `game_started`, which base modes publish **before** the countdown (`games/base.py` `run`: the `GAME_STARTED` publish comes before `_countdown`; `game_session.py` `on_event_state_sync`). | stale doc / semantics |
| D-G5 | Events `game_end`, `score_update`, `player_death` (FFA), `team_victory` | Published names are `game_ended`, `team_winner`; `score_update` never published; `player_death` published only by NonStop (FFA/Teams record it as a span event). | stale doc |
| D-G6 | Zombie duration "~3 min for 4 players" (`zombie.py:60-62` docstring) | Formula `((n*3)/16)*60` gives 45 s for 4 players, 135 s for 12 (`zombie.py:71`). | stale comment or bug |
| D-G7 | Register a mode in `games/__init__.py` / `server.py`, override `_get_death_thresholds()` | Register in `game_factory.py:56-67` (`games/__init__.py` is empty); the hook is `_compute_effective_thresholds` (`games/base.py` `_compute_effective_thresholds`). | **Resolved by PR #1289** (`.claude/rules/development.md` and `games/README.md` name `_GAME_CLASSES` + `_compute_effective_thresholds`) |
| D-G8 | Tournament waiting players cannot die | Waiting players stay `alive=True` and are not filtered; a waiting player crossing the threshold reaches base `_kill_player` side effects before `_kill_player_impl` returns early (`tournament.py:311,666-667`). Verified by reading. | likely bug |
| D-G9 | Fight Club EMA follows `perception.ema_weight` | Hardcoded weight 4, no EMA reset in grace, no warning debounce (`fight_club.py:691-705`). | bug (issue #1347, audit A14); backlog, not scheduled (FFA focus) |
| D-G10 | Traitor / RandomTeams team-colour pulse and traitor rumble | Additional phases run before the gameplay stream exists, so those effects are skipped silently (`base.py` `run`: additional phases at :2902, `_start_gameplay_stream` at :2907; `traitor.py:284`). Verified by reading. | likely bug |
| D-G11 | `ListGames` player score | `PlayerInfo.score` hardcoded 0 (`servicer.py` `_build_game_info`). | gap |
| D-G12 | UC-025 step 5: Tournament ends with `tournament_champion`, a champion rainbow and voice | `_gameplay_loop` breaks when one or zero players are left and never calls `_check_win_condition()`, the only place that publishes `tournament_champion` / `tournament_no_winner`; `game_ended.champion=None`, no rainbow, no voice (`games/tournament.py`). Audit A4. | bug (issue #1337); backlog, not scheduled (FFA focus) |
| D-G13 | UC-026: the NonStop time limit ends the game and the winner gets the rainbow | The mode's `start_time` override drops the setter, so the time limit never ends the game; the winner rainbow is skipped (`games/nonstop_joust.py`, `games/base.py`). Audit A5. | bug (issue #1338); backlog, not scheduled (FFA focus) |
| D-G14 | UC-023 / UC-024: a converted zombie or swapped player shows the new colour | Base `_kill_player` sends `GAME_EFFECT_PLAYER_DEATH` after the mode's `_kill_player_impl`, so the LED goes black while the player is still in the game (`games/base.py` `_kill_player`, `games/swapper.py`, `games/zombie.py`). Audit A9. | bug (issue #1342); backlog, not scheduled (FFA focus) |
| D-G15 | UC-027: a force end declares no winner (FFA) | FFA `_end_game_impl` still sends the rainbow, `VOX_CONGRATULATIONS` and survivor `victory` events when one player happens to be alive at a force end (`games/ffa.py`). Audit A10, FFA part. | bug; **resolved by #1370** (a force-end records outcome `force_ended` with the reason; no rainbow, no congratulations; survivors get `player_survived`, not `victory`; UC-020 A4, BR-003) |
| D-G16 | UC-027: a force end declares no winner (other modes) | Werewolf, Zombies, Swapper, Fight Club and NonStop still publish a winner, the rainbow, the win voice and `game_ended` after a force end. Audit A10. | bug (issue #1343); backlog, not scheduled (FFA focus) |
| D-G17 | UC-024 A1: a disconnected Swapper player is eliminated | The disconnect goes through the swap path, so the player changes team instead of leaving (ghost team member) (`games/swapper.py`). Audit A11. | bug (issue #1344); backlog, not scheduled (FFA focus) |
| D-G18 | UC-025 A2: tied Fight Club players fight a face-off | `_start_round` overwrites the preset fighter with `queue.pop(0)`, the tied players stay in the queue and `face_off_mode` is never reset (`games/fight_club.py` `_should_end_game`, `_start_round`). Audit A12. | bug (issue #1345); backlog, not scheduled (FFA focus) |
| D-G19 | UC-022: Traitor count from `traitor.count_tiers`; minimum 4 players | A tier asking for at least as many traitors as players spins forever in the selection loop (no `await`, hangs the coordinator loop); with 2 players the traitor wins on the first sample (`games/traitor.py`; minimum not enforced, D-G2). Audit A13. | bug (issue #1346); backlog, not scheduled (FFA focus) |
| D-G20 | UC-021 A1 random formation of 2 to 6 teams; Werewolf all-dead outcome | Random Teams can leave a team empty (2 players / 2 teams land on one team with p = 1/3, instant `team_winner`); Werewolf all-dead publishes `werewolf_winner` with `winner=none` instead of `game_tie` (`games/random_teams.py` `_assign_random_teams`, `games/werewolf.py`). Audit A25. | bug (issue #1354); backlog, not scheduled (FFA focus) |

### 4.4 Menu and admin (D-M)

| ID | Doc claim | Code | Kind |
|---|---|---|---|
| D-M1 | MOVE cycles games in the lobby (controller-guide) | SELECT is wired to cycle (`services/menu/handlers/connected.py` `handle_button`), but calls `self._state_manager.settings`, which does not exist (`connected.py` `_handle_select` before #1282; `state_manager.py` `StateManager`); unit tests mask it with a MagicMock. Working paths: admin Cross, `ProcessInput`. | **Resolved by PR #1282** (SELECT cycles via the shared selection path; a handler error no longer drops the button stream; `controller-guide.md` says SELECT) |
| D-M2 | `menu_auto_start` equals StartMenu | The boot path sets RUNNING and FFA only: no lobby music, no idle monitor, no `menu_started`, ignores `current_game` (`services/menu/server.py:94-98`). | likely bug |
| D-M3 | Admin: Trigger/Cross change values, 3 or 8 options, 3 sensitivity levels, option colours | Select/Start change values, Cross cycles mode, Trigger hold force-starts; 4 options (sensitivity, num_teams, force_all_start, agent_control); 5 sensitivity levels (`admin.py` module docstring, `AdminModeHandler.option_names`, `sensitivity_colors`). | stale doc |
| D-M4 | Admin timeout "60 s of inactivity" | 60 s from entry, never extended (`admin.py` `entry_time`, `_start_timeout_task`); exit always returns to CONNECTED (un-readies). | stale doc |
| D-M5 | `admin_action` menu event | Never published. Auto-start publishes no `game_requested` (only force start does). | stale doc |
| D-M6 | #818 truthful display via `_effective_variant` (OWNERSHIP_MODEL §6) | Defined, never called; admin feedback shows the on-disk baseline (`admin.py` `_effective_variant`). | likely bug; the dead helpers (`_effective_variant`, `handle_game_mode_change`, `reset_on_disconnect`) are listed for removal in the #1322 cleanup |
| D-M7 | Controller states DISCONNECTED → CONNECTED → READY | Menu states CONNECTED, READY, ADMIN, IDLE; disconnected controllers are removed (`services/menu/handlers/base.py:68-75`). | **Resolved by PR #1289** (`.claude/rules/architecture.md` lists the menu states) |
| D-M8 | Zombies / NonStop announced and coloured in the lobby | Voice and colour maps use keys `Zombie`, `NonstopJoust`; enum names are `Zombies`, `NonStop` → no announcement, orange like FFA (`services/menu/utils/audio.py:38-51`, `utils/led.py:14-27`). | likely bug (issue #1276) |
| D-M9 | Dashboard mode selector starts the chosen mode | `start_game` web command ignores `mode`; the selector offers Commander / Ninja and omits Traitor, Fight Club, NonStop (`services/dashboard/src/index.html:19-28`; `services/menu/servicer.py` `_handle_web_command`). | likely bug; **resolved by #1483** (`start_game` selects the named mode and rejects an unknown or unimplemented one; the selector is built from the new menu `ListGameModes` RPC) |
| D-M10 | UC-013: the admin kill switch steps `interventions_allowed` towards `none` and stops there | The ladder wraps: "restrict" at `none` sets `full` (`services/menu/handlers/admin.py`). SAFETY. Audit A7. | bug; **resolved by #1373** (the ladder is clamped at both ends, `_next_agent_level`) |
| D-M11 | UC-012 / LOBBY_ENTRY: exactly one controller can be in ADMIN | A second controller can enter admin mode while one is active; the timeout and exit are cross-wired between the two sessions (`services/menu/servicer.py`). SAFETY. Audit A8. | bug; **resolved by #1373** (single admin lock with stale-lock self-heal, UC-012 BR-004) |
| D-M12 | UC-010 A3: fewer than 2 players in the roster publishes `game_start_cancelled` | Published only when the state is already `GAME_STARTING`, which only the admin force start sets first; the ready-up / auto-start and `ProcessInput` / web paths publish nothing (`services/menu/servicer.py` `_run_game_lifecycle`). Audit A15. | bug; **resolved by #1374** (the roster abort publishes `game_start_cancelled` on every start path) |
| D-M13 | UC-011 A1: a web mode selection publishes `game_selection_changed` | The web `select_game` path never publishes it; auto start does not restore `current_game` (`services/menu/servicer.py`). Audit A16. | bug; web event **resolved by #1374** (web `select_game` also publishes `game_selection_changed`); auto start not restoring `current_game` stays open (issue #1285) |
| D-M14 | UC-012: changing sensitivity in admin mode is confirmed by voice | Select / Start change the sensitivity with LED feedback only; no voice line plays (`services/menu/handlers/admin.py`). Audit A17. | bug; **resolved by #1374** (Select / Start on `sensitivity` play the per-level voice line) |

### 4.5 Controllers, pairing, LEDs (D-C)

| ID | Doc claim | Code | Kind |
|---|---|---|---|
| D-C1 | 1000 Hz poll → 60 Hz active / 10 Hz idle stream (`.claude/rules/architecture.md`, ARCHITECTURE, `services/controller_manager/README.md:147-157`, and the stale docstrings `services/controller_manager/discovery_loop.py:242,466`) | HID ≈ 1 kHz; CM loop fixed 100 Hz; stream 60 Hz by flag (30 Hz fallback); no idle stream; the EMA lives in the game coordinator. | stale doc; **partly resolved**: `.claude/rules/architecture.md` now states HID ≈ 1 kHz → 100 Hz loop → 60 Hz stream; `services/controller_manager/README.md` (adaptive 60 / 10 Hz polling), `docs/ARCHITECTURE.md` (bannered) and the `discovery_loop.py` docstrings remain |
| D-C2 | Chaos wrapping only when chaos is configured | `_is_chaos_enabled()` returns true whenever the flag evaluates, ignoring the value, so real adapters are always wrapped (`services/controller_manager/backend_factory.py:95-106`); faults still gated by `chaos_fault_type=none`. | likely bug (issue #1274) |
| D-C3 | LED colours, 50 % dim, orange warning, 1 s countdown phases, 2 s rainbow, battery tiers (controller-feedback, led-feedback, menu README) | `lib/colors.py`, `DIM_FACTOR = 0.3` (`services/menu/utils/led.py:33`), white warning flash, 750 ms phases, 3000 ms rainbow, 5 battery tiers (`services/controller_manager/feedback_manager.py`). Low-battery effect has no producer. | stale doc |
| D-C4 | Pairing daemon is a host systemd service; LED feedback during pairing | Compose container (`docker-compose.yml:283-321`); no LED writes in `psmove_pairing/`. | stale doc |
| D-C5 | RSSI metric `controller_rssi_dbm`, weak-signal LED | `bluetooth_device_rssi_dbm` from the pairing daemon; CM hardcodes rssi 0 (`state_cache.py` `_build_controller_state_message`); `lib/hci_rssi.py` was dead code. | stale doc; **dead `lib/hci_rssi.py` resolved by the #1322 cleanup** (file and its test deleted; the RSSI doc claim stays stale) |
| D-C6 | Pairing backend flag values `hidapi` / `rust` | Flag values are `python` / `rust` / `unstable`; anything but `rust` fell back to hidapi (`psmove_pairing/config.py` `get_adapter_routing_default` before #1320). | vocabulary mismatch; **resolved by #1320 / #1371** (the daemon maps every flag variant: `rust` → rust-hid, `python` / `unstable` → hidapi, unknown → `rust`; UC-001 BR-003) |
| D-C7 | `SimulateMovement` sets a persistent acceleration (`tools/demo/demo_driver.py:32`) | The next mock poll (100 Hz) overwrites it with noise, so scripted motion lasts about 10 ms; demo scenarios and the shadow-game runner drive mostly noise; only `SimulateDeath` is reliable (`services/controller_manager/multiplexer/mock_adapter.py:160-168` @ e59cd617, before the fix). | **Resolved by PR #1296** (issue #1279: `SimulateMovement` holds its vector until the next call or `ResetController` (`mock_adapter.py:166-177` held branch, `hold_accel` / `clear_held_accel` at `:212` / `:234`); the death latch still wins for its 2 frames; UC-003 BR-002 / A3) |
| D-C8 | UC-003 A1: `SetAutoGameEnd` kills the game's players so the game reaches a winner | Kills every mock controller except the last added, including reserved shadow-game mocks and mocks not in the game; since #1308 it spares only pattern controllers (`services/controller_manager/mock_control_service.py`). No test. Audit A22. | bug; resolved by deleting the RPC in the #1322 cleanup (UC-003 A1 and services.md change in the same PR) |

### 4.6 Agent (D-A)

| ID | Doc claim | Code | Kind |
|---|---|---|---|
| D-A1 | Startup self-heal of a corrupt `interventions.json` (#924; agent-act-runbook) | Never runs: `services/agent/main.go:558` type-asserts `*actions.Writer` but the sink is a `GatedActionSink` (`main.go:141-152`). Validate-after-write still works. | **bug** (issue #1271); **Resolved by PR #1418** (`main.go` builds the shared `Writer` once and calls `HealIfCorrupt` on it directly) |
| D-A2 | **Canonical statement.** Interventions affect only the deciding game (coordinator docstring `interventions.py:555-561`) | The writer emits no `gameId` targeting, so an un-targeted state flip resolves for every live session, including the real game when a shadow loop decided (`services/agent/actions/writer.go:405-466`). Needs a runtime check; design issue #920. `end_game` is addressed to its game since #1310 (UC-040 BR-011); per-player grants apply once per decision since #1313 (BR-012); per-player entries are pruned when their game retires and level factors no longer carry over into the next game since #1323/#1317 (BR-016, BR-017). | **likely bug** (FR-069) (issue #1269); **partly addressed by PR #1315** (`end_game` scoped to the deciding game, #1310; per-player grants once per decision, #1313); every other intervention is still un-targeted; Resolved by #1417 (every intervention is written for its game on the flag service, FR-136/FR-137) |
| D-A3 | OWNERSHIP_MODEL invariants 1 and 2: agent never writes `game.json`; write surfaces disjoint | Experiment writer adds shadow-scoped targeting to `game.json`; the autonomous promoter writes the real default; menu and agent share `game.json` without a lock. The `:ro` agent.json mount in comments does not exist (`docker-compose.yml:800`). | **Resolved by #1291** (stale doc: OWNERSHIP_MODEL §1 invariants and C-008 now allow shadow-scoped targeting, gated autonomous promotion and the `rollout.json` write, and state the missing-lock risk; `services/agent/README.md` no longer claims a `:ro` mount. The lock itself remains a design gap.) |
| D-A4 | `AGENT_EXPERIMENTS_ENABLED` gates the experiment registry (agent README:1031) | Loop always built; live flag `experiments_enabled` governs, env is a fallback when flagd is unreachable (`services/agent/experiment_loop.go:473-492,1238-1246`). | stale doc |
| D-A5 | The experiment proposer obeys the kill switch | M7-4 proposer checked neither `enabled`, `experiments_enabled` nor the LLM budget (`services/agent/main.go:795-809` @ 81c9ecea); with `AGENT_INFERENCE_BACKEND=claude` it called Claude and wrote shadow-scoped `game.json` targeting while the agent was off. | **Resolved by PR #1284** (issue #1270: `ProposerConfig.Permit` reads `enabled` + `experiments_enabled` at each game end and again before the write; `decision.LLMAdmission` charges the shared budget for real calls) |
| D-A6 | Promotion governed by `code_improvement.*` flags | Also env-gated (`AGENT_CODE_IMPROVEMENT_ENABLED`, `AGENT_AUTONOMOUS_ENABLED`, `GITHUB_*`, ...; `services/agent/promote/realdefault.go:117-124`), none passed through compose, so the stock stack only records no-op promotions. Issue mode needs `target=github` at startup. | stale doc + gap |
| D-A7 | Log line `Agent intervention writes enabled (#730)` (act / demo runbooks) | Now `Agent intervention writes gated by LIVE interventions_enabled flag ...` (`main.go:149`). | stale doc |
| D-A8 | tools/demo README edits base `services/flagd/agent.json` | `make dry-run` serves `services/flagd/ci/`. | stale doc |
| D-A9 | `agent_interventions_applied_total` counts applied interventions | Also counts applies turned into no-ops while `interventions_enabled` is off (`actions/gated.go:60-65`). | likely bug |
| D-A10 | `code_improvement.engine` selects the proposer backend | Recorded on the span only; `AGENT_INFERENCE_BACKEND` selects. | misleading flag |
| D-A11 | Stale comment "propose → validate → promote is a documented follow-up" (`main.go:817`) | Cohort → promote is wired (`registry.go:1383`); only the M7-7 validator is unwired (#964). | stale comment |
| D-A12 | `remediation_allowed` is a flagd flag | Read from `rollout.json` directly (defaultVariant only), bypassing OpenFeature (`actions/remediation_reader.go`); rollout cooldown is in memory only. | design inconsistency; Resolved by #1417 (read through flagd, `flags.Flags.RemediationAllowed`; the cooldown stays in memory) |
| D-A13 | FR-041 and `gamecontext/gamekind.go`: an empty or unknown `game_kind` never counts as real (fail closed) | `gameKindOrDefault` in `services/agent/experiment_loop.go` (@ 7d25ade4) mapped an empty kind to `real`, so an unlabeled non-experiment game seeded the recent-real verdict baseline and an unlabeled experiment-game sample was stored as real. | **Resolved by #1291** (bug: only an explicit `real` / `primary` label seeds the baseline; tests `services/agent/experiment_fitness_store_test.go::TestOnGameEnd_EmptyOrUnknownKindNotInRealBaseline`, `TestRecordFinalizedFitness_EmptyKindNotLabeledReal`) |
| D-A14 | FR-042 / UC-045: `enabled` stops "all agent decisions" | The infrastructure loop (`services/agent/decision/infra_loop.go`) never reads `enabled`; rollout expansion and rollback are gated only by `rollout_enabled` and `remediation_allowed`. | **Resolved by #1291** (stale doc, maintainer decision: scope the spec, no code change; FR-042 says in-game decisions, UC-045 A4 covers the rollout gates) |
| D-A15 | FR-041: one context per game id, decisions for different games never mix | Six game-wide gauges (`game_music_tempo`, `game_effective_death_threshold`, `game_effective_warning_threshold`, `game_sensitivity`, `game_current_mode`, `game_actual_update_frequency_hz`) carry no `game_id`; the agent records them on whichever partition is selected, so a real and a shadow game overwrite each other (`services/game_coordinator/metrics.py`, `services/agent/gamecontext/extract_metrics.go`). SAFETY. Audit A3. | bug; **resolved by #1379** (the game-state gauges carry `game_kind, game_id`; `game_current_mode` is emitted per game, #1336) |
| D-A16 | UC-045 A4 / UC-050 A3: turning `rollout_enabled` back on resumes the rollout | A dry run (`rollout_enabled` off) updates `InfraLoop` state as if the write landed (`lastWritten`, `rollbackInFlight`, cooldown), so after re-enabling there is no expansion or rollback until the agent restarts (`services/agent/decision/infra_loop.go`). SAFETY. Audit A6. | bug; **resolved by #1375** (a dry run commits no rollout state; re-enabling resumes without a restart; UC-050 BR-003) |
| D-A17 | UC-040 A3 / decide-contract §7: a same-cycle fallback is recorded with `decide.fallback_reason` | `unknown_backend`, `backend_unavailable` and `async_not_wired` fall back to rules; when rules emit nothing, `OnEvaluate` returns before the decide span, so there is no span and no metric (`decision/decide_hook.go` `decideRemote`, `decision/decision.go` `OnEvaluate`). Audit A18. | bug; **resolved by #1377** (configuration fallbacks counted every cycle and, on a quiet cycle, a throttled `agent.decide` span; `agent.decide.backend_misconfigured` on state changes) |
| D-A18 | UC-040 A17: game end clears the shield feedback of every player whose shield is still running | `_send_shield_feedback` keeps only the latest grant's end time per player, so a full shield followed by a shorter partial shield is missed by the clear at game end (`games/base.py` `_send_shield_feedback`, `_end_shield_feedback`). Low impact. Audit A23. | bug; **resolved by #1380** (`_send_shield_feedback` keeps the latest end time of any running shield per player) |
| D-A19 | FR-053: per-player sensitivity and handicap interventions revert to neutral | `player_sensitivity_factor` / `player_handicap_factor` are never reset to neutral in game when every player reverts (#1317). | bug; **resolved by #1418** (`player_sensitivity_factor` / `player_handicap_factor` reset to neutral when every player reverts, UC-040 A24, #1317) |

### 4.7 Decide contract (D-K)

| ID | Doc claim | Code | Kind |
|---|---|---|---|
| D-K1 | `Noul` dropped as unknown (old comment on `decide/decide.go` `QuestionKind`) | Real Jev primitive (#1229). | **Resolved by PR #1261** (`decide.KindNoul`, `Answer.Noul`) |
| D-K2 | `fallback_reason=unavailable` / `invalid` (jev README, #1229 comments) | `backend_unavailable` / `invalid_answer` (`decision/decide_registry.go:46,54`). | **Resolved by PR #1261** (README rewritten; #1229 comments still use the old words) |
| D-K3 | Epic #1225 body: decide span child of `agent.decision`; outcomes act / shadow / fallback; rules confidence = top-1 margin; held answers queued | Sibling (rules) / parent (remote); outcomes act, held, noop, invalid, fallback, discarded; rules confidence = 1 − p(noop) when an intervention is chosen, p(noop) when noop is chosen; held does nothing. | stale issue text |
| D-K4 | #1231 body cites the #1207 length gate (8 s) | The gate was removed from the code by PR #1252 (the change for issue #1234: LiteLLM gateway and legacy in-game LLM path removal); `remoteDecideAdmitted` is always true by design (`services/agent/decision/decide_hook.go` `remoteDecideAdmitted`, comment updated in PR #1257). | stale issue text |
| D-K5 | Agent README: `enabled=false` stops "before any spans are emitted" | A throttled `agent.disabled` span is emitted (`decision/decision.go`). | stale doc, **Resolved**: `services/agent/README.md` documents the throttled `agent.disabled` span ("Kill-switch trace"); the "before any spans" text is gone |
| D-K6 | Shipped defaults made remote Claude decisions time out: 2500 ms timeout vs 3.4 to 5.5 s measured (#1231) | `decision.claude.timeout_ms` default 5000 ms, `stage` budget variant, leaner output; warm ≈ 2.7 to 3.0 s. | **Resolved by PR #1257** |
| D-K7 | `services/agent/decide/backends/jev/jev.go:7` package comment says `fallback_reason=unavailable` | The value is `backend_unavailable` (`decision/decide_registry.go`). Doc-only fix inside `services/agent/decide/**`, not edited by the spec PRs. | stale comment, **Resolved by #1281** |

### 4.8 Flags (D-F)

| ID | Finding | Kind |
|---|---|---|
| D-F1 | `game_instructions` has no reader: the admin toggle has no effect. | likely bug (issue #1277) |
| D-F2 | feature-flags.md misses ~30 flags and says agent lifecycle flags need a restart (hot-reloaded since #927). | stale doc |
| D-F3 | `code_improvement.validation_games`, `fitness_improvement_threshold`, `revert_on_degradation` have no production reader (#964); the FR-067 validator is constructed but `Validate` is never called (PR #1378 open removes the unused chain, #1322). | gap |
| D-F4 | `policy.movement_variance_window` is read and logged but the rules engine uses a static 10 s (`decision/config.go:125`). | likely bug |
| D-F5 | Experiment fitness scoring uses static thresholds, not the live `fitness.*` flags (`experiment_loop.go` `recordFinalizedRealFitness`, `defaultGameFitness`: `decision.DefaultStaticConfig().FitnessThresholds`). | likely bug |
| D-F6 | `services/flagd/controller.ci-rust.json` is loaded by nothing. | dead file; Resolved by #1417 (deleted, #1416) |

### 4.9 Telemetry (D-T)

| ID | Doc claim | Code | Kind |
|---|---|---|---|
| D-T1 | Each service exposes `/metrics`; "Jaeger exporter" | OTLP push to the collector; no Python service serves `/metrics` (the controller-manager prometheus_client endpoint on 8000 was removed in #1447). | stale doc |
| D-T2 | `agent.signal_received` is a trace root; `agent.span_received` exists (agent-trace-viz) | Remote child of the coordinator's `game_cycle` (#1325, was `gameplay_phase` since #1195); `agent.span_received` retired (#1053). | stale doc |
| D-T3 | Alerts `GameCrashRate`, `ServiceUpdatePending` | Use `games_force_ended_total` and `joustmania_service_update_pending`, which no code defines: they can never fire (`services/prometheus/alerts.yml:90-101`). | bug (issue #1275); **resolved by removal** (#1425: Prometheus alerting dropped, FR-083 Rejected) |
| D-T4 | Span names `player_{serial}_lifecycle`, `ffa_game_loop`, `GrpcInstrumentor*` (DISTRIBUTED_TRACING, PER_PLAYER_TRACING) | `player_lifecycle` (renamed `player: {name}`), `team_lifecycle`, custom interceptors. | stale doc; **superseded**: both docs carry a "Superseded" banner pointing at telemetry.md |
| D-T5 | Metrics `game_loop_latency_ms`, `game_configured_update_frequency_hz`, `game_loop_iterations_total`, `config_changes_total` | Not defined; nearest are `game_loop_jitter_ms`, `game_current_update_frequency_hz`, `game_config_changes_total`. | stale doc; **resolved** (#1427 step 6: observability-quickstart.md uses the defined metrics) |
| D-T6 | Naming rules in `.claude/rules/development.md` | Violations: gauge `active_controllers_total`; `games_*`, `bluetooth_*`, `event_bus_*` lack a service prefix; `audio_sounds_played_total` / `audio_playback_errors_total` emitted by audio **and** game-coordinator with different labels; five histograms without `_seconds` / `_bytes`; span examples do not exist. | convention drift |
| D-T7 | `game.kind` has one vocabulary | Coordinator emits `primary` / `shadow`; agent spans emit normalised `real` / `shadow` under the same key (#1249). | documented dual vocabulary |
| D-T8 | Retro prompt content gated like decide content | `llm.retro.user` always carries the capped prompt; `decision.capture_content` gates only decide backends. | privacy gap |
| D-T9 | Agent logs correlate with traces | Agent slog lines carry no trace id; Grafana's Loki datasource has no trace link. | gap |
| D-T10 | telemetry.md §4.4, UC-030 step 9: `game_player_deaths_total` counts player deaths; the agent's death fallback reads it | Defined in `services/game_coordinator/metrics.py`, never incremented, so the agent fallback and UC-030 step 9 get no data. Audit A1. | bug; **resolved by #1376** (counted once per death as `{mode, game_kind}`; the agent no longer ingests it, deaths come from `game_player_elimination_order` and the `player_death` span event) |
| D-T11 | FR-083 (Rejected, #1425): `HighInputLatency` alert and the backend-comparison latency panels | `controller_input_lag_seconds` is defined in `services/controller_manager/metrics.py` but never observed, so the alert cannot fire and the panels are empty (third dead alert next to D-T3). Audit A2. | bug; **resolved by #1376** (observed per game-stream frame as sample age from the CM poll pick-up) |
| D-T12 | UC-035 / FR-083: dashboards show service errors; `LowGameLoopHz` signals a slow loop | Three `service-health-overview.json` panels filter on `grpc_code`, a label `grpc_client_request_duration_seconds` does not have, so every call counts as an error; `LowGameLoopHz` (< 55 Hz) fires permanently at the 15 / 30 Hz `game_loop.update_frequency_hz` variants (`services/prometheus/alerts.yml`). Audit A20. | bug (issue #1353); **resolved by #1425** (panels read `grpc_client_errors_total` grouped by its `code` label; `LowGameLoopHz` went with the removal of Prometheus alerting) |

### 4.10 Audio, platform, backends (D-AU, D-P, D-B)

| ID | Doc claim | Code | Kind |
|---|---|---|---|
| D-AU1 | pygame mixer, scipy resampling, WAV assets, CRITICAL for voice | miniaudio, soxr, 297 OGG files; only CRITICAL steals a channel and nothing sends CRITICAL; voices go at HIGH / priority 2. `music_player.py:8` claims "without pitch shifting" but soxr resampling changes pitch. | stale doc / comment |
| D-AU2 | `agent_intervention_cue` is an audio cue | Drives a magenta LED pulse only (`services/game_coordinator/interventions.py` `_emit_agent_cue`). | naming |
| D-AU3 | UC-031 A1 / FR-037: `play_audio` is hot-reloaded both ways | If the audio service started with `play_audio` off, `AudioManager(silent=True)` never opens the hardware and the flag handler only sets `audio_enabled`, so turning it on stays silent until a restart (`services/audio/servicer.py`). Audit A19. | bug; **resolved by #1387** (turning `play_audio` on opens the hardware at runtime; a failed open stays muted and retries) |
| D-P1 | Bluetooth: 7 per adapter (hardware guide) vs 5 recommended (25-controller doc) | Both are guidance; no code limit: the pairing daemon picks the adapter with the fewest devices known to BlueZ (`services/pairing_daemon/psmove_pairing/adapter_manager_modern.py` `select_least_loaded_adapter`). | doc inconsistency; **maintainer decision 2026-10-16 (audit A21)**: 7 is a recommended hardware limit, not enforced and not a bug; C-011 and UC-001 BR-001 say so. #1429: the "5" was Realtek-specific; both docs now give chipset-dependent figures (C-011) |
| D-P2 | "~3.2 GB memory budget for a 4 GB Pi" (`docker-compose.yml:31`) | Sum of limits ≈ 4.67 GB (Jaeger alone 1024 MiB). | stale comment / capacity risk |
| D-B1 | Compose header: controller backend is the `backend` flag | Per-controller routing is `bluetooth_backend` + rollout; `backend` selects which adapters load (startup). | stale comment |
| D-B2 | CANARY_RUNBOOK RustServiceAdapter stub (#612) | Fully implemented. | stale doc |
| D-B3 | `/canary/*` reachable from the host | Envoy routes `/chaos/` but not `/canary/` (`services/envoy/envoy.yaml:99-123`). | likely bug or stale doc |

### 4.11 Broken links found

| Source | Target | Problem |
|---|---|---|
| `docs/agent-act-runbook.md:122` | `#probe-mode-full-path-without-touching-the-game` | Heading slug has a double hyphen (em dash). |
| `docs/controller-feedback.md:246-247`, `docs/led-feedback.md:237,249` | `../planning/...` | `planning/` does not exist. |
| `docs/research/774-shadow-games.md:71` | `../../services/flagd/controller.ci.json` | File does not exist. |
| `services/controller_manager/MOCK_ENVIRONMENT.md:511-512` | `../game_coordinator/PER_PLAYER_TRACING.md`, `../../planning/...` | Wrong directory / missing. |
| `tests/integration/README.md:374` | `../../planning/PHASE_14_MOCK_HARDWARE.md` | Missing. |

## 5. Couldn't verify

- Runtime behaviour of the likely bugs above (D-A2, D-G3, D-G8, D-G10, D-M1) was established by reading code, not by running a game.
- End-to-end death-detection latency (NFR-004) has no measurement on record; only the game-side share is measured (`services/game_coordinator/tests/performance/test_game_loop_timing.py`, #1394). Flag propagation (NFR-050, which replaced NFR-014) is measured by `tests/integration/test_flag_propagation.py` (#1394).
- The ≈ 4.67 GB memory sum (D-P2) is the sum of compose limits, not measured usage.
