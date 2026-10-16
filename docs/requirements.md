# Requirements

> AIUP stage 2 of 5: [vision](vision.md) → **requirements** → [entity model](entity_model.md) →
> [use cases](use_cases.puml) → [technical specs](specs/). Index: [docs/README.md](README.md).
>
> State: `main` @ 7d25ade4 (2026-10-16, includes PRs #1256, #1257, #1259, #1261, #1265, #1266, #1282, #1284). **Status** follows the AIUP reference: `Open` (not built),
> `In Progress` (open PR), `Implemented` (on `main`), `Verified` (on `main` and covered by an automated test
> named in the use case or technical spec), `Deferred`, `Rejected`. Planned items carry their issue / PR number.
> Measured values cite their source; numbers taken from code cite the file. Some NFRs deliberately pin **code constants**
> (rates, defaults) so that changing one is a visible spec change; NFRs whose target is not agreed yet say
> "Target TBD (to measure)". The **Notes** column holds context; the Status column holds exactly one AIUP status.

## Roles

| Role | Who |
|---|---|
| Player | Holds a PS Move controller and plays. |
| Host | The game host: picks modes, calibrates, starts games from a controller (admin mode). |
| Operator | Runs and observes the stack (Pi or laptop), edits flags, rolls infrastructure. |
| Presenter | Runs the "Two Standards Walk Into an Agent" talk demo. |
| Agent | The Go service that observes via OTel and acts via flags (a system actor). |
| Maintainer | Changes the system; owns specs, reviews tier-3 proposals. |

## ID blocks

| Block | Area |
|---|---|
| FR-001 to FR-009 | Pairing and controllers |
| FR-010 to FR-019 | Lobby, menu and admin mode |
| FR-020 to FR-029 | Game modes and game lifecycle |
| FR-030 to FR-034 | Death detection and controller feedback |
| FR-035 to FR-039 | Audio |
| FR-040 to FR-043 | Agent: observing and governance |
| FR-044 to FR-054 | Agent: in-game decisions and interventions |
| FR-055 to FR-059 | Agent: retro, proposals, dossier |
| FR-060 to FR-069 | Agent: experiments, shadow games, promotion |
| FR-070 to FR-079 | Infrastructure rollout and rollback |
| FR-080 to FR-089 | Observability for the operator |
| FR-090 to FR-099 | Talk demo |
| NFR-001 to NFR-009 | Gameplay timing |
| NFR-010 to NFR-019 | Agent timing, budgets, degradation |
| NFR-020 to NFR-029 | Platform, capacity, telemetry pipeline |
| NFR-030 to NFR-039 | Maintainability |
| C-001 to C-009 | Northstar constraints |
| C-010 to C-019 | Platform constraints and product decisions |

## Functional Requirements

| ID | Title | User Story | Priority | Status | Notes |
|---|---|---|---|---|---|
| FR-001 | USB pairing | As a player, I want to plug my controller into the Pi via USB once so that it is paired to the Pi's Bluetooth adapter and trusted. | High | Implemented |  |
| FR-002 | Bluetooth connect | As a player, I want my paired controller to connect over Bluetooth when I press the PS button so that I can join without cables. | High | Implemented |  |
| FR-003 | Connection state on the LED | As a player, I want my controller's LED to show whether I am connected (dim) or ready (bright) so that I know my state without a screen. | High | Implemented |  |
| FR-004 | Controller names | As an operator, I want each controller to have a stable human-readable name that I can rename so that I can tell controllers apart in the dashboard. | Low | Implemented |  |
| FR-005 | Mock controllers | As an operator, I want to run the whole system with simulated controllers and drive them through an API so that I can develop, test and demo without hardware. | High | Verified |  |
| FR-006 | Selectable controller I/O backend | As an operator, I want to choose the controller I/O backend (python, rust, mock or combinations) by flag so that I can run on different hardware stacks. | Medium | Implemented |  |
| FR-007 | Ghost reconciliation | As a host, I want a force-started game to drop controllers that are no longer connected so that games do not wait for absent players. | Medium | Implemented |  |
| FR-008 | Battery display | As a host, I want to show every controller's battery level on its LED from admin mode so that I can swap low controllers before a game. | Medium | Implemented |  |
| FR-010 | Ready up | As a player, I want to press the trigger to mark myself ready and Move to un-ready so that the game starts only with willing players. | High | Implemented |  |
| FR-011 | Select game mode | As a host, I want to cycle through the playable game modes from a controller and have the selection announced and persisted so that the next game uses it. | High | Implemented | Lobby SELECT path fixed by PR #1282 (D-M1); before that only admin Cross and `ProcessInput` worked. |
| FR-012 | Auto start | As a player, I want the game to start automatically once at least two players are ready and every connected controller is ready so that nobody has to press start. | High | Implemented |  |
| FR-013 | Force start | As a host, I want to hold the trigger in admin mode to start immediately (with the ready players, or with all connected controllers when `force_all_start` is on) so that a game can start with stragglers. | Medium | Implemented |  |
| FR-014 | Instructions toggle | As a host, I want to switch spoken game instructions on or off so that experienced groups skip them. | Low | Open | The admin toggle writes `game_instructions`, but nothing reads it (D-F1, #1277). |
| FR-015 | Idle mode and sentinels | As a host, I want unattended connected controllers to switch their LEDs off after an idle timeout, with a few "sentinel" controllers breathing, so that batteries last and the room still shows the game is on. | Low | Implemented |  |
| FR-016 | Admin mode | As a host, I want to enter an admin mode by pressing the four face buttons together (exit with PS or after 60 s) so that settings are reachable only on purpose. | High | Implemented |  |
| FR-017 | Calibrate from the controller | As a host, I want to change sensitivity (5 levels), team count and force-all-start from admin mode with LED and voice feedback so that I can calibrate the game for the room. | High | Implemented |  |
| FR-018 | Persist settings as flags | As a host, I want admin changes written to the flag files so that they survive restarts and are visible to every service. | High | Verified |  |
| FR-019 | Agent kill switch on the controller | As a host, I want to step the agent's allowed interventions (none, ambient, standard, full) from admin mode so that I can stop the agent without a laptop. | High | Verified |  |
| FR-020 | Joust Free-for-All | As a player, I want to play an elimination game where the last player standing wins so that we have a simple default game. | High | Verified |  |
| FR-021 | Team Joust | As a player, I want to play in 2 to 6 colour teams (fixed or random formation) where the last team standing wins so that we can play as groups. | High | Verified |  |
| FR-022 | Hidden-role games | As a player, I want to play Traitor and Werewolf, where some players secretly play for the other side so that the game has bluffing. | Medium | Verified |  |
| FR-023 | Zombies | As a player, I want to play Zombies, where killed humans become zombies and humans win by surviving the timer so that we have an infection mode. | Medium | Verified |  |
| FR-024 | Swapper | As a player, I want to switch teams when I die until everyone is on one team so that the game keeps everyone playing. | Medium | Verified |  |
| FR-025 | 1v1 formats | As a player, I want Tournament (bracket) and Fight Club (winner stays) 1v1 formats with start-of-match invincibility so that we can run duels. | Medium | Verified |  |
| FR-026 | Non-Stop Joust | As a player, I want to respawn after dying and be scored by deaths so that nobody waits on the side. | Medium | Verified |  |
| FR-027 | Game start and end cues | As a player, I want a colour-and-beep countdown before a game and a rainbow plus voice for the winner so that start and end are unmistakable. | High | Implemented |  |
| FR-028 | Force end | As an operator, I want to end a running game (or a specific game by id) so that a stuck game never blocks the lobby. | High | Verified |  |
| FR-029 | Isolated shadow games | As the agent, I want to run headless shadow games with mock controllers alongside or between real games, each with its own game id and event stream, so that experiments never touch real players. | High | Verified |  |
| FR-030 | Movement-based elimination | As a player, I want to be eliminated when my controller's smoothed acceleration exceeds the death threshold for the current sensitivity so that the game rewards control. | High | Verified |  |
| FR-031 | Death warning | As a player, I want a white flash and rumble when I approach the threshold so that I can calm down before I die. | High | Implemented |  |
| FR-032 | Tempo-driven thresholds | As a player, I want the allowed movement to rise when the music speeds up and fall when it slows down so that the music sets the pace. | High | Implemented |  |
| FR-033 | Grace and invincibility | As a player, I want grace periods (respawn, conversion, match start, agent shield) during which I cannot die so that transitions are fair. | Medium | Implemented |  |
| FR-034 | Death feedback | As a player, I want a rumble, red hold and fade-out on my controller when I die so that I know I am out. | High | Implemented |  |
| FR-035 | Game music with tempo | As a player, I want looping game music that changes tempo between slow and fast windows so that tension rises and falls. | High | Implemented |  |
| FR-036 | Sound effects and voice | As a player, I want sound effects (beeps, explosions) and voice announcements (mode names, winners) mixed over the music so that events are audible. | High | Implemented |  |
| FR-037 | Audio preferences | As a host, I want to choose the announcer voice, mute audio and set volume levels by flag so that the game fits the venue. | Medium | Implemented |  |
| FR-038 | Visible agent intervention | As a player, I want a short magenta LED pulse when the agent applies a hard intervention so that agent influence is visible. | Low | Implemented |  |
| FR-040 | Observe through OTel | As the agent, I want to receive every trace and OTLP metric through the collector fan-out so that my view of the game is OpenTelemetry only. | High | Verified |  |
| FR-041 | Per-game context | As the agent, I want to build one context per game id, with the coordinator's `primary` normalised to `real` (`shadow`, empty and unknown kinds pass through unchanged and never count as real: fail closed), so that decisions for different games never mix. | High | Implemented |  |
| FR-042 | Existence kill switch | As an operator, I want the `enabled` flag to stop all in-game agent decisions within one decision cycle, and every post-game model call (retro, proposer) from the next game end, so that I can switch the agent off without a restart. | High | Verified | post-game half: PR #1284 (#1270). Scope: `enabled` does not govern the infrastructure rollout; expansion and rollback are gated by `rollout_enabled` / `remediation_allowed` (UC-045 A4, UC-050, UC-051). |
| FR-043 | Decisions as audit spans | As an operator, I want every agent evaluation, decision, block and action recorded as spans linked to the game trace so that the trace is the audit log. | High | Verified |  |
| FR-044 | Rules engine | As the agent, I want an in-process rules engine that ranks interventions by objective weights and fitness pressure so that I can always decide without a network. | High | Verified |  |
| FR-045 | Live backend selection | As an operator, I want to select the decision backend (rules, claude, openai, jev) with the `decision.backend` flag and have it apply on the next decision so that I can switch backends live (role split: C-018). | High | Verified |  |
| FR-046 | Decision contract | As the agent, I want every backend to answer the same typed questions and have answers validated strictly so that a backend can be swapped without changing the game. | High | Verified | Since PR #1265 the decide state carries per-player danger signals and Jev answers in two stages; since #1281 Jev's acting decision is gated by a noul (synthesised routing answer, confidence = p_yes) and its magnitude is the most likely level (see specs/decide-contract.md §3.1, §3.2). |
| FR-047 | Confidence routing | As an operator, I want remote answers below the act-confidence threshold of the backend that answered held instead of applied (Jev: `decision.jev.act_confidence_threshold`, default 0.6, `inherit` = use the global; claude/openai: `decision.act_confidence_threshold`, default 0.8) so that only confident decisions act and each backend's confidence scale gets its own bar. | High | Verified | Per-backend threshold since #1287. For Jev the routed confidence is the gate's calibrated p_yes (#1281: calm ~0.16, dragging ~0.35, collapsing ~0.68, near-threshold ~0.72), so its own default 0.6 acts on collapsing and near-threshold; the global `gate` variant (0.6) remains for claude/openai. |
| FR-048 | Rules fallback | As a player, I want the rules engine to decide whenever a remote backend is unavailable, slow, over budget, ineligible or returns an invalid answer so that the game never waits for a model. | High | Verified |  |
| FR-049 | Discard stale answers | As an operator, I want an in-flight remote answer discarded when the kill switch flips, the backend changes, the game ends or the target is gone so that late answers never act on a changed situation. | High | Verified |  |
| FR-050 | Allow-list | As an operator, I want an allow-list of intervention types (`interventions_allowed`) enforced by the agent and again by the game coordinator so that the agent can only do what I permit. | High | Verified |  |
| FR-051 | Actuation gate | As an operator, I want the `interventions_enabled` flag to gate every write of an intervention so that decisions can be observed without acting. | High | Implemented |  |
| FR-052 | Intervention rate limits | As an operator, I want a weighted per-game intervention budget in the agent and a global backstop in the coordinator so that the agent cannot spam a game. | High | Implemented |  |
| FR-053 | Apply interventions in the game | As the agent, I want the game coordinator to apply interventions written to `interventions.json` (tempo, sensitivity, difficulty, shields, eliminate, revive, end game, cues, volume) through the game's own code paths so that interventions behave like native game events. | High | Verified |  |
| FR-054 | Noul question kind | As the agent, I want a `noul` (probability-of-yes) question kind so that the Jev backend can answer its native primitive. | Medium | Verified | PR #1261; used internally by the Jev gate since #1281 (with `true` / `false` criteria). |
| FR-055 | Tier-3 proposals | As a maintainer, I want a promoted experiment to be proposed as a GitHub issue or PR (or a local commit) for human review so that changes needing review never apply silently. | Medium | Implemented | inert in the stock stack: env gates not passed through, see D-A6 |
| FR-056 | Post-game retro | As the agent, I want a retrospective after each eligible game (session assessment and suggestions) recorded on a span, calling the model only while `enabled` is on, so that I learn from real games and stop paid calls when switched off. | Medium | Verified | kill-switch gate PR #1284 (#1270); tests `services/agent/decision/retro_capture_test.go` |
| FR-057 | Experiment proposer | As the agent, I want to propose a shadow experiment (flag, value, rationale) after games, at most every `code_improvement.min_interval_seconds`, only while `enabled` and `experiments_enabled` are on, and (for a model call) only for an eligible game kind and within the shared LLM budget (NFR-011), so that experiments come from observed play and stop when the agent is switched off. | Medium | Verified | gates added by PR #1284 (#1270, D-A5); tests `services/agent/experiment/proposer_gate_test.go`, `services/agent/proposer_gate_main_test.go` |
| FR-058 | Retro-seeded theses | As the agent, I want retro suggestions to seed experiment theses so that the loop closes from observation to experiment. | Low | Implemented | off by default, `AGENT_EXPERIMENT_PROPOSER_ENABLED` |
| FR-059 | Experiment dossier | As an operator, I want a read-only HTTP view of every experiment's journal at `/experiments` so that I can follow an experiment without reading traces. | Medium | Implemented |  |
| FR-060 | Declare experiments | As the agent, I want to declare an experiment on a `game.json` flag (seeded by env, proposed, or chosen by the dynamic declarer) with an objective and target sample size so that tuning is hypothesis-driven. | High | Verified |  |
| FR-061 | Paired shadow games | As the agent, I want to spawn experimental and control shadow games in pairs that share a random seed so that only the flag under test differs between arms. | High | Verified |  |
| FR-062 | Shadow-scoped targeting | As an operator, I want experiment variants written only as targeting that matches non-real games, and rejected if they would change what a real game resolves, so that real players never see an untested variant. | High | Verified |  |
| FR-063 | Cohort verdict | As the agent, I want a verdict per experiment from per-arm samples (minimum N, minimum pairs, Cohen's d, raw-effect floor, recent-real baseline anchor) so that only real effects are promoted. | High | Implemented |  |
| FR-064 | Route promotion | As the agent, I want a promote verdict routed to the promoter in the mode set by `code_improvement.mode` (issue, pr, autonomous) so that the authority tier is a flag. | High | Implemented |  |
| FR-065 | Autonomous promotion with watch and revert | As an operator, I want autonomous promotion to apply the real default, watch real games and revert on degradation so that tier-2 changes are reversible. | Medium | Implemented | gated off in the stock stack |
| FR-066 | Abort on kill switch | As an operator, I want all running experiments aborted, and no new experiment proposed, when `enabled` or `experiments_enabled` is off so that the experiment loop and the post-game proposer obey the kill switch. | High | Implemented | proposer half: PR #1284 (#1270, D-A5) |
| FR-067 | Live validation stage | As the agent, I want a promoted candidate re-validated in fresh shadow games (`code_improvement.validation_games`) before it reaches real players so that verdicts are confirmed. | Medium | Open | #964 |
| FR-068 | Candidate game code | As the agent, I want to test a candidate game-logic module in shadow games and promote it as a PR so that tier-3 code changes are evidence-backed. | Low | Open | #1011 |
| FR-069 | Per-game intervention targeting | As an operator, I want each intervention scoped to the game whose loop decided it so that a shadow-game decision can never affect the real game. | High | Open | Canonical statement of the gap: [specs-inventory D-A2](specs-inventory.md#46-agent-d-a) (#1269, design #920). |
| FR-070 | Manual backend canary | As an operator, I want to move individual controllers (or a fraction) to another HID backend over HTTP (`/canary/*`) and roll back so that I can canary a backend by hand. | Medium | Implemented | not routed by envoy, see D-B3 |
| FR-071 | Progressive rollout | As an operator, I want the agent to expand a backend rollout controller by controller while infrastructure fitness passes so that a new backend reaches players gradually. | High | Verified |  |
| FR-072 | Automatic rollback | As an operator, I want the agent to roll the rollout back to zero controllers when infrastructure fitness fails and remediation is allowed so that a bad backend is removed without me. | High | Verified |  |
| FR-073 | Chaos injection | As an operator, I want to inject controller faults (poll drop, accel spike, LED failure, disconnect) over HTTP (`/chaos/*`) so that I can demonstrate degradation. | Medium | Implemented |  |
| FR-074 | Rollout state in telemetry | As an operator, I want the rollout target, strategy and count on the ~1 Hz `controller.bluetooth_health` span so that rollout decisions are explainable from traces. | Medium | Implemented |  |
| FR-080 | Traces | As an operator, I want every service's traces in Jaeger at `/jaeger/` with game traces rooted per game so that I can follow a game end to end. | High | Implemented |  |
| FR-081 | Metrics and dashboards | As an operator, I want service, game, controller and agent metrics in VictoriaMetrics with Grafana dashboards at `/grafana/` so that I can watch health and gameplay. | High | Implemented |  |
| FR-082 | Logs | As an operator, I want all container logs in Loki so that I can search logs next to traces. | Medium | Implemented |  |
| FR-083 | Alerts | As an operator, I want Prometheus alerts for controller, latency, resource and agent-write problems so that I notice failures. | Medium | Implemented | 2 alerts reference undefined metrics, see D-T3 |
| FR-084 | Dashboard SPA | As an operator, I want a browser dashboard at `/` showing controllers, the agent's decision feed, experiments, Grafana and Jaeger so that one URL shows the system. | High | Implemented |  |
| FR-085 | Single entry point | As an operator, I want all UIs behind one reverse proxy on the host's port 80 so that only one port needs to be reachable. | High | Implemented |  |
| FR-090 | Demo stack | As a presenter, I want one command (`make dry-run`) to start the latest images with mock controllers, the agent and a seeded experiment so that the demo starts from a known state. | High | Implemented |  |
| FR-091 | Stage flag flips | As a presenter, I want scripts that flip the decision backend, model, threshold, content capture and the kill switch so that each talk beat is one command. | High | Implemented |  |
| FR-092 | Scripted game scenarios | As a presenter, I want a demo driver that plays scripted scenarios (skill gap, statue, death cascade, ...) with mock controllers so that agent behaviour is reproducible on stage. | Medium | Implemented |  |
| FR-093 | Pre-flight check | As a presenter, I want a read-only pre-flight check of containers, flag baseline, keys, Jaeger and clock skew so that I find problems before going on stage. | Medium | Verified | `scripts/talk-preflight.sh` (PR #1256), stub-tested by `scripts/tests/test-talk-preflight.sh`. |
| FR-094 | Talk runbook | As a presenter, I want a runbook with the beat table, timings, fallbacks and reset so that the talk is rehearsable. | Medium | Implemented | [docs/talk/two-standards-runbook.md](talk/two-standards-runbook.md) (PR #1256); rehearsal on the demo hardware still open in #743. |

## Non-Functional Requirements

| ID | Title | Requirement | Category | Priority | Status | Notes |
|---|---|---|---|---|---|---|
| NFR-001 | HID sampling | Each HID backend polls every open controller at least every 1 ms plus read time (≈1 kHz) (`services/rust-hid/src/device_manager.rs:114-121`; `services/python_hid/device_manager.py:42-43`). | Performance | High | Implemented |  |
| NFR-002 | Controller-manager loop | The controller-manager state loop runs at 100 Hz, discovery checks at 1 Hz and LED batches at 20 Hz (`services/controller_manager/discovery_loop.py:139-147,309-315`). | Performance | High | Implemented |  |
| NFR-003 | Gameplay stream and game loop rate | Gameplay data is streamed and the game loop runs at `game_loop.update_frequency_hz`, default 60 Hz (variants 15/30/60); the controller manager clamps its flag override to 1 to 100 Hz and falls back to 30 Hz when no rate is requested (`services/flagd/system.json`; `services/controller_manager/servicer.py:319,459,634-676`). | Performance | High | Implemented |  |
| NFR-004 | Death detection latency | Target TBD (to measure). From a movement sample crossing the death threshold to the kill, the game adds at most one game-loop frame (16.7 ms at 60 Hz) plus EMA smoothing lag; the EMA weight is the flag `perception.ema_weight` (responsive 2 / default 4 / smooth 6). Example: at weight 4 a constant 3 g step at medium sensitivity kills on the 3rd frame (≈ 50 ms) against the slow table's 1.8 g; against the fast table's 2.8 g it takes more frames (derived from `services/game_coordinator/games/base.py:1516-1613`). The older target "< 10 ms p95" (`services/game_coordinator/games/README.md:245`) is unverified. | Performance | High | Open | Not measured end to end. |
| NFR-005 | Countdown length | The pre-game countdown lasts 3 phases × `countdown_phase_duration_ms` (default 750 ms, 2.25 s total; 0 skips) (`services/game_coordinator/games/base.py:1081-1151`). | Usability | Medium | Implemented |  |
| NFR-006 | Admin timeout | Admin mode exits 60 s after entry (`services/menu/handlers/admin.py:152-154`). | Usability | Medium | Implemented |  |
| NFR-007 | Pairing cadence | The pairing daemon polls USB every `pairing.poll_interval` (default 10 s) and Bluetooth every `pairing.bt_monitor_interval` (default 5 s) (`services/flagd/system.json`; `services/pairing_daemon/psmove_pairing/config.py:14-15`). | Performance | Low | Implemented |  |
| NFR-010 | Remote decision timeout | A remote decision call is abandoned after `decision.<backend>.timeout_ms` and rules decide instead: Claude 5000 ms by default (variants 1500 / 2500 / 5000 / 8000, PR #1257), OpenAI-compatible and Jev 2500 ms. Measured warm latency fits: Claude Haiku ≈ 2.7 to 3.0 s and Sonnet ≈ 2.85 s after PR #1257 (before it, Haiku 5.55 s cold / 3.43 s warm, #1231 comment); Jev 240 to 315 ms per call (PR #1261); a Jev act round makes 2 calls, 469 to 525 ms (PR #1265), 473 to 567 ms with the gate (#1281). | Performance | High | Implemented |  |
| NFR-011 | Global remote-call budget | At most `llm.max_requests_per_minute` (default 6; variants 3 / 6 / 12 and `stage` = 30 for the talk, PR #1257) remote model calls per minute across all games, the retro and the experiment proposer; `AGENT_LLM_MAX_REQUESTS_PER_MINUTE`, read once at startup, **replaces** the flag value while set, so `stage` has no effect then (`services/agent/main.go:336-350`, `services/agent/decision/decision.go:318-341`, `services/agent/decision/llmbudget.go:22-67`). The proposer draws one slot per real (non-stub) model call, after its cadence floor; when the budget is full it skips the call and records `agent_llm_gated_total{reason=llm_budget_exhausted}` (PR #1284, `services/agent/decision/llm_admission.go`). The budget is a sliding 60 s window: with a remote `decision.backend` at `decision.min_interval_ms` 3000 one game wants about 20 calls a minute against the default 6, so the retro and the proposer are usually gated at game end; use `stage` (30) or `decision.backend=rules` when post-game model output is needed. | Performance | High | Implemented |  |
| NFR-012 | Per-game decision cadence | At most one remote decision per game every `decision.min_interval_ms` (default 3000 ms) and never two in flight for one game (`services/agent/decision/decide_hook.go:237-241,292-303`). | Performance | High | Verified |  |
| NFR-013 | Never stall the game | The game coordinator makes 0 calls to the agent or to a model (it has no agent client; it only reacts to flag changes), so a remote decision adds 0 ms to the game loop. The agent never blocks a decision cycle on a model call: the call is asynchronous with at most one in flight per game, bounded by `decision.<backend>.timeout_ms`. A remote failure yields a rules decision for the current context. | Availability | High | Verified |  |
| NFR-014 | Flag propagation | Target TBD (to measure). A flag file edit is expected to reach all live readers within ~1 s (documented claims: "~1 s" in `scripts/agent-backend.sh`, "< 100 ms" in `lib/flag_config_writer.py:24`). | Performance | High | Open | Not measured. |
| NFR-015 | Intervention budget | The agent admits at most `policy.max_interventions_per_minute` (default 2) weighted interventions per game per minute (soft 0.5, medium 1, hard 2) and the coordinator at most `policy.coordinator_backstop_per_minute` (default 60) globally. | Security | High | Implemented |  |
| NFR-016 | Rollout pacing | Rollout stages advance no faster than one per `AGENT_ROLLOUT_DWELL_SECONDS` (default 15 s); after a rollback, re-expansion waits `AGENT_ROLLOUT_COOLDOWN_SECONDS` (default 30 s, in memory only) (`services/agent/decision/infra_loop.go:89,131`). | Availability | Medium | Implemented |  |
| NFR-017 | Infrastructure fitness | A rollout stage passes when event gap ≤ 50 ms, dropped events ≤ 2 % and movement update rate ≥ 10 Hz (flags `fitness.bluetooth.*`, re-read every infra cycle). | Availability | High | Implemented |  |
| NFR-018 | Real/shadow capacity | At most `max_concurrent_games` (default 8) live games, of which at most `max_shadow_games` (default 4); with `shadow_policy=block` no shadow game starts while a real game runs, and a real start preempts live shadows (`services/game_coordinator/servicer.py:66-136,372-400`). | Scalability | High | Verified |  |
| NFR-019 | Decision freshness | Folded into FR-049 (not measurable on its own). | Security | High | Rejected | See FR-049. |
| NFR-020 | Pi memory budget | The full stack fits the memory of the target Raspberry Pi. The compose header states "~3.2 GB for a 4 GB Pi" (`docker-compose.yml:27`), but the sum of container limits is ≈ 4.67 GB; the lite stack (`docker-compose.lite.yml`) drops the observability services. Target TBD (to measure): the real budget is for the maintainer to confirm. | Portability | High | Open | Budget mismatch, D-P2. |
| NFR-021 | Telemetry pipeline memory | The collector backpressures at 600 MiB (spike 150 MiB) under a 768 MiB container limit (`services/otel-collector/config.yaml:221-224`; `docker-compose.yml:217`). | Availability | Medium | Implemented |  |
| NFR-022 | Metric export interval | Python services export metrics every `metrics_export_interval_ms` (default 1000 ms; 100 ms for controller-manager and game-coordinator by targeting); Go and Rust services every 1 s. | Performance | Medium | Implemented |  |
| NFR-023 | Retention | VictoriaMetrics keeps 7 days, Prometheus 30 days (`docker-compose.yml:507,539`); Jaeger span TTL is bounded in `services/jaeger/config.yaml`. | Maintainability | Low | Implemented |  |
| NFR-024 | Agent fan-out is best effort | The collector delivers to the agent with a 2 s timeout, no retries and a 1000-item queue, so an agent outage never stalls Jaeger or VictoriaMetrics (`services/otel-collector/config.yaml:342-351`). | Availability | High | Implemented |  |
| NFR-025 | Concurrent controllers | The system supports up to 25 controllers on one Pi with 5 Bluetooth adapters (`docs/25-controller-setup.md`); per-adapter limit 7 (Bluetooth piconet), recommended 5. Not verified by an automated test. | Scalability | Medium | Implemented | unverified |
| NFR-030 | Spec first | Every PR that changes observable behaviour changes the spec (requirements, use case or technical spec) in the same or a preceding PR, or states `Spec changes: none: <reason>`. | Maintainability | High | Implemented | process; CI enforcement in progress (#1263) |
| NFR-031 | Spec accuracy | Every factual statement in `docs/specs/*.md` cites a file (and line where practical) on `main`; discrepancies are logged in `docs/specs-inventory.md`. | Maintainability | High | Implemented |  |
| NFR-032 | Lint clean | `make lint` passes on every PR (ruff, shellcheck-based script tests). | Maintainability | High | Implemented |  |

## Constraints

| ID | Title | Constraint | Category | Priority | Status | Notes |
|---|---|---|---|---|---|---|
| C-001 | OTel only | The agent observes the game only through OpenTelemetry spans and metrics; it never reads game code or game state through other channels ([NORTHSTAR.md](NORTHSTAR.md)), except the shadow-game driver's own `StreamGameEvents` stream and `ListGames`, used for controller hygiene (C-005; `services/agent/gamerunner/operations.go`, `services/agent/gamerunner/sweep.go`). | Technical | High | Implemented |  |
| C-002 | Flags only | Every change the agent makes to the game, and every human setting, is an OpenFeature flag change (flagd JSON files). | Technical | High | Implemented |  |
| C-003 | Shadow gate | Persistent (tier 2) changes reach real players only after shadow validation; tier 1 interventions apply directly because they are scoped, observable and reversible. | Technical | High | Implemented | FR-062 enforces variant isolation; FR-069 per-game scoping open |
| C-004 | Degrade, never stall | Hosted inference may be used; when it is slow or unreachable the rules engine decides and the game keeps playing. | Technical | High | Implemented |  |
| C-005 | No control plane beyond OTel and OpenFeature | The only other outbound paths are: driving shadow games through the game-coordinator and mock-controller gRPC APIs, and tier-3 proposals as issues or PRs. Inference backends are dependencies behind the decision contract, selected by a flag. | Technical | High | Implemented |  |
| C-006 | No local inference | A local or offline model runtime is not a goal; the rules engine is the offline floor. | Business | Medium | Implemented |  |
| C-007 | Tier 3 human review | Changes needing a compile step are proposals only (issue or PR) reviewed by a human. | Business | High | Implemented |  |
| C-008 | Human owns pre-game intent | The human admin owns game mode, baseline settings and start; the agent owns bounded in-game deltas in `interventions.json`. The agent's only other writes are shadow-scoped experiment targeting in `game.json` (never changes a real resolution, FR-062), the gated autonomous promotion of a shadow-validated default into `game.json` (FR-065, tier 2) and `current_controller_count` in `rollout.json` (FR-071); it never writes `user.json` or `agent.json` ([OWNERSHIP_MODEL.md](OWNERSHIP_MODEL.md) §1). | Business | High | Implemented | Menu and agent share `game.json` without a lock (known risk, OWNERSHIP_MODEL §1); D-A3 resolved by #1291. |
| C-009 | No player identity | A controller is not a person; no cross-session player profiles or rewards (issue #23, closed). | Business | Medium | Implemented |  |
| C-010 | Raspberry Pi 5 | The production target is a Raspberry Pi 5 (4 to 8 GB RAM) running Docker Compose; USB audio adapter required (no 3.5 mm jack). | Technical | High | Implemented |  |
| C-011 | Bluetooth | Controllers connect over Bluetooth Classic through BlueZ; the pairing daemon needs the host network namespace and privileged access; max 7 controllers per adapter. | Technical | High | Implemented |  |
| C-012 | PS Move controllers | Supported controllers are Sony PS Move ZCM1, ZCM2 and ZCM2E (USB VID 0x054C, PIDs 0x03D5, 0x042F, 0x0C5E) (`lib/psmove_hid.py:22-26`). | Technical | High | Implemented |  |
| C-013 | Runtimes | Python services require Python ≥ 3.14, < 3.15 (`pyproject.toml:7`); the agent and connect-proxy are Go (go 1.25); rust-hid is Rust 2024 edition; the dashboard is a TypeScript SPA. | Technical | High | Implemented |  |
| C-014 | gRPC between services | Services talk gRPC with protobuf definitions in `proto/`; browsers reach them through the Connect proxy. | Technical | High | Implemented |  |
| C-015 | Single host port | Only envoy on host port 80 is published in the base stack (plus flagd 8015, collector 4317/8889 and rust-hid 50058 for the host-networked pairing daemon). | Operational | Medium | Implemented |  |
| C-016 | Secrets out of flags | API keys come from the environment (`.env.local` or the shell), are read once at agent startup and never appear in flag files or spans. | Regulatory | High | Implemented |  |
| C-017 | Licences | Code is MIT, audio assets CC BY-SA 4.0 (`LICENSE`). | Regulatory | Medium | Implemented |  |
| C-018 | Decision backend roles | Maintainer decision (epic #1225): **Jev** is the main in-game backend; **Claude** serves the post-game retro / summary and the experiment proposer, and is the in-game **backup** selected by a manual `decision.backend` flip; the **rules** engine is the automatic floor whenever the selected backend cannot answer. The shipped default of `decision.backend` is still `rules`, so Jev is selected per deployment (e.g. `scripts/agent-backend.sh jev`). | Business | High | Implemented |  |
