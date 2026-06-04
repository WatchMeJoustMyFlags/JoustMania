# Research: JoustMania Intervention Surface Analysis (#722)

Analysis of what an agent can change at runtime in JoustMania, mapped to session
objectives and policy constraints. This document populates `interventions.allowed`
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

Mode differences that matter for interventions:

| Mode family | Modes | Relevant property |
|---|---|---|
| Permanent elimination | FFA, Teams, Random Teams, Traitor, Werewolf | revive = re-entering the game; high impact |
| Respawn | Nonstop Joust, Zombie (zombies) | revive/eliminate are routine, low impact |
| Hidden role | Werewolf, Traitor | LED interventions can **leak roles** — restricted |
| Asymmetric thresholds | Zombie, Werewolf | per-role threshold overrides already exist |
| 1v1 queue | Tournament, Fight Club | most players idle; per-player interventions only meaningful for active fighters |

## 3. Intervention Inventory

### 3.1 What exists today (callable now, no new code)

| # | Intervention | Code path | Granularity | Reversible | Player-perceptible |
|---|---|---|---|---|---|
| E1 | Change music tempo | `Audio.ChangeTempo` RPC ([audio.proto](../../proto/audio.proto)); scales thresholds via LERP | session | yes | yes (audible) |
| E2 | Play sound / announcement | `Audio.PlaySound` RPC | session | n/a | yes |
| E3 | Set master volume | `Audio.SetVolume` RPC | session | yes | yes |
| E4 | Controller effect (rumble, LED pulse/flash, color) | `GameEffectCommand` via `StreamButtonEvents` / `StreamGameplayData` ([controller_manager.proto](../../proto/controller_manager.proto)) | per player or broadcast | yes (auto-restore) | yes |
| E5 | Force end game | `GameCoordinator.ForceEndGame` RPC | session | **no** | yes |
| E6 | Pre-game config (sensitivity, teams, time limits, invincibility, reveal time) | flagd `game_settings.json` → `StartGameConfig` | next game only | yes | between games |

**Caveat on E1:** the running game's `_music_loop` owns tempo scheduling and will
override an external `ChangeTempo` at its next scheduled transition, and the
`track_id` must match the current track ([servicer.py:379-396](../../services/audio/servicer.py)).
A direct call "works" but races the game. The M2 API must route tempo
interventions **through the game coordinator** so the music loop adopts the
agent's tempo instead of fighting it.

**Caveat on E4:** in hidden-role modes (Werewolf, Traitor) LED interventions can
reveal roles; the API must consult a per-mode capability matrix (§9).

**Excluded:** chaos fault injection and canary routing endpoints
(`connect-proxy/chaos.go`, `canary.go`) mutate the *infrastructure*, not the
game, and are test tooling — they do not belong in `interventions.allowed`.

### 3.2 What must be created (M2, #730)

The user-facing hypothesis "there is currently little to no surface" is **half
right**: ambient/session-level surface exists (above), but **no per-player
gameplay intervention has a runtime code path today**. These must be built:

| # | Intervention | Existing foundation | New work needed |
|---|---|---|---|
| N1 | Set per-player sensitivity | `Player.sensitivity_factor` field + threshold math already implemented (`base.py:877-881`) | RPC + plumbing to running game instance |
| N2 | Set global sensitivity mid-game | threshold tables exist; `self.sensitivity` frozen at init | RPC + safe live-update of `self.sensitivity` |
| N3 | **Grant shield** (temporary invulnerability) | `grace_until` (base), `invincible_until` (Tournament/FightClub), `spawn_protected` (Nonstop) | unify as a shield primitive in `BaseGameMode`; LED feedback effect; **FFA first**, inherited by all modes |
| N4 | Eliminate player | `_kill_player()` exists, internal only (`base.py:1174-1208`) | RPC wrapping the existing kill path with reason `agent_intervention` |
| N5 | Revive player | `player_revive` event exists; respawn logic only in Nonstop/Zombie | RPC; mode capability matrix (invalid where elimination is the win condition unless mode opts in) |
| N6 | Set music tempo (coordinated) | `_apply_tempo_change()` (`base.py:1748-1800`) | RPC on game coordinator that overrides the music loop's schedule |

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
rumble pulses. The rate limiter should live **server-side in the intervention
API** (§8), not only in the agent, so a misbehaving agent cannot exceed it.

## 6. Proposed `interventions.allowed` Values

