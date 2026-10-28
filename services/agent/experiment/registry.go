package experiment

import (
	"context"
	"errors"
	"fmt"
	"hash/fnv"
	"log/slog"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/patterngame"
	"github.com/joustmania/agent/telemetry/keys"
)

// registry.go is the agent-owned experiment COORDINATOR (design §7, #978). It is
// the experiment-timescale counterpart to the per-game LoopSet/Multiplexer
// (#845): where those manage one decision loop/store per live game, the Registry
// manages one entry per live EXPERIMENT. It mints experiment ids, holds each
// experiment's intent + status, allocates finite shadow capacity across the live
// set, decides (experiment_id, arm) at spawn, folds concluded games into the
// journal, drives the verdict + status machine, and tears the experiment down on
// a terminal status.
//
// SOURCE OF TRUTH: the durable journal (#983). The in-memory Registry is a VIEW
// rebuilt from disk on startup via Rehydrate (the #831 in-memory-loss fix) — the
// journal's append-only event log + rolling summary are authoritative; the
// Registry never holds state the journal does not.
//
// SEAMS (registry_seams.go): every consequential effect is injected —
// ShadowSpawner (#976), CohortVerdict (#979), Promoter (#961/#980),
// TargetingWriter (#977). The Registry owns ONLY the lifecycle logic, so it is
// unit-testable with recording fakes.

// Status is an experiment's lifecycle status (design §7.2). The set is closed so
// the journal/dashboards can enumerate every state.
//
//	PROPOSED ─▶ RUNNING ─▶ CONCLUDED ─▶ {PROMOTING ─▶ DONE | DISCARDED}
//	                 │
//	                 └─▶ ABORTED   (kill-switch / capacity reclaim / error)
type Status string

const (
	// StatusProposed — intent recorded; targeting not yet written; no games.
	StatusProposed Status = "proposed"
	// StatusRunning — targeting written; the experiment may accrue shadow games.
	StatusRunning Status = "running"
	// StatusConcluded — target N reached (or under-powered timeout); verdict computed.
	StatusConcluded Status = "concluded"
	// StatusPromoting — a concluded+promote experiment being routed to the Promoter.
	StatusPromoting Status = "promoting"
	// StatusDone — terminal: promotion finished; targeting torn down; capacity freed.
	StatusDone Status = "done"
	// StatusDiscarded — terminal: concluded without promotion; torn down.
	StatusDiscarded Status = "discarded"
	// StatusAborted — terminal: kill-switch / capacity reclaim / error; torn down.
	StatusAborted Status = "aborted"
)

// AbortReasonStartFailed is the abort reason of an experiment whose start failed
// (UC-043 A8, UC-042 A4): it never stays PROPOSED holding its flag (#1700).
const AbortReasonStartFailed = "start_failed"

// AbortReasonPromotionInterrupted is the abort reason of an experiment restored
// as promoting with no journaled promotion event (UC-043 A15, #2042): what its
// promotion did is unknown, so it is never promoted again.
const AbortReasonPromotionInterrupted = "promotion_interrupted"

// IsTerminal reports whether a status is terminal (the experiment is finished and
// its capacity + targeting have been released).
func (s Status) IsTerminal() bool {
	return s == StatusDone || s == StatusDiscarded || s == StatusAborted
}

// IsLive reports whether the experiment still occupies a registry slot and may be
// allocated shadow capacity (PROPOSED/RUNNING — concluded experiments hold their
// game map for the verdict but are not allocated new capacity).
func (s Status) IsLive() bool {
	return s == StatusProposed || s == StatusRunning
}

// DefaultMaxShadowGames is the fixed cap on concurrent shadow games across ALL
// live experiments (epic #982 decision 2: a fixed configured cap, not dynamic
// host-headroom). Not configurable (the env override is gone, #1907). 20 mirrors the issue's
// suggested default. Real games still preempt shadow at the coordinator (the
// registry does not fight that — this cap bounds only the registry's own
// in-flight shadow spawns).
const DefaultMaxShadowGames = 20

// DefaultEffectiveConcurrency is the default cap on the number of in-flight
// shadow spawns the registry will hold at once — the EFFECTIVE concurrency, as
// distinct from the registry's capacity bookkeeping (DefaultMaxShadowGames).
//
// #1018 (option b) taught the game-coordinator to run CONCURRENT shadow games:
// the single-game guard now applies to REAL games only, and shadow sessions run
// concurrently up to GAME_MAX_SHADOW_GAMES (coordinator default 4). So the
// registry can keep multiple shadow starts in flight without each over-spawn
// being a doomed "Game already in progress" start. Defaulting to 4 matches the
// coordinator's default shadow cap and makes #1003's same-tick atomic pair
// reservation activate (it needs effective concurrency >= 2 to reserve BOTH arms
// in one tick → tighter CRN pairing). 4 is a deliberately conservative >1 value
// for a single-interpreter coordinator on Pi 5-class hardware; it is well under
// the DefaultMaxShadowGames=20 registry ceiling and is clamped to it. An
// operator raises/lowers it via the experiment_shadow_effective_concurrency flag
// (this constant equals its flag default; the env fallback is gone, #1907). Backpressure
// still applies: a coordinator-at-capacity rejection is release+retry next tick.
const DefaultEffectiveConcurrency = 4

// DefaultMaxGamesPerExperiment is the per-experiment TERMINATION BUDGET (#1001):
// the absolute upper bound on games a single experiment may accrue before the
// registry gives up and concludes it INCONCLUSIVE. Without it, an experiment
// whose verdict can never reach signal (e.g. all-zero fitness ⇒ both arms
// zero-variance ⇒ undefined effect size ⇒ a permanent "inconclusive" verdict)
// spawns games forever: the live M8 dry run hit 235 games (target_n=3 exceeded
// ~70×) and never concluded. 50 is a deliberately small ceiling for cheap
// headless shadow games; tune via AGENT_EXPERIMENT_MAX_GAMES. A non-positive
// budget DISABLES this guard (the inconclusive-past-target and
// degenerate-variance guards below still guarantee termination).
const DefaultMaxGamesPerExperiment = 50

// maxGamesPerExperimentEnv overrides DefaultMaxGamesPerExperiment.
const maxGamesPerExperimentEnv = "AGENT_EXPERIMENT_MAX_GAMES"

// Arm names. The experimental arm resolves the candidate value; the control arm
// falls through to the existing default (within-experiment baseline). Both are
// game_kind="shadow" (the hard invariant). These mirror the targeting contract
// (ArmExperimental in targeting.go) and #975's lib values.
const (
	ArmControl = "control"
)

// DefaultArms is the standard two-arm split (epic #982 decision 5: a
// within-experiment shadow control arm IS the decision basis, with the recent-real
// baseline as a sanity cross-check). Round-robin allocation interleaves them.
func DefaultArms() []string { return []string{ArmExperimental, ArmControl} }

// Intent is the agent's definition of an experiment — the structured hypothesis
// the registry persists to the journal at PROPOSED. It is the registry-level
// counterpart to a Proposal (which carries only the flag write): an Intent adds
// the experiment framing (objective, arms, target N, stop criteria). Minting the
// experiment_id is the registry's job (Declare), so it is NOT a field here.
type Intent struct {
	// Hypothesis is the agent's reasoning: what change + why it might help.
	Hypothesis string
	// FlagKey is the game flag under experiment. It is the Proposal's FlagKey.
	FlagKey string
	// ExperimentalValue is the candidate value the experimental arm resolves.
	ExperimentalValue any
	// Objective is the fitness objective being optimized (one of "endurance"/"balanced"/"accelerate"; chaos is rejected at Declare, #1015).
	Objective string
	// Arms are the arm names. Empty ⇒ DefaultArms() (experimental + control).
	Arms []string
	// TargetNPerArm is the minimum games per arm before a verdict can be conclusive.
	// Non-positive ⇒ the verdict seam decides (it stays inconclusive until it does).
	TargetNPerArm int
}

// MintExperimentID returns a fresh experiment id in the "exp_<uuid hex[:12]>"
// shape, mirroring the game_<...> game-id shape (#845). Exported so a caller can
// pre-mint an id (e.g. to correlate a span) before Declare.
func MintExperimentID() string {
	return "exp_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:12]
}

// MaxGamesPerExperimentFromEnv resolves the per-experiment termination budget
// (#1001) from AGENT_EXPERIMENT_MAX_GAMES, falling back to
// DefaultMaxGamesPerExperiment on an unset, empty, or non-numeric value. A 0 or
// negative value is honored verbatim as "disable the games budget" — unlike the
// shadow-cap (where a non-positive value is misconfiguration), disabling the
// games budget is a legitimate choice because the other termination guards still
// bound the experiment.
func MaxGamesPerExperimentFromEnv() int {
	if v := strings.TrimSpace(os.Getenv(maxGamesPerExperimentEnv)); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return DefaultMaxGamesPerExperiment
}

// entry is the registry's in-memory view of one experiment. It is a cache of the
// journal: status + the (game_id → arm) map + the round-robin cursor. The journal
// remains authoritative; on restart Rehydrate rebuilds every entry from disk.
type entry struct {
	id      string
	journal *journal.Journal
	status  Status
	flagKey string
	// arms is the experiment's arm list (from intent), used for round-robin.
	arms []string
	// games maps a live (in-flight) shadow game_id → its arm. A game is removed on
	// conclusion. len(games) is the experiment's current in-flight shadow count.
	games map[string]string
	// gamePairs maps a bound (in-flight) shadow game_id → its Common-Random-Numbers
	// pair index (#1003), recorded at bind alongside the game_assigned event. It lets
	// ConcludeGame / ReleaseGame recover the pair index for the game_concluded /
	// game_released journal event so the paired-difference verdict (#1004) can match
	// the two games of a pair. Reservation placeholders are NOT tracked here (they
	// carry no real game_id yet); the entry is added in bindGame and removed when the
	// game concludes or is released. It is rebuilt by Rehydrate's empty-in-flight
	// reset just like games.
	gamePairs map[string]int
	// armCursor indexes arms for round-robin arm assignment within the experiment,
	// so an experiment fills its arms evenly (experimental, control, experimental…).
	//
	// CRN pairing (#1003): consecutive arms form (experimental, control) PAIRS. The
	// pair index is armCursor / len(arms) and the shared seed = pairSeed(id,
	// pairIndex), so both arms of a pair derive the SAME seed automatically (it only
	// changes at a pair boundary). The pair index is stamped on each game_assigned
	// journal event (Event.PairIndex) so the paired-difference verdict (#1004) can
	// recover which two games share a seed. The seed is a pure function of
	// (experimentID, pairIndex) — NEVER a clock — so it is reproducible.
	armCursor int

	// promotionInFlight is set while the experiment's promotion runs and its own
	// promotion event is not journaled yet; RecordPromotion holds later outcomes in
	// laterPromotions until then (UC-043 A17: the deferred queue may journal first).
	promotionInFlight bool
	laterPromotions   []PromotionResult
	// recommendOwner is the writer owning the real overlay when the experiment's
	// promotion was refused with 412 writer_mismatch (UC-043 A19, #2276). Non-empty
	// = every experiment.progress snapshot carries the recommendation attributes.
	recommendOwner string
	// endReason is the teardown reason of a terminal experiment; a recommendation
	// snapshot repeats it as experiment.end_reason (the dashboard reason line).
	endReason string

	// span is the long-lived experiment ROOT span (#1188, epic #1181 PR2): the
	// agent.experiment span started when the experiment becomes RUNNING (Start /
	// Rehydrate) and ended on the terminal transition (teardown). spanCtx is its
	// SpanContext, exposed via ExperimentSpanContext so the per-experiment analysis
	// spans (retro / summary, emitted from other components) can re-parent under it.
	//
	// LIFECYCLE INVARIANT (no leak): the span is started exactly once per entry (the
	// startSpanLocked guard is idempotent) and End()ed exactly once, in teardown — the
	// SINGLE terminal chokepoint every terminal transition (concluded → discarded/done,
	// aborted) routes through. A nil span (tracer unwired in tests, or never started) is
	// always safe to End/read.
	//
	// RESTART DISCONTINUITY (#1188): the SpanContext is NOT persisted. A rehydrated
	// experiment (Rehydrate, after a restart) gets a BRAND-NEW root span with a fresh
	// trace_id, so its post-restart analysis spans hang under the new root — the
	// pre-restart trace is a separate, already-closed trace. This is deliberate for this
	// cut: restoring a SpanContext across a process boundary would require persisting +
	// re-injecting trace ids the journal does not record, for a span that can no longer
	// be appended to anyway.
	span    trace.Span
	spanCtx trace.SpanContext

	// pin is the experiment's pinned archetype selection (UC-046 step 2): nil until
	// its first allocation pass resolves it; restored from the journal on rehydrate
	// (BR-015).
	pin *patterngame.Pin
	// pairReruns counts reruns per pair; pairSkipped marks archetypes given up
	// after maxPairReruns (BR-015). pairPreemptions counts the reruns after a
	// real-game preemption, which do not use up maxPairReruns (bounded by
	// maxPairPreemptions); pairStaleReruns likewise counts the reruns after a stale
	// end (UC-041 A4, bounded by maxPairStaleReruns).
	pairReruns      map[int]int
	pairPreemptions map[int]int
	pairStaleReruns map[int]int
	pairSkipped     map[int]bool
	// patternGames / patternTimeouts / patternAgentEnded count finished pattern
	// games, timeouts and agent ends (A14) per arm (BR-013 reporting).
	patternGames      map[string]int
	patternTimeouts   map[string]int
	patternAgentEnded map[string]int
}

// inFlight returns the experiment's current count of live (unconcluded) shadow
// games — the slots it occupies against the global cap.
func (e *entry) inFlight() int { return len(e.games) }

