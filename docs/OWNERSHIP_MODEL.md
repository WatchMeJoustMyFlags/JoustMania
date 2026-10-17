# Human / Agent Settings Ownership & Arbitration Model

This document is the **authoritative contract** for how the human admin and the
adaptive agent share control of game parameters. It records the ownership model
ratified with the maintainer (epic [#814](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/814),
doc issue [#820](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/820))
and is the spec the code fixes #816 (sensitivity), #817 (grace/shield), #818
(visibility) and #819 (kill-switch) are implemented against. Where this document
and the current code disagree, **this document describes the intended end state**;
the "Today" notes call out where the code has not caught up yet.

## 1. The Ownership Principle

> **The human admin owns pre-game intent. The agent owns bounded in-game deltas.**

- **Human admin** decides *what game is played and how it is calibrated*: the game
  mode, the baseline settings, and when the game starts. The menu's admin mode
  ([`services/menu/handlers/admin.py`](../services/menu/handlers/admin.py)) writes
  them to the `game` / `user` flag sets through the flag service
  (`FlagServiceWriter`, [`lib/flag_service_writer.py`](../lib/flag_service_writer.py))
  as **persisted admin values** (`"persist": true`), kept in the flag service's state
  file `/var/lib/flag-service/admin.json`.
- **Agent** decides *how to nudge the experience while the game runs*: bounded
  overrides, factors, and shields. It writes them to the `interventions` flag set
  through the flag service, scoped to one game (the gated tier-2 and rollout writes
  are listed under the invariants below)
  ([`services/agent/actions/writer.go`](../services/agent/actions/writer.go)); the
  game coordinator's `InterventionManager`
  ([`services/game_coordinator/interventions.py`](../services/game_coordinator/interventions.py))
  evaluates them live with its `gameId`.

Since the flag service (epic #1407) **no process writes a flag file**: the files in
`services/flagd/` are the read-only baseline the flag service loads at start (C-033),
every runtime write is an HTTP call to the flag service, and only flags in its
mutable-flag registry ([flags.md §13](specs/flags.md#13-mutable-flag-registry)) can
change, within their bounds.

Two invariants follow and are **load-bearing**. Both were amended (spec
consistency pass, epic #1260, D-A3) to match the northstar's tier 2 and the code:
the agent has three narrow, gated writes beyond the `interventions` set.

1. **The agent never persists a human baseline it was not allowed to change.**
   Its in-game write surface is the `interventions` set, always game-scoped
   (`scope.game_id`, plus `scope.serial` for per-player values; one-shot
   interventions are events in the game's event list). The flag service drops a
   game's values when the coordinator ends the game (UC-053 BR-003). The agent
   never writes the `user` or `agent` set and never passes `"persist": true`.
   Two exceptions write the `game` set, and both are tier 2 ([NORTHSTAR](NORTHSTAR.md)):
   - **Shadow-scoped experiment arms**: `PUT /game/{flag}` with scope
     `{experiment_id, arm}`; the flag service renders a rung that matches only
     `game_kind != "real"` plus that experiment and arm, and accepts only flags in
     the experiment catalog (flags.md §13.3) within their bounds (FR-062). What a
     real game resolves is unchanged.
   - **Gated autonomous promotion** (`promote/realdefault.go`): in
     `code_improvement.mode=autonomous`, and only when `AGENT_CODE_IMPROVEMENT_ENABLED`
     and `AGENT_AUTONOMOUS_ENABLED` are set and `enabled` is on, a shadow-validated
     winner becomes a `real` overlay (`scope.game_kind=real`, writer `promotion`),
     reverted on degradation with `DELETE ?game_kind=real` (FR-065). The overlay is
     in memory and lasts until the flag service restarts; the durable path is the
     promotion PR. The stock stack passes none of these env gates (D-A6), so this
     write is off by default.

   The agent also writes `rollout/current_controller_count` (through
   `actions/rollout_writer.go`) for the infrastructure rollout, gated by
   `rollout_enabled` and `remediation_allowed` (FR-071, FR-072). That flag is
   infrastructure state, not a human game setting.
2. **The write surfaces are disjoint by scope.** The menu writes persisted admin
   values of `game` / `user` flags (and the single `agent` key
   `interventions_allowed`); the agent writes game-scoped `interventions` values,
   `rollout/current_controller_count`, and the experiment-arm and `real` overlays
   of `game` flags. The flag service holds each kind of value separately per flag
   (baseline, persisted admin value, stack overlays, `real` overlay, experiment
   arms, game-scoped values) and serialises every write, so an agent write can no
   longer overwrite a concurrent admin write. The old risk of two read-modify-write
   writers on `game.json` without a shared lock (#1292) is gone with the files.

The contested ground is therefore not the *flag sets* but the *runtime
state* the two write paths converge on inside the game object (sensitivity, grace
periods). Sections 4–5 define those compositions precisely.

## 2. Flag Domain Ownership

| Flag set (baseline `services/flagd/<set>.json`, read-only) | Owner | Writer (through the flag service) | Read by | Lifetime |
|---|---|---|---|---|
| `game` | Human admin | Menu admin mode → `FlagServiceWriter` (persisted admin values); agent: experiment arms (shadow only), gated `real` promotion overlay (§1) | Menu builds `StartGameConfig` at start; coordinator freezes at init | Baseline per deployment; admin values persist across restarts; agent overlays in memory |
| `user` | Human admin | Menu admin mode → `FlagServiceWriter` (persisted admin values) | Menu / audio (volumes, voice, instructions) | Per-deployment |
| `interventions` | Agent | Agent ActionSink → `writer.go` (game-scoped values and events) | Coordinator `InterventionManager`, live | One game: dropped when the game ends |
| `rollout` | Operator (human) for the ladder; agent for `current_controller_count` | Agent `actions/rollout_writer.go` (gated by `rollout_enabled`, `remediation_allowed`); operator via `scripts/flags.sh` | Controller manager rollout router, agent infra loop | In memory until the flag service restarts |
| `agent` | Operator (human) | Operator / stage scripts (`scripts/flags.sh`, `agent-killswitch.sh`, `agent-backend.sh`, `talk-stage.sh`: in-memory overlays) **and** menu admin mode → `FlagServiceWriter` (`interventions_allowed` only, persisted, #819) | Agent + coordinator policy gates | Baseline per deployment; overlays until restart |

The `agent` set holds the agent's own governor — `enabled`, `decision.backend`, `objectives`,
`interventions_allowed`, and the `policy.*` budgets. As of #819, **menu admin mode
may write the single `interventions_allowed` key** (the on-device kill-switch);
other `agent` keys change through operator overlays (only those in the registry)
or a reviewed baseline edit. The **agent process never writes the `agent` set** —
that invariant is unchanged (menu writes `game` / `user` admin values and the one
agent-policy key; the agent writes `interventions`, `rollout/current_controller_count`
and the gated `game` writes of §1). The human owns agent policy; the agent cannot
persist its own governor (§1, §6.2).

> **How the agent-never-writes-the-`agent`-set invariant is enforced.** No service
> mounts the flag directory writable any more; only the flag service mounts it, read-only.
> The agent's flag-service client writes fixed flag sets: interventions to
> `interventions`, the rollout count to `rollout`, experiment arms and promotion to
> `game` ([`services/agent/experiment/gate.go`](../services/agent/experiment/gate.go)
> rejects a proposal naming another set, `non_game_target`, and the promotion
> writer's set is always `game`, #1293). The flag service has no roles or tokens
> (local party game, decision D11 of the [architecture note](architecture/flag-service.md)),
> so the guarantee rests on this client code; a write that would change the
> `agent` set is visible as a `flagservice.write` span with `flagservice.writer`.

> **Historical: the nonroot menu and the flag dir (#1031).** The menu used to write
> the bind-mounted flag files and needed a one-shot `flag-dir-init` sidecar to make
> the directory group-writable. Both are gone: the menu writes over HTTP to the flag
> service, and the flag directory is mounted read-only.

## 3. Parameter Ownership Table

The eight admin flags. All are read once by the menu into `StartGameConfig`
([`services/menu/servicer.py:_build_game_config`](../services/menu/servicer.py),
~line 788) and frozen into the game object in its constructor
([`services/game_coordinator/games/base.py:__init__`](../services/game_coordinator/games/base.py),
~lines 490–580). "Frozen at start" means a mid-game admin flag change **silently
no-ops** for that game — the game already captured its value.

| Parameter | Owner | Write path | Frozen / Live | Arbitration rule |
|---|---|---|---|---|
| `sensitivity` | Human (baseline) | `game` set, persisted admin value (menu) | Frozen at init → `game.sensitivity` + `game.configured_sensitivity` | **Contested with agent `global_sensitivity_override`.** See §4.1. |
| `num_teams` | Human | `game` set, persisted admin value (menu) | Frozen at init | Agent has no override; human-only. |
| `random_assignment` | Human | `game` set baseline (`game.json`) | Frozen at init | Agent has no override; human-only. |
| `nonstop.time_limit_seconds` | Human | `game` set baseline (`game.json`) | Frozen at init | Agent has no override; human-only. |
| `invincibility_seconds` | Human (baseline) | `game` set baseline (`game.json`) | Frozen at init → `_invincibility_duration` (Tournament/Fight Club) | **Adjacent to agent `shield_seconds`.** See §4.2. |
| `fight_club.min_rounds` | Human | `game` set baseline (`game.json`) | Frozen at init | Agent has no override; human-only. |
| `werewolf.reveal_time_seconds` | Human | `game` set baseline (`game.json`) | Frozen at init | Agent has no override; human-only. |
| `force_all_start` | Human | `game` set, persisted admin value (menu) | **Live** (read at start by menu, not frozen into a game) | Human-only; the one admin flag with no freeze. |

### 3.1 Controller-cycled admin surface (issue [#815](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/815))

The eight flags above are all human-owned, but they are **not** all worth cycling
blind on a controller (MOVE to select an option, SELECT/START to nudge its value,
with only an LED pulse + voice line as feedback). Deep per-mode knobs that are
frozen at start and rarely changed at party time are awkward and error-prone on
that surface, so #815 trimmed the **controller-cycled** list to the human
essentials. **Every flag still exists in the `game` set**; the moved ones change
through a reviewed edit of the baseline `services/flagd/game.json` plus
`scripts/flags.sh restart` (they are not stack-mutable in the flag service's
registry) — only the controller-cycle UI shrinks.

| Flag | Decision | Rationale |
|---|---|---|
| `sensitivity` | **KEEP cycled** | Baseline calibration the admin tunes by feel; the primary human knob. |
| `num_teams` | **KEEP cycled** | Structural pre-game choice; quick to set on the controller. |
| `force_all_start` | **KEEP cycled** | Live-evaluated at force start (`admin.py` `handle_force_start`); human-essential. |
| `random_assignment` | **MOVE → flag-only** | Folds into game-mode selection; baseline file only. |
| `nonstop.time_limit_seconds` | **MOVE → flag-only** | Per-mode knob, frozen at start; baseline file only. |
| `invincibility_seconds` | **MOVE → flag-only** | Per-mode knob overlapping agent `shield_seconds` (§4.2); baseline file only. |
| `fight_club.min_rounds` | **MOVE → flag-only** | Per-mode knob, rarely changed at party time; baseline file only. |
| `werewolf.reveal_time_seconds` | **MOVE → flag-only** | Per-mode knob, frozen at start; baseline file only. |

The moved flags keep their definitions in
[`services/flagd/game.json`](../services/flagd/game.json) and are still read at
game start by [`services/menu/servicer.py`](../services/menu/servicer.py)
`_build_game_config` (`random_assignment`, `nonstop.time_limit_seconds`,
`invincibility_seconds`, `fight_club.min_rounds`, `werewolf.reveal_time_seconds`).
The controller cycle is defined by `option_names` / `option_colors` in
[`services/menu/handlers/admin.py`](../services/menu/handlers/admin.py).

The agent's live intervention surface (all in the `interventions` set, game-scoped, bounded by the flag service's registry):

| Parameter | Shape | Runtime target | Composition |
|---|---|---|---|
| `global_sensitivity_override` | state (int 0-4, −1=none) | mutates `game.sensitivity` | §4.1 — collides with human baseline |
| `player_sensitivity_factor` | per-serial state (0.5–2.0) | `player.sensitivity_factor` | Multiplies into thresholds; agent-only, no human analogue |
| `global_difficulty_factor` | state (0.5–2.0) | `game.global_difficulty_factor` | Multiplies into thresholds; agent-only |
| `shield_seconds` | per-serial state (>0) | extends `player.grace_until` | §4.2 — adjacent to human `invincibility_seconds` |
| `music_tempo_override` | state (float) | `game.tempo_override` | Agent-only; no human lever |
| `pacing_profile` | state (preset name) | `game.music_windows` | Agent-only; reverts to init windows |
| `volume_override` | state (float) | audio volume | Agent-only at runtime; human owns the `user` set baseline volumes |
| `eliminate_player` / `revive_player` / `audio_cue` / `controller_effect` / `end_game` | one-shot event (per-game event list) | one-shot through native paths | Agent-only; no persisted state |

## 4. Composition Rules (the spec for #816 / #817)

These are the only two parameters both actors touch. Each gets a defined,
implementable composition rule.

### 4.1 Sensitivity (`#816`)

**Runtime state.** `game.sensitivity` (a `Sensitivity` enum 0–4) is the single
value `_compute_effective_thresholds`
([`base.py:1187`](../services/game_coordinator/games/base.py)) reads every frame.
The human baseline is frozen into it at init, and `game.configured_sensitivity`
preserves that baseline ([`base.py:559-561`](../services/game_coordinator/games/base.py)).
The agent's `global_sensitivity_override` handler
([`difficulty_handlers.py:handle_global_sensitivity_override`](../services/game_coordinator/difficulty_handlers.py))
**overwrites `game.sensitivity` in place**; on clear (value −1) it restores
`game.configured_sensitivity`.

**The bug today.** `configured_sensitivity` captures the **game-start** value, not
the admin's *current* setting. So:

- A mid-game admin sensitivity change no-ops (frozen), while the agent's override
  works — an asymmetry that reads as "admin mode is broken".
- When the agent clears its override, it restores the start-of-game level, which
  may differ from what the admin most recently set.

**Ratified rule.**

1. The human baseline is **authoritative** and is the value the agent's override
   composes *on top of*, never erases. The agent override is a **transient delta**,
   not a new baseline.
2. **Restore-on-clear targets the live human baseline.** When the agent clears
   `global_sensitivity_override`, the game must restore the **current** human
   baseline — i.e. re-read the `sensitivity` flag of the `game` set (live, by
   `game_id`) rather than replaying the frozen `configured_sensitivity`. If flagd
   is unreachable, fall back to `configured_sensitivity` (fail-safe to the
   known-good start value).
3. **Invalidation (see §5).** A human baseline change while an agent override is
   active **cancels the active override** for `sensitivity`: the new human value
   becomes effective immediately and the agent's override is dropped (the agent
   may re-propose against the new baseline on its next decision).

   > Implementation note for #816: because `sensitivity` is otherwise init-frozen,
   > honoring a *live* admin change is itself a behavior change. The minimal,
   > scoped version is to make the **clear path** and the **invalidation path**
   > re-read the live flag; promoting `sensitivity` to a fully live-evaluated
   > admin flag is a larger change and out of scope unless #815 elects to do it.

4. The `player_sensitivity_factor` and `global_difficulty_factor` levers are pure
   **multiplicative deltas** in `_compute_effective_thresholds`
   ([`base.py:1206-1211`](../services/game_coordinator/games/base.py), clamped to
   [0.5, 2.0]) and do **not** mutate the human-owned `game.sensitivity`. They are
   the **preferred** agent levers precisely because they compose without
   contention. #816 should prefer steering the agent toward factors over the
   in-place `global_sensitivity_override` where the mode allows.

### 4.2 Grace / Invincibility / Shield (`#817`)

There are **three** distinct grace mechanisms, and they must not be conflated:

| Mechanism | Owner | State field | Set by |
|---|---|---|---|
| Spawn / post-death grace | Game logic | `player.grace_until` (`BaseGameMode`) | Death respawn, spawn protection |
| Round-start invincibility | Human baseline | `player.invincible_until` (Fight Club / Tournament only) | `invincibility_seconds` → `_invincibility_duration`, applied at round/match start ([`fight_club.py:256`](../services/game_coordinator/games/fight_club.py), [`tournament.py:444`](../services/game_coordinator/games/tournament.py)) |
| Agent shield | Agent | `player.grace_until` (`BaseGameMode`) | `shield_seconds` → `grant_shield` ([`base.py:1503`](../services/game_coordinator/games/base.py)) |

**The collision.** Human invincibility uses a **mode-local** field
(`invincible_until`, checked independently in each mode's kill path), while the
agent shield extends the **mode-agnostic** `grace_until` on the base class.
**Today these are not composed:** Fight Club and Tournament kill paths check
*only* `invincible_until` ([`fight_club.py:410`](../services/game_coordinator/games/fight_club.py)/[`:673`](../services/game_coordinator/games/fight_club.py), [`tournament.py:666`](../services/game_coordinator/games/tournament.py)) — an
agent shield (`grace_until`) is **not** honored in those modes' kill checks,
while base-class modes consult `grace_until` only. There is no single composed
grace window, and protection depends on which check the kill path happens to
run, with no defined precedence.

**Ratified rule.**

1. **Effective grace = the max of all active grace sources.** A player is
   protected while `now < max(grace_until, invincible_until)`. Protection is
   **monotonic-extend, never-shorten**: no source may pull in a longer protection
   already granted by another. `grant_shield` already implements extend-not-shorten
   for `grace_until` ([`base.py:1543-1551`](../services/game_coordinator/games/base.py));
   #817 must make the composition explicit so a shield and a round-start
   invincibility do not clobber each other.
2. **Human invincibility is the floor; the agent shield is additive on top.** The
   agent may **extend** a player's protection beyond the human-configured
   invincibility window but may **not reduce it below** the human baseline. The
   human's `invincibility_seconds` is the guaranteed minimum at round start; the
   shield can only lengthen the protected window.
3. **The agent shield never shortens or revokes human invincibility.** Reverting
   `shield_seconds` to neutral (0) is a **no-op** — existing protection simply
   expires naturally ([`lifecycle_handlers.py:handle_shield_seconds`](../services/game_coordinator/lifecycle_handlers.py)).
   It must never reach in and clear `invincible_until`.
4. **Implementation seam for #817.** Unify the kill-path check behind one helper
   (e.g. `is_protected(player, now)` returning `now < max(grace_until,
   invincible_until)`) so every mode composes the same way, and route both the
   human round-start path and the agent shield through extend-not-shorten writes.
   This removes the per-mode divergence without changing the human's guaranteed
   floor.

## 5. The Invalidation Rule

> **A human baseline change invalidates active agent overrides for that
> parameter.**

The human is the source of truth for intent. When the admin changes a baseline
that the agent is currently overriding, the human's new value wins **immediately**
and the agent's active override for *that parameter* is dropped:

- The agent does **not** get to keep an override pinned over a fresh human
  decision. (Otherwise the admin's deliberate change would silently no-op while
  the agent's transient delta persisted — the exact asymmetry #816 fixes.)
- Invalidation is **per-parameter**, not global: changing `sensitivity` cancels an
  active `global_sensitivity_override` but leaves an active `shield_seconds`
  untouched.
- After invalidation the agent is free to **re-propose** against the new baseline
  on its next decision cycle; it is not permanently locked out. The human change is
  a reset of the baseline the agent composes against, not a veto of the agent.
- For the (currently init-frozen) admin flags, "honor a live baseline change"
  reduces in practice to: on the agent's clear/restore path, read the **current**
  flag, not the start-of-game snapshot (see §4.1 rule 2). #816 implements this for
  `sensitivity`; the same shape applies to any future contested flag.

## 6. Visibility & Kill-Switch Contract (the spec for #818 / #819)

The model above is incomplete without a human feedback loop and an on-device
control. Today the human gets **zero signal** when the agent acts, and **no
on-device way** to constrain it (`interventions_allowed` lives in the `agent`
set, which admin mode could not touch before #819). These two gaps are #818 and #819.

### 6.1 Visibility (`#818`)

The human admin MUST be able to tell agent activity apart from a malfunction.
A conforming implementation must provide, at minimum:

- A **distinguishable signal** when the agent applies an in-game delta — an LED
  pattern / audio cue / dashboard indicator that is clearly *agent action*, not a
  game event and not "admin mode is broken".
- The signal must identify **what** changed at the granularity of the parameter
  table in §3 (e.g. "agent raised difficulty", "agent shielded player X"), so the
  admin can reconcile observed behavior with their baseline.
- Agent actions are already audited as spans (`agent.decision`,
  intervention events) and metrics (`game_interventions_total`); #818 is the
  *human-facing*, on-device surfacing of that same activity, not new instrumentation.

**Implemented (#818).** Two surfaces:

1. **Truthful admin display.** Admin mode resolves option values through the
   live flagd "game" client and maps the resolved value back to a variant name
   (`AdminModeHandler._effective_variant`,
   [`services/menu/handlers/admin.py`](../services/menu/handlers/admin.py)), so an
   operator cycling `sensitivity` sees the value the game is *actually* using
   (including an active agent `global_sensitivity_override`), not the baseline
   `defaultVariant`. Falls back to the flag service's effective default variant
   when flagd is unreachable or the resolved value matches no variant.
2. **On-device cue.** When the coordinator applies a **HARD-class** intervention
   (`spec.weight >= WEIGHT_HARD` — `adjust_global_sensitivity`,
   `eliminate_player`, `revive_player`; not `end_game`, whose game has just
   ended, UC-040 BR-018), it emits a short magenta
   `GAME_EFFECT_PULSE` on the affected controller(s) (broadcast for non-targeted
   actions) via `InterventionManager._emit_agent_cue`
   ([`services/game_coordinator/interventions.py`](../services/game_coordinator/interventions.py)).
   This is gated behind the **`agent_intervention_cue`** toggle in the `user` set
   (default ON; CI default OFF) so it can be muted for demos, and is best-effort
   (failures never break the intervention path). MEDIUM/SOFT interventions are
   intentionally silent to avoid disrupting gameplay.

### 6.2 Kill-switch / policy control (`#819`)

The admin MUST have an **on-device** way to constrain the agent without a
terminal (`scripts/flags.sh`) or a baseline edit. A conforming implementation must let the admin:

- **Disable the agent entirely** for the current deployment (a hard kill-switch —
  the agent applies no deltas), and re-enable it.
- Optionally **scope** what the agent may do, mapping onto the existing
  `interventions_allowed` allow-list and `policy.*` budgets that the coordinator
  already enforces (the enforcement chain — allow-list → mode matrix → battery
  guard → rate limit — lives in
  [`interventions.py:_check_chain`](../services/game_coordinator/interventions.py),
  ~lines 1023-1050; it gates the *agent* only, never the human).
- The control must be **load-bearing at the gate**: flipping the kill-switch off
  means the coordinator's allow-list rejects every agent intervention, so the
  switch degrades safely even if the agent process ignores it.

> Note: the existing guardrails (`interventions.py`: allow-list,
> mode-capability matrix, battery guard, weighted rate-limit backstop) all
> constrain the **agent**. Nothing today governs the **human** path — the admin
> write path has no policy layer, by design (the human is trusted). #819 adds the
> human's *control over the agent*, not a constraint on the human.

**Implemented (#819).** Admin mode gains a new controller-cycled option,
`agent_control` (MOVE to select it, SELECT/START to change), that cycles the
GLOBAL `interventions_allowed` policy in the `agent` set through the levels
`none → ambient → standard → full` and back
(`AdminModeHandler.handle_agent_control`,
[`services/menu/handlers/admin.py`](../services/menu/handlers/admin.py)). `none`
is the hard panic-off: it is **load-bearing at the gate** because the
coordinator's allow-list (`_check_chain`, step (a)) then rejects every agent
intervention regardless of what the agent process does. Each level has a distinct
LED color (red = off / Turquoise / Orange / green = full) and a voice line.

- **Write-ownership decision (recorded).** The menu writes the single
  `interventions_allowed` key of the `agent` set through the flag service with a
  dedicated `FlagServiceWriter` for the `agent` set
  (`MenuServicer.agent_settings_writer`), as a persisted admin value
  (`PUT /agent/interventions_allowed` with `"persist": true`), so the level
  survives a restart; the agent process never writes the `agent` set. The write
  surfaces stay disjoint (§2), so no real-resolution invariant is touched — this
  is global policy, read by the agent and coordinator with a bare
  (non-game-scoped) `EvaluationContext`.
- **Live.** The flag service pushes the write to flagd over the sync stream
  (NFR-050: p95 at most 200 ms) and the agent re-reads `interventions_allowed`
  live per decision, so the change takes effect without a restart.

## 7. Source Map

| Concern | File |
|---|---|
| Human write path | [`services/menu/handlers/admin.py`](../services/menu/handlers/admin.py), [`lib/flag_service_writer.py`](../lib/flag_service_writer.py) → flag service ([`services/flag-service/`](../services/flag-service/)) |
| Baseline → game start | [`services/menu/servicer.py`](../services/menu/servicer.py) (`_build_game_config`), [`services/game_coordinator/game_factory.py`](../services/game_coordinator/game_factory.py) |
| Baseline freeze | [`services/game_coordinator/games/base.py`](../services/game_coordinator/games/base.py) (`__init__`, ~490-580) |
| Agent write path | [`services/agent/actions/writer.go`](../services/agent/actions/writer.go) |
| Agent guardrails | [`services/game_coordinator/interventions.py`](../services/game_coordinator/interventions.py) |
| Sensitivity contention | [`services/game_coordinator/difficulty_handlers.py`](../services/game_coordinator/difficulty_handlers.py), [`base.py` `_compute_effective_thresholds`](../services/game_coordinator/games/base.py) |
| Grace / shield | [`services/game_coordinator/lifecycle_handlers.py`](../services/game_coordinator/lifecycle_handlers.py), [`base.py` `grant_shield`](../services/game_coordinator/games/base.py), [`fight_club.py`](../services/game_coordinator/games/fight_club.py), [`tournament.py`](../services/game_coordinator/games/tournament.py) |
| Flag domains | read-only baseline [`services/flagd/`](../services/flagd/) (`game.json`, `user.json`, `interventions.json`, `agent.json`); mutable-flag registry [`services/flag-service/registry.yaml`](../services/flag-service/registry.yaml) ([flags.md §13](specs/flags.md#13-mutable-flag-registry)) |

## See Also

- [Architecture](ARCHITECTURE.md)
- [Intervention Surface research (#722)](research/722-intervention-surface.md)
- [Agent README](../services/agent/README.md)
- [Feature flags](feature-flags.md)