Flag values for the permission layer in #725. Names are stable identifiers; the
API maps them to RPCs.

```json
"interventions.allowed": {
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

Persistence: the `game_id` label (pattern already established by
`game_player_peak_accel`) keeps per-game history in the TSDB; `PlayerAnalytics`
already persists end-of-game summaries (incl. `std_accel`) to Redis
(`analytics.py:319`), so per-game analytics persistence needs no new mechanism —
only the windowed/derived values above need to be added to the live gauges.

Cardinality note: `serial` and `game_id` labels are bounded (≤ ~16 controllers,
games are short); this matches the existing exposure and is safe for
VictoriaMetrics.

## 8. M2 Intervention API Sketch (#730)

Single entry point on the **game coordinator** (it owns game state and the
music loop; routing everything through it avoids the E1 tempo race and gives one
place to enforce policy):

```proto
// proto/game_coordinator.proto (additions)
service GameCoordinator {
  // ... existing RPCs ...
  rpc ApplyIntervention(InterventionRequest) returns (InterventionResponse);
}

message InterventionRequest {
  string intervention_id = 1;      // idempotency key
  string reason = 2;               // human-readable, recorded on span + event
  string objective = 3;            // endurance|balanced|accelerate|chaos
  oneof action {
    AdjustMusicTempo adjust_music_tempo = 10;          // speed 1.0–1.3, transition_s
    AdjustPlayerSensitivity adjust_player_sensitivity = 11;  // serial, factor 0.5–2.0
    AdjustGlobalSensitivity adjust_global_sensitivity = 12;  // sensitivity 0–4
    GrantShield grant_shield = 13;                     // serial, duration_s
    EliminatePlayer eliminate_player = 14;             // serial
    RevivePlayer revive_player = 15;                   // serial
    ControllerEffect controller_effect = 16;           // reuse GameEffectCommand
    PlayAudioCue play_audio_cue = 17;                  // file_pattern, volume
    EndGame end_game = 18;
  }
}

message InterventionResponse {
  bool applied = 1;
  string blocked_reason = 2;  // "not_in_allowed" | "rate_limited" |
                              // "battery_policy" | "mode_unsupported" | "no_active_game"
}
```

Server-side enforcement (defense in depth — the agent also checks flags, per
the #726 decision loop, but the API is the backstop):

1. `interventions.allowed` membership (flag re-evaluated per request)
2. weighted rate limit (§5) per `policy.max_interventions_per_minute`
3. battery guard per `policy.battery_threshold`
4. mode capability matrix (§9)
5. every decision recorded as a `game.intervention` span + `agent_intervention`
   EventBus event + `game_interventions_total` increment

Implementation notes:

- `grant_shield`: implement in `BaseGameMode` as
  `grant_shield(serial, duration)` ⇒ `player.grace_until = now + duration` + LED
  pulse effect; **FFA is the first target** (no protection mechanic today);
  Tournament/FightClub map it onto their existing `invincible_until` so semantics
  stay consistent.
- `eliminate_player` wraps the existing `_kill_player()` with reason
  `agent_intervention` so spans/events/metrics fire identically to a natural death.
- `adjust_player_sensitivity` only sets the already-implemented
  `sensitivity_factor`; the threshold math needs no change.
- `revive_player` requires per-mode opt-in (see §9).

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

## 10. Summary of Findings

1. **Session-level ambient surface exists today** (tempo, audio, controller
   effects, force-end); **per-player gameplay surface does not** — it must be
   created in M2 (#730), but the foundations (`sensitivity_factor`,
   `grace_until`, `_kill_player()`) are already implemented and only need an RPC
   and plumbing.
2. **Music tempo is the strongest existing lever** because it directly scales
   death thresholds — but it must be routed through the game coordinator to
   avoid racing the game's own music loop.
3. **Shields** (`grant_shield`) are the recommended new balancing primitive,
   FFA-first, built on the existing grace-period mechanism and generalized in
   `BaseGameMode`.
4. **`interventions.allowed`** should ship as a variant flag
   (`none`/`ambient`/`standard`/`full`) with `ambient` as the rollout default (§6).
5. **The OBSERVE layer can largely read existing metrics**; four additive
   metrics close the gap to the schema's signal list, and the `game_id` label +
   existing Redis analytics persistence cover per-game history (§7).
6. All interventions are assessed against the three policy constraints in §5;
   enforcement must be server-side in the intervention API, with weighted
   rate-limit costs rather than a flat count.