// Registry is the agent-owned experiment coordinator (design §7). Safe for
// concurrent use: a single mutex serializes the lifecycle/capacity book-keeping,
// matching the LoopSet/Multiplexer pattern. The journal handles their own
// locking; the registry holds at most ONE journal handle per experiment (the
// single-writer-per-experiment invariant the journal package requires).
type Registry struct {
	root string
	// onTerminal, when set, is told an experiment's id once it reaches a terminal
	// status (teardown), outside the registry lock. The cross-game window uses it to
	// evict the experiment's linked shadow set (UC-042 BR-005). Set before serving.
	onTerminal func(experimentID string)
	// maxCap is the registry's capacity-bookkeeping cap on total in-flight shadow
	// games across all experiments (DefaultMaxShadowGames). It governs the fair
	// per-experiment share split.
	maxCap int
	// effectiveCap bounds the number of in-flight spawns the registry actually
	// holds at once — the coordinator's real concurrency (#998). It is <= maxCap;
	// the registry stops allocating once in-flight reaches it, so it never hammers
	// the single-game coordinator with doomed concurrent starts.
	effectiveCap int
	spawner      ShadowSpawner
	verdict      CohortVerdict
	promo        Promoter
	target       TargetingWriter
	clock        func() time.Time
	log          *slog.Logger
	// tracer emits the long-lived agent.experiment ROOT span (#1188). nil-safe like
	// the #1142 experiment_loop tracer: a nil tracer disables the root span entirely
	// (every start/end/read is guarded), so the registry stays fully functional in
	// unit tests that do not wire telemetry. Production injects otel.Tracer(...).
	tracer trace.Tracer

	// maxGames is the per-experiment termination budget (#1001): the registry gives
	// up and concludes an experiment INCONCLUSIVE once it has accrued this many
	// concluded games. <= 0 disables the games-budget guard (the other guards still
	// guarantee termination).
	maxGames int
	// minN is the verdict min-N-per-arm gate, mirrored here ONLY for two
	// registry-side concerns: reconciling a too-small Intent.TargetNPerArm up to it
	// (so target_n < min-N cannot cause a perpetual under-powered loop), and the
	// "inconclusive past target N" termination guard. It starts at the same
	// compiled default the verdict starts at and both follow the live verdict_min_n
	// via Reconfigure, so the two stay coherent without widening the CohortVerdict
	// seam.
	minN int

	// shutdown is the context whose cancellation means the agent's graceful stop has
	// begun (UC-043 A20, #2332); the experiment loop sets it to its run context. Once
	// it is cancelled the registry declares and starts no experiment and a conclusion
	// does not call the promoter. Unset: never stopping (test wiring).
	shutdown atomic.Pointer[context.Context]

	mu      sync.Mutex
	entries map[string]*entry
	// rrCursor is the cross-experiment round-robin cursor for fair capacity
	// allocation: AllocateAndSpawn rotates the start of the live-experiment scan so
	// no single experiment is always served first (equal-share, no starvation).
	rrCursor int
	// releaseTombstones records game_ids whose terminal callback (ReleaseGame) fired
	// BEFORE the registry bound the game_id into e.games — the bind-vs-fast-terminal
	// race (#1014). A game that hits a terminal outcome the instant it starts (e.g.
	// game_error at admission) fires its TerminalCallback while only the reservation
	// placeholder is in e.games, so findGameLocked cannot match it and ReleaseGame
	// would no-op, then bindGame would record the game_id permanently and leak the
	// slot forever. To close the race, ReleaseGame for an unfound game_id leaves a
	// tombstone here ONLY WHILE A SPAWN IS IN FLIGHT (spawnsInFlight > 0); bindGame
	// checks it and, on a hit, drops the reservation WITHOUT binding (freeing the
	// slot, non-counting) and consumes the tombstone.
	//
	// BOUNDEDNESS: a tombstone is only meaningful for a bind that is still coming, and
	// a bind can only be pending while a spawn is in flight. So we (1) refuse to record
	// a tombstone when spawnsInFlight == 0 (an unfound ReleaseGame then is provably a
	// stale/duplicate call — already concluded or already released — not the race), and
	// (2) clear the whole map whenever spawnsInFlight returns to 0 (no bind can be
	// pending, so any survivor is a proven orphan). The map is therefore bounded by the
	// number of CONCURRENT in-flight spawns — genuinely O(in-flight) — and is empty
	// whenever the registry is quiescent. This is what makes the conclude-then-release
	// and double-release orderings (which would otherwise leave an un-consumable
	// tombstone) self-cleaning.
	releaseTombstones map[string]string // game_id → release reason
	// spawnsInFlight counts spawns currently between nextAllocation's reservation and
	// bindGame (the only window in which a real game_id can be reported-terminal before
	// it is bound). It gates and bounds releaseTombstones (see above).
	spawnsInFlight int

	// patterns pins archetype selections (UC-046). Production always wires it; nil
	// is a test-only seam for the allocation mechanics (the production spawner
	// refuses any spawn without an archetype).
	patterns PatternPinner
	// holdGate checks an experiment on a hold flag against the current defaults
	// (UC-043 A17); nil skips the check (test seam).
	holdGate HoldGate
	// pendingPromotions lists the flags whose promotion write waits in the
	// promoter's deferred queue (UC-043 A17 step 3); they count as under test. nil =
	// none. Called without mu held.
	pendingPromotions func() []string
	// restoredReverts are the waiting hold-key reverts Rehydrate found in done
	// journals (UC-043 A17); TakeRestoredReverts hands them to the promoter.
	// Guarded by mu.
	restoredReverts []RestoredRevert
	// flagDigest computes UC-046 BR-011's digest for an arm before it spawns.
	flagDigest func(ctx context.Context, experimentID, arm string) string
	// patternTracker force-ends in-flight pattern games on the kill switch (A12).
	patternTracker *patterngame.Tracker
}

// RegistryConfig configures a Registry. Every seam may be nil — a nil seam
// degrades to a safe but DEGRADED behavior (not merely an inert no-op), so a
// Registry is always safe to construct in a test or before a follow-up wires the
// real implementation. Production always injects all four seams. The degraded
// behaviors are:
//
//   - nil Spawner   ⇒ no game is ever started (the no-op stub errors), so the
//     experiment runs but never accrues shadow games.
//   - nil Verdict   ⇒ the verdict is never recomputed, so the experiment NEVER
//     reaches CONCLUDED on its own — it runs until explicitly aborted. (It does
//     not silently conclude; it simply has no terminal trigger.)
//   - nil Promoter  ⇒ a concluded+promote experiment is recorded as routed but the
//     promotion is a no-op; teardown still proceeds (capacity is freed).
//   - nil Targeting ⇒ Start still transitions the experiment to RUNNING, but with
//     NO shadow-scoped flag rule written. Its games therefore resolve the flag
//     DEFAULT (the non-experimental value): a silent, non-experimental no-op
//     experiment that produces no arm differentiation. Production always injects
//     #977's writer; a nil writer is a test/degraded shape only.
type RegistryConfig struct {
	// Root is the journal experiments root (journal.DirFromEnv() in production; a
	// t.TempDir in tests). Empty ⇒ journal.DefaultDir.
	Root string
	// MaxShadowGames is the fixed concurrent-shadow-game cap. Non-positive ⇒
	// DefaultMaxShadowGames.
	MaxShadowGames int
	// EffectiveConcurrency bounds the in-flight spawns the registry holds at once
	// to the coordinator's REAL concurrency (#998). The game-coordinator runs one
	// game at a time, so allocating more than this many concurrent starts just
	// produces doomed "Game already in progress" rejections. Non-positive ⇒
	// DefaultEffectiveConcurrency. It is clamped to <= MaxShadowGames.
	EffectiveConcurrency int
	// MaxGamesPerExperiment is the per-experiment termination budget (#1001): the
	// upper bound on concluded games before the registry gives up and concludes the
	// experiment INCONCLUSIVE. Zero ⇒ MaxGamesPerExperimentFromEnv() (the default
	// path). A NEGATIVE value explicitly DISABLES the games budget; the
	// inconclusive-past-target and degenerate-variance guards still terminate the
	// experiment, so it can never loop forever even with the budget off.
	MaxGamesPerExperiment int
	// VerdictMinN is the verdict's min-N-per-arm gate, mirrored into the registry
	// for target_n reconciliation and the inconclusive-past-target guard. Zero ⇒
	// DefaultMinNPerArm (the verdict_min_n flag default; the live value arrives via
	// Reconfigure on each enabled tick). Negative ⇒
	// disabled (no min-N reconciliation; the inconclusive-past-target guard then
	// keys off target_n alone).
	VerdictMinN int
	// Spawner / Verdict / Promoter / Targeting are the injected seams.
	Spawner   ShadowSpawner
	Verdict   CohortVerdict
	Promoter  Promoter
	Targeting TargetingWriter
	// Clock is injected so Declare/AppendEvent timestamps are deterministic in
	// tests. nil ⇒ time.Now (UTC).
	Clock func() time.Time
	// Log nil ⇒ slog.Default().
	Log *slog.Logger
	// Tracer emits the agent.experiment ROOT span (#1188). nil ⇒ otel.Tracer(...) with
	// the agent instrumentation scope (the production default; the global provider is a
	// no-op until configured, so this is safe). A test may inject a recording tracer to
	// assert the root span, or leave it nil to disable the span. The root span and its
	// re-parenting are OBSERVABILITY ONLY — a nil tracer changes no lifecycle behavior.
	Tracer trace.Tracer
	// Patterns pins the experiment.mock_archetypes selection in each experiment's
	// first allocation pass (UC-046). Production always wires it; nil is a test-only
	// seam for the allocation mechanics.
	Patterns PatternPinner
	// HoldGate returns why an experiment on a hold flag cannot run on the current
	// defaults ("" = it can), at its pin and at every tick (UC-043 A17 step 2). An
	// error keeps the experiment. nil skips the check (test seam).
	HoldGate HoldGate
	// PendingPromotions lists the flags whose deferred promotion write still waits
	// (promote.Promoter.PendingFlags, UC-043 A17 step 3): Declare refuses them and
	// ActiveFlags reports them busy. nil = none.
	PendingPromotions func() []string
	// FlagDigest computes the arm's resolved-values flag digest (UC-046 BR-011)
	// before the spawn; nil ⇒ no digest.
	FlagDigest func(ctx context.Context, experimentID, arm string) string
	// PatternTracker is the live pattern-game tracker the kill switch force-ends
	// games through (UC-046 A12). nil ⇒ no force-end.
	PatternTracker *patterngame.Tracker
}

// NewRegistry builds a Registry over the injected seams. It does NOT touch the
// filesystem or rehydrate — call Rehydrate after construction to rebuild the live
// set from the journal.
func NewRegistry(cfg RegistryConfig) *Registry {
	maxCap := cfg.MaxShadowGames
	if maxCap <= 0 {
		maxCap = DefaultMaxShadowGames
	}
	effectiveCap := cfg.EffectiveConcurrency
	if effectiveCap <= 0 {
		effectiveCap = DefaultEffectiveConcurrency
	}
	// The registry can never hold more in-flight than its capacity bookkeeping
	// allows, so clamp the effective cap to maxCap (a misconfigured higher value
	// would otherwise silently never bind).
	if effectiveCap > maxCap {
		effectiveCap = maxCap
	}
	// Termination budget (#1001): zero means "read the env default"; a negative
	// config value is an explicit disable, normalized to 0 (the guard treats <= 0 as
	// off).
	maxGames := cfg.MaxGamesPerExperiment
	switch {
	case maxGames == 0:
		maxGames = MaxGamesPerExperimentFromEnv()
	case maxGames < 0:
		maxGames = 0
	}
	// The env default may itself be non-positive (operator disabled it); normalize.
	if maxGames < 0 {
		maxGames = 0
	}
	// Min-N mirror (#1001): zero means the compiled default; negative disables
	// min-N reconciliation (normalized to 0, so the inconclusive-past-target guard
	// then keys off target_n alone).
	minN := cfg.VerdictMinN
	switch {
	case minN == 0:
		minN = DefaultMinNPerArm
	case minN < 0:
		minN = 0
	}
	clock := cfg.Clock
	if clock == nil {
		clock = func() time.Time { return time.Now().UTC() }
	}
	log := cfg.Log
	if log == nil {
		log = slog.Default()
	}
	tracer := cfg.Tracer
	if tracer == nil {
		// Default to the agent instrumentation scope (#1188). The global provider is a
		// no-op until the agent configures OTEL, so this never emits spurious spans in a
		// test that does not set up a provider.
		tracer = otel.Tracer(experimentInstrumentationName)
	}
	return &Registry{
		root:              cfg.Root,
		maxCap:            maxCap,
		effectiveCap:      effectiveCap,
		maxGames:          maxGames,
		minN:              minN,
		spawner:           cfg.Spawner,
		verdict:           cfg.Verdict,
		promo:             cfg.Promoter,
		target:            cfg.Targeting,
		clock:             clock,
		log:               log,
		tracer:            tracer,
		entries:           make(map[string]*entry),
		releaseTombstones: make(map[string]string),
		patterns:          cfg.Patterns,
		holdGate:          cfg.HoldGate,
		pendingPromotions: cfg.PendingPromotions,
		flagDigest:        cfg.FlagDigest,
		patternTracker:    cfg.PatternTracker,
	}
}

// experimentInstrumentationName scopes the registry's root-span tracer. It matches
// the decision package's instrumentationName so every agent span shares one
// instrumentation scope in Jaeger.
const experimentInstrumentationName = "github.com/joustmania/agent"

