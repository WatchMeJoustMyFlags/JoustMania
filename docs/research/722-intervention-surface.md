# Research: JoustMania Intervention Surface Analysis (#722)

Analysis of what an agent can change at runtime in JoustMania, mapped to session
objectives and policy constraints. This document populates `interventions_allowed`
in the flag schema (#725) and informs the M2 intervention API (#730).

## 1. Scope & Method

JoustMania is a physical game: players hold PS Move controllers, the game tracks
acceleration, and a player is eliminated when their (smoothed) movement exceeds a
threshold. There is no virtual health and no kill relationships — **the only
gameplay state is thresholds, aliveness, and time**. Every meaningful intervention
therefore acts on one of three axes:

1. **Difficulty** — move the death/warning thresholds (globally or per player)
2. **Aliveness** — change who is in the game (eliminate, revive, shield)
3. **Ambience** — influence player behavior without touching game state (audio, LEDs, rumble)

A codebase-wide constant audit (2026-06) surfaced a second, distinct surface:
**calibration** — hardcoded constants *upstream* of the intervention pipeline
(threshold tables, EMA weight, music schedule windows, role distributions).
Calibration knobs are not interventions: they are tuned per venue/session, not
mid-game per decision, and are exempt from the intervention policy budget (§5).
They do, however, bound what interventions can achieve. See §2b for the
distinction and the placement rule.

The analysis is grounded in the observable signals from the schema (#725):

| Signal | Source today | Gap |
|---|---|---|
| `player.movement_intensity` | `game_player_accel_magnitude{serial}` gauge ([metrics.py:165](../../services/game_coordinator/metrics.py)) | — |
| `player.movement_variance` | computed per player via Welford's algorithm ([analytics.py:157,229](../../services/game_coordinator/games/analytics.py)) but cumulative-only and **not exported as a metric** | needs rolling window + metric |
| `player.battery_pct` | `controller_battery_level{serial}` on a **0–5 scale** ([controller_state.py:120](../../services/controller_manager/controller_state.py)); Rust HID path already uses 0–100 ([psmove_hid.proto:102](../../proto/psmove_hid.proto)) | needs pct normalization |
| `player.skill_level` | `game_player_playstyle{serial}` (0=calm…3=aggressive) is a proxy ([metrics.py:183](../../services/game_coordinator/metrics.py)) | needs derived skill metric |
| `player.active` | `game_player_alive{serial}` gauge | — |
| `session.duration_seconds` | `game_duration_seconds` gauge | — |
| `session.active_player_count` | `game_players_alive` gauge | — |
| `session.elimination_sequence` | EventBus events only (`player_death`, `player_out`) | needs per-game metric |

See §7 for the proposal that closes these gaps.

## 2. Game Mechanics Primer

The death-detection pipeline ([base.py](../../services/game_coordinator/games/base.py)):

```
accel magnitude = √(x²+y²+z²)                      (base.py:818-832)
  → EMA smoothing: (prev·4 + raw)/5                 (base.py:834-851)
  → threshold = lerp(SLOW[sens], FAST[sens], music_speed_pct)   (base.py:853-881)
  → effective = threshold / player.sensitivity_factor  (clamped 0.5–2.0)
  → smoothed > effective_death  ⇒ kill
  → smoothed > effective_warn   ⇒ warning (white flash + rumble)
```

Tunable inputs to this pipeline:

- **Sensitivity index 0–4** — selects the threshold row (`base.py:88-98`). Set
  once at game start from `StartGameConfig.sensitivity` (`base.py:237-243`); the
  flagd `sensitivity` flag is read only when the menu builds the config
  ([servicer.py:798](../../services/menu/servicer.py)). **A mid-game flag change
  has no effect on a running game.**
- **Music speed 1.0–1.3×** — LERPs between the slow/fast threshold tables.
  Faster music ⇒ *higher* thresholds ⇒ mechanically harder to die, while socially
  demanding more movement. Owned by the game's `_music_loop`
  (`base.py:1819-1846`), which schedules tempo changes and calls the audio
  service's `ChangeTempo` RPC.
- **`player.sensitivity_factor`** — per-player multiplier, divides the threshold
  (higher factor ⇒ easier to die). Exists in the `Player` dataclass
  (`base.py:134-137`) and in `PlayerInfo` proto, **but no code path sets it at
  runtime** — it is infrastructure waiting for an API.
- **`player.grace_until`** — timestamp before which death/warning checks are
  skipped (`base.py:1068-1072`). Used for respawn grace and game-start grace.
  This is the natural mechanism for a **shield**: Tournament and Fight Club
  already implement the same idea as `invincible_until`, Nonstop as
  `spawn_protected`.

Fixed (hardcoded) parameters of the same pipeline — equally upstream, but
currently **not** tunable at all (see §2b for where they should live):

- **The threshold tables themselves** — `SLOW_WARNING` / `SLOW_MAX` /
  `FAST_WARNING` / `FAST_MAX` (`base.py:95-98`). The sensitivity index and
  `global_sensitivity_override` only *select a row*; the rows are constants.
  Flagging the tables (or a continuous scale factor over them, §8) turns 5
  discrete difficulty steps into a continuous range.
- **EMA smoothing weight** — `(prev·4 + raw)/5` (`base.py:850`). Changes what
  "smoothed movement" means; affects every threshold comparison *and* the
  variance baseline (§5).
- **Music schedule windows** — `MIN/MAX_MUSIC_FAST_TIME`,
  `MIN/MAX_MUSIC_SLOW_TIME` and the end-game variants (`base.py:72-81`). These
  define the *rhythm* of the fast/slow cycle that `music_tempo_override` can
  only momentarily interrupt.
- **Per-mode threshold overrides** — Zombie and Werewolf carry their own
  hardcoded tables (`zombie.py:41-47`, `werewolf.py:34-40`).

Mode differences that matter for interventions:

| Mode family | Modes | Relevant property |
|---|---|---|
| Permanent elimination | FFA, Teams, Random Teams, Traitor, Werewolf | revive = re-entering the game; high impact |
| Respawn | Nonstop Joust, Zombie (zombies) | revive/eliminate are routine, low impact |
| Hidden role | Werewolf, Traitor | LED interventions can **leak roles** — restricted |
| Asymmetric thresholds | Zombie, Werewolf | per-role threshold overrides already exist |
| 1v1 queue | Tournament, Fight Club | most players idle; per-player interventions only meaningful for active fighters |

## 2b. Calibration Surface (distinct from interventions)

> Added 2026-06 from the codebase-wide constant audit. Numbered `2b` to keep
> existing §3–§10 cross-references stable.

**Calibration ≠ intervention.** An *intervention* is a runtime decision the
agent makes mid-game, subject to the policy budget (§5), the permission flag
(§6), and the mode matrix (§9), flowing through `interventions.json`. A
*calibration* knob is a parameter of the system itself — tuned per venue,
session, or experiment — that belongs in `game.json` / `agent.json` /
`system.json` with **no nonce, no rate limit, and (usually) read-at-game-init
semantics**.

The placement rule for new flags:

| Question | Yes → | No → |
|---|---|---|
| Does the agent change it *mid-game* in response to signals? | `interventions.json` (policy-enforced, §8) | calibration domain |
| Does it shape gameplay/difficulty? | `game.json` | — |
| Does it shape the agent's own perception/decision loop? | `agent.json` | — |
| Is it infrastructure cadence (poll rates, timeouts)? | `system.json` / `controller.json` | — |

### Calibration candidates by tier

**Tier 1 — bounds the difficulty axis** (`game.json`, read at game init —
inherits the §2 "frozen at init" semantics, which is *correct* for calibration):

| Candidate | Today | Why |
|---|---|---|
| Death/warning threshold tables | `base.py:95-98`, hardcoded arrays | The real difficulty surface behind every sensitivity flag |
| Music schedule windows | `base.py:72-81` | Pacing rhythm; complements `music_tempo_override` |
| Grace periods (`DEATH_GRACE_PERIOD`, spawn protection) | `base.py:106`, `nonstop_joust.py:33` | Complements `shield_seconds` |
| Round/match durations, time between matches | `fight_club.py:29`, `tournament.py:40-42` | Session-length lever for `accelerate` |
| Role distributions (werewolf ~44%, traitor tiers, initial zombies) | `werewolf.py:117`, `traitor.py:110-116`, `zombie.py:32-34` | Between-game composition lever for `chaos` (extends E6) |
| Nonstop scoring weights (`100 − deaths·10`) | `nonstop_joust.py:494` | Handicapping lever |

**Tier 2 — the agent's own perception layer** (`agent.json`, next to the
existing `fitness.*` block):

| Candidate | Today | Why |
|---|---|---|
| Analytics zone boundaries (still/active/warning g) | `runtime_config.py:40-42` | Feed playstyle → proposed `skill_level` (§7); static baselines drift when the agent shifts difficulty |
| Playstyle classification thresholds (30 % / 70 %) | `analytics.py:266-270` | Same |
| EMA smoothing weight | `base.py:850` | Changes the observed signal; calibration-only — see §5 caveat |
| Agent freshness gates (`playerTTL` 5 s, `sessionGrace` 15 s, `evictEvery` 1 s) | `services/agent/main.go:35-37` | Hardcoded assumptions under the whole OBSERVE layer |
| Decision-loop throttle (1/s) | `services/agent/decision/decision.go:35` | Should evolve with `prompt_variant` / `mode` |

**Tier 3 — ambient parameterization** (`game.json` / `user.json`; these
parameterize E2–E4, they are *not* new interventions):

- Feedback effect timings (warning flash 200 ms @ 5 Hz, death rumble 255/150 ms,
  death fade 700 ms — `feedback_manager.py:351-380`)
- Per-channel volumes (game 0.7 / countdown 0.15 — `base.py:84-85`; sound 0.8 /
  voice 0.9 / lobby 0.4 — `menu/utils/audio.py`)
- Sentinel idle animation (brightness 9–30 %, 4 s breath, 30 s hue —
  `menu/idle_monitor.py:30-34`)
- Team/effect color palettes (`teams_base.py:29-38`, `fight_club.py:35-37`)

**Explicitly not worth flagging:** HID protocol constants (report sizes,
report ID `0x06`), accelerometer scale (4096 ADC/g), ALSA sample rate/channels,
gRPC/shutdown timeouts and retry backoffs (operational; change-via-PR is fine),
and state-machine enums.

## 3. Intervention Inventory

### 3.1 What exists today (callable now, no new code)

| # | Intervention | Code path | Granularity | Reversible | Player-perceptible |
|---|---|---|---|---|---|
| E1 | Change music tempo | `Audio.ChangeTempo` RPC ([audio.proto](../../proto/audio.proto)); scales thresholds via LERP | session | yes | yes (audible) |
| E2 | Play sound / announcement | `Audio.PlaySound` RPC | session | n/a | yes |
| E3 | Set master volume | `Audio.SetVolume` RPC | session | yes | yes |
| E4 | Controller effect (rumble, LED pulse/flash, color) | `GameEffectCommand` via `StreamButtonEvents` / `StreamGameplayData` ([controller_manager.proto](../../proto/controller_manager.proto)) | per player or broadcast | yes (auto-restore) | yes |
| E5 | Force end game | `GameCoordinator.ForceEndGame` RPC | session | **no** | yes |
| E6 | Pre-game config (sensitivity, teams, time limits, invincibility, reveal time) | flagd `game.json` → `StartGameConfig` | next game only | yes | between games |

**Caveat on E1:** the running game's `_music_loop` owns tempo scheduling and will
override an external `ChangeTempo` at its next scheduled transition, and the
`track_id` must match the current track ([servicer.py:379-396](../../services/audio/servicer.py)).
A direct call "works" but races the game. The M2 design must route tempo
interventions **through the game coordinator** (tempo-override flag, §8) so the
music loop adopts the agent's tempo instead of fighting it.

**Caveat on E4:** in hidden-role modes (Werewolf, Traitor) LED interventions can
reveal roles; the API must consult a per-mode capability matrix (§9).

**E6 is broader than listed:** the constant audit (§2b) shows the real
next-game-configurable surface also includes role distributions, round/match
durations, respawn timing, grace periods, and scoring weights — today hardcoded,
all promotable to `game.json` with zero mid-game risk. For `accelerate` and
`chaos` objectives, between-game composition changes are the *cheapest*
interventions: fully reversible, no trust risk, no policy budget.

**Excluded:** chaos fault injection and canary routing endpoints
(`connect-proxy/chaos.go`, `canary.go`) mutate the *infrastructure*, not the
game, and are test tooling — they do not belong in `interventions_allowed`.

### 3.2 What must be created (M2, #730)

The user-facing hypothesis "there is currently little to no surface" is **half
right**: ambient/session-level surface exists (above), but **no per-player
gameplay intervention has a runtime code path today**. These must be built.

**Control plane decision:** interventions are controlled via **OpenFeature
feature flags** (a dedicated flagd `interventions` domain), *not* via new gRPC
RPCs. The agent ACTs by writing flag config; the game coordinator subscribes to
flag changes and applies them. This reuses infrastructure that already exists
end-to-end: domain-scoped flagd providers ([feature_flags.py:81-124](../../lib/feature_flags.py)),
event-driven refresh on `PROVIDER_CONFIGURATION_CHANGED` with <100 ms
propagation ([runtime_config.py](../../services/game_coordinator/runtime_config.py)),
and the flag write path already used by admin mode
([flag_config_writer.py](../../lib/flag_config_writer.py)). See §8 for the design.

| # | Intervention | Existing foundation | New work needed |
|---|---|---|---|
| N1 | Set per-player sensitivity | `Player.sensitivity_factor` field + threshold math already implemented (`base.py:877-881`) | intervention flag with per-serial targeting + live re-evaluation in the game loop |
| N2 | Set global sensitivity mid-game | threshold tables exist; `self.sensitivity` frozen at init | intervention flag + safe live-update of `self.sensitivity` |
| N3 | **Grant shield** (temporary invulnerability) | `grace_until` (base), `invincible_until` (Tournament/FightClub), `spawn_protected` (Nonstop) | unify as a shield primitive in `BaseGameMode`, driven by a per-serial shield flag; LED feedback effect; **FFA first**, inherited by all modes |
| N4 | Eliminate player | `_kill_player()` exists, internal only (`base.py:1174-1208`) | edge-triggered intervention flag wrapping the existing kill path with reason `agent_intervention` |
| N5 | Revive player | `player_revive` event exists; respawn logic only in Nonstop/Zombie | edge-triggered intervention flag; mode capability matrix (invalid where elimination is the win condition unless mode opts in) |
| N6 | Set music tempo (coordinated) | `_apply_tempo_change()` (`base.py:1748-1800`) | tempo-override flag the music loop adopts instead of its own schedule |

**N3 (shields)** is called out per project direction: target **FFA first** — it
currently has no protection mechanic at all — but implement at the
`BaseGameMode` level so every mode inherits it. A shield is:
`player.grace_until = now + duration` + a distinct LED effect (e.g. pulsing
white/blue) + auto-restore. The existing `GAME_EFFECT_PULSE` covers the visual.
Shields are the **balancing intervention of choice** in permanent-elimination
modes because they protect without changing difficulty for everyone else, and
they are time-bounded and self-reverting.

## 4. Objective Mapping

Objectives from the flag schema (#725): `endurance` (long sessions),
`balanced` (close games, small skill gaps), `accelerate` (short sessions),
`chaos` (unpredictability).

| Intervention | endurance | balanced | accelerate | chaos | Mechanism |
|---|:-:|:-:|:-:|:-:|---|
| E1/N6 `adjust_music_tempo` | ✅ slow ⇒ lower thresholds, calmer play | — | ✅ fast pacing pressures players | ✅ rapid oscillation | thresholds LERP with tempo |
| E2 `play_audio_cue` | ✅ calm cues | ✅ targeted call-outs | ✅ urgency cues | ✅ misdirection | psychological only |
| E4 `send_controller_effect` | ✅ re-engage idle players (rumble nudge) | ✅ pressure leader / reassure trailing player | ✅ broadcast urgency | ✅ fake warnings, random pulses | psychological only |
| N1 `adjust_player_sensitivity` | ✅ lower factor for at-risk players | ✅✅ primary balancing lever: handicap dominant players (factor↑), protect weak ones (factor↓) | ✅ raise factors globally via per-player loop | ✅ randomize factors | divides threshold per player |
| N2 `adjust_global_sensitivity` | ✅ drop to ULTRA_SLOW | — | ✅ raise to ULTRA_FAST | ✅ oscillate | swaps threshold row |
| N3 `grant_shield` | ✅ keep field large longer | ✅✅ protect players the elimination sequence shows dying early | — | ✅ random shields flip expected outcomes | grace period skips death checks |
| N4 `eliminate_player` | — | ✅ remove runaway leader (drastic; prefer N1) | ✅✅ primary accelerate lever | ✅ random elimination | wraps `_kill_player()` |
| N5 `revive_player` | ✅ re-grow the field | ✅ second chance for early eliminations | — | ✅ surprise returns | re-enter player |
| E5 `end_game` | — | — | ✅ terminal action | — | hard stop |
| E6 next-game config (incl. §2b Tier 1: durations, role distributions, thresholds) | ✅ longer rounds, lower thresholds | ✅ handicaps via scoring weights | ✅✅ shorter rounds — cheapest accelerate lever | ✅ reshape role composition each game | applied at next `StartGameConfig`; zero mid-game risk |

Signal → intervention examples (input to the rules engine, #726):

- `session.duration_seconds` high + objective `accelerate` ⇒ N2/N6 up, then N4, finally E5
- `player.movement_intensity` spread large + objective `balanced` ⇒ N1 on outliers
- `session.elimination_sequence` shows same player always first out + `balanced` ⇒ N3 shield that player at next game's start
- `player.movement_variance` near zero (player gaming the EMA by holding perfectly still) + `chaos` ⇒ E4 rumble nudge or N1 factor↑
- `player.battery_pct < policy.battery_threshold` ⇒ see §5; prefer N3/N4 as *graceful exit* over letting the controller die mid-round

## 5. Policy Constraint Assessment

### `policy.battery_threshold` (default 20)

| Intervention | Interaction |
|---|---|
| E4 effects | rumble and bright LEDs are the dominant battery drains — **blocked** for players below threshold |
| E1/N2/N6 difficulty↑ | raising movement demand on a low-battery controller risks mid-game death-by-disconnect — blocked below threshold |
| N1 factor↓, N3 shield | battery-safe; *preferred* actions for low-battery players |
| N4 eliminate | the *graceful degradation* path: eliminate a near-dead-battery player cleanly (with E2 announcement) instead of an ugly disconnect |

Note: battery today is 0–5 (`get_battery()`); the policy is expressed in percent.
The normalization (§7) must land before this constraint is enforceable.

### `policy.movement_variance_window` (default 10 s)

- Any intervention **triggered by** `movement_variance` must wait until the
  rolling window has at least `window` seconds of samples (game start, post-respawn,
  post-shield).
- Difficulty interventions (E1, N1, N2, N6) **invalidate the variance baseline** —
  players change behavior in response. After any of these, variance-triggered
  decisions must observe a cooldown of one full window before re-evaluating.
- The same invalidation applies to the **EMA smoothing weight** (`base.py:850`)
  if it becomes tunable (§2b Tier 2): changing it redefines what "smoothed
  movement" means. For this reason the EMA weight must be **calibration-only**
  (frozen per game, never changed mid-game) — it is not an intervention.
- Current Welford implementation is cumulative over the whole game
  (`analytics.py:110-157`); the windowed variant is new work (§7).

### `policy.max_interventions_per_minute` (default 2)

With a budget of 2/min, interventions must be **weighted**, not counted equally:

| Class | Interventions | Cost |
|---|---|---|
| Soft (psychological, self-reverting) | E2, E3, E4 | 0.5 |
| Medium (tunable, reversible) | E1/N6, N1, N3 | 1 |
| Hard (state-changing, player-visible as unfair if wrong) | N2, N4, N5 | 2 |
| Terminal | E5 | 2 + requires `accelerate` weight dominant |

Rationale: a physical party game tolerates ambient nudges far better than
visible state changes; one wrong `eliminate_player` damages trust more than ten
rumble pulses. The rate limiter should live **in the game coordinator's
flag-application layer** (§8), not only in the agent, so a misbehaving agent
flipping flags rapidly cannot exceed it — excess flag changes are ignored and
recorded as `blocked`.

## 6. Proposed `interventions_allowed` Values

Flag values for the permission layer in #725 (implemented as the
`interventions_allowed` flag in the `agent` flagSetId domain,
`services/flagd/agent.json`). Names are stable identifiers; the
game coordinator maps each to the intervention flag(s) it is allowed to honor (§8).

```json
"interventions_allowed": {
  "variants": {
    "none":     [],
    "ambient":  ["play_audio_cue", "send_controller_effect", "adjust_volume"],
    "standard": ["play_audio_cue", "send_controller_effect", "adjust_volume",
                 "adjust_music_tempo", "adjust_player_sensitivity", "grant_shield"],
    "full":     ["play_audio_cue", "send_controller_effect", "adjust_volume",
                 "adjust_music_tempo", "adjust_player_sensitivity", "grant_shield",
                 "adjust_global_sensitivity", "eliminate_player", "revive_player",
                 "end_game"]
  },
  "defaultVariant": "ambient"
}
```

- **`none`** — kill switch (complements `agent.enabled`).
- **`ambient`** — recommended M1/M2-rollout default: only what exists today and
  cannot alter game state. Safe to enable while the rules engine is being tuned.
- **`standard`** — recommended steady state once #730 lands: adds the three
  reversible gameplay levers (tempo, per-player sensitivity, shields).
- **`full`** — adds irreversible actions; for supervised sessions and demos.

## 7. OBSERVE via Metrics (per-player / per-game metrics proposal)

The agent scaffold (#723) consumes OTel spans. This research recommends a
**complementary metrics path**: the per-player signals are continuous gauges,
which Prometheus/VictoriaMetrics already store, persist, and make queryable —
spans are better for *decision audit*, metrics for *state observation*. The
agent's `extract_game_context()` can be a handful of PromQL instant queries.

Existing metrics already cover most of the signal list (§1). New/changed metrics
to close the gaps (all in [game_coordinator/metrics.py](../../services/game_coordinator/metrics.py)
unless noted, following the existing `game_player_*{serial}` pattern):

| New metric | Type / labels | Definition |
|---|---|---|
| `game_player_movement_variance{serial}` | Gauge | rolling variance of accel magnitude over `policy.movement_variance_window` seconds (windowed variant of the existing Welford code in `analytics.py`) |
| `game_player_skill_level{serial}` | Gauge 0.0–1.0 | derived score: normalized blend of survival time percentile, warnings survived (`game_player_warnings_total` vs deaths), and peak accel control; the existing `game_player_playstyle` stays as the behavioral classification |
| `controller_battery_pct{serial}` | Gauge 0–100 | normalized from the 0–5 scale (`level × 20`); keep `controller_battery_level` for compatibility ([controller_manager/metrics.py](../../services/controller_manager/metrics.py)) |
| `game_player_elimination_order{serial, game_id}` | Gauge | order index (1 = first out); makes `session.elimination_sequence` queryable; follows the `game_player_peak_accel{serial, game_id}` labeling pattern |
| `game_interventions_total{type, objective, blocked}` | Counter | every agent intervention attempt — the ACT layer's own audit metric (also needed for the rate limiter and #731 fitness functions) |

**Perception thresholds must be flags, not constants.** The proposed
`game_player_skill_level` builds on the playstyle classification, whose
thresholds (30 %/70 %, `analytics.py:266-270`) and zone boundaries
(1.1/1.5/2.0 g, `runtime_config.py:40-42`) are hardcoded today. If the agent
shifts difficulty, these static baselines drift — they belong in `agent.json`
next to the `fitness.*` block (§2b Tier 2). The same goes for the agent's own
freshness gate (`playerTTL` = 5 s, `services/agent/main.go:35`), which silently
defines what "fresh data" means for `ShouldEvaluate`.

Persistence: the `game_id` label (pattern already established by
`game_player_peak_accel`) keeps per-game history in the TSDB; `PlayerAnalytics`
already persists end-of-game summaries (incl. `std_accel`) to Redis
(`analytics.py:319`), so per-game analytics persistence needs no new mechanism —
only the windowed/derived values above need to be added to the live gauges.

Cardinality note: `serial` and `game_id` labels are bounded (≤ ~16 controllers,
games are short); this matches the existing exposure and is safe for
VictoriaMetrics.

## 8. M2 Intervention Design: OpenFeature Flags as the Control Plane (#730)

**Interventions are controlled via feature flags, not gRPC.** The agent
manipulates the game exclusively through OpenFeature: it *writes* intervention
flag config, and the **game coordinator** evaluates those flags and applies the
effects. The game coordinator remains the single application point (it owns game
state and the music loop, which avoids the E1 tempo race and gives one place to
enforce policy) — but its inbound interface is flag evaluation, not an RPC.

```
agent (#726 rules / LLM)
  │ writes flag config            (FlagConfigWriter — same path admin mode uses,
  ▼                                lib/flag_config_writer.py)
flagd  `interventions` domain     (new flag file, flagSetId="interventions")
  │ PROVIDER_CONFIGURATION_CHANGED, <100 ms
  ▼                                (existing event-driven refresh pattern,
game coordinator                   runtime_config.py:99-154)
  │ re-evaluates intervention flags, enforces policy, applies effects
  ▼
running game (BaseGameMode)
```

### New flagd domain: `interventions`

The flag file `services/flagd/interventions.json` (`flagSetId:
"interventions"`), one of the eight domains shipped with #749 (see
[feature-flags.md](../feature-flags.md#domains)). Initialized via the existing
`init_flag_domain()` ([feature_flags.py:81](../../lib/feature_flags.py)).

Two flag shapes are needed, because flags are **declarative state, not
commands**:

**a) State-shaped interventions** — natural fit; the game converges on the flag
value, and reverting the flag reverts the intervention:

| Flag | Type | Evaluation | Maps to |
|---|---|---|---|
| `music_tempo_override` | number, `0` = no override, else `1.0–1.3` | session | N6 — `_music_loop` adopts the override instead of its own schedule |
| `global_sensitivity_override` | int, `-1` = no override, else `0–4` | session | N2 — live update of `self.sensitivity` |
| `player_sensitivity_factor` | number `0.5–2.0`, default `1.0` | **per player**: flagd targeting rules keyed on `targetingKey = serial` | N1 — game loop evaluates per player and sets `sensitivity_factor` |
| `shield_seconds` | number (remaining duration s), default `0` | per player (targeting on serial) | N3 — `BaseGameMode` sets `grace_until = now + value` on rising edge + LED pulse; expiry is game-side, agent doesn't need to flip it back |
| `volume_override` | number, `-1` = no override | session | E3 |

**b) Edge-triggered (one-shot) interventions** — a flag change *is* the
command. The variant value carries a **nonce** (monotonic `intervention_id`) so
the game coordinator can distinguish a new trigger from a re-read, and
re-evaluation after reconnect is idempotent:

| Flag | Value shape | Maps to |
|---|---|---|
| `eliminate_player` | `"<nonce>:<serial>"` (empty = none) | N4 — wraps `_kill_player()` with reason `agent_intervention` |
| `revive_player` | `"<nonce>:<serial>"` | N5 |
| `audio_cue` | `"<nonce>:<sound_id>"` | E2 |
| `controller_effect` | `"<nonce>:<serial>:<effect>"` (serial empty = broadcast) | E4 |
| `end_game` | `"<nonce>"` | E5 — wraps the existing `ForceEndGame` path |

The game coordinator stores the last-applied nonce per flag; a changed nonce
triggers exactly one application. State-shaped flags should be preferred
whenever an intervention can be expressed as state (which is why `shield` is a
duration value, not a one-shot).

### Enforcement in the game coordinator (defense in depth)

The agent already checks `interventions_allowed` in its decision loop (#726,
#728); the game coordinator's flag-application layer is the backstop — it
ignores (and records as blocked) any flag change that fails:

1. `interventions_allowed` membership — the permission flag is evaluated by
   **both** sides; an intervention flag not covered by the current variant is
   never applied
2. weighted rate limit (§5) per `policy.max_interventions_per_minute`, counted
   on applied flag changes
3. battery guard per `policy.battery_threshold`
4. mode capability matrix (§9)
5. every applied/blocked intervention recorded as a `game.intervention` span +
   `agent_intervention` EventBus event + `game_interventions_total{type,
   objective, blocked}` increment — this is also how flag evaluation becomes
   visible in traces (#729)

### Implementation notes

- `grant_shield`: implement in `BaseGameMode` as
  `grant_shield(serial, duration)` ⇒ `player.grace_until = now + duration` + LED
  pulse effect; **FFA is the first target** (no protection mechanic today);
  Tournament/FightClub map it onto their existing `invincible_until` so semantics
  stay consistent.
- Per-player targeting uses OpenFeature `EvaluationContext` with
  `targetingKey = serial` — the global context infrastructure already exists
  (`feature_flags.py:39-78`); the game loop adds the per-player context when
  evaluating per-player intervention flags.
- The agent's write path is `FlagConfigWriter` (already proven by admin mode,
  [admin.py:755-820](../../services/menu/handlers/admin.py)); flagd file-watch
  picks up the change. No new transport, no new proto.
- `eliminate_player` wraps the existing `_kill_player()` with reason
  `agent_intervention` so spans/events/metrics fire identically to a natural death.
- `adjust_player_sensitivity` only sets the already-implemented
  `sensitivity_factor`; the threshold math needs no change.
- `revive_player` requires per-mode opt-in (see §9).
- Open question for #730: whether per-player values use flagd **targeting
  rules** (one flag, rules per serial — richer, but the agent must rewrite rule
  blocks) or **per-serial flag keys** (`shield_seconds.<serial>` — simpler
  writes, more keys). Targeting rules are the OpenFeature-idiomatic choice and
  keep `interventions.json` schema-stable; recommended starting point.

### Candidate additions from the calibration audit (proposed, decide in #730)

Two further **state-shaped** flags follow from §2b and stay consistent with the
"prefer state-shaped" principle:

| Flag | Type | Evaluation | Rationale |
|---|---|---|---|
| `global_difficulty_factor` | number `0.5–2.0`, default `1.0` | session | Continuous global analogue of `player_sensitivity_factor` — same threshold-division math (`base.py:877-881`), no init-freeze problem. Strictly finer-grained than `global_sensitivity_override`'s 5 discrete rows; the preferred lever for `balanced`. |
| `pacing_profile` | string (`calm`, `default`, `frantic`) | session | Selects a preset over the music schedule windows (§2 fixed parameters) so the `_music_loop` *itself* paces differently — declarative, instead of the agent repeatedly flipping `music_tempo_override` against the loop's schedule. |

Placement note: the threshold **tables** themselves stay calibration
(`game.json`, next-game-only, §2b Tier 1) — flagging them as live interventions
would inherit N2's "safe live-update" problem for no benefit once
`global_difficulty_factor` exists. Cleanest split: **tables = calibration,
factor = live intervention.**

## 9. Mode Capability Matrix

| Intervention | FFA | Teams/Random | Nonstop | Tournament | FightClub | Zombie | Werewolf | Traitor | Swapper |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| adjust_music_tempo | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| play_audio_cue / volume | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| send_controller_effect | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ rumble only — LEDs leak roles | ⚠️ rumble only | ✅ |
| adjust_player_sensitivity | ✅ | ✅ | ✅ | ⚠️ active fighters only | ⚠️ active fighters only | ✅ (roles already asymmetric) | ✅ | ✅ | ✅ |
| adjust_global_sensitivity | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ interacts with role threshold overrides | ⚠️ same | ⚠️ same | ✅ |
| grant_shield | ✅ **first target** | ✅ | ✅ (exists as spawn protection) | ✅ (exists as invincibility) | ✅ (exists) | ✅ | ⚠️ shield can signal role | ⚠️ same | ✅ |
| eliminate_player | ✅ | ✅ | ✅ (respawns) | ✅ (forfeits match) | ✅ | ✅ | ✅ | ✅ | ⚠️ swaps team instead — mode semantics |
| revive_player | ⚠️ opt-in | ⚠️ opt-in | ✅ native | ❌ breaks bracket | ❌ breaks queue | ✅ native (zombies) | ❌ breaks hidden roles | ❌ same | ❌ team state ambiguous |
| end_game | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

The proposed `global_difficulty_factor` inherits the `adjust_global_sensitivity`
row (same ⚠️ in modes with role threshold overrides); `pacing_profile` inherits
the `adjust_music_tempo` row (✅ everywhere).

## 10. Summary of Findings

1. **Session-level ambient surface exists today** (tempo, audio, controller
   effects, force-end); **per-player gameplay surface does not** — it must be
   created in M2 (#730), but the foundations (`sensitivity_factor`,
   `grace_until`, `_kill_player()`) are already implemented and only need
   flag-driven plumbing.
2. **OpenFeature flags are the control plane, not gRPC** — the agent writes
   intervention flag config (a new flagd `interventions` domain); the game
   coordinator subscribes, enforces policy, and applies. State-shaped flags for
   continuous interventions, nonce'd edge-triggered flags for one-shots (§8).
   The entire path (domain providers, <100 ms change events, flag writer) is
   existing, proven infrastructure.
3. **Music tempo is the strongest existing lever** because it directly scales
   death thresholds — but it must be applied by the game coordinator (tempo
   override flag) to avoid racing the game's own music loop.
4. **Shields** (`grant_shield`) are the recommended new balancing primitive,
   FFA-first, built on the existing grace-period mechanism and generalized in
   `BaseGameMode`.
5. **`interventions_allowed`** should ship as a variant flag
   (`none`/`ambient`/`standard`/`full`) with `ambient` as the rollout default (§6).
6. **The OBSERVE layer can largely read existing metrics**; five additive
   metrics close the gap to the schema's signal list, and the `game_id` label +
   existing Redis analytics persistence cover per-game history (§7).
7. All interventions are assessed against the three policy constraints in §5;
   enforcement must live in the game coordinator's flag-application layer, with
   weighted rate-limit costs rather than a flat count.
8. **The intervention surface sits on top of a calibration surface** (§2b):
   hardcoded threshold tables, music schedule windows, role distributions, and
   perception thresholds bound what interventions can achieve. Promoting them
   to flags is separate work from #730 — calibration flags go to `game.json` /
   `agent.json` with no policy budget, and only two new state-shaped levers
   (`global_difficulty_factor`, `pacing_profile`) touch `interventions.json`
   (§8). See §11 for the follow-up plan.

## 11. Follow-Up Work (calibration promotion plan)

What must land, in dependency order, to make the calibration surface real and
keep docs/schema consistent. Each item is one focused PR.

| # | Work item | Domain | Depends on |
|---|---|---|---|
| F1 | Promote death/warning threshold tables + per-mode overrides to flags (read at game init) | `game.json` | — |
| F2 | Promote grace periods, round/match durations, role distributions, scoring weights | `game.json` | — |
| F3 | Promote music schedule windows; define the `pacing_profile` presets over them | `game.json` (+ §8 flag in #730) | F2 |
| F4 | Move perception thresholds (zone boundaries, playstyle %, EMA weight) to flags; EMA frozen-per-game | `agent.json` | — |
| F5 | Replace agent Go constants (`playerTTL`, `sessionGrace`, `evictEvery`, throttle) with agent-domain flags — requires a Go OpenFeature/flagd client in the agent service | `agent.json` | — |
| F6 | Add `global_difficulty_factor` + `pacing_profile` to `interventions.json` with policy class *Medium* (§5) | `interventions.json` | #730, F3 |
| F7 | Ambient parameterization (effect timings, per-channel volumes, sentinel animation) | `game.json` / `user.json` | — |
| F8 | Update [feature-flags.md](../feature-flags.md) flag tables as each of F1–F7 lands; keep §2b placement rule and the doc's "Adding New Flags" guidance in sync | docs | F1–F7 |

Consistency rules for all of the above:

- Follow the #725 naming convention (bare `snake_case`, dots only for genuine
  sub-structure, no domain prefix in keys).
- Every promoted constant keeps its current value as the flag default — the
  promotion itself must be behavior-neutral.
- Calibration flags are read at game init (no live re-evaluation) unless a §8
  intervention flag explicitly covers the live path.
