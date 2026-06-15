package decision

import (
	"sync"
	"time"
)

// This file is the UNIFIED weighted rate limiter for the agent (reconciliation
// of #726's decision/limiter.go windowLimiter and #728's decision/ratelimit.go
// rateLimiter — the redundant limiter.go was deleted). The two limiters were
// the same weighted sliding-window budget; they are merged here as follows:
//
//   - Semantics & API are #728's: a `rateLimiter` with `allow(now, cost, budget)`
//     over a trailing one-minute window, budget <= 0 fully closed, weights
//     soft 0.5 / medium 1 / hard 2 per docs/research/722-intervention-surface.md §5.
//   - Concurrency safety is #726's: the limiter is now mutex-guarded, because it
//     is shared across the concurrent OTLP Export handler goroutines that drive
//     OnEvaluate. (#728's version was only ever touched from the single decision
//     loop and so was unsynchronized.)
//   - Charging point moved from the engine (#726, which charged every *emitted*
//     decision because it could not see permissions) to the loop's permission
//     chain (#728): the budget is now spent only on decisions that pass the
//     allow-list and battery gates AND fit — a strict improvement, since the
//     unified loop *can* see permissions. The engine no longer rate-limits.
//   - Unknown-intervention cost is #728's medium (1.0). #726 used the hard cost
//     (2.0) as a fail-safe; the medium default is kept for parity with #728 and
//     because the game-coordinator flag-application layer (#730) remains a
//     generous GLOBAL backstop that catches anything mis-weighted here.
//
// Budget-ownership contract (#919): THIS limiter is the AUTHORITATIVE PER-GAME
// governor. Each game_id gets its own *Loop with its own rateLimiter (see
// loopset.go), so one game exhausting policy.max_interventions_per_minute never
// starves another. The game-coordinator's _RateLimiter
// (services/game_coordinator/interventions.py) is NOT a second per-game governor:
// it is a single, deliberately generous, process-global BACKSTOP keyed off a
// SEPARATE flag (policy.coordinator_backstop_per_minute) that only trips on a
// runaway agent. The two layers read different flags on purpose so they cannot
// silently drift (bug classes #800/#848).

// Intervention identifiers — exactly the interventions_allowed vocabulary from
// the flagd agent schema (services/flagd/agent.json) and the #722 research §6.
// Used by the rules engine (ruleset.go) and the weight table below.
const (
	InterventionPlayAudioCue            = "play_audio_cue"
	InterventionSendControllerEffect    = "send_controller_effect"
	InterventionAdjustVolume            = "adjust_volume"
	InterventionAdjustMusicTempo        = "adjust_music_tempo"
	InterventionAdjustPlayerSensitivity = "adjust_player_sensitivity"
	// InterventionSetPlayerHandicap (#1107, #1103 MVP action 1) is a per-player
	// MULTIPLICATIVE handicap on the death threshold that composes with — does not
	// replace — adjust_player_sensitivity (>1 harder to die / "help", <1 easier /
	// "rein in"). SHADOW-game-only initially: allow-listed only via the
	// `shadow_experimental` interventions_allowed variant (services/flagd/agent.json),
	// never the real-facing ambient/standard/full variants.
	InterventionSetPlayerHandicap       = "set_player_handicap"
	InterventionGrantShield             = "grant_shield"
	InterventionAdjustGlobalSensitivity = "adjust_global_sensitivity"
	InterventionAdjustGlobalDifficulty  = "adjust_global_difficulty"
	InterventionSetPacingProfile        = "set_pacing_profile"
	// InterventionRampTempo (#1117, #1103 MVP action 2) is a session-scoped
	// scheduled tempo CURVE ("<target>:<seconds>:<curve>") that fills the #1114
	// agent-mode tempo seam — one budget charge buys a ramp instead of re-emitting
	// discrete adjust_music_tempo steps. SHADOW-game-only initially: allow-listed
	// only via the `shadow_experimental` interventions_allowed variant
	// (services/flagd/agent.json), never the real-facing ambient/standard/full.
	InterventionRampTempo       = "ramp_tempo"
	InterventionEliminatePlayer = "eliminate_player"
	InterventionRevivePlayer    = "revive_player"
	InterventionEndGame         = "end_game"

	// InterventionNoop is the probe-mode synthetic intervention (ProbeRules). It
	// carries no game effect: when dispatched through the action sink it is a
	// no-op success, so probe mode exercises the full OBSERVE→DECIDE→ACT path
	// (including the action span) harmlessly. It is allow-listed only via the
	// `probe` variant of interventions_allowed (services/flagd/agent.json).
	InterventionNoop = "noop"
)