// Declare defines a new experiment: it mints an experiment_id, persists the
// immutable intent to the journal (journal.Create), records it as PROPOSED in the
// registry, and returns the id. It does NOT spawn games or write targeting yet —
// Start moves the experiment to RUNNING and writes the shadow targeting. Declare
// fails if the journal already has this id (re-mint collision is astronomically
// unlikely; a real collision is surfaced, not silently clobbered).
func (r *Registry) Declare(in Intent) (string, error) {
	// UC-043 BR-002 (no chaos experiments).
	// Fail-fast on a non-experimentable objective (#1015). chaos has no fitness
	// function — every game on it would fold the worst-case 0 and the experiment
	// would run its whole budget (up to the #1001 target) only to conclude
	// inconclusive. Reject it at declaration so the wasted run never starts; the
	// experimentable objectives (endurance/balanced/accelerate, and the empty
	// objective which folds as balanced) pass through unchanged.
	if !decision.IsExperimentable(in.Objective) {
		return "", fmt.Errorf("registry: declare experiment: objective %q is not experimentable: no fitness function", in.Objective)
	}
	// UC-043 A17 step 1 (#2096): shield.activation is experimented on only toward
	// hold, whoever declares it (the declarer forces it; a proposer cannot pick another
	// value and experiment its way out of hold).
	if err := checkHoldValue(in.FlagKey, in.ExperimentalValue); err != nil {
		return "", fmt.Errorf("registry: declare experiment: %w", err)
	}

	arms := in.Arms
	if len(arms) == 0 {
		arms = DefaultArms()
	}
	id := MintExperimentID()

	// #1731: hold r.mu from before the journal file exists until the entry is in
	// r.entries. Rehydrate loads under r.mu and skips an id already registered, so
	// a declare racing the first enable's Rehydrate (proposer goroutine vs the
	// experiment tick) is either fully registered when Rehydrate sees its journal,
	// or not on disk yet. Otherwise Rehydrate would read the fresh PROPOSED journal
	// as a leftover of a crash and abort it with start_failed (UC-043 A8).
	// UC-043 A17 step 3 (opposer M6): a flag whose deferred promotion write still
	// waits counts as under test; a new experiment's baseline would ignore it.
	if _, waiting := r.pendingPromotionSet()[in.FlagKey]; waiting {
		return "", fmt.Errorf("registry: declare experiment: %s has a deferred promotion waiting for the end of the real game (UC-043 A17)", in.FlagKey)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	// UC-043 A20 (#2332): checked under r.mu, where AbortAll takes its snapshot, so
	// an experiment declared before the stop began is in that snapshot and none is
	// declared after it.
	if r.shuttingDown() {
		return "", fmt.Errorf("registry: declare experiment: %w", ErrShuttingDown)
	}
	// Keep this call inside the lock: TestDeclareCreatesJournalUnderRegistryLock_1731
	// probes r.mu through r.clock().
	now := r.clock()

	// Reconcile target_n vs the verdict min-N (#1001). A target_n below the verdict
	// min-N can NEVER satisfy the verdict's conclusive gate, so the experiment would
	// be under-powered forever. Raise it to the min-N floor (the persisted intent
	// records the effective value). This is the coherent semantic chosen for the
	// issue: the lifecycle's "target reached" point is at least the verdict's min-N,
	// so reaching target and reaching a conclusive-eligible sample coincide.
	targetN := in.TargetNPerArm
	if r.minN > 0 && targetN < r.minN {
		if targetN > 0 {
			r.log.Info("experiment.target_n_reconciled",
				"experiment_id", id, "requested_target_n", targetN, "min_n", r.minN)
		}
		targetN = r.minN
	}

	jrnl, err := journal.Create(r.root, journal.Intent{
		ExperimentID:      id,
		CreatedAt:         now,
		Hypothesis:        in.Hypothesis,
		FlagKey:           in.FlagKey,
		ExperimentalValue: in.ExperimentalValue,
		Objective:         in.Objective,
		TargetNPerArm:     targetN,
		Arms:              arms,
	})
	if err != nil {
		return "", fmt.Errorf("registry: declare experiment: %w", err)
	}

	r.entries[id] = &entry{
		id:        id,
		journal:   jrnl,
		status:    StatusProposed,
		flagKey:   in.FlagKey,
		arms:      arms,
		games:     make(map[string]string),
		gamePairs: make(map[string]int),
	}
	// Record the PROPOSED transition as a decision event for the audit trail.
	r.recordDecision(jrnl, StatusProposed, "experiment declared")
	r.log.Info("experiment.declared", "experiment_id", id, "flag_key", in.FlagKey, "objective", in.Objective)
	return id, nil
}

// Start moves a PROPOSED experiment to RUNNING and writes its experiment-scoped
// shadow targeting via the TargetingWriter (#977). After Start the experiment may
// be allocated shadow capacity. A no-op (already RUNNING) returns nil. If the
// targeting write fails the experiment stays PROPOSED (it never accrues games it
// cannot scope) and the error is returned.
//
// DEGRADED (nil TargetingWriter): Start still transitions the experiment to
// RUNNING, but no shadow-scoped flag rule is written — its games resolve the flag
// DEFAULT, making it a silent, non-experimental no-op experiment (no arm
// differentiation). Production always injects #977's writer; a nil writer is a
// test/degraded shape only (see RegistryConfig).
func (r *Registry) Start(ctx context.Context, id string) error {
	r.mu.Lock()
	e := r.entries[id]
	if e == nil {
		r.mu.Unlock()
		return fmt.Errorf("registry: start: unknown experiment %q", id)
	}
	if e.status == StatusRunning {
		r.mu.Unlock()
		return nil
	}
	if e.status != StatusProposed {
		st := e.status
		r.mu.Unlock()
		return fmt.Errorf("registry: start: experiment %q is %s, not proposed", id, st)
	}
	if r.shuttingDown() {
		// UC-043 A20 (#2332): no targeting write once the stop began; the caller aborts
		// the PROPOSED experiment (start_failed), or the shutdown AbortAll does.
		r.mu.Unlock()
		return fmt.Errorf("registry: start %q: %w", id, ErrShuttingDown)
	}
	intent := e.journal.Intent()
	r.mu.Unlock()

	if r.target != nil {
		p := Proposal{
			FlagKey:           intent.FlagKey,
			ExperimentID:      id,
			ExperimentalValue: intent.ExperimentalValue,
			Rationale:         intent.Hypothesis,
		}
		if err := r.target.Apply(ctx, p); err != nil {
			return fmt.Errorf("registry: start: write targeting for %q: %w", id, err)
		}
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	// Re-fetch: a concurrent Abort may have removed/terminated it while unlocked.
	e = r.entries[id]
	if e == nil || e.status != StatusProposed {
		return nil
	}
	e.status = StatusRunning
	// #1188: start the long-lived agent.experiment ROOT span now that the experiment is
	// RUNNING (idempotent — a no-op if it is somehow already started). The analysis
	// spans re-parent under it; teardown ends it.
	r.startSpanLocked(e, intent)
	r.recordDecision(e.journal, StatusRunning, "experiment started")
	r.emitProgressLocked(e, ProgressTriggerStarted, "", e.spanCtx) // #2059
	r.log.Info("experiment.started", "experiment_id", id)
	return nil
}

// startSpanLocked starts the experiment's long-lived agent.experiment ROOT span
// (#1188) and records its SpanContext on the entry. It is idempotent (a no-op when
// the span already exists), nil-tracer-safe (no-op), and assumes r.mu is held.
//
// The span is started from context.Background() — it is the trace ROOT, with no
// inbound parent (the experiment is an autonomous, long-lived activity, not a
// request). intent supplies the identifying attributes; on a partial/zero intent the
// per-field guards simply skip the empty attrs.
func (r *Registry) startSpanLocked(e *entry, intent journal.Intent) {
	if r.tracer == nil || e == nil || e.span != nil {
		return
	}
	attrs := []attribute.KeyValue{
		attribute.String(keys.AttrExperimentID, e.id),
	}
	// #1184: stamp the explicit THESIS (the immutable Intent.Hypothesis — what idea +
	// expected effect) on the root span so the trace carries the experiment's WHY, not
	// just the flag/value mechanics. Same string the journal persists + the dossier
	// surfaces. Guarded so a thesis-less (legacy/test) intent adds no empty attr.
	if intent.Hypothesis != "" {
		attrs = append(attrs, attribute.String(keys.AttrExperimentThesis, intent.Hypothesis))
	}
	if intent.FlagKey != "" {
		attrs = append(attrs, attribute.String(keys.AttrExperimentFlagKey, intent.FlagKey))
	}
	if intent.Objective != "" {
		attrs = append(attrs, attribute.String(keys.AttrExperimentObjective, intent.Objective))
	}
	if intent.TargetNPerArm > 0 {
		attrs = append(attrs, attribute.Int(keys.AttrExperimentTargetN, intent.TargetNPerArm))
	}
	if len(intent.Arms) > 0 {
		attrs = append(attrs, attribute.String(keys.AttrExperimentArms, strings.Join(intent.Arms, ",")))
	}
	_, span := r.tracer.Start(context.Background(), keys.SpanExperiment, trace.WithAttributes(attrs...))
	e.span = span
	e.spanCtx = span.SpanContext()
}

// endSpanLocked ends the experiment's root span exactly once (#1188). It is the
// inverse of startSpanLocked, called from teardown — the single terminal chokepoint —
// so the span never leaks regardless of which terminal transition fired. Idempotent
// (clears e.span) and nil-safe. Assumes r.mu is held.
func (r *Registry) endSpanLocked(e *entry) {
	if e == nil || e.span == nil {
		return
	}
	e.span.End()
	e.span = nil
	// Clear the recorded SpanContext too so ExperimentSpanContext reports unavailable
	// after teardown: a torn-down experiment is no longer a re-parent target (its span
	// has ended, so a late analysis span must fall back to its own-root behavior).
	e.spanCtx = trace.SpanContext{}
}

// AllocateAndSpawn fills FREE shadow capacity across the live experiments by
// equal-share / round-robin (epic #982 decision 3), spawning at most one game per
// experiment per pass until the EFFECTIVE concurrency cap is reached or no live
// experiment wants another game. It is the registry's capacity scheduler: call it
// whenever capacity may have freed (a game concluded, an experiment started). It
// returns the number of games spawned this call.
//
// Fairness: the live experiments are scanned starting from a rotating cursor and
// each gets at most one new game per pass, so K experiments share the cap evenly
// and none starves. The effective cap (#998) bounds in-flight to the
// coordinator's REAL concurrency so the registry never over-spawns doomed starts.
//
// Backpressure (#998): the game-coordinator runs ONE game at a time and rejects a
// concurrent start with "Game already in progress". That is NOT a failure — the
// spawner returns it wrapped as ErrSpawnBackpressure. AllocateAndSpawn releases
// the reservation cleanly (no slot leak), logs at DEBUG (not WARN), and stops
// this pass so the start is retried on the next tick. Only a GENUINE spawn error
// still WARNs as experiment.spawn_failed.
func (r *Registry) AllocateAndSpawn(ctx context.Context) int {
	if r.spawner == nil {
		return 0
	}
	// UC-043 A17 step 2: a running hold-flag experiment whose condition flipped ends.
	r.discardHoldStale(ctx)
	// UC-046 step 2: pin the archetype selection of experiments that have not spawned yet.
	r.pinPending(ctx)
	// UC-046 A6 / BR-015: conclude pattern experiments whose archetypes are all used
	// or skipped, also when the last arms failed at spawn.
	defer r.concludeExhaustedPatterns(ctx)
	spawned := 0
	for {
		// Each iteration is one round-robin PASS: pick the next live experiment (from
		// the rotating cursor) with capacity and reserve the next arm(s) of its
		// current Common-Random-Numbers pair (#1003). When the effective cap admits
		// two slots a whole (experimental, control) pair is reserved atomically; when
		// it admits one (the single-game coordinator) one arm is reserved now and its
		// seed-matched sibling follows on a later tick. Re-evaluate capacity each pick.
		plan, ok := r.nextPair()
		if !ok {
			return spawned
		}
		// ATOMIC for the slots reserved THIS tick (#1003): spawn the reserved arm(s)
		// with the shared seed. If a multi-arm reservation's second spawn fails (or
		// hits backpressure), spawnPair rolls the WHOLE same-tick group back — including
		// a sibling that already started — so a half-pair never enters the cohort
		// statistics from a single tick. (A pair split across ticks under effective
		// cap 1 is completed by the verdict's pair_index matching in #1004.)
		boundThisPair, stop := r.spawnPair(ctx, plan)
		spawned += boundThisPair
		if stop {
			return spawned
		}
	}
}

// pairPlan is the result of reserving the next arm(s) of one experiment's current
// CRN pair: the experiment, the shared per-pair seed and pair index, and the arm(s)
// reserved THIS tick (one or both, depending on the free effective budget). Their
// reservation slots are already held; spawnPair turns them into real games or rolls
// the same-tick group back.
type pairPlan struct {
	id        string
	seed      uint64
	pairIndex int
	arms      []string // arm(s) reserved this tick, in reservation order
	// pattern is set for a pattern experiment's arm (UC-046 step 3).
	pattern *PatternSpawn
}

// startedArm is one arm of a CRN pair that successfully spawned a live game. It is
// tracked during spawnPair so a later-arm failure can force-release the live
// sibling(s) and keep the pair atomic.
type startedArm struct {
	arm    string
	gameID string
}

// spawnPair spawns BOTH arms of a reserved CRN pair with the shared seed and binds
// them. It returns how many arms it genuinely bound (a live in-flight game) and
// whether AllocateAndSpawn should STOP this pass (backpressure or a genuine spawn
// failure). On any spawn error it releases EVERY still-held reservation for the
// pair — including a sibling arm that already spawned a live game — so a half-pair
// never enters the cohort statistics (the atomic-pair invariant).
func (r *Registry) spawnPair(ctx context.Context, plan pairPlan) (bound int, stop bool) {
	// Spawn-traceparent propagation (#1182, epic #1181): carry this experiment's
	// long-lived ROOT span context on the spawn ctx as a REMOTE parent. The
	// spawner reads it off the ctx and (for an experiment-bound shadow game)
	// injects it as the outgoing traceparent so the coordinator's game span
	// becomes a CHILD of the experiment span. When the experiment has no valid
	// root span (tracer unwired / torn down) the ctx is unchanged, so nothing is
	// injected and the game stays own-rooted — graceful, exactly as before.
	spawnCtx := ctx
	if sc, ok := r.ExperimentSpanContext(plan.id); ok {
		spawnCtx = trace.ContextWithRemoteSpanContext(ctx, sc)
	}
	if plan.pattern != nil {
		if r.flagDigest != nil {
			plan.pattern.FlagDigest = r.flagDigest(ctx, plan.id, plan.arms[0])
		}
		spawnCtx = WithPatternSpawn(spawnCtx, *plan.pattern)
	}
	if err := r.reassertArmValue(ctx, plan.id); err != nil {
		r.rollbackPair(plan, 0, nil)
		r.log.Warn("experiment.arm_value_reassert_failed", "experiment_id", plan.id, "error", err)
		return 0, true
	}
	var live []startedArm
	for i, arm := range plan.arms {
		gameID, err := r.spawner.Spawn(spawnCtx, plan.id, arm, plan.seed)
		if err != nil {
			// Roll the WHOLE pair back atomically (#1003): release the failed arm and
			// every arm AFTER it (whose Spawn was never attempted, still reserved), and
			// force-release any earlier arm that already became a live game — so no
			// half-pair survives into the cohort statistics. All cap slots return.
			r.rollbackPair(plan, i, live)
			if errors.Is(err, ErrSpawnBackpressure) {
				r.log.Debug("experiment.spawn_backpressure",
					"experiment_id", plan.id, "arm", arm, "seed", plan.seed, "error", err)
				return 0, true
			}
			r.log.Warn("experiment.spawn_failed",
				"experiment_id", plan.id, "arm", arm, "seed", plan.seed, "error", err)
			return 0, true
		}
		if r.bindGame(plan.id, gameID, arm, plan.pairIndex, plan.pattern) {
			live = append(live, startedArm{arm: arm, gameID: gameID})
		}
		// If bindGame did NOT bind (the #1014 bind-vs-fast-terminal race freed the
		// reservation instead) the slot is already back in the cap; the sibling arm
		// still spawns so the pair completes if it can.
	}
	return len(live), false
}

// reassertArmValue makes sure the experiment's arm value is still served before
// a pair spawns: a flag-service restart drops it (the targeting writer compares
// boot epochs and writes it again). A targeting writer without the capability,
// or an experiment without a flag, needs nothing.
func (r *Registry) reassertArmValue(ctx context.Context, id string) error {
	ra, ok := r.target.(interface {
		Reassert(ctx context.Context, p Proposal) error
	})
	if !ok {
		return nil
	}
	r.mu.Lock()
	e := r.entries[id]
	if e == nil {
		r.mu.Unlock()
		return nil
	}
	intent := e.journal.Intent()
	r.mu.Unlock()
	if intent.FlagKey == "" {
		return nil
	}
	return ra.Reassert(ctx, Proposal{
		FlagKey:           intent.FlagKey,
		ExperimentID:      id,
		ExperimentalValue: intent.ExperimentalValue,
		Rationale:         intent.Hypothesis,
	})
}

// rollbackPair undoes a partially-spawned CRN pair on a spawn error (#1003), keeping
// the pair atomic. failedIdx is the index in plan.arms whose Spawn just errored. It:
//  1. releases the failed arm's still-held reservation (failedIdx),
//  2. releases every arm AFTER failedIdx — their Spawn was never attempted, so they
//     still hold a reservation placeholder, and
//  3. force-releases (ReleaseGame) every earlier arm that already became a LIVE game,
//     so a half-pair never enters the cohort statistics.
//
// Arms before failedIdx that did NOT become live (the #1014 bind-vs-fast-terminal
// race already freed their reservation) need no action. Takes the lock per helper.
func (r *Registry) rollbackPair(plan pairPlan, failedIdx int, liveSiblings []startedArm) {
	// (1)+(2): the failed arm and all later, never-attempted arms still hold a
	// reservation placeholder — release one per arm.
	for i := failedIdx; i < len(plan.arms); i++ {
		r.releaseReservation(plan.id, plan.arms[i])
	}
	// (3): tear down any earlier arm that already started a live game.
	for _, s := range liveSiblings {
		r.ReleaseGame(s.gameID, "crn pair rollback: sibling arm spawn failed (#1003)")
	}
	// UC-046 BR-015: a failed pattern spawn uses up no archetype; reserve it again.
	if plan.pattern != nil {
		r.rewindFailedSpawn(plan.id, len(plan.arms)-failedIdx)
	}
}

// nextPair reserves the next Common-Random-Numbers PAIR (#1003) for a live
// experiment under both capacity caps, or reports ok=false when no live experiment
// can fit a full pair. A pair is two arms (experimental + control) sharing one seed
// = pairSeed(experimentID, pairIndex). It reserves BOTH arm slots atomically (under
// r.mu) so the pair is all-or-nothing against the cap; spawnPair rolls them back on
// a spawn failure. It advances the cross-experiment round-robin cursor (fairness),
// the chosen experiment's per-arm cursor (even arm fill), and its monotonic pair
// cursor. Assumes nothing; takes the lock.
func (r *Registry) nextPair() (pairPlan, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()

	// Gate on the EFFECTIVE concurrency cap (#998): the registry must not hold more
	// in-flight spawns than the coordinator can actually run. At least ONE free slot
	// is required to make any progress.
	free := r.effectiveCap - r.inFlightLocked()
	if free <= 0 {
		return pairPlan{}, false
	}
	// UC-046 BR-010: no other shadow game while a pattern game is in flight.
	if r.patternInFlightLocked() {
		return pairPlan{}, false
	}
	live := r.liveIDsLocked()
	if len(live) == 0 {
		return pairPlan{}, false
	}
	// Scan starting from the rotating cursor so a different experiment leads each
	// pass — equal-share over time even when the cap is the binding constraint.
	start := r.rrCursor % len(live)
	for i := 0; i < len(live); i++ {
		cand := live[(start+i)%len(live)]
		e := r.entries[cand]
		nArms := len(e.arms)
		if nArms == 0 {
			continue
		}
		// Equal-share guard: bound an experiment to its fair ceil(cap/K) share so K
		// live experiments converge to an even split. Skip an experiment with no
		// room for even a single arm.
		share := r.perExperimentShareLocked(len(live))
		shareRoom := share - e.inFlight()
		if shareRoom <= 0 {
			continue
		}

		// CRN pairing (#1003): the per-experiment arm cursor lays out games as
		// consecutive (experimental, control) PAIRS. The pair index is armCursor /
		// nArms, and the shared seed = pairSeed(id, pairIndex) — so BOTH arms of a
		// pair derive the SAME seed automatically (the seed only changes at a pair
		// boundary). We reserve as many of the CURRENT pair's REMAINING arms as fit
		// the free + share budget THIS tick:
		//   - effective cap >= 2 (and a fresh pair) ⇒ reserve BOTH arms atomically,
		//     so spawnPair rolls the whole pair back if either start fails.
		//   - effective cap == 1 (the single-game coordinator) ⇒ reserve ONE arm now;
		//     its seed-matched sibling is reserved on a later tick (after this game
		//     concludes and frees the slot) with the SAME derived seed. The pair is
		//     still seed-matched; it just spans two ticks.
		pairIndex := e.armCursor / nArms
		armInPair := e.armCursor % nArms
		remainingInPair := nArms - armInPair // arms left to dispatch in this pair
		budget := free
		if shareRoom < budget {
			budget = shareRoom
		}
		toReserve := remainingInPair
		if budget < toReserve {
			toReserve = budget
		}
		if toReserve <= 0 {
			continue
		}
		var pattern *PatternSpawn
		if e.pin == nil && r.patterns != nil {
			continue // not pinned yet (pinPending runs first; a fresh Start waits a tick)
		}
		if r.patterns != nil && !e.patternActive() {
			continue // archetype-only: never spawn without a pinned archetype (UC-046 A1)
		}
		if e.patternActive() {
			// UC-046 BR-010: a pattern arm spawns only when no shadow game is in flight,
			// one at a time. A6: one pair per pinned archetype, then no more.
			if r.inFlightLocked() > 0 || pairIndex >= len(e.pin.Archetypes) {
				continue
			}
			toReserve = 1
			pattern = &PatternSpawn{
				Archetype:      e.pin.Archetypes[pairIndex],
				MaxGameSeconds: e.pin.MaxGameSeconds,
				PairIndex:      pairIndex,
			}
		}

		seed := pairSeed(cand, pairIndex)
		r.rrCursor = (start + i + 1) % len(live)

		arms := make([]string, 0, toReserve)
		for s := 0; s < toReserve; s++ {
			arm := e.arms[e.armCursor%nArms]
			e.armCursor++
			// Reserve against the cap by pre-incrementing via a placeholder game id; it
			// is replaced by the real game id in bindGame, or released on spawn failure.
			placeholder := reservationKey(cand, e.armCursor)
			e.games[placeholder] = arm
			// A spawn is now in flight (reserved, not yet bound). It is the only window
			// in which a real game_id can be reported-terminal before bind, so this
			// gates and bounds the tombstone map. Decremented in bindGame /
			// releaseReservation (one per reserved arm).
			r.spawnsInFlight++
			arms = append(arms, arm)
		}
		return pairPlan{id: cand, seed: seed, pairIndex: pairIndex, arms: arms, pattern: pattern}, true
	}
	return pairPlan{}, false
}

// perExperimentShareLocked is the equal-share ceiling per live experiment:
// ceil(cap / K). It bounds any one experiment to its fair slice so the cap splits
// evenly across K live experiments (no starvation). Assumes r.mu is held.
func (r *Registry) perExperimentShareLocked(k int) int {
	if k <= 0 {
		return r.maxCap
	}
	return (r.maxCap + k - 1) / k
}

// reservationKey builds the placeholder game id a reserved-but-not-yet-spawned
// slot occupies in entry.games. It is distinct from any coordinator game_id
// (which is "game_<hex>"), so bindGame/releaseReservation can find and replace it.
func reservationKey(id string, seq int) string {
	return "__reserved__" + id + "__" + strconv.Itoa(seq)
}

// pairSeed derives the shared Common-Random-Numbers seed for a (experimental,
// control) pair from (experimentID, pairIndex) using FNV-1a/64 (#1003). It is a
// PURE function of its inputs — NEVER a clock — so the same experiment/pair maps
// to the same seed across process restarts and journal rehydrates, which is what
// makes a paired arm reproducible. Both arms of a pair receive this identical
// value, so their shadow games face the same RNG-driven bracket/role/team schedule
// and the only between-arm difference is the flag under test.
//
// The seed is never 0 for a real pair: a 0 seed means "entropy" coordinator-side,
// so if the FNV digest happens to be 0 we nudge it to 1 (astronomically rare, but
// it keeps a legitimate pair from accidentally falling into the entropy path).
func pairSeed(experimentID string, pairIndex int) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(experimentID))
	_, _ = h.Write([]byte{0}) // domain separator so id|index can't alias
	_, _ = h.Write([]byte(strconv.Itoa(pairIndex)))
	seed := h.Sum64()
	if seed == 0 {
		seed = 1
	}
	return seed
}

