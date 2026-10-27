# Stage test checklist (stage Pi on the real power bank)

The checklist the maintainer works through on the stage Pi before the talk, powered by the **real power bank**
(not the bench supply). It turns the hardware checks of [#1465](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/1465)
and the findings of the live verification log [#2088](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2088)
into steps with an expected result. It complements the [runbook](two-standards-runbook.md) (the script of the
talk) and its pre-flight list: the runbook says what to do on stage, this page proves that the hardware does it.

How to use it: work the blocks in order (block 8 runs in the background from block 5 on), write
`pass`, `fail` or a note in the last column, and report as described in [How to report](#how-to-report).
Every step names the use case (UC) or business rule (BR) it verifies. A step marked **(known)** has an open issue:
note whether the behaviour matches the issue, and do not count it as a new finding.

Conventions:

- `PI` is the Pi's address, `$` commands run on the Pi over ssh from `~/JoustMania`.
- "LED" is the primary feedback (venue audio is unintelligible): a step that expects a voice line also expects an LED.
- Record the version under test first: `./scripts/setup/joustmania-upgrade.sh --status` and the Pi's `git rev-parse --short HEAD`.

| Field | Value |
|---|---|
| Date / tester | |
| Version (`--status` current) | |
| Power source | power bank, model / capacity: |
| Controllers (count, ZCM1 / ZCM2) | |
| Operator pad attached (`operator-pad` profile on: 18 containers, off: 17) | yes / no |

## 1. Boot

| Id | Action | Expected result | Verifies | Result |
|---|---|---|---|---|
| ST-1.1 | Cold boot with **no network** (no WiFi, no hotspot, no cable): power on and start a stopwatch | The ACT LED goes solid on; `Finished joustmania.service` appears about 30 s after boot in `journalctl -b -u joustmania`; no pull error stops the start (a failed `git pull` / `compose pull` is logged and skipped) | UC-060 main 1-5, BR-001, BR-002 | |
| ST-1.2 | Same boot: read the game state | Dashboard (`http://PI/`) opens from the presenter laptop once on the Pi's network; `docker ps` lists the stack with every healthcheck `healthy` (`docker ps --filter health=unhealthy` is empty) | UC-060 main 5, BR-005 | |
| ST-1.3 | Power swap: pull the power, switch to the power bank, boot again | Same result as ST-1.1. The clock is stale at boot (the RTC battery is dead, [#2089](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2089)): the stack starts anyway, `docker ps` may show "Up 2 hours" after the NTP jump (cosmetic), and traces from the first minutes carry the stale time. Stale clock is **accepted**; a registry TLS error must not stop the start | UC-060 BR-001, BR-002 | |
| ST-1.4 | **Time to playable**: stopwatch from power on to the first controller in the lobby (press PS on a known controller) and to a started FFA | Write both times down. Target (proposed limit): lobby within about 90 s of power on (the start script ends at about 30 s; add controller connect time). Anything over 2 min is a finding | UC-002, UC-060 | |
| ST-1.5 | `./scripts/setup/joustmania-upgrade.sh --status` | Prints `current:` and `previous:` with image tags, no `WARN` (no cut-off rollback, HEAD equals a recorded version) and no `start-limit-hit`; `joustmania-status` lists no service that is down | UC-060 BR-005, A2, A3 | |
| ST-1.6 | Agent defaults after the reboot: dashboard Agent tab, or `./scripts/flags.sh` read of `enabled` and `interventions_enabled` | Back at the fail-closed defaults (`enabled=off`, `interventions_enabled=off`); the persisted level and backend stay. Run `./scripts/talk-stage.sh reset` and then `./scripts/talk-preflight.sh` before block 6 | UC-045, UC-070 | |

## 2. Controllers

| Id | Action | Expected result | Verifies | Result |
|---|---|---|---|---|
| ST-2.1 | USB-pair one new or reset controller: plug it in, wait, unplug, press PS | Log `Controller paired successfully`; after the PS press it connects and **stays lit**, and the controller manager tracks it. **(known, [#2091](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2091))**: no LED cue while pairing, and each new pairing restarts `bluetooth.service` | UC-001 main, BR-002, BR-005 | |
| ST-2.2 | USB-pair **three controllers in a row** (plug the next one as soon as the last is unplugged) | All three pair and connect; note how many Bluetooth restarts happen and which already-connected controllers drop each time (#2091: lobby controllers drop on every restart and must reconnect with PS). Pair them **before** the lobby is full, not in front of the audience | UC-001 BR-002, BR-005, UC-002 | |
| ST-2.3 | Reconnect after a Bluetooth restart: with 3 or more controllers in the lobby run `sudo systemctl restart bluetooth` | Controllers drop (LED off), then each reconnects after a PS press and returns to the lobby (CONNECTED, dim LED) without a stack restart; nobody is stuck as a ghost on the dashboard | UC-002 A-flows, UC-001 BR-005 | |
| ST-2.4 | Identify: dashboard, controller card, **Identify** | The right controller flashes white at 2 Hz with a short rumble, then restores its lobby colour. Starting a game during the flash shows a clean countdown | UC-004 main, BR-001 | |
| ST-2.5 | Controller card battery (dashboard, [#2111](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2111)) | One card per connected controller with its battery percentage that falls in the same colour band as the Triangle display (admin mode, Triangle: [UC-012](../use_cases/UC-012.md) A2); compare the cards to find the lowest. Record the lowest percentage. Swap or charge any controller under 40 % before the talk (the #2088 data had 40 % and 60 %) | UC-035 BR-007, UC-012 A2 | |
| ST-2.6 | Unpair one controller from the dashboard | It leaves the roster and is removed on every adapter; it pairs again over USB (ST-2.1). In a running game the player would be eliminated (do not test in front of players) | UC-005 | |
| ST-2.7 | Connection quality: look at the quality badge on each controller near and at the far end of the stage | `good` near the Pi; note any `poor` at the stage distance, and the adapter in use per controller | UC-035 BR-005 | |

## 3. Lobby and admin mode

| Id | Action | Expected result | Verifies | Result |
|---|---|---|---|---|
| ST-3.1 | Ready up all but one of the connected controllers (3 or more connected): press Trigger | Each goes bright (CONNECTED dim to READY bright) and the dashboard shows the same; the one controller left alone stays dim | UC-010 main, UC-002 | |
| ST-3.2 | Mode select with a controller (admin mode, Cross) | Mode colour pulse and the mode's voice line; the dashboard shows the new mode. Cycle through all modes once | UC-011, UC-012 BR-005 | |
| ST-3.3 | Sensitivity: admin mode, Circle (cycle) or Move to the sensitivity option then Select / Start | Colour per level (blue, turquoise, green, orange, red) and one voice line. **(known, [#2092](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2092))**: the level is announced **twice**; note whether it still is | UC-012 main, A1, BR-005, BR-006 | |
| ST-3.4 | Teams: admin mode, Move to `num_teams`, Select / Start | Gradient colour from green (2) to red (6) and the team count by voice; the value stays within 2 to 6 | UC-012 BR-005, UC-021 | |
| ST-3.5 | Instructions toggle (Square) | Green or red pulse and voice; the next game plays or skips the instructions | UC-012 A3 | |
| ST-3.6 | **Admin kill switch**: admin mode, Move to `agent_control`, Start (restrict) down the ladder to the stop, then Select (escalate) back up | Own colours and numbered voice per step; at the bottom the agent is stopped, then restores its saved level. **(known, [#2095](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2095))**: a flag-service write that times out client-side but applies server-side makes the next step read stale; note any step that jumps or repeats. Confirm in the dashboard and with `./scripts/flags.sh` that `interventions_allowed` matches what the controller announced | UC-013, UC-012 A6, UC-045 | |
| ST-3.7 | Leave admin mode (PS); open a second admin attempt with another controller while one is open | The controller returns to CONNECTED; the second controller is not given the admin session | UC-012 BR-004 | |

## 4. Operator pad

Skip this block when no pad is attached. Table of keys: [UC-015 BR-016](../use_cases/UC-015.md#br-016-keys-by-f-key)
(also in [DEVELOPMENT.md](../DEVELOPMENT.md)). "Knob" means: hold the key, turn the knob, release to commit. The pad
is the Vial keyboard `cb00:1337`; check that `operator_pad.device` matches. The Pi must run a version with the
knob-step fix ([#2109](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2109), main from f66cc0db on).

| Id | Action | Expected result | Verifies | Result |
|---|---|---|---|---|
| ST-4.1 | Pad attach: start the stack with the profile on (`COMPOSE_PROFILES` in `.env.local` has `operator-pad`) and check `docker ps` and the pad container log | Container up, the pad node grabbed (no stray key reaches the console); an attach that fails retries on the next rescan | UC-015 Pre-1, Pre-2, BR-014 | |
| ST-4.2 | **F13 K1 GAME**: tap in the lobby with enough ready players | A game starts (the same as UC-010 start); tapped while a game runs it is refused with the refusal cue | UC-015 BR-016, A1 | |
| ST-4.3 | **F13 + knob**: hold K1, turn the knob one step at a time, release | One mode step per notch (not many: #2109); on release the mode's voice line and a 600 ms mode-colour pulse on **every lobby controller** (not the admin controller); an end-of-range step gives no voice and no pulse | UC-015 BR-005, BR-015, A7 | |
| ST-4.4 | **F14 K2 SENS**: tap, then hold K2 and turn | Tap announces the sensitivity; with the knob it steps 0 to 4 (lobby only) | UC-015 BR-016, A7 | |
| ST-4.5 | **F18 K3 AGENT**: tap; hold 1 s; hold K3 and turn | Tap announces the agent status; the 1 s hold stops the agent, the next 1 s hold resumes it at the saved level; the knob sets the intervention level | UC-015 BR-002, BR-003, BR-004, UC-013 | |
| ST-4.6 | **F19 K4 END**: hold 1.5 s in the lobby, then during a game | Lobby: force-start with the connected players; running: force-end. A tap does nothing | UC-015 BR-016, BR-008, UC-014, UC-027 | |
| ST-4.7 | **F20 K5 TEAMS**: tap; hold and turn | Tap announces the number of teams; the knob sets 2 to 6 (lobby only) | UC-015 BR-016 | |
| ST-4.8 | **F21 K6 INSTR**: tap in the lobby | Spoken instructions toggle (voice confirms) | UC-015 BR-016 | |
| ST-4.9 | **F22 K7 BACKEND**: tap; hold and turn | Tap announces the decision backend; the knob steps the backend (rules, jev, claude): end the test on `jev` or the backend the talk uses | UC-015 BR-004, BR-016 | |
| ST-4.10 | **F23 K8 PAGE**: tap (reserved), then lock: hold 3 s | Tap does nothing; the hold locks the pad (cue); a locked pad ignores every key except the K3 stop hold; a second 3 s hold unlocks | UC-015 BR-016, A6 | |
| ST-4.11 | **Knob press F15** and **knob F16 / F17** with no layer key | Press mutes / unmutes (fires at the press); left and right change the master volume, committed after a 250 ms pause; the volume audibly follows | UC-015 BR-006, BR-005 | |
| ST-4.12 | **Layers**: hold each layer key (K1, K2, K3, K5, K7) and press the knob | The knob press belongs to the layer: **no** mute; releasing the layer key commits its setting once | UC-015 BR-007, BR-005 | |
| ST-4.13 | **Unplug and replug** the pad (also unplug while a layer key is held) | The pad container detaches without an error and re-attaches on the next rescan; keys work again without a restart; a half-held gesture is dropped | UC-015 A4, BR-014 | |
| ST-4.14 | **K4 stale**: open the dashboard on a second tab, force-start from the dashboard, then within a second hold K4 on the pad (or hold K4 over the moment the state changes) | The pad refuses a command whose state changed meanwhile (`stale`) with the refusal cue; nothing is ended or started by mistake | UC-015 A5, BR-012 | |
| ST-4.15 | **K3 with an unknown agent state** ([#2101](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2101)): hold K3 right after boot or while the flag service is restarting (`docker restart flag-service`) | An unknown status takes the fail-closed direction: the hold **stops** the agent (idempotent), it never resumes it. **(known, #2101)**: the menu may report an agent flag it cannot resolve as an empty variant; note what K3 did | UC-015 A10, BR-013, BR-010 | |
| ST-4.16 | Flag service down: stop the flag service, then hold K2 / K5 / K7 and turn the knob, and hold K3 for 1 s | The flag-writing gestures are refused with the failure cue (K1 tap and volume still work), never queued; after the flag service is back, nothing replays | UC-015 A3, BR-010 | |
| ST-4.17 | Cancelled gestures: do ten taps and ten holds at your normal speed | Few `cancelled` gestures; the #2088 data had 26 cancelled: note how many of the 20 cancel, and which key (tap / hold bands too tight?) | UC-015 BR-007 | |

## 5. Game

| Id | Action | Expected result | Verifies | Result |
|---|---|---|---|---|
| ST-5.1 | **FFA** with 4 or more controllers: countdown, play, eliminate players by shaking | LED and voice countdown; warning then death rumble (1 s); music tempo changes; last player wins and the winner LED shows; the dashboard ends the game and the lobby returns (controllers back to CONNECTED). Run it at least twice | UC-020, UC-030 | |
| ST-5.2 | One **team mode**: Team Joust | Teams coloured in the lobby, team win condition, game ends when one team remains | UC-021 | |
| ST-5.3 | One **other mode** (pick one: Zombies, Swapper, a 1v1 format or Non-Stop) | Mode voice line and rules as in its use case; the lobby returns after the end | UC-023, UC-024, UC-025 or UC-026 | |
| ST-5.4 | **Force start** from a controller (admin mode, trigger hold) or the pad (K4 hold in the lobby) | The charging LED, then the countdown with the controllers connected at that moment | UC-014 | |
| ST-5.5 | **Force end** during a game (dashboard button or pad K4 hold) | The game ends at once with outcome `force_ended`, controllers return to the lobby; no controller stays in a game colour | UC-027 | |
| ST-5.6 | **Refused start with too few players**: only 1 ready controller, try to start (dashboard, K1 tap, trigger hold) | The start is refused with the "too few players" feedback (voice / LED); no game process starts; the pad does not crash ([#2105](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2105) fixed this) | UC-010 A8, UC-014 A3, UC-015 A1 | |
| ST-5.7 | Mid-game drop: switch one controller off during a game | The player is eliminated, nobody else drops, the game continues | UC-030, UC-001 BR-005 | |

## 6. Agent

Setup: `./scripts/talk-stage.sh reset`, then `./scripts/talk-preflight.sh` (all PASS; WARN lines noted). Keys are in
`.env.local` (`JEV_API_KEY`, `ANTHROPIC_API_KEY`). The agent overlays (`enabled`, `interventions_enabled`) start at
off after a reboot.

| Id | Action | Expected result | Verifies | Result |
|---|---|---|---|---|
| ST-6.1 | `./scripts/talk-stage.sh reset`, then `./scripts/talk-preflight.sh` | Reset exits 0; the pre-flight has no FAIL; flagd holds its sync stream to the flag service; rust-hid is healthy (stage stack, `backend=rust`). Known: rust-hid may not get a first flagd sync ([#2090](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2090)): note the pre-flight line | UC-070 BR-003, UC-053 | |
| ST-6.2 | **Rules backend**: `./scripts/agent-backend.sh rules`, play an FFA with the agent on | Decisions appear in the dashboard Agent tab; interventions only when the agent is on and the write gate is open | UC-040, UC-041 | |
| ST-6.3 | **Jev backend**: `./scripts/agent-backend.sh jev`, play an FFA | Remote decisions in about 0.2 to 0.6 s per round; on a Jev failure (disconnect the network) the same cycle falls back to rules and the game never waits | UC-040, runbook section 7 | |
| ST-6.4 | **Claude backend**: `./scripts/agent-backend.sh claude`, play an FFA; then game end | Decisions in about 3 s (Claude is the backup, not the main backend); the post-game summary appears (needs `ANTHROPIC_API_KEY`) | UC-040, UC-042 | |
| ST-6.5 | **Write gate**: with `enabled=on` and `interventions_enabled=off` play a round; then flip `interventions_enabled=on` | With the gate off nothing is written to the game; **(known, [#2093](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2093))**: the dashboard may still show `act · allowed`, so check the `game_interventions_total{blocked="true"}` counter, not the label. With the gate on the intervention reaches the game | UC-040, UC-045, UC-053 | |
| ST-6.6 | **Shields in trigger mode** (`shield.activation=trigger`, the FFA default): play FFA rounds with the agent granting charges | The charged controller shows a slow breathing LED and the new charge rumble ([#2094](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2094), shipped in #2098: shield charge and ending patterns) that is **clearly distinct** from the 1 s death rumble and from the warning; pressing the trigger activates the shield responsively; an unused charge expires (`shield.charge_expiry_seconds`) with the ending rumble. Rollback if wrong: `shield.activation` variant `instant` | UC-040 (shield rows), UC-030 | |
| ST-6.7 | `./scripts/talk-stage.sh permissions none 12` during a game | Interventions stop within one decision; the baseline variant is restored after the hold | UC-070 A7, UC-053 | |
| ST-6.8 | **Kill switch**: stop the agent from the pad (K3 hold) or by `enabled=off`, during a game | The agent stops at once; no intervention after the stop; the resume restores the saved level | UC-013, UC-045, UC-015 BR-002 | |
| ST-6.9 | `./scripts/talk-stage.sh reset` again | Idempotent; the pre-flight is clean again | UC-070 | |

## 7. Operations

Run these on a version change, with the stack idle. Every command is `./scripts/setup/joustmania-upgrade.sh ...`.
A new version needs the images built by CI on main first.

| Id | Action | Expected result | Verifies | Result |
|---|---|---|---|---|
| ST-7.1 | `joustmania-upgrade.sh <joustmania-vX.Y.Z or sha>` (not the running one) | Pulls into a temporary worktree while the old version runs, switches, restarts and health-checks; about 1.5 to 2 min; record written; `--status` shows the new `current:` and the old `previous:`; 17 containers (18 with the `operator-pad` profile on), none unhealthy | UC-060 A2, BR-003 | |
| ST-7.2 | `joustmania-upgrade.sh --rollback` | Back on the previous version in about 1 min with **local images only** (works with the network off); `--status` swaps `current` and `previous` | UC-060 A3 | |
| ST-7.3 | **Upgrade within 15 min of the last restart** ([#2102](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2102), fixed in #2103): upgrade, then upgrade again at once, then `--rollback` at once | All three succeed; no `refused by systemd (start-limit-hit)`; unit `Result=success`. Needs the fixed script on **both** sides (the first run on the fixed version may still use the old script) | UC-060 BR-006, A5 | |
| ST-7.4 | `--status` after each step | No `WARN`; `joustmania-status` reports `ok` and the ACT LED is solid | UC-060 BR-005 | |
| ST-7.5 | Recovery: stack down with `start-limit-hit`: `sudo systemctl reset-failed joustmania && sudo systemctl start joustmania` | The stack comes up; try it only if the state occurs, otherwise just confirm the command is in your notes | UC-060 BR-006 | |
| ST-7.6 | Recovery from a cut-off run: pull the power during an upgrade (only on a spare version) and then run `--status` and re-run the same command | `--status` WARNs `HEAD ... is neither recorded version` or `a rollback ... was cut off`; the re-run repairs the record or `--rollback` finishes the move; then `sudo systemctl restart joustmania` | UC-060 A2, A3, DEVELOPMENT.md | |
| ST-7.7 | Git remote check | `git remote -v` shows an **HTTPS** remote (an SSH remote without a key blocks `git fetch --tags` in the upgrade, #2088 finding 3) | UC-060 A2 | |
| ST-7.8 | Disk and memory | `df -h /` shows plenty free (123 GB free in #2088); `free -m` while the stack runs is inside the Pi memory budget | NFR-020 | |

## 8. Long session (30+ minutes on the power bank)

Start this block in the background from block 5 on: leave the Pi on the power bank and play FFA games back to back
(or run a 300 s shadow game with the mock controllers via the dry-run stack) for **30 minutes or more**. Take the
first readings at the start and the rest every 10 minutes.

| Id | Action | Expected result | Verifies | Result |
|---|---|---|---|---|
| ST-8.1 | `vcgencmd get_throttled` at the start and every 10 min | `throttled=0x0` all session. Bit 0 (`0x1`) is undervoltage now, bit 1 (`0x2`) frequency capped, bit 2 (`0x4`) throttled, bit 3 (`0x8`) soft temperature limit; bits 16 to 19 (`0x10000`...) mean it happened since boot. Any non-zero value: note the bits, the power bank and the cable | NFR-020, C-011 | |
| ST-8.2 | `vcgencmd measure_temp` and `vcgencmd measure_clock arm` every 10 min | Target (untested; no 75 C figure in #2088): under about 75 C at the end and the ARM clock stays at its maximum (2.4 GHz on a Pi 5); note the case and fan | NFR-020 | |
| ST-8.3 | **Input lag p99** (query below) | p50 about 11 ms, p95 under about 112 ms, **p99 well under the #2088 baseline of 467 ms**; note the value per 10 min and whether the tail follows a power or Bluetooth event | NFR-004, NFR-002, UC-030 | |
| ST-8.4 | **Dropped frames and frame consistency** (queries below) | The dropped-frame count stays small (the #2088 data: 4533 dropped frames across 44 games, 149 stream frame overruns, frame consistency average 96.7 %); record the per-game consistency and the increase over the session | NFR-002, UC-030 | |
| ST-8.5 | Controllers over the session | `controller_disconnect_total` does not grow with the controllers in place; batteries on the cards drop by a plausible amount | UC-035 | |
| ST-8.6 | Memory and containers at the end | Same 17 containers (18 with the `operator-pad` profile on), none unhealthy or restarted (`docker ps`, `docker inspect -f '{{.RestartCount}}'` on the services); `free -m` shows no growth trend | NFR-020 | |
| ST-8.7 | Power bank: note the remaining charge at the start and the end | The session cost, so the venue run time can be planned; the bank holds the Pi plus the pad and the Bluetooth adapters without a throttled flag | C-011 | |

### Where to read the numbers

**Dashboard.** `http://PI/` shows the controller cards with battery and connection quality, and the Agent tab with
decisions and writes. **Grafana** is at `http://PI/grafana/` (dashboards from `services/grafana/dashboards/`): use the
game loop and controller panels for lag and frame health. **Jaeger** (`http://PI/jaeger/`) has the `agent.decide` and game
spans (Jaeger service `game-coordinator-service` for the game spans). **VictoriaMetrics** (Prometheus query API) is at `http://PI/victoria-metrics/`: the query UI at
`http://PI/victoria-metrics/vmui/` or the API from the Pi:

```bash
q() { curl -s --data-urlencode "query=$1" http://localhost/victoria-metrics/api/v1/query; }
# input lag p99 and p50 over the last 10 minutes (seconds)
q 'histogram_quantile(0.99, sum by (le) (rate(controller_input_lag_seconds_bucket[10m])))'
q 'histogram_quantile(0.50, sum by (le) (rate(controller_input_lag_seconds_bucket[10m])))'
# dropped frames over the session (30 m) and since the metrics began
q 'sum(increase(game_loop_frames_dropped_total[30m]))'
# last 3 days (the #2088 window)
q 'sum(last_over_time(game_loop_frames_dropped_total[3d]))'
# stream frame overruns
q 'sum(increase(controller_stream_frame_overruns_total[30m]))'
# frame consistency and jitter of live games
q 'game_loop_frame_consistency_percent'
q 'max_over_time(game_loop_jitter_ms[30m])'
# event-loop contention in the controller manager (poll lateness p99)
q 'histogram_quantile(0.99, sum by (le) (rate(controller_poll_loop_lateness_seconds_bucket[10m])))'
# disconnects and batteries
q 'sum(increase(controller_disconnect_total[30m]))'
q 'controller_battery_pct'
```

The VictoriaMetrics label and series APIs only look back a short window by default: pass `start=` (for example `start=-3d`).

Metric definitions: [telemetry spec](../specs/telemetry.md) (controller-manager and game-coordinator rows). Host side:
`vcgencmd get_throttled`, `vcgencmd measure_temp` and `journalctl -b -k | grep -i -E 'under-voltage|throttl'` (the
kernel logs `Under-voltage detected!`).

## How to report

One issue per live run (project rule). Post the filled table as a comment on
[#2088](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2088) when it is the next run of the same Pi, or open a
new verification issue ("Stage test run YYYY-MM-DD, version <sha8>") and link it from #1465. Copy this page's tables with
the **Result** column filled (`pass`, `fail` or a note) and the header table (version, power bank, controllers). For each
`fail`: the step id, what you saw, the log line or screenshot, and an issue number (file one, or name the known issue).
Add the block 8 readings (throttled flags, temperature, p50 / p95 / p99 lag, dropped frames) as a short list. Do not fix on
the day: file, then re-run only the failed steps after the fix.
