// Package journal is the agent's durable experiment journal (#983, epic #982):
// the THIRD state class in the M7 model. Distinct from services/flagd/agent.json
// (`:ro` governance the agent only reads) and services/flagd/game.json (the
// control plane the agent writes), the journal is the agent's OWN working memory
// — the agent both writes and reads it, and nothing else governs it (design §9).
//
// An experiment is a long-lived, evolving entity. This package defines HOW its
// goal + progression + outcome are stored WITHOUT unbounded context growth, in a
// three-part record per experiment (design §9.1):
//
//	(a) INTENT — immutable. Written once at creation: the hypothesis + full
//	    experiment config. Never rewritten. On disk: intent.json.
//	(b) EVENT LOG — append-only. One JSON object per material event, in order,
//	    never rewritten. The audit trail and the ONLY place raw per-game fitness
//	    samples are retained. On disk: events.jsonl.
//	(c) ROLLING SUMMARY — rewritten atomically each conclusion. Per-arm
//	    count/mean/variance via Welford (§8.2) plus the current verdict. O(1) size
//	    in N. The compact view the live registry and the LLM read. On disk:
//	    summary.json.
//
// Bounded-LLM-context discipline (§9.2, load-bearing): a caller building an LLM
// prompt reads CompactView — intent + the rolling per-arm aggregates + a SHORT
// recent tail — never the full game-by-game log, so prompt size stays roughly
// constant as N grows from 1 to hundreds of games.
//
// Durability (§9.3): the journal is authoritative on a local mounted volume and
// survives agent restart — the live in-memory state is a VIEW rebuilt from disk
// via Load (the #831 in-memory-loss fix). Telemetry is a view, never the source
// of truth. Persistence reuses the proven gamesummary pattern (atomic temp +
// fsync + rename, env-overridable dir).
//
// Scope (this issue): STORAGE + STATE machinery ONLY. It does NOT wire into the
// registry (#978), the aggregator (#979), or emit telemetry (#981); those consume
// this package later. Decisions baked in (epic #982, 2026-06-13): journal backend
// is JSONL + JSON (NOT SQLite); statistics are per-arm Welford running stats.
package journal

import "time"

// SchemaVersion is bumped when the on-disk intent.json / summary.json shape
// changes in a way a reader must notice. Carried on both records so the registry
// (#978) and offline tooling can branch on it rather than guess. Mirrors
// gamesummary.SchemaVersion.
const SchemaVersion = 1

// Intent is the immutable goal of an experiment (design §9.4a, intent.json).
// Written exactly once by Create and never rewritten — the audit record of WHAT
// the agent tried and WHY. Field names are stable wire contract.
type Intent struct {
	// SchemaVersion is SchemaVersion at write time.
	SchemaVersion int `json:"schema_version"`
	// ExperimentID is the stable id, e.g. "exp_a1b2c3d4e5f6". It is the journal
	// directory name (sanitized) and the join key for every event and the summary.
	ExperimentID string `json:"experiment_id"`
	// CreatedAt is when the experiment was proposed (injected, never read off a
	// clock here, so Create is deterministic for tests).
	CreatedAt time.Time `json:"created_at"`
	// Hypothesis is the agent's reasoning: what change + why it might help.
	Hypothesis string `json:"hypothesis"`
	// FlagKey is the game flag under experiment (e.g. "death_grace_period_seconds").
	FlagKey string `json:"flag_key"`
	// ExperimentalValue is the candidate value the experimental arm resolves. Any
	// JSON scalar/object; stored verbatim for the audit trail and later attribution.
	ExperimentalValue any `json:"experimental_value"`
	// Objective is the fitness objective being optimized (one of "endurance"/"balanced"/"accelerate"; chaos is rejected at Declare, #1015).
	Objective string `json:"objective"`
	// TargetNPerArm is the minimum games per arm before a verdict can be conclusive.
	TargetNPerArm int `json:"target_n_per_arm"`
	// Arms are the arm names, e.g. ["experimental", "control"]. The summary keeps
	// one Welford accumulator per arm; an event for an unknown arm is recorded in
	// the log but folds into no accumulator (the log stays the complete audit trail).
	Arms []string `json:"arms"`
}

// EventKind enumerates the material events in the append-only log (design §9.1b).
// The set is closed so offline replay / dashboards can enumerate every kind.
type EventKind string

const (
	// KindGameAssigned — a shadow game was bound to an arm (spawn binding, §5).
	KindGameAssigned EventKind = "game_assigned"
	// KindGameConcluded — a game finished and carries its fitness sample. This is
	// the ONLY event that folds into an arm's Welford accumulator.
	KindGameConcluded EventKind = "game_concluded"
	// KindGameReleased — a shadow game ended WITHOUT a usable fitness sample
	// (game_error / timeout / abort) so its in-flight slot was freed WITHOUT
	// folding a sample (#1014). It is a NON-COUNTING release: it never touches an
	// arm's Welford accumulator, so a transient game failure cannot pollute the
	// cohort with a fabricated 0-fitness sample. It is recorded purely for the
	// audit trail (why a bound game vanished without a conclusion).
	KindGameReleased EventKind = "game_released"
	// KindInterimVerdict — the rolling verdict recorded after a conclusion.
	KindInterimVerdict EventKind = "interim_verdict"
	// KindDecision — a status transition (continue/stop/promote).
	KindDecision EventKind = "decision"
	// KindOutcome — the terminal result (promoted/rejected/inconclusive).
	KindOutcome EventKind = "outcome"
)