// bindGame replaces the most-recent reservation for the experiment with the real
// coordinator game_id, records a game_assigned journal event, and (re)allocation
// is the caller's concern. Assumes the reservation exists (nextAllocation made it).
//
// BIND-VS-FAST-TERMINAL RACE (#1014): a game can hit a terminal outcome — and fire
// its TerminalCallback → ReleaseGame(gameID) — BEFORE this bind runs (the drive
// goroutine starts inside Spawn, before Spawn returns the game_id). When that
// happens ReleaseGame cannot find the not-yet-bound game_id, so it leaves a
// tombstone instead. bindGame detects that tombstone and, rather than recording the
// game_id permanently (which would leak the slot forever — the terminal callback
// already fired and will never fire again), it drops the reservation WITHOUT binding
// and records a non-counting release. The slot returns to the cap immediately.
//
// It returns true when it bound the game_id (a live in-flight game), false when it
// freed the reservation without binding (the race lost, or the experiment vanished)
// — the caller counts only genuinely-started games.
//
// pairIndex is the Common-Random-Numbers pair this game belongs to (#1003); it is
// stamped onto the game_assigned event (Event.PairIndex) so the paired-difference
// verdict (#1004) can recover which two games share a seed.
func (r *Registry) bindGame(id, gameID, arm string, pairIndex int, pattern *PatternSpawn) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	// The spawn that produced this game_id has resolved (whichever branch we take
	// below). Account for it so the tombstone map stays bounded and self-cleaning.
	defer r.spawnResolvedLocked()
	e := r.entries[id]
	if e == nil {
		// Experiment vanished (torn down) between spawn and bind: nothing to bind, and
		// teardown already cleared the reservation. Drop any tombstone so it cannot
		// outlive the experiment.
		delete(r.releaseTombstones, gameID)
		return false
	}

	// TERMINAL-STATUS GUARD (#1004 carried-over item 3): a Spawn in flight during
	// teardown (which clears e.games and sets a terminal status) could otherwise
	// bindGame the real game_id back into a CONCLUDED/ABORTED experiment, re-adding a
	// live slot to an experiment that is already done — a leak widened by CRN's
	// two-arm reservation (two spawns in flight per pair). Refuse to bind into a
	// terminal experiment: release the in-flight game externally (its drive goroutine
	// is still running) and record a non-counting release for the audit trail, rather
	// than resurrecting the experiment's in-flight set.
	if e.status.IsTerminal() {
		delete(r.releaseTombstones, gameID)
		r.appendEvent(e.journal, journal.Event{
			Kind:   journal.KindGameReleased,
			GameID: gameID,
			Arm:    arm,
			Note:   "bind refused: experiment terminal at bind time (#1004)",
		})
		r.log.Warn("experiment.bind_refused_terminal (#1004)",
			"experiment_id", id, "game_id", gameID, "arm", arm, "status", string(e.status))
		return false
	}

	// If the game already terminated before we could bind it, do NOT record the
	// game_id — free the reservation slot instead and consume the tombstone.
	if reason, tombstoned := r.releaseTombstones[gameID]; tombstoned {
		delete(r.releaseTombstones, gameID)
		r.dropOneReservationLocked(e, arm)
		r.appendEvent(e.journal, journal.Event{
			Kind:   journal.KindGameReleased,
			GameID: gameID,
			Arm:    arm,
			Note:   reason,
		})
		// UC-046 BR-015: the pattern arm ended without a sample; its pair runs again.
		r.rerunPairLocked(e, pairIndex, reason)
		r.log.Warn("experiment.game_released_before_bind (#1014)",
			"experiment_id", id, "game_id", gameID, "arm", arm, "reason", reason)
		return false
	}

	// Replace the matching reservation placeholder (an arbitrary one for this arm)
	// with the real game id. We find ANY reservation for this arm to convert.
	r.dropOneReservationLocked(e, arm)
	e.games[gameID] = arm
	e.gamePairs[gameID] = pairIndex
	pi := pairIndex
	r.appendEvent(e.journal, journal.Event{
		Kind:      journal.KindGameAssigned,
		GameID:    gameID,
		Arm:       arm,
		PairIndex: &pi,
		Pattern:   e.patternFactsLocked(pairIndex, pattern),
	})
	r.emitProgressLocked(e, ProgressTriggerGameAssigned, "", e.spanCtx) // #2059
	r.log.Info("experiment.game_assigned",
		"experiment_id", id, "game_id", gameID, "arm", arm, "pair_index", pairIndex)
	return true
}