// interventionWeights is the cost of each intervention against the per-minute
// budget. The authoritative table lives in
// docs/research/722-intervention-surface.md §5:
//
//	soft   (psychological, self-reverting) = 0.5
//	medium (tunable, reversible)           = 1
//	hard   (state-changing, terminal)      = 2
//
// An intervention not in the table is treated as medium (1) — a safe middle
// cost so an unknown action still draws down the budget.
var interventionWeights = map[string]float64{
	// Soft (0.5)
	InterventionPlayAudioCue:         0.5,
	InterventionSendControllerEffect: 0.5,
	InterventionAdjustVolume:         0.5,
	// Medium (1)
	InterventionAdjustMusicTempo:        1,
	InterventionAdjustPlayerSensitivity: 1,
	InterventionSetPlayerHandicap:       1, // #1107 (#1103 MVP action 1)
	InterventionGrantShield:             1,
	InterventionAdjustGlobalDifficulty:  1, // #766 F6
	InterventionSetPacingProfile:        1, // #766 F6
	InterventionRampTempo:               1, // #1117 (#1103 MVP action 2)
	// Hard (2)
	InterventionAdjustGlobalSensitivity: 2,
	InterventionEliminatePlayer:         2,
	InterventionRevivePlayer:            2,
	InterventionEndGame:                 2,
}

// defaultInterventionWeight is charged for interventions absent from the table.
const defaultInterventionWeight = 1.0

// interventionCost returns the budget cost (weight) of an intervention.
func interventionCost(intervention string) float64 {
	if w, ok := interventionWeights[intervention]; ok {
		return w
	}
	return defaultInterventionWeight
}

// costOf is the rules engine's name for interventionCost: the engine uses the
// weight as a deterministic tie-break (cheaper/safer decisions sort first), even
// though it no longer enforces the budget itself (the loop does).
func costOf(intervention string) float64 { return interventionCost(intervention) }

// rateWindow is the trailing window over which the weighted budget applies.
const rateWindow = time.Minute

// rateLimiter is the unified weighted sliding-window budget over dispatched
// interventions. It admits interventions until the summed weight within the
// trailing one-minute window would exceed the budget, then blocks (does not
// queue) further ones. Older entries are pruned on each call, so the budget
// replenishes as time passes. Safe for concurrent use.
type rateLimiter struct {
	mu      sync.Mutex
	entries []rateEntry
}

type rateEntry struct {
	at     time.Time
	weight float64
}

// allow reports whether an intervention of the given weight fits within budget
// at time now. When it fits, the entry is recorded and true is returned; when it
// would exceed the budget, nothing is recorded and false is returned (blocked).
// A budget <= 0 admits nothing (fully closed). cost is the intervention's weight.
func (r *rateLimiter) allow(now time.Time, cost, budget float64) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.prune(now)
	if budget <= 0 {
		return false
	}
	if r.used()+cost > budget {
		return false
	}
	r.entries = append(r.entries, rateEntry{at: now, weight: cost})
	return true
}

// used returns the summed weight of all entries currently in the window. Callers
// hold r.mu and must prune first (allow does).
func (r *rateLimiter) used() float64 {
	var sum float64
	for _, e := range r.entries {
		sum += e.weight
	}
	return sum
}

// prune drops entries older than the trailing window ending at now. Caller holds
// r.mu.
func (r *rateLimiter) prune(now time.Time) {
	cutoff := now.Add(-rateWindow)
	kept := r.entries[:0]
	for _, e := range r.entries {
		if e.at.After(cutoff) {
			kept = append(kept, e)
		}
	}
	r.entries = kept
}