// Event is one entry in the append-only log (design §9.4b, one JSON object per
// line in events.jsonl). It is a UNION across event kinds: Seq/At/Kind are always
// set; the remaining fields are populated per kind (omitempty keeps each line
// minimal and the kind self-describing). Never rewritten once appended.
type Event struct {
	// Seq is the 1-based monotonic sequence number assigned by the journal. It is
	// the append order and the count of events; a gap would signal a lost write.
	Seq int `json:"seq"`
	// At is the event timestamp (injected by the caller, not read off a clock here).
	At time.Time `json:"at"`
	// Kind is the event kind (see EventKind).
	Kind EventKind `json:"kind"`

	// GameID is the game this event concerns (game_assigned / game_concluded). "" otherwise.
	GameID string `json:"game_id,omitempty"`
	// Arm is the arm the game belongs to (game_assigned / game_concluded). "" otherwise.
	Arm string `json:"arm,omitempty"`
	// PairIndex is the Common-Random-Numbers pair this game belongs to (#1003): the
	// per-experiment monotonic index whose two games (one experimental, one control)
	// share an RNG seed. Stamped on game_assigned so the paired-difference verdict
	// (#1004) can recover which two games form a pair. A pointer so pair 0 is
	// distinguishable from "not a paired event" and survives a rehydrate.
	PairIndex *int `json:"pair_index,omitempty"`
	// Fitness is the game's fitness sample (game_concluded only). Folded into the
	// arm's Welford accumulator. A pointer so a genuine 0.0 sample is distinct from
	// "no fitness on this event kind" and is preserved across a rehydrate.
	Fitness *float64 `json:"fitness,omitempty"`
	// DurationS is the game's wall-clock duration in seconds (game_concluded),
	// retained for the audit trail; not folded into statistics.
	DurationS float64 `json:"duration_s,omitempty"`

	// Verdict is the rolling verdict carried on an interim_verdict or outcome event.
	Verdict *Verdict `json:"verdict,omitempty"`
	// Status is the new status on a decision event (e.g. "running", "concluded").
	Status string `json:"status,omitempty"`
	// Note is a short free-text annotation for any event (optional audit context).
	Note string `json:"note,omitempty"`
}

// ArmStat is the per-arm rolling sufficient statistics (design §9.4c). It embeds
// the Welford accumulator so count/mean/M2 persist exactly across a rehydrate.
type ArmStat struct {
	// Welford carries count, mean, and M2 (variance is derived).
	Welford
}

// Verdict is the current comparison verdict carried on the rolling summary and on
// interim_verdict/outcome events. This package does NOT compute significance — the
// aggregator (#979) supplies the verdict; the journal only stores it (it stays the
// agent's working memory, not the statistics engine). Fields mirror design §9.4c.
type Verdict struct {
	// Outcome is the verdict label ("inconclusive" / "promote" / "discard"), kept a
	// plain string so the journal does not depend on the experiment package's
	// Outcome enum and can store labels it does not interpret.
	Outcome string `json:"outcome"`
	// Delta is experimental mean minus control mean (or whatever the aggregator
	// computes); informational here.
	Delta float64 `json:"delta"`
	// Significant is whether the verdict cleared the significance bar.
	Significant bool `json:"significant"`
	// Reason is a short human-readable explanation (e.g. "n_per_arm < target (20)").
	Reason string `json:"reason,omitempty"`
}

// Summary is the rolling sufficient-statistics snapshot (design §9.4c,
// summary.json). Rewritten atomically by the journal after each conclusion. O(1)
// size in N: one ArmStat per arm regardless of how many games each arm has seen.
type Summary struct {
	// SchemaVersion is SchemaVersion at write time.
	SchemaVersion int `json:"schema_version"`
	// ExperimentID joins the summary to its intent and events.
	ExperimentID string `json:"experiment_id"`
	// Status is the experiment lifecycle status ("running", "concluded", ...). It is
	// set from decision/outcome events; defaults to "running" at Create.
	Status string `json:"status"`
	// UpdatedAt is the timestamp of the most recent fold (the At of the last event,
	// so the summary's freshness tracks the log without reading a clock).
	UpdatedAt time.Time `json:"updated_at"`
	// EventCount is the number of events folded so far (the last Seq). Lets a reader
	// verify the summary is in sync with events.jsonl after a rehydrate.
	EventCount int `json:"event_count"`
	// Arms is the per-arm rolling statistics, keyed by arm name. Seeded with every
	// arm from Intent.Arms at Create so a never-played arm reports count 0.
	Arms map[string]*ArmStat `json:"arms"`
	// Verdict is the current rolling verdict, or nil before any has been recorded.
	Verdict *Verdict `json:"verdict,omitempty"`
}