// dropOneReservationLocked deletes one reservation placeholder for the arm from the
// experiment's game map (an arbitrary one — they are interchangeable for the same
// arm). It is the shared helper for bindGame (convert/free a reservation) and
// releaseReservation (free on spawn failure). Assumes r.mu is held.
func (r *Registry) dropOneReservationLocked(e *entry, arm string) {
	for k, a := range e.games {
		if isReservation(k) && a == arm {
			delete(e.games, k)
			return
		}
	}
}

// releaseReservation drops one reservation placeholder for the arm (used when a
// spawn fails) so a flaky spawn does not permanently consume a capacity slot.
func (r *Registry) releaseReservation(id, arm string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	// The spawn resolved (with an error); account for it whether or not the
	// experiment still exists, so spawnsInFlight cannot drift positive and pin the
	// tombstone map open.
	defer r.spawnResolvedLocked()
	e := r.entries[id]
	if e == nil {
		return
	}
	r.dropOneReservationLocked(e, arm)
}

// spawnResolvedLocked records that one in-flight spawn has resolved (bound, freed,
// or failed). When the count returns to zero NO bind can be pending, so any tombstone
// still in the map is a proven orphan (a stale conclude-then-release or double-release
// that no bindGame will ever consume) and is swept — keeping releaseTombstones bounded
// by concurrent in-flight spawns and empty whenever the registry is quiescent. Assumes
// r.mu is held.
func (r *Registry) spawnResolvedLocked() {
	if r.spawnsInFlight > 0 {
		r.spawnsInFlight--
	}
	if r.spawnsInFlight == 0 && len(r.releaseTombstones) > 0 {
		for k := range r.releaseTombstones {
			delete(r.releaseTombstones, k)
		}
	}
}

// isReservation reports whether a game-map key is a reservation placeholder
// rather than a real coordinator game_id.
func isReservation(k string) bool { return strings.HasPrefix(k, "__reserved__") }

// ConcludeGame records that a shadow game ended (design §7.1, §8.1): it appends a
// game_concluded journal event with the game's fitness sample (folding it into
// the matching arm's Welford accumulator), then calls the CohortVerdict seam to
// recompute the interim verdict, records it, and transitions the experiment to
// CONCLUDED when the verdict warrants (promote/discard, or an under-powered
// timeout the verdict signals). It looks the game up in the (game_id →
// experiment_id, arm) map the registry owns. An unknown game_id is ignored (it
// was not one of ours). On CONCLUDED the experiment is routed to promotion (if the
// verdict promotes) and torn down. Returns the resulting status.
func (r *Registry) ConcludeGame(ctx context.Context, gameID string, fitness float64, durationS float64) (Status, error) {
	r.mu.Lock()
	id, arm, e := r.findGameLocked(gameID)
	if e == nil {
		r.mu.Unlock()
		return "", nil // not one of ours; ignore.
	}
	delete(e.games, gameID)
	// Recover the game's Common-Random-Numbers pair index (#1003) recorded at bind so
	// the game_concluded event carries it — the journal's paired-difference fold
	// (#1004) matches the two games of a pair by it. A bound game always has an entry;
	// a missing one (legacy/rehydrated in-flight, which Rehydrate never restores)
	// simply leaves PairIndex nil and the conclusion folds only into the two-arm path.
	var pairIdx *int
	if pi, ok := e.gamePairs[gameID]; ok {
		p := pi
		pairIdx = &p
		delete(e.gamePairs, gameID)
	}
	// Drop any release tombstone for this id: a bound game that concludes normally
	// can never have one (bindGame consumes it before binding), but clear it
	// defensively so an out-of-order unfound-release cannot linger.
	delete(r.releaseTombstones, gameID)
	f := fitness
	r.appendEvent(e.journal, journal.Event{
		Kind:      journal.KindGameConcluded,
		GameID:    gameID,
		Arm:       arm,
		PairIndex: pairIdx,
		Fitness:   &f,
		DurationS: durationS,
	})
	jrnl := e.journal
	r.mu.Unlock()

	// Recompute the interim verdict from the seam (outside the registry lock — the
	// journal has its own lock). The verdict seam consumes the full Summary (raw
	// per-arm Welford: Count/Mean/M2) so #979 can compute an unbiased / Welch
	// estimator — CompactView would drop the raw M2. Record it as an interim_verdict
	// event.
	var verdict journal.Verdict
	haveVerdict := false
	if r.verdict != nil {
		verdict, haveVerdict = r.verdict.Evaluate(jrnl.Summary())
		if haveVerdict {
			r.appendEvent(jrnl, journal.Event{
				Kind:    journal.KindInterimVerdict,
				Verdict: &verdict,
			})
		}
	}
	// #2059: after the fold AND the interim verdict, so the snapshot counts exactly
	// the folded games and its verdict.* is this game's. An experiment torn down in
	// between (AbortAll) already emitted its terminal snapshot.
	r.mu.Lock()
	if e := r.entries[id]; e != nil && !e.status.IsTerminal() {
		r.emitProgressLocked(e, ProgressTriggerGameConcluded, "", e.spanCtx)
	}
	r.mu.Unlock()

	r.log.Info("experiment.game_concluded",
		"experiment_id", id, "game_id", gameID, "arm", arm, "fitness", fitness)

	// Decide whether to conclude the experiment. A promote/discard verdict
	// concludes it; inconclusive keeps it running. (An under-powered timeout is the
	// verdict seam's call — it returns promote/discard once it is willing to.)
	if haveVerdict && verdict.Outcome != OutcomeInconclusive {
		return r.conclude(ctx, id, verdict)
	}

	// TERMINATION GUARD (#1001): the verdict is inconclusive (or absent). Without a
	// bound, an experiment that can never reach signal — e.g. all-zero fitness ⇒
	// both arms zero-variance ⇒ undefined effect size ⇒ permanent "inconclusive" —
	// spawns games forever. If any give-up condition holds, conclude+teardown the
	// experiment as a FINAL inconclusive instead of leaving it RUNNING.
	if reason, giveUp := r.shouldGiveUp(jrnl.Summary(), jrnl.Intent().TargetNPerArm); giveUp {
		final := journal.Verdict{
			Outcome:     OutcomeInconclusive,
			Significant: false,
			Reason:      reason,
		}
		if haveVerdict {
			final.Delta = verdict.Delta
		}
		r.log.Info("experiment.terminated_inconclusive",
			"experiment_id", id, "reason", reason)
		return r.conclude(ctx, id, final)
	}

	// UC-046 A6: a pattern experiment whose archetypes are all used concludes once
	// its last game has ended.
	if st, done := r.concludeIfPatternExhausted(ctx, id); done {
		return st, nil
	}
	return StatusRunning, nil
}

// ReleaseGame frees the in-flight slot held by a shadow game that ended WITHOUT
// producing a usable fitness sample (#1014): a game that terminated in
// game_error, was force-ended on timeout, or was aborted. Unlike ConcludeGame it
// does NOT fold a fitness sample, recompute the verdict, or advance the
// termination budget — a transient game failure must not pollute the cohort with
// a fabricated 0-fitness sample (epic #982: fitness is observed, never assumed).
// It simply drops the (game_id → arm) binding so the slot returns to the cap,
// records a non-counting release event for the audit trail, and lets the next
// AllocateAndSpawn refill it. This is the BACKSTOP that guarantees the
// effective_concurrency=1 loop can never deadlock on an errored game even if the
// telemetry-driven ConcludeGame path never fires for it.
//
// It is IDEMPOTENT and SAFE to call alongside ConcludeGame: an unknown game_id
// (already concluded/released, or not one of ours) is ignored — and never records a
// lingering tombstone unless a bind is genuinely pending (spawnsInFlight > 0), so the
// conclude-then-release and double-release orderings leave no orphan. Returns whether
// a slot was actually freed.
func (r *Registry) ReleaseGame(gameID, reason string) bool {
	r.mu.Lock()
	id, arm, e := r.findGameLocked(gameID)
	if e == nil {
		// The game_id is not bound. This is EITHER the bind-vs-fast-terminal race
		// (#1014) — the terminal callback fired before AllocateAndSpawn could bind the
		// real game_id, so only the reservation placeholder is in e.games — OR a
		// genuinely stale call (already concluded/released, or not one of ours). We tell
		// them apart by spawnsInFlight: the race can ONLY happen while a spawn is in
		// flight (a bind is still coming). So tombstone ONLY then; bindGame will detect
		// it and free the reservation WITHOUT recording the id (closing the leak), and
		// spawnResolvedLocked sweeps any survivor when the last spawn resolves. When NO
		// spawn is in flight an unfound release is provably stale — recording a tombstone
		// would just leak an entry no bind will ever consume, so we skip it.
		if r.spawnsInFlight > 0 {
			if _, dup := r.releaseTombstones[gameID]; !dup {
				r.releaseTombstones[gameID] = reason
			}
		}
		r.mu.Unlock()
		return false // no bound slot freed (the bind, if any, frees it).
	}
	delete(e.games, gameID)
	// Recover the pair index so the journal can drop a parked pending half whose
	// partner is now gone (#1004 carried-over item 1): a released game that was the
	// first-concluded half of an open pair must NEVER fold a fabricated difference.
	var pairIdx *int
	if pi, ok := e.gamePairs[gameID]; ok {
		p := pi
		pairIdx = &p
		delete(e.gamePairs, gameID)
	}
	// A tombstone can never coexist with a bound game (bindGame consumes it before
	// binding), but drop defensively so a prior unfound-release for a since-rebound id
	// cannot linger.
	delete(r.releaseTombstones, gameID)
	r.appendEvent(e.journal, journal.Event{
		Kind:      journal.KindGameReleased,
		GameID:    gameID,
		Arm:       arm,
		PairIndex: pairIdx,
		Note:      reason,
	})
	// UC-046 BR-015: a pattern arm released without a sample reruns its pair.
	if pairIdx != nil {
		r.rerunPairLocked(e, *pairIdx, reason)
	}
	r.emitProgressLocked(e, ProgressTriggerGameReleased, "", e.spanCtx) // #2059
	r.mu.Unlock()

	r.log.Info("experiment.game_released",
		"experiment_id", id, "game_id", gameID, "arm", arm, "reason", reason)
	// UC-046 A6: a released last game of an exhausted pattern experiment concludes it.
	r.concludeIfPatternExhausted(context.Background(), id)
	return true
}

// shouldGiveUp implements the #1001 termination guard: it decides whether an
// experiment that is still inconclusive must be concluded INCONCLUSIVE (rather
// than left RUNNING forever). It returns a human-readable reason and true when
// ANY of the give-up conditions holds:
//
//	(a) GAMES BUDGET — total concluded games across arms has reached r.maxGames
//	    (AGENT_EXPERIMENT_MAX_GAMES; the absolute ceiling, the primary backstop).
//	(b) INCONCLUSIVE PAST TARGET — every arm has reached the effective target N
//	    (max(target_n, min-N)), so the cohort is fully powered yet the verdict is
//	    still inconclusive: more games will not change a within-noise result.
//	(c) DEGENERATE VARIANCE — every arm is past target N AND has zero variance
//	    (all-equal fitness, the #997 all-zero case): the effect size is undefined
//	    and re-evaluating forever cannot help.
//
// The guard reads only the rolling Summary (per-arm Welford count/M2) plus the
// persisted intent's target_n (passed in by the caller), so it is O(arms) and
// needs no extra book-keeping.
func (r *Registry) shouldGiveUp(s journal.Summary, targetN int) (string, bool) {
	// Total concluded games across all arms (reservations are not in the Summary).
	total := 0
	for _, a := range s.Arms {
		if a != nil {
			total += a.Count
		}
	}

	// (a) Absolute games budget. <= 0 disables this guard.
	if r.maxGames > 0 && total >= r.maxGames {
		return fmt.Sprintf("games budget exhausted: %d games >= max=%d, still inconclusive",
			total, r.maxGames), true
	}

	// Effective target N: the larger of the persisted target_n and the verdict
	// min-N. (Declare already reconciles target_n up to min-N, so they normally
	// coincide; this max is a belt-and-braces floor for rehydrated/legacy intents.)
	target := targetN
	if r.minN > target {
		target = r.minN
	}

	// Both (b) and (c) require every arm to have reached the effective target N.
	// Until then the cohort is genuinely under-powered and keeps running (bounded by
	// the games budget above). A non-positive target makes these guards inert (the
	// budget alone bounds the experiment).
	if target <= 0 {
		return "", false
	}
	allReachedTarget := len(s.Arms) > 0
	allDegenerate := true
	for _, a := range s.Arms {
		if a == nil || a.Count < target {
			allReachedTarget = false
			break
		}
		if a.M2 != 0 {
			allDegenerate = false
		}
	}
	if !allReachedTarget {
		return "", false
	}

	// (c) Degenerate variance past target N — checked first for a precise reason.
	if allDegenerate {
		return fmt.Sprintf("degenerate variance past target N=%d on all arms (zero spread); effect size undefined",
			target), true
	}

	// (b) Inconclusive past target N — fully powered but still within noise.
	return fmt.Sprintf("inconclusive past target N=%d on all arms; no significant effect", target), true
}

// conclude transitions an experiment to CONCLUDED on a final verdict, routes a
// promote verdict to the Promoter (PROMOTING → DONE when the promotion acted,
// PROMOTING → DISCARDED when it did not, #1701), discards otherwise, and tears down
// the targeting + frees capacity. It is the verdict→terminal path.
//
// It concludes an experiment at most once (UC-043 BR-006, #2034): the game-end
// handler and the allocation pass can both decide to conclude the same
// experiment, so only a live (proposed / running) experiment is concluded; a
// later caller gets the current status (concluded, promoting or terminal) back.
func (r *Registry) conclude(ctx context.Context, id string, verdict journal.Verdict) (Status, error) {
	r.mu.Lock()
	e := r.entries[id]
	if e == nil || !e.status.IsLive() {
		// Copy the status under the lock: the winning caller keeps writing it
		// (promotion, teardown) after we unlock.
		var current Status
		if e != nil {
			current = e.status
		}
		r.mu.Unlock()
		return current, nil
	}
	e.status = StatusConcluded
	// UC-046 BR-013: report pattern timeouts per arm with the conclusion.
	if summary := e.timeoutSummaryLocked(); summary != "" {
		if verdict.Reason != "" {
			verdict.Reason += "; "
		}
		verdict.Reason += summary
	}
	// The final verdict rides on the concluded decision (#2042): the journal
	// summary, the promoter and a restart (UC-043 A15) read the verdict the
	// experiment concluded on, also after a give-up or pattern-exhausted conclusion.
	finalVerdict := verdict
	r.appendEvent(e.journal, journal.Event{
		Kind:    journal.KindDecision,
		Status:  string(StatusConcluded),
		Note:    "target N reached / verdict " + verdict.Outcome,
		Verdict: &finalVerdict,
	})
	r.emitProgressLocked(e, ProgressTriggerConcluded, "", e.spanCtx) // #2059
	jrnl := e.journal
	flagKey := e.flagKey
	r.mu.Unlock()

	r.log.Info("experiment.concluded", "experiment_id", id, "outcome", verdict.Outcome)

	var promotion *PromotionResult
	if verdict.Outcome == CohortOutcomePromote {
		res := r.promote(ctx, id, jrnl)
		promotion = &res
	}

	// Terminal: DONE when the promotion acted, DISCARDED otherwise (#1701). A
	// terminal status set while the promotion ran (AbortAll: kill switch or
	// experiments_enabled off) is kept, never overwritten.
	r.mu.Lock()
	e = r.entries[id]
	final := StatusDiscarded
	switch {
	case e != nil && e.status.IsTerminal():
		final = e.status
	case promotion != nil && promotionActed(*promotion):
		final = StatusDone
	}
	r.mu.Unlock()

	note := "experiment concluded: " + verdict.Outcome
	if verdict.Reason != "" {
		note += " (" + verdict.Reason + ")"
	}
	if promotion != nil {
		note += "; promotion " + promotion.Outcome
		if promotion.Reason != "" {
			note += ": " + promotion.Reason
		}
	}
	r.teardown(ctx, id, flagKey, final, note)
	// teardown never overwrites a terminal status, so report the one that holds.
	if st, ok := r.Status(id); ok {
		return st, nil
	}
	return final, nil
}

// promote routes a concluded+promote experiment to the Promoter seam
// (#961/#980), transitioning it to PROMOTING, and journals what the promotion
// achieved (#1701). It returns the result; conclude sets the terminal status. A nil
// Promoter is a promotion that could not act (discarded). A failed promotion never
// blocks teardown.
func (r *Registry) promote(ctx context.Context, id string, jrnl *journal.Journal) PromotionResult {
	r.mu.Lock()
	e := r.entries[id]
	if e == nil {
		r.mu.Unlock()
		return PromotionResult{Outcome: PromotionDiscarded, Reason: "experiment no longer registered"}
	}
	if e.status.IsTerminal() {
		// AbortAll (kill switch, experiments_enabled off) won the race after the
		// conclusion: the promotion does not act.
		st := e.status
		r.mu.Unlock()
		return PromotionResult{Outcome: PromotionDiscarded, Reason: "experiment " + string(st) + " before promotion"}
	}
	if e.status != StatusConcluded {
		// UC-043 BR-006 / BR-007 (#2042): only a concluded experiment is promoted, so
		// one already promoting is never promoted a second time.
		st := e.status
		r.mu.Unlock()
		return PromotionResult{Outcome: PromotionDiscarded, Reason: "experiment " + string(st) + ", not concluded"}
	}
	if r.shuttingDown() {
		// UC-043 A20 (#2332): a conclusion that lands after the graceful stop began
		// keeps its sample and verdict but does not promote: no issue, PR or real
		// default write while the agent goes down. Journaled `discarded: shutdown`, the
		// reason a deferred write dropped at shutdown gets (A17 step 3), so the
		// experiment ends discarded and a restart finds the outcome (A15 step 3).
		res := PromotionResult{Outcome: PromotionDiscarded, Reason: PromotionReasonShutdown}
		promotion := res
		r.appendEvent(e.journal, journal.Event{
			Kind:      journal.KindPromotion,
			Status:    res.Outcome,
			Note:      res.Reason,
			Promotion: &promotion,
		})
		r.mu.Unlock()
		r.log.Info("experiment.promotion_skipped_at_shutdown", "experiment_id", id)
		return res
	}
	e.status = StatusPromoting
	// A later outcome (deferred queue, kill switch) journaled before this promotion's
	// own event waits until it, so the journal keeps their order (UC-043 A17).
	e.promotionInFlight = true
	// UC-043 BR-007 (#2042): write-ahead. The promoter acts only once the promoting
	// decision is durable, so a restart never finds a promotion that acted without
	// one (A15). A summary rewrite that fails after the line is durable still counts.
	journalErr := r.appendEventErr(e.journal, journal.Event{
		Kind:   journal.KindDecision,
		Status: string(StatusPromoting),
		Note:   "routing to promoter",
	})
	if errors.Is(journalErr, journal.ErrSummaryWrite) {
		journalErr = nil
	}
	r.emitProgressLocked(e, ProgressTriggerPromoting, "", e.spanCtx) // #2059
	// #1188: re-parent the code_improvement.promote span as the experiment's TERMINAL
	// child. The promote span is started from the ctx passed to Promoter.Promote, so
	// installing the still-live experiment ROOT span on the ctx (an in-process
	// parent-child, the span lives in THIS process) makes the promotion the experiment's
	// terminal child with no change to the promote package. The span is captured under
	// the lock (it is ended later, in teardown, after this returns). A nil span (tracer
	// unwired) leaves ctx unchanged — graceful.
	span := e.span
	r.mu.Unlock()

	if span != nil {
		ctx = trace.ContextWithSpan(ctx, span)
	}

	res := PromotionResult{Outcome: PromotionDiscarded, Reason: "no promoter wired"}
	switch {
	case journalErr != nil:
		// Refused: nothing acted. The result is still journaled below, so a restart
		// finds a promotion event and does not report promotion_interrupted.
		res = PromotionResult{Outcome: PromotionDiscarded, Reason: "promotion not journaled: " + journalErr.Error()}
	case r.promo != nil:
		res = r.promo.Promote(ctx, jrnl.CompactView())
	}
	if !promotionActed(res) {
		r.log.Warn("experiment.promote_failed", "experiment_id", id,
			"outcome", res.Outcome, "reason", res.Reason)
	}

	// Journal the result whatever the experiment's status is now: an AbortAll that
	// raced the promotion does not undo an issue, PR or real default it produced.
	r.mu.Lock()
	if e := r.entries[id]; e != nil {
		promotion := res
		r.appendEvent(e.journal, journal.Event{
			Kind:      journal.KindPromotion,
			Status:    res.Outcome,
			Note:      res.Reason,
			Promotion: &promotion,
		})
		if res.ForeignOwner != "" {
			e.recommendOwner = res.ForeignOwner
		}
		r.flushLaterPromotionsLocked(e)
	}
	r.mu.Unlock()
	return res
}

// flushLaterPromotionsLocked journals the later promotion outcomes RecordPromotion
// held back while e's promotion ran, after its own event. Assumes r.mu is held.
func (r *Registry) flushLaterPromotionsLocked(e *entry) {
	e.promotionInFlight = false
	for _, res := range e.laterPromotions {
		promotion := res
		r.noteRecommendationLocked(e, res)
		r.appendEvent(e.journal, journal.Event{
			Kind:      journal.KindPromotion,
			Status:    res.Outcome,
			Note:      res.Reason,
			Promotion: &promotion,
		})
	}
	e.laterPromotions = nil
}

// RestoredRevert is a hold-key watch or re-watch revert that waited when the agent
// stopped (its experiment's last promotion event has RevertPending, UC-043 A17).
type RestoredRevert struct {
	ExperimentID string
	FlagKey      string
	// Rewatch: the re-watch queued it (its journal reason prefix).
	Rewatch bool
	// Value is the value the experiment promoted (its intent's experimental value,
	// which is what the promotion wrote). The revert runs only while the flag's
	// promotion overlay still holds it, so a restart never deletes a newer
	// promotion's value.
	Value any
}

// DeferredLostAtRestart is the reason journaled for a deferred hold-key write a
// restart lost (UC-043 A17 step 3, A15).
const DeferredLostAtRestart = "lost_at_restart"

// sweepDeferredLocked handles a done journal whose last promotion event a restart
// interrupted (UC-043 A17 step 3): a write still `deferred` was lost with the
// in-memory queue and is journaled `discarded: lost_at_restart`; a revert still
// pending is kept for TakeRestoredReverts. Assumes r.mu is held.
func (r *Registry) sweepDeferredLocked(id string, jrnl *journal.Journal) {
	events, err := jrnl.Events()
	if err != nil {
		return
	}
	var last *journal.Promotion
	for i := len(events) - 1; i >= 0; i-- {
		if events[i].Kind == journal.KindPromotion && events[i].Promotion != nil {
			last = events[i].Promotion
			break
		}
	}
	switch {
	case last == nil:
	case last.Outcome == PromotionDeferred:
		promotion := PromotionResult{Outcome: PromotionDiscarded, Reason: DeferredLostAtRestart}
		r.appendEvent(jrnl, journal.Event{Kind: journal.KindPromotion, Status: promotion.Outcome, Note: promotion.Reason, Promotion: &promotion})
		r.log.Warn("experiment: a deferred hold-key promotion was lost at restart (UC-043 A17)", "experiment_id", id)
	case last.RevertPending:
		r.restoredReverts = append(r.restoredReverts, RestoredRevert{
			ExperimentID: id, FlagKey: jrnl.Intent().FlagKey, Rewatch: strings.HasPrefix(last.Reason, "re-watch: "),
			Value: jrnl.Intent().ExperimentalValue,
		})
	}
}

// TakeRestoredReverts returns, once, the waiting reverts Rehydrate found.
func (r *Registry) TakeRestoredReverts() []RestoredRevert {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := r.restoredReverts
	r.restoredReverts = nil
	return out
}

// pendingPromotionSet returns the flags with a waiting deferred promotion write.
// It must be called without r.mu held.
func (r *Registry) pendingPromotionSet() map[string]struct{} {
	out := map[string]struct{}{}
	if r.pendingPromotions == nil {
		return out
	}
	for _, flag := range r.pendingPromotions() {
		out[flag] = struct{}{}
	}
	return out
}

// RecordPromotion journals a later outcome of an experiment's promotion (UC-043
// A1, #1704: the kill switch reverted its autonomous real default). It appends a
// promotion event whatever the experiment's status is, and never changes that
// status. An unknown experiment id is ignored (logged).
func (r *Registry) RecordPromotion(id string, res PromotionResult) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.entries[id]
	if e == nil {
		// A done experiment is not held in memory after a restart (Rehydrate skips
		// it), yet the deferred queue and the kill switch journal on it (UC-043 A17).
		if jrnl, err := journal.Load(r.root, id); err == nil {
			promotion := res
			r.appendEvent(jrnl, journal.Event{Kind: journal.KindPromotion, Status: res.Outcome, Note: res.Reason, Promotion: &promotion})
			if res.ForeignOwner != "" {
				// Not held in memory: build the snapshot from the journal (UC-043 A19).
				view := jrnl.CompactView()
				ghost := &entry{
					id: id, journal: jrnl, status: Status(view.Status),
					arms: append([]string(nil), view.Intent.Arms...), recommendOwner: res.ForeignOwner,
				}
				events, _ := jrnl.Events()
				ghost.endReason = teardownReasonOf(events)
				r.emitProgressLocked(ghost, ProgressTriggerRecommendation, ghost.endReason, trace.SpanContext{})
			}
			return
		}
		r.log.Warn("experiment.promotion_unrecorded", "experiment_id", id, "outcome", res.Outcome, "reason", res.Reason)
		return
	}
	if e.promotionInFlight {
		// Its own promotion event is not journaled yet: keep the order (UC-043 A17).
		e.laterPromotions = append(e.laterPromotions, res)
		return
	}
	promotion := res
	r.appendEvent(e.journal, journal.Event{
		Kind:      journal.KindPromotion,
		Status:    res.Outcome,
		Note:      res.Reason,
		Promotion: &promotion,
	})
	r.noteRecommendationLocked(e, res)
}

// noteRecommendationLocked records a refused promotion's foreign owner (UC-043 A19,
// #2276) and, for a later outcome of an experiment that already ended (the deferred
// queue), emits a snapshot so the dashboard sees the recommendation. Assumes r.mu
// is held.
func (r *Registry) noteRecommendationLocked(e *entry, res PromotionResult) {
	if res.ForeignOwner == "" {
		return
	}
	e.recommendOwner = res.ForeignOwner
	if e.status.IsTerminal() {
		r.emitProgressLocked(e, ProgressTriggerRecommendation, e.endReason, e.spanCtx)
	}
}

// Abort terminates an experiment (kill-switch / capacity reclaim / error): it
// records the ABORTED transition, tears down the targeting, and frees capacity.
// A terminal experiment is a no-op. reason is recorded for the audit trail.
func (r *Registry) Abort(ctx context.Context, id, reason string) error {
	r.mu.Lock()
	e := r.entries[id]
	if e == nil {
		r.mu.Unlock()
		return fmt.Errorf("registry: abort: unknown experiment %q", id)
	}
	if e.status.IsTerminal() {
		r.mu.Unlock()
		return nil
	}
	flagKey := e.flagKey
	r.mu.Unlock()

	r.teardown(ctx, id, flagKey, StatusAborted, reason)
	return nil
}

// AbortAll aborts every live (non-terminal) experiment — the kill-switch path
// (design §7.2 / §10: auto-stop on agent.json `enabled` false). It is called when
// the agent is disabled. Returns the ids aborted.
func (r *Registry) AbortAll(ctx context.Context, reason string) []string {
	r.mu.Lock()
	ids := make([]string, 0, len(r.entries))
	for id, e := range r.entries {
		if !e.status.IsTerminal() {
			ids = append(ids, id)
		}
	}
	r.mu.Unlock()

	sort.Strings(ids) // deterministic order (tests + stable logs)
	// UC-046 A12: force-end in-flight pattern games, marked non-exact kill_switch.
	var patternGames []string
	if r.patternTracker != nil {
		r.mu.Lock()
		patternGames = r.patternGamesLocked(ids)
		r.mu.Unlock()
	}
	for _, gid := range patternGames {
		r.patternTracker.Abort(gid, patterngame.ReasonKillSwitch)
	}
	for _, id := range ids {
		_ = r.Abort(ctx, id, reason)
	}
	return ids
}

// teardown is the inverse of Start (design §10): it strips the experiment's
// shadow targeting branch via the TargetingWriter (#977), records the terminal
// decision, sets the terminal status, and frees capacity (clearing the in-flight
// game map so the freed slots are available to other experiments on the next
// AllocateAndSpawn). Idempotent on a terminal experiment.
func (r *Registry) teardown(ctx context.Context, id, flagKey string, terminal Status, reason string) {
	// Runs after the registry lock below is released (defers are LIFO).
	if r.onTerminal != nil {
		defer r.onTerminal(id)
	}
	// Strip the targeting OUTSIDE the registry lock (it does file I/O). Stripping a
	// shadow branch only ever affects shadow games, so it is invariant-safe.
	if r.target != nil {
		if err := r.target.Strip(ctx, flagKey, id); err != nil {
			r.log.Warn("experiment.teardown_strip_failed", "experiment_id", id, "error", err)
		}
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.entries[id]
	if e == nil {
		return
	}
	// #1188: end the long-lived agent.experiment ROOT span here — teardown is the
	// SINGLE terminal chokepoint every terminal transition routes through (conclude →
	// discarded/done, abort), so ending it here guarantees no leak on ANY terminal path.
	// It is placed BEFORE the idempotency guard (and is itself idempotent) so a path
	// that finds the status already terminal still ends the span exactly once.
	progressLink := e.spanCtx // #2059: the Link target, read before the span ends
	r.endSpanLocked(e)
	// A terminal status is never overwritten: an AbortAll that raced a conclusion
	// keeps the experiment aborted (#1701).
	if e.status.IsTerminal() {
		return
	}
	e.status = terminal
	e.endReason = reason
	// Free capacity: drop the in-flight games (reservations + bound games). Their
	// slots return to the global cap for other experiments. gamePairs is the parallel
	// (game_id → pair index) map; clear it too so no stale binding survives teardown.
	e.games = make(map[string]string)
	e.gamePairs = make(map[string]int)
	r.appendEvent(e.journal, journal.Event{
		Kind:   journal.KindOutcome,
		Status: string(terminal),
		Note:   reason,
	})
	r.emitProgressLocked(e, ProgressTriggerTerminal, reason, progressLink) // #2059
	r.log.Info("experiment.torn_down", "experiment_id", id, "status", string(terminal), "reason", reason)
}

// SetShutdownContext sets the context whose cancellation marks the start of the
// agent's graceful stop (UC-043 A20, #2332). The experiment loop calls it with its
// run context before any proposer or tick can reach the registry's write paths.
func (r *Registry) SetShutdownContext(ctx context.Context) { r.shutdown.Store(&ctx) }

// shuttingDown reports whether the graceful stop has begun.
func (r *Registry) shuttingDown() bool {
	p := r.shutdown.Load()
	return p != nil && (*p).Err() != nil
}

// SetTerminalHook registers f to be told an experiment's id when it reaches a
// terminal status (UC-042 BR-005: its linked shadow history is evicted). Call it
// before the registry serves; f must not call back into the registry.
func (r *Registry) SetTerminalHook(f func(experimentID string)) { r.onTerminal = f }

// Rehydrate rebuilds the live registry from the durable journal on startup (the
// #831 fix, design §7.2): it scans the journal root for experiment dirs, Loads
// each, and reconstructs an entry from its compact view (status + arms). It does
// NOT re-load the in-flight game map — a restart loses no DURABLE state (concluded
// games are folded into the journal summary), and any game that was in-flight at
// crash time is no longer running, so the registry starts each rehydrated
// experiment with an empty in-flight set and refills capacity on the next
// AllocateAndSpawn. Terminal experiments are skipped (their work is done).
//
// CRN cursors (#1003): like armCursor, pairCursor restarts at 0 on rehydrate —
// the in-flight set is empty after a crash, so the registry resumes allocating
// pairs from index 0. A pairIndex may therefore repeat across a restart boundary
// (same seed, but a genuinely NEW pair of games); the paired-difference verdict
// (#1004) matches the two games of a pair by the (pair_index + the game_ids
// assigned to it) recorded contiguously in the journal, so a post-restart repeat
// is a distinct pair, not an aliasing hazard. This mirrors the accepted armCursor
// restart semantics and keeps Rehydrate free of an event replay.
func (r *Registry) Rehydrate(ids []string) error {
	r.mu.Lock()
	var lost, unstarted, unfinished []string
	var errs []error
	defer func() {
		r.mu.Unlock()
		// UC-046 BR-015: a pattern experiment whose pin cannot be restored is discarded.
		for _, id := range lost {
			r.teardown(context.Background(), id, r.flagKeyOf(id), StatusDiscarded, patterngame.DiscardPinLost)
		}
		// UC-043 A8 (#1700): a restored PROPOSED experiment never started (its start
		// failed, or the agent stopped between declare and start) and nothing starts
		// it again, so abort it rather than let it hold its flag and a budget slot.
		for _, id := range unstarted {
			r.teardown(context.Background(), id, r.flagKeyOf(id), StatusAborted, AbortReasonStartFailed)
		}
		// UC-043 A15 (#2042): a restored CONCLUDED or PROMOTING experiment is ended
		// without a promotion; nothing else would ever finish it.
		for _, id := range unfinished {
			r.finishRestored(id)
		}
	}()
	for _, id := range ids {
		// #1731: an id this registry already holds was declared by this process
		// (Declare registers it under r.mu together with its journal), never a
		// leftover of a restart: leave it to its declarer.
		if _, known := r.entries[id]; known {
			continue
		}
		jrnl, err := journal.Load(r.root, id)
		if err != nil {
			// UC-043 A15 (#2042): one bad journal does not stop the restore of the
			// others; every failure is logged and returned.
			err = fmt.Errorf("registry: rehydrate %q: %w", id, err)
			r.log.Error("experiment.rehydrate_failed", "experiment_id", id, "error", err)
			errs = append(errs, err)
			continue
		}
		view := jrnl.CompactView()
		st := Status(view.Status)
		if st == "" {
			st = StatusRunning
		}
		if st.IsTerminal() {
			// Done experiment: nothing to manage, except a deferred hold-key promotion
			// the restart interrupted (UC-043 A17 step 3).
			r.sweepDeferredLocked(id, jrnl)
			continue
		}
		e := &entry{
			id:        id,
			journal:   jrnl,
			status:    st,
			flagKey:   view.Intent.FlagKey,
			arms:      append([]string(nil), view.Intent.Arms...),
			games:     make(map[string]string),
			gamePairs: make(map[string]int),
		}
		if events, evErr := jrnl.Events(); evErr == nil {
			e.recommendOwner = lastForeignOwner(events) // UC-043 A19
		}
		// #1188 RESTART DISCONTINUITY: a rehydrated experiment gets a BRAND-NEW root
		// span with a fresh trace_id — the SpanContext is NOT persisted across the
		// restart (the pre-restart trace is already closed and un-appendable). The new
		// span outlives this rehydrated entry exactly like a freshly-Started one and is
		// ended in teardown. Rehydrate is the only place a RUNNING entry is created
		// WITHOUT going through Start, so the span must be started here too. A rehydrated
		// PROPOSED entry also gets one; if Start later runs, startSpanLocked is a no-op
		// (idempotent), so no double span.
		r.startSpanLocked(e, jrnl.Intent())
		r.entries[id] = e
		if st == StatusProposed {
			unstarted = append(unstarted, id)
			continue
		}
		if st == StatusConcluded || st == StatusPromoting {
			unfinished = append(unfinished, id)
			continue
		}
		if r.patterns != nil && !r.restorePatternLocked(e) {
			lost = append(lost, id)
			continue
		}
		// #2059: once per restored experiment that stays live (the torn-down ones
		// above emit their terminal snapshot instead).
		r.emitProgressLocked(e, ProgressTriggerRehydrate, "", e.spanCtx)
	}
	return errors.Join(errs...)
}

// finishRestored ends an experiment restored as CONCLUDED or PROMOTING (UC-043
// A15, #2042) without ever calling the promoter: its in-flight games are lost and
// its evidence predates the restart. It needs only the local journal and the
// targeting strip (teardown), so it is safe inside Rehydrate.
//   - a promotion event after the last concluded decision (any status): the
//     promotion ran, so end done / discarded from it (BR-006: once);
//   - promoting without one: unknown outcome, abort promotion_interrupted + WARN;
//   - concluded without one: discarded, "concluded before restart: <outcome>; not promoted".
func (r *Registry) finishRestored(id string) {
	r.mu.Lock()
	e := r.entries[id]
	if e == nil || (e.status != StatusConcluded && e.status != StatusPromoting) {
		r.mu.Unlock()
		return
	}
	jrnl, flagKey, st := e.journal, e.flagKey, e.status
	r.mu.Unlock()

	ctx := context.Background()
	if res, ok := r.journaledPromotion(id, jrnl); ok {
		final := StatusDiscarded
		if promotionActed(res) {
			final = StatusDone
		}
		note := "promotion " + res.Outcome
		if res.Reason != "" {
			note += ": " + res.Reason
		}
		r.teardown(ctx, id, flagKey, final, note+"; finished after an agent restart")
		return
	}
	if st == StatusPromoting {
		r.log.Warn("experiment.promotion_interrupted", "experiment_id", id, "flag_key", flagKey)
		r.teardown(ctx, id, flagKey, StatusAborted, AbortReasonPromotionInterrupted)
		return
	}
	outcome := "unknown"
	if v := jrnl.CompactView().Verdict; v != nil && v.Outcome != "" {
		outcome = v.Outcome
	}
	r.teardown(ctx, id, flagKey, StatusDiscarded, "concluded before restart: "+outcome+"; not promoted")
}

// journaledPromotion returns the first promotion event after the experiment's
// last concluded decision: the result of its one promotion (later ones are
// kill-switch or re-watch reverts of it). An unreadable log reports none.
func (r *Registry) journaledPromotion(id string, jrnl *journal.Journal) (PromotionResult, bool) {
	evs, err := jrnl.Events()
	if err != nil {
		r.log.Warn("experiment.rehydrate_events_unreadable", "experiment_id", id, "error", err)
		return PromotionResult{}, false
	}
	start := -1
	for i, ev := range evs {
		if ev.Kind == journal.KindDecision && ev.Status == string(StatusConcluded) {
			start = i
		}
	}
	if start < 0 {
		return PromotionResult{}, false
	}
	for _, ev := range evs[start+1:] {
		if ev.Kind != journal.KindPromotion {
			continue
		}
		if ev.Promotion != nil {
			return *ev.Promotion, true
		}
		return PromotionResult{Outcome: ev.Status, Reason: ev.Note}, true
	}
	return PromotionResult{}, false
}

// --- read accessors (telemetry / tests) ---

// Status returns the current status of an experiment, or ("", false) if unknown.
func (r *Registry) Status(id string) (Status, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.entries[id]
	if e == nil {
		return "", false
	}
	return e.status, true
}

// InFlight returns the experiment's current in-flight shadow-game count (bound
// games + reservations), or 0 if unknown.
func (r *Registry) InFlight(id string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	if e := r.entries[id]; e != nil {
		return e.inFlight()
	}
	return 0
}

// TotalInFlight returns the total in-flight shadow games across all experiments —
// the registry's current draw against the cap.
func (r *Registry) TotalInFlight() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.inFlightLocked()
}

// Capacity returns the configured fixed shadow-game cap (the capacity-bookkeeping
// bound, DefaultMaxShadowGames).
func (r *Registry) Capacity() int { return r.maxCap }

// EffectiveCapacity returns the effective in-flight concurrency cap (#998) — the
// number of concurrent shadow spawns the registry will actually hold, bounded to
// the coordinator's real concurrency (the experiment_shadow_effective_concurrency
// flag, default 4) and clamped to Capacity.
func (r *Registry) EffectiveCapacity() int { return r.effectiveCap }

// MaxGames returns the live per-experiment games budget (0 = no limit), as last
// set by Reconfigure (#1044) — the effective min(env ceiling, flag) since #1810.
func (r *Registry) MaxGames() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.maxGames
}

// Reconfigure live-updates the registry's runtime caps (#1044) so a hot-reload of
// experiment_shadow_effective_concurrency / experiment_max_games / verdict_min_n
// takes effect with NO restart. The experiment loop calls it each tick from the
// live agent.json flags; the compiled default (equal to the flag default, #1907) is what the
// flag falls back to, so a fresh agent.json reproduces byte-identical behavior.
//
// SAFETY (the #1044 hard-part-2 requirement): every field is updated UNDER r.mu,
// the same lock that guards all capacity/lifecycle bookkeeping (spawnsInFlight,
// the games maps, tombstones, the round-robin cursors). It only changes the cap
// VALUES the allocator reads on its next pass — it never touches an in-flight
// game, a reservation, or a tombstone — so lowering a cap mid-run simply stops NEW
// allocations until in-flight drains below it; existing shadow games conclude /
// release normally. minN is mirrored onto the verdict too (via SetThresholds) so
// the two stay coherent, exactly as construction did. Non-positive inputs are
// treated per-field as the constructor does:
//   - effectiveConcurrency <= 0 ⇒ ignored (keep current; a zero cap would stall
//     every experiment — misconfiguration);
//   - maxGames < 0 ⇒ normalized to 0 (explicit "disable the games budget"); 0 is a
//     legitimate disable and is honored verbatim;
//   - minN <= 0 ⇒ ignored (keep current; the verdict's own floor also guards).
//
// minPairs is verdict-only (the registry does not mirror it); it is forwarded to
// the verdict via SetThresholds. A negative minPairs is ignored there.
func (r *Registry) Reconfigure(effectiveConcurrency, maxGames, minN, minPairs int) {
	r.mu.Lock()
	if effectiveConcurrency > 0 {
		eff := effectiveConcurrency
		if eff > r.maxCap {
			eff = r.maxCap // never exceed the capacity-bookkeeping bound.
		}
		r.effectiveCap = eff
	}
	if maxGames >= 0 {
		r.maxGames = maxGames // 0 ⇒ disable the games budget (other guards still terminate).
	}
	if minN > 0 {
		// LIVE minN raise (#1051): updating r.minN here applies the new floor to FUTURE
		// Declare calls (which clamp target_n up to r.minN at declare time) and to the
		// per-experiment "give up" guard (shouldGiveUp computes target = max(target_n,
		// r.minN) on every read), but it does NOT retro-clamp the PERSISTED target_n of an
		// already-declared experiment. That is HARMLESS and deliberately left
		// un-retro-clamped: the verdict's own minN — mirrored below via SetThresholds — is
		// the real conclusiveness backstop (an experiment can never reach a conclusive
		// verdict below it regardless of its stored target_n), and the give-up guard already
		// lifts the lifecycle "target reached" point to max(target_n, r.minN) on every read.
		// Rewriting the durable intent mid-flight would disturb in-flight game accounting for
		// no behavioral gain, so we don't.
		r.minN = minN
	}
	r.mu.Unlock()

	// Mirror the verdict gates outside r.mu (the verdict has its OWN lock; holding
	// both is unnecessary and invites a lock-ordering question). SetThresholds
	// ignores a non-positive value per gate, so a transient flagd hiccup never
	// loosens the verdict.
	if v, ok := r.verdict.(*Verdict); ok {
		v.SetThresholds(minN, minPairs)
	}
}

// ReconfigureVerdictFloats live-mirrors the four migrated FLOAT verdict knobs (#1214)
// onto the verdict so a hot-reload of verdict_effect_threshold / verdict_min_raw_effect
// / verdict_sd_floor / verdict_anchor_margin takes effect on the NEXT verdict with no
// restart — the float analog of the minN/minPairs mirroring in Reconfigure. The
// experiment loop calls it each tick from the live agent.json flags, alongside
// Reconfigure; the compiled default (equal to the flag default, #1907) is what the flag falls back to.
// These knobs are verdict-only (the registry itself reads none of them), so unlike
// minN this method only forwards to the verdict — it touches no registry state and
// takes no registry lock. SetFloatThresholds ignores a non-positive value per knob, so
// a transient flagd hiccup never loosens a gate. A non-*Verdict CohortStat (a custom
// test seam) is left untouched.
func (r *Registry) ReconfigureVerdictFloats(effectThreshold, minRawEffect, sdFloor, anchorMargin float64) {
	if v, ok := r.verdict.(*Verdict); ok {
		v.SetFloatThresholds(effectThreshold, minRawEffect, sdFloor, anchorMargin)
	}
}

// Live returns the ids of experiments that may be allocated shadow capacity —
// i.e. those in RUNNING (targeting written, ready to accrue games), sorted. A
// PROPOSED experiment occupies a registry slot but is not yet spawnable, so it is
// not returned here.
func (r *Registry) Live() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.liveIDsLocked()
}

// ActiveFlags returns the set of flag keys that have at least one LIVE
// (non-terminal: PROPOSED or RUNNING) experiment, plus the flags whose deferred
// promotion write still waits (UC-043 A17 step 3, opposer M6). The dynamic
// declarer (#995) uses it to avoid stacking a second concurrent experiment on a
// flag that is already under test. Any other flag torn down (terminal) is not
// included — its targeting branch is stripped, so it is free to experiment on again.
func (r *Registry) ActiveFlags() map[string]struct{} {
	out := r.pendingPromotionSet()
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, e := range r.entries {
		if !e.status.IsTerminal() {
			out[e.flagKey] = struct{}{}
		}
	}
	return out
}

// LiveCount returns the number of LIVE (non-terminal) experiments the registry is
// managing — PROPOSED + RUNNING + CONCLUDED/PROMOTING (anything not yet torn
// down). The dynamic declarer (#995) uses it as the over-declaration backstop:
// it stops declaring once the live set fills the concurrent-experiment budget.
func (r *Registry) LiveCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := 0
	for _, e := range r.entries {
		if !e.status.IsTerminal() {
			n++
		}
	}
	return n
}

// ExperimentSpanContext returns the SpanContext of an experiment's long-lived
// agent.experiment ROOT span (#1188), for re-parenting that experiment's analysis
// spans (retro / summary) under it from components OUTSIDE the registry. It is a
// clean READ accessor: it touches no lifecycle/mutation state, takes the lock only to
// read the entry's recorded SpanContext, and never mutates.
//
// ok=false (and the zero SpanContext) when the experiment is unknown, has no root span
// (tracer unwired, or already torn down + span ended), or the SpanContext is invalid —
// every "no usable parent" case. Callers fall back to their CURRENT behavior on
// ok=false, so a NON-experiment game (empty/unknown experiment_id) never re-parents:
// graceful, never breaking span emission.
func (r *Registry) ExperimentSpanContext(id string) (trace.SpanContext, bool) {
	r.mu.Lock()
	e := r.entries[id]
	var sc trace.SpanContext
	if e != nil {
		sc = e.spanCtx
	}
	r.mu.Unlock()
	if e == nil || !sc.IsValid() {
		return trace.SpanContext{}, false
	}
	return sc, true
}

// CompactView returns the bounded journal view for an experiment (intent +
// per-arm aggregates + recent tail) — the read surface the dashboard / LLM
// consume. ok=false if unknown.
func (r *Registry) CompactView(id string) (journal.CompactView, bool) {
	r.mu.Lock()
	e := r.entries[id]
	r.mu.Unlock()
	if e == nil {
		return journal.CompactView{}, false
	}
	return e.journal.CompactView(), true
}

// --- locked helpers ---

// inFlightLocked sums in-flight games across all experiments. Assumes r.mu held.
func (r *Registry) inFlightLocked() int {
	total := 0
	for _, e := range r.entries {
		total += e.inFlight()
	}
	return total
}

// liveIDsLocked returns the sorted ids of PROPOSED/RUNNING experiments. Assumes
// r.mu held. Only RUNNING experiments are spawnable, but PROPOSED ones still
// occupy a registry slot; nextAllocation filters to RUNNING via the share check
// implicitly — only RUNNING experiments are returned here for allocation fairness.
func (r *Registry) liveIDsLocked() []string {
	ids := make([]string, 0, len(r.entries))
	for id, e := range r.entries {
		if e.status == StatusRunning {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	return ids
}

// IsBound reports whether gameID is an in-flight game bound to one of this
// registry's experiments (UC-046 BR-016, #1405). It is read-only: the experiment
// loop uses it to tell its own shadow games, whose telemetry end waits for the
// runner's terminal outcome, from foreign or already-released ids.
func (r *Registry) IsBound(gameID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	_, _, e := r.findGameLocked(gameID)
	return e != nil
}

// ExperimentFlag returns the flag under test in a game (UC-040 A40, BR-028; #1975):
// the experiment bound to gameID at spawn, else the experiment experimentID names.
// ok=false when the registry knows neither, or the experiment is in a terminal status.
func (r *Registry) ExperimentFlag(gameID, experimentID string) (string, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if gameID != "" {
		if _, _, e := r.findGameLocked(gameID); e != nil {
			return e.flagKey, true
		}
	}
	// A torn-down experiment (aborted, concluded, discarded) is not known any more:
	// its leftover games decide as before (UC-040 A40 step 4).
	if e := r.entries[experimentID]; experimentID != "" && e != nil && !e.status.IsTerminal() {
		return e.flagKey, true
	}
	return "", false
}

// findGameLocked looks up the experiment + arm owning a coordinator game_id.
// Assumes r.mu held.
func (r *Registry) findGameLocked(gameID string) (id, arm string, e *entry) {
	for eid, ent := range r.entries {
		if a, ok := ent.games[gameID]; ok {
			return eid, a, ent
		}
	}
	return "", "", nil
}

// recordDecision appends a KindDecision status-transition event. Assumes the
// caller holds r.mu (the journal serializes its own write). Errors are logged,
// never fatal — the in-memory transition already happened and Rehydrate would
// re-derive from the prior durable state.
func (r *Registry) recordDecision(jrnl *journal.Journal, status Status, note string) {
	r.appendEvent(jrnl, journal.Event{
		Kind:   journal.KindDecision,
		Status: string(status),
		Note:   note,
	})
}

// appendEvent stamps the event time from the registry clock (if unset) and
// appends it to the journal, logging any error. Centralizes the time injection so
// every registry-emitted event carries a timestamp.
func (r *Registry) appendEvent(jrnl *journal.Journal, e journal.Event) {
	_ = r.appendEventErr(jrnl, e)
}

// appendEventErr is appendEvent that also returns the error, for a caller that
// must know whether the event is durable (UC-043 BR-007).
func (r *Registry) appendEventErr(jrnl *journal.Journal, e journal.Event) error {
	if e.At.IsZero() {
		e.At = r.clock()
	}
	err := jrnl.AppendEvent(e)
	if err != nil {
		r.log.Warn("experiment.journal_append_failed", "kind", string(e.Kind), "error", err)
	}
	return err
}

// lastForeignOwner returns the writer of the latest promotion event the flag
// service refused with 412 writer_mismatch, or "" (UC-043 A19, #2276).
func lastForeignOwner(events []journal.Event) string {
	for i := len(events) - 1; i >= 0; i-- {
		if p := events[i].Promotion; p != nil && p.ForeignOwner != "" {
			return p.ForeignOwner
		}
	}
	return ""
}

// teardownReasonOf returns the teardown reason (the note of the last outcome event).
func teardownReasonOf(events []journal.Event) string {
	for i := len(events) - 1; i >= 0; i-- {
		if events[i].Kind == journal.KindOutcome {
			return events[i].Note
		}
	}
	return ""
}
