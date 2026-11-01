// Package decision holds the agent's decision loop: a gated, throttled hook that
// turns a GameContext snapshot into interventions via a pluggable rules engine
// and action sink, and emits the agent.signal_received -> agent.decision ->
// agent.action audit trace for every evaluation that produces decisions
// (issue #724). ObjectiveRules (#726) is the objective-weighted rules engine;
// the action sink stays a no-op until the intervention API (#730).
//
// On every gated cycle the loop evaluates the agent's OpenFeature control flags
// across all four layers (issues #727, #728) — never cached at startup — and
// applies them in order:
//
//	Existence:  enabled    kill switch; false short-circuits the whole loop
//	                       (a throttled agent.disabled span still records the
//	                       evaluated flags so "agent off" is visible in traces)
//	Objective:  objectives session-goal weights driven into the rules engine via
//	                       a LiveObjectives source (#726 engine reads it each cycle)
//	Backend:    decision.backend  who decides: the rules engine (default) or a
//	                       remote decide backend (decide_hook.go, #1226)
//	Permission: interventions_allowed   allow-list gate (#727)
//	            policy.battery_threshold blocks player-targeted interventions when
//	                                     the target's battery is below threshold
//	            policy.max_interventions_per_minute  weighted sliding-window rate
//	                                     limit across all dispatched interventions
//
// Every decision that the rules return is audited as an agent.decision ->
// agent.action span pair (#724); decisions blocked by any permission gate carry
// decision.blocked=true (with the specific BlockReason) and are NOT applied,
// never silently dropped. Every evaluated flag value plus the per-decision
// outcomes are captured in a LayerState (see layerstate.go) — the span-attribute
// source of truth for #729 — which OnEvaluate returns and retains.
package decision

import (
	"context"
	"log/slog"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	otelmetric "go.opentelemetry.io/otel/metric"
	semconv "go.opentelemetry.io/otel/semconv/v1.34.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/prediction"
)

// Decision is a single intervention the rules engine wants applied.
type Decision struct {
	// Intervention is an identifier from interventions.allowed
	// (docs/research/722-intervention-surface.md §6), e.g. "grant_shield".
	Intervention string
	// TargetSerial scopes the intervention to one player; empty = session-scoped.
	TargetSerial string
	// Value is the optional per-intervention payload the action sink (#730) needs
	// to render a flag value: the sound id for play_audio_cue, the effect name for
	// send_controller_effect, or the numeric target for the state-shaped
	// interventions (music tempo, volume, sensitivity factor, global sensitivity,
	// shield seconds) as a decimal string. Empty means "use the action sink's
	// per-type default" — the rules engine (#726) leaves it empty today, so the
	// Writer (services/agent/actions) supplies sane defaults documented there. The
	// eliminate/revive/end_game edge interventions ignore Value (the target comes
	// from TargetSerial).
	Value string
	// Reason is a human-readable explanation, recorded as decision.reason.
	Reason string
	// ObjectiveServed names the session objective this decision serves
	// (endurance/balanced/accelerate/chaos), recorded as
	// decision.objective_served.
	ObjectiveServed string
	// Fitness holds the fitness values the rule evaluated to reach this
	// decision (e.g. session_duration, target_session_seconds), recorded as
	// fitness.evaluated.
	Fitness map[string]float64
	// Objectives holds the active objective weights used to score this
	// decision, recorded as agent.objectives. Nil/empty (the Noop/Probe/stub
	// engines) renders the "unset" placeholder.
	Objectives map[string]float64
	// GameID is the game this decision was made for (the deciding loop's
	// GameContext.SessionID). runDecision stamps it just before the ActionSink
	// call; engines leave it empty. The writer scopes every intervention to this
	// game on the flag service (UC-053, FR-136/FR-137), so it reaches only this
	// game and dies with it; a decision without a game id is not written.
	GameID string `json:",omitempty"`
	// GameKind is the deciding game's kind in the flag vocabulary ("real" /
	// "shadow", GameContext.GameKind), stamped with GameID before the ActionSink
	// call so the writer can label agent_intervention_writes_total{game_kind}
	// (#2294). Engines leave it empty.
	GameKind string `json:",omitempty"`
	// TargetSerials is the set of players of a group intervention
	// (grant_group_shield, #2467 UC-040 A47): 2 to 6 distinct serials, sorted.
	// Empty for every other intervention, which uses TargetSerial.
	TargetSerials []string `json:",omitempty"`
}

// RulesEngine turns a context snapshot into zero or more Decisions. The
// context carries the active trace; an LLM-backed engine records its inference
// call as a gen_ai.* span (GenAI semantic conventions). The objective weights
// are supplied out-of-band through the engine's ObjectivesSource (the loop
// publishes the per-cycle flag value via LiveObjectives before calling here).
type RulesEngine interface {
	Evaluate(context.Context, gamecontext.GameContext) []Decision
}

// ActionSink applies one decision to the outside world. It is called once per
// decision inside that decision's agent.action span.
type ActionSink interface {
	Apply(context.Context, Decision) error
}

// ActionApplier is optionally implemented by a sink that can turn an Apply into a
// no-op (actions.GatedActionSink while interventions_enabled is off). It reads the
// gate and writes in one call and reports which happened: applied=false means
// nothing was written, so the loop does not count the decision in
// agent_interventions_applied_total (UC-040 A11, D-A9, #1681).
type ActionApplier interface {
	ApplyReported(context.Context, Decision) (applied bool, err error)
}

// FlagSource resolves the agent control flags for a single decision cycle.
// *flags.Flags satisfies it; tests supply a fake.
type FlagSource interface {
	Evaluate(ctx context.Context) flags.Snapshot
}

// objectivePublisher is the optional seam the loop uses to push the per-cycle
// objectives flag into the rules engine. *ObjectiveRules (via its LiveObjectives
// source) satisfies it; engines that ignore objectives (Noop/Probe) do not, and
// the loop simply skips publication for them.
type objectivePublisher interface {
	SetObjectives(map[string]float64)
}

// fitnessPublisher is the optional seam the loop uses to push the per-cycle
// fitness thresholds (fitness.* flags, #731) into the rules engine, mirroring
// objectivePublisher. *ObjectiveRules (via its LiveFitness source) satisfies it;
// engines that ignore fitness (Noop/Probe) do not.
type fitnessPublisher interface {
	SetFitness(FitnessThresholds)
}

// fitnessReader is the optional seam the loop uses to read back the per-cycle
// fitness evaluation the engine computed, so the cycle-level values can be lifted
// onto the decision span as fitness.evaluated (#731). *ObjectiveRules satisfies
// it; engines without a fitness function do not, and the span simply carries no
// cycle-level fitness values for them.
type fitnessReader interface {
	LastFitness() FitnessEvaluation
}

// EvalTrigger describes the OTLP Export that triggered an evaluation, used to
// annotate the agent.signal_received root span with rpc.* semconv attributes and
// backdate it to the moment the Export arrived.
type EvalTrigger struct {
	// Signal is the OTLP signal type: "traces" or "metrics".
	Signal string
	// RPCService is the full OTLP gRPC service name, e.g.
	// "opentelemetry.proto.collector.metrics.v1.MetricsService".
	RPCService string
	// T0 is when the Export handler started processing.
	T0 time.Time
}

// DefaultThrottleInterval bounds how often the evaluate log line and the
// agent.disabled span are emitted, when no explicit throttle is configured. It
// is the behavior-neutral default for the decision.throttle_seconds flag (#766
// F5); callers override it at startup via SetThrottle.
const DefaultThrottleInterval = time.Second

// Loop wires the flag source, rules engine, and action sink together and is
// invoked once per gated signal update. It is safe for concurrent use: the gRPC
// trace and metrics Export handlers each invoke OnEvaluate from their own
// goroutines.
type Loop struct {
	Flags FlagSource
	Rules RulesEngine
	// ProbeRules is the demo/verification engine (decision.probe_decisions, #1810).
	// Built ONCE per loop (it keeps its rate-limit state across flag flips) and
	// swapped in by rulesFor only while the flag is on. nil: the flag has no effect.
	ProbeRules RulesEngine
	Actions    ActionSink
	Log        *slog.Logger
	// Tracer produces the audit spans; tests inject a recording tracer.
	Tracer trace.Tracer

	// mu guards the log-throttle state and lastLayer below.
	mu      sync.Mutex
	lastLog time.Time
	// lastQuietFallback throttles, per fallback reason, the agent.decide span of a
	// configuration fallback on which the rules emitted nothing (emitQuietFallback).
	// Its own timestamps, so the evaluate log line does not use up the window and
	// one reason does not hide another. Lazily allocated; guarded by mu.
	lastQuietFallback map[string]*time.Time
	now               func() time.Time
	// throttle bounds how often the evaluate log line and agent.disabled span are
	// emitted. The STATIC fallback value, set via SetThrottle; a zero value means
	// DefaultThrottleInterval. Used only when throttleSource is nil.
	throttle time.Duration
	// throttleSource, when non-nil, is the LIVE decision.throttle_seconds source
	// (#927): shouldLog() reads it every cycle so a mid-session config-change to the
	// throttle flag takes effect with no restart. It points at the shared
	// flags.LifecycleHolder.DecisionThrottle (wired in main.go), which is itself
	// refreshed only by the OpenFeature configuration-change event — so this is a
	// lock-free atomic load on the hot path, NOT a per-cycle flagd evaluation. When
	// nil (the construction default, and all existing tests) shouldLog falls back to
	// the static throttle, preserving the #766 F5 read-at-startup behavior.
	throttleSource func() time.Duration

	// limiter is the unified weighted per-minute rate limiter spanning all
	// dispatched interventions across cycles (#726 + #728, see ratelimit.go). It
	// is mutex-guarded internally for the concurrent Export handlers.
	limiter rateLimiter
	// lastLayer is the most recent fully-evaluated LayerState, retained so #729
	// (span attribution) and tests can read the per-cycle layer snapshot.
	lastLayer LayerState

	// --- remote decide call gate (#847, #1226) ---
	// llmGate guards the per-game cadence state of the remote decide gate
	// (lastDecideAttempt). It is separate from mu because the cadence
	// read-modify-write happens inside decide(), which mu does not span.
	llmGate sync.Mutex
	// lastDecideAttempt is the time this game's most recent remote decide call was
	// admitted (#1226): the decision.min_interval_ms cadence clock.
	lastDecideAttempt time.Time
	// lastDecideEnded is the time this game's most recent remote decide call
	// returned (answer, timeout or failure), set by noteDecideEnded while the call
	// still holds the in-flight slot, before the flag re-read and any action
	// write; a later discard does not move it (#2123): the
	// decision.min_gap_after_answer_ms clock.
	lastDecideEnded time.Time
	// skippingNoInterventions is true while this game's remote decide cycles are
	// gated because the allow-list is empty (#1298). Guarded by llmGate; it only
	// drives the log-on-state-change debug line.
	skippingNoInterventions bool
	// misconfiguredReason is the configuration fallback reason (unknown_backend,
	// backend_unavailable, async_not_wired) this game's remote decide cycles are
	// currently in, "" when none. Guarded by llmGate; it only drives the
	// log-on-state-change lines of noteMisconfigured.
	misconfiguredReason string
	// budget is the GLOBAL per-minute LLM request cap shared across ALL loops and
	// the post-game retro (#847): llm.max_requests_per_minute bounds remote decide
	// calls and retro calls together. It is injected via SetLLMBudget from the one instance main.go
	// constructs and closes over in the Loop factory, so concurrent games draw down
	// a single budget. Nil until injected; a nil budget gates every remote decide
	// call with llm_budget_exhausted (fail-closed: no shared cap means no LLM).
	budget *llmBudget
	// budgetCeiling, when > 0, is the operator's env ceiling on the per-cycle
	// llm.max_requests_per_minute flag value (AGENT_LLM_MAX_REQUESTS_PER_MINUTE,
	// #1788, NFR-011, FR-152, C-034): the enforced cap is min(ceiling, flag), so a
	// flag value can only tighten it, never loosen it. Zero (unset env, every
	// existing test) means no ceiling: the flag governs. Set at construction.
	budgetCeiling int

	// llmGated counts gated remote decide cycles (agent_llm_gated_total{reason=...}). Defaults
	// to a no-op so a Loop built without metrics wiring still works.
	llmGated otelmetric.Int64Counter
	// decideDiscarded counts remote agent.decide records that carry a
	// decide.discarded_reason (agent_decide_discarded_total{reason}, #2123).
	// Defaults to the global meter provider's counter.
	decideDiscarded otelmetric.Int64Counter
	// predictionDropped counts model predictions dropped by llmio.Parse
	// (agent_prediction_dropped_total{backend,reason}, #2479, UC-040 A45).
	predictionDropped otelmetric.Int64Counter

	// evaluations counts EVERY eval cycle (agent_evaluations_total{signal,outcome},
	// #1053), so signal arrival is observable even on idle cycles where no rule fires
	// and no audit span is emitted. Defaults to a no-op so a Loop built without metrics
	// wiring still works.
	evaluations otelmetric.Int64Counter

	// decisions counts EVERY decision the engine returns, applied or blocked
	// (agent_decisions_total{action,blocked,block_reason}, #790), so the demo
	// dashboard can split applied vs blocked by reason. Defaults to a no-op so a
	// Loop built without metrics wiring still works.
	decisions otelmetric.Int64Counter

	// interventionsApplied counts decisions that pass every permission gate AND
	// apply successfully through the action sink (agent_interventions_applied_total
	// {action}, #790) — the numerator for the applied-rate panel. Defaults to a
	// no-op so a Loop built without metrics wiring still works.
	interventionsApplied otelmetric.Int64Counter

	// asyncInferState is the per-Loop async machinery (see async_state.go): the
	// pile-up guard, the in-flight wait group, this loop's game id, the
	// re-validation context provider, and the agent root context. The remote decide
	// path runs on it; a nil ctxProvider makes a remote backend fall back to rules
	// (decide.fallback_reason=async_not_wired).
	asyncInferState

	// effectSampler is the per-Loop intervention-effect MEASUREMENT machinery (#918):
	// the follow-up window, the in-flight wait group, the bounded pending set, the
	// effect-delta metric, and the timer test seam. Embedded so the effect surface lives
	// in one place (see effect.go). It reuses asyncInferState's ctxProvider/rootCtx/gameID
	// to re-read the game's CURRENT signals at +window. Inert without a context provider,
	// so a Loop not wired for effect measurement is behavior-unchanged.
	effectSampler

	// interventionRecorder is the PER-GAME seam into the #916 rolling narrative
	// timeline (#945): runDecision calls it with each decision's outcome (the
	// intervention id, its target serial, and whether a permission gate BLOCKED it)
	// so the timeline accumulates the agent's own intervention history alongside the
	// state deltas / eliminations / phase transitions the Store observes itself.
	//
	// The Loop and the per-game Store are SEPARATE objects: the receiver pipeline
	// holds both (the gamecontext.Multiplexer's Store per game_id, and this LoopSet's
	// Loop). The factory in main.go closes this over the SAME game_id the Loop serves
	// (mux.AppendInterventionEvent(gameID, ...)), exactly mirroring how #917 wired the
	// per-game ContextProvider (SetContextProvider) into each Loop. That closure is
	// what preserves per-game isolation (#845): an intervention lands in the timeline
	// of the game it targeted, never a shared/global one, so two concurrent games keep
	// independent intervention histories.
	//
	// Nil-safe: a Loop without a recorder (the construction default, rules-only
	// deployments, and all existing tests) simply does not record — purely additive
	// over the pre-#945 behavior. The Store's AppendInterventionEvent it ultimately
	// reaches is mutex-guarded (#916), so this is race-safe under the concurrent
	// trace + metrics Export-handler goroutines that drive runDecision.
	interventionRecorder func(intervention, serial string, blocked bool)
	// applyOutcomeRecorder is the per-game seam for how a dispatched decision's
	// write ended (applied / gated_off / write_failed, #1771), so the game summary
	// can split applied from blocked. nil = record nothing.
	applyOutcomeRecorder func(intervention string, outcome gamecontext.ApplyOutcome)

	// playerName resolves a serial to the player's display name for the
	// player.name attribute on targeted decisions (#1295), injected via
	// SetPlayerNameResolver. Nil (the default) leaves player.name off.
	playerName func(serial string) string

	// decideBackends is the SHARED decision-backend registry (#1226), injected via
	// SetDecideRegistry. It maps decision.backend to a factory for the remote
	// backends; rules is the built-in floor and needs no entry. Nil = no remote
	// backend known (any non-rules value falls back to rules, unknown_backend).
	decideBackends *DecideRegistry
	// pattern observes pattern games (UC-046): remote-backend marking and the
	// decision sequence. nil = not wired (no pattern bookkeeping).
	pattern PatternObserver

	// charges is this game's shield-charge ledger (UC-040 A31 step 1, chargeledger.go),
	// guarded by its own mutex. shieldChargeMode is its trigger-mode source; nil keeps
	// the ledger inert.
	charges          chargeLedger
	shieldChargeMode ShieldChargeMode
	// promptProfile is the decision.prompt_profile source of remote decide requests
	// (#2467 PR 5, UC-040 BR-036); nil leaves Request.PromptProfile empty (v1).
	promptProfile PromptProfileSource
	// goalSource is the goal-profile source of the prompt's goal lines (#2479);
	// nil renders none.
	goalSource GoalSource
	// shieldSeconds is the grant_shield length source (UC-040 BR-029, #2395): the
	// interventions/shield_seconds "default" variant, read per grant. nil (the golden
	// harness, every older test) leaves the decision's value empty, so the writer and
	// the ledger use DefaultShieldSeconds.
	shieldSeconds ShieldSecondsSource

	// flagState is this partition's agent.flag_state digest (#2059, flag_state.go).
	flagState flagStateTracker
	// applied is this game's applied-value ledger (UC-040 A35, BR-023,
	// appliedledger.go), guarded by its own mutex. suppressReapplied switches the
	// already-applied check on; alreadyAppliedRecorder is its timeline seam.
	applied                appliedLedger
	suppressReapplied      bool
	alreadyAppliedRecorder func(intervention, serial string)
	// experimentFlag looks up the flag under test in a game (UC-040 A40, BR-028;
	// experimentflag.go). nil skips nothing.
	experimentFlag func(c gamecontext.GameContext) string
	// experimentFlagSeen quiets repeated experiment_flag blocks (UC-040 A40 step 6).
	experimentFlagSeen experimentFlagRefusals
	// predictions is the SHARED prediction book (#2479, prediction_hook.go). nil
	// registers nothing.
	predictions *prediction.Book
}

// NewLoop builds a Loop with the no-op rules/actions stubs, the global tracer,
// and the given flag source. flagSource may be nil, in which case a disabled
// source is used so the kill switch defaults closed. log may be nil, in which
// case slog.Default() is used.
func NewLoop(flagSource FlagSource, log *slog.Logger) *Loop {
	if log == nil {
		log = slog.Default()
	}
	if flagSource == nil {
		flagSource = disabledFlags{}
	}
	return &Loop{
		Flags:                flagSource,
		Rules:                noRules{},
		Actions:              discardActions{},
		Log:                  log,
		Tracer:               otel.Tracer(instrumentationName),
		now:                  time.Now,
		throttle:             DefaultThrottleInterval,
		llmGated:             defaultLLMGatedCounter(),
		decideDiscarded:      defaultDecideDiscardedCounter(),
		predictionDropped:    defaultPredictionDroppedCounter(),
		evaluations:          defaultEvaluationsCounter(),
		decisions:            defaultDecisionsCounter(),
		interventionsApplied: defaultInterventionsAppliedCounter(),
		effectSampler: effectSampler{
			effectDelta: defaultEffectDeltaHistogram(),
		},
	}
}

// SetLLMBudget injects the GLOBAL per-minute LLM request budget (#847). main.go
// constructs ONE budget and calls this on every Loop the factory builds, so all
// per-game loops share a single budget instance and 4 concurrent games can never
// collectively exceed llm.max_requests_per_minute with remote decide calls. Not safe to call concurrently
// with OnEvaluate — set it during construction, before the loop serves.
func (l *Loop) SetLLMBudget(b *llmBudget) { l.budget = b }

// SetLLMBudgetCeiling sets the operator's per-minute ceiling on remote model calls
// (AGENT_LLM_MAX_REQUESTS_PER_MINUTE, #1788). The cap enforced each cycle is
// min(ceiling, llm.max_requests_per_minute): the stricter wins (C-034). A
// non-positive value means no ceiling (the flag governs). Set during construction.
func (l *Loop) SetLLMBudgetCeiling(maxPerMinute int) {
	if maxPerMinute > 0 {
		l.budgetCeiling = maxPerMinute
	}
}

// budgetCap returns the per-minute LLM request cap to enforce this cycle: the live
// llm.max_requests_per_minute flag value (#847), capped by the env ceiling.
func (l *Loop) budgetCap(flagValue int) int {
	return ceilBudget(l.budgetCeiling, flagValue)
}

// SetInterventionRecorder installs the PER-GAME seam that feeds each decision's
// dispatched/blocked outcome into the #916 rolling narrative timeline (#945).
// main.go's loopFactory passes a closure over the Multiplexer and THIS loop's
// game_id (mux.AppendInterventionEvent(gameID, ...)), so the outcome lands in the
// timeline of the game the Loop serves and nowhere else — the per-game isolation
// (#845) the multiplexer already enforces for signals, extended to interventions.
// This mirrors SetContextProvider's #917 wiring (a per-game closure over the same
// mux+gameID). A nil recorder (the construction default and all existing tests)
// leaves the loop recording nothing, so this is purely additive. Set at
// construction, before the loop serves; not safe to call concurrently with
// OnEvaluate.
func (l *Loop) SetInterventionRecorder(rec func(intervention, serial string, blocked bool)) {
	l.interventionRecorder = rec
}

// SetApplyOutcomeRecorder installs the per-game seam that receives how each
// dispatched decision's write ended (#1771). main.go wires a closure over the
// Multiplexer and this loop's game_id, like SetInterventionRecorder. Set at
// construction, before the loop serves.
func (l *Loop) SetApplyOutcomeRecorder(rec func(intervention string, outcome gamecontext.ApplyOutcome)) {
	l.applyOutcomeRecorder = rec
}

// recordApplyOutcome feeds one dispatched decision's write outcome to the seam;
// nil-safe.
func (l *Loop) recordApplyOutcome(intervention string, outcome gamecontext.ApplyOutcome) {
	if l.applyOutcomeRecorder == nil {
		return
	}
	l.applyOutcomeRecorder(intervention, outcome)
}

// SetPlayerNameResolver injects the serial -> display-name lookup (#1295) used to
// stamp player.name on agent.decision / agent.action when a decision targets a
// player. main.go wires the Multiplexer's controller_info view. A nil resolver,
// or one returning "", leaves player.name off. Set at construction, before the
// loop serves.
func (l *Loop) SetPlayerNameResolver(resolve func(serial string) string) {
	l.playerName = resolve
}

// targetAttributes is the targeted-player view of a decision (#1295):
// player.serial (the game coordinator's own key) and, when known, player.name.
// Empty for a session-scoped decision; no player.name in a shadow game.
func (l *Loop) targetAttributes(serial, gameKind string) []attribute.KeyValue {
	if serial == "" {
		return nil
	}
	attrs := []attribute.KeyValue{attribute.String(AttrPlayerSerial, serial)}
	// Shadow games run on mock serials (MOCK000N) that collide with real mock
	// games' serials, so the process-wide name view would name the wrong player:
	// a shadow decision carries player.serial only.
	if l.playerName != nil && gameKind != gamecontext.GameKindShadow {
		if name := l.playerName(serial); name != "" {
			attrs = append(attrs, attribute.String(AttrPlayerName, name))
		}
	}
	return attrs
}

// recordIntervention feeds one decision's outcome into the per-game timeline via
// the #945 recorder seam, if one is wired. Nil-safe so an un-wired Loop records
// nothing. The recorder it calls ultimately reaches the per-game Store's
// mutex-guarded AppendInterventionEvent (#916), so this is safe to call from the
// concurrent Export-handler goroutines that drive runDecision.
func (l *Loop) recordIntervention(intervention, serial string, blocked bool) {
	if l.interventionRecorder == nil {
		return
	}
	l.interventionRecorder(intervention, serial, blocked)
}

// recordEvaluation bumps agent_evaluations_total for one eval cycle (#1053). It
// is called on EVERY enabled cycle — idle or not — so signal arrival is queryable
// even when no rule fires. signal is the triggering OTLP signal (metrics|traces),
// outcome is decided|idle. Labels are kept to these two low-cardinality dimensions
// (4 series max) on purpose: no per-game/per-serial labels. The counter is never
// nil (NewLoop seeds a no-op), so this is safe from the concurrent Export-handler
// goroutines that drive OnEvaluate.
func (l *Loop) recordEvaluation(ctx context.Context, signal, outcome string) {
	l.evaluations.Add(ctx, 1, otelmetric.WithAttributes(
		attribute.String("signal", signal),
		attribute.String("outcome", outcome),
	))
}

// metricAction maps a Decision to the bounded action label for the #790 counters:
// the intervention id, or "noop" when empty (a contract-following engine noop
// yields an empty intervention). Keeping the empty case as the literal "noop"
// matches the intervention vocabulary and keeps the label a small closed set.
func metricAction(intervention string) string {
	if intervention == "" {
		return "noop"
	}
	return intervention
}

// recordDecision bumps agent_decisions_total for one decision the engine returned
// (#790), applied OR blocked. block_reason is empty when applied and the specific
// BlockReason ("not_allowed"/"battery_threshold"/"rate_limit") when blocked, so the
// dashboard can split applied vs blocked by reason. Labels are bounded: game_kind
// (primary|shadow, #2294), action (the
// intervention vocabulary), blocked (bool), block_reason (the closed BlockReason
// set). The counter is never nil (NewLoop seeds a no-op), so this is safe from the
// concurrent Export-handler goroutines that drive runDecision.
func (l *Loop) recordDecision(ctx context.Context, gameKind, action string, blocked bool, reason BlockReason) {
	l.decisions.Add(ctx, 1, otelmetric.WithAttributes(
		attribute.String("game_kind", gamecontext.TelemetryGameKind(gameKind)),
		attribute.String("action", metricAction(action)),
		attribute.Bool("blocked", blocked),
		attribute.String("block_reason", string(reason)),
	))
}

// recordInterventionApplied bumps agent_interventions_applied_total after a
// decision passes every permission gate AND applies successfully through the action
// sink (#790) — the numerator for the applied-rate panel. Label: action. The
// counter is never nil (NewLoop seeds a no-op), so this is safe from the concurrent
// Export-handler goroutines that drive runDecision.
func (l *Loop) recordInterventionApplied(ctx context.Context, action string) {
	l.interventionsApplied.Add(ctx, 1, otelmetric.WithAttributes(
		attribute.String("action", metricAction(action)),
	))
}

// SetThrottle overrides the log/span throttle interval. It is read once at
// startup from decision.throttle_seconds (#766 F5); a non-positive value leaves
// the default in place. Not safe to call concurrently with OnEvaluate — set it
// during construction, before the loop starts serving.
func (l *Loop) SetThrottle(d time.Duration) {
	if d <= 0 {
		return
	}
	l.throttle = d
}

// SetThrottleSource installs the LIVE decision.throttle_seconds source (#927):
// shouldLog() reads it every cycle instead of the static SetThrottle value, so a
// mid-session config-change to the throttle flag takes effect with no restart.
// main.go passes the shared flags.LifecycleHolder.DecisionThrottle, which is a
// lock-free atomic load refreshed only by the OpenFeature config-change event —
// the hot path never evaluates flagd. A nil source (or a source returning a
// non-positive duration) leaves the static throttle / default in place, so this
// is purely additive over the #766 F5 behavior. Not safe to call concurrently
// with OnEvaluate — set it during construction, before the loop serves.
func (l *Loop) SetThrottleSource(src func() time.Duration) {
	l.throttleSource = src
}

// OnEvaluate runs one evaluation pass and returns the cycle's LayerState (also
// retained on the loop for #729). Steps:
//  1. evaluate all four flag layers (never cached);
//  2. existence — if enabled is false, emit a throttled agent.disabled span with
//     the evaluated-flag attribution and return the disabled LayerState before
//     any rules run;
//  3. publish the objectives flag into the rules engine and run the rules;
//  4. when the rules return at least one decision, emit the audit trace lazily
//     (idle evaluations cost no spans); the root span is backdated to trig.T0 so
//     its duration covers the whole Export processing;
//  5. per decision: run the permission chain (allow-list -> battery threshold ->
//     weighted rate limit), record the outcome (dispatched or the BlockReason)
//     in the LayerState and on the decision/action spans, and apply only the
//     fully-permitted decisions through the action sink.
//
// UC-013 BR-002: the agent-side allow-list check (step 5); the coordinator enforces
// the same list again in _check_chain.
func (l *Loop) OnEvaluate(ctx context.Context, c gamecontext.GameContext, trig EvalTrigger) LayerState {
	now := l.now
	if now == nil {
		now = time.Now
	}

	// #2059: the snapshot is resolved with the flag-evaluation events buffered, and
	// agent.flag_state is emitted on the partition's first cycle or a changed snapshot,
	// BEFORE the kill-switch short-circuit so enabled=false is visible too.
	snapshot, flagEvents := l.evaluateFlags(ctx)
	l.emitFlagState(ctx, snapshot, c, flagEvents)
	state := newLayerState(snapshot)
	// Existence layer: enabled=false short-circuits before any rules evaluation.
	// This is the safe default when flagd is unreachable. The disabled state is
	// still recorded AND emitted as a throttled agent.disabled span so a Jaeger
	// trace shows "agent off" with the flags in effect (#729). Throttled so a
	// disabled agent under heavy signal load does not flood the trace backend.
	// UC-045 BR-001: `enabled` is the existence gate of the three.
	if !snapshot.Enabled {
		l.emitDisabledSpan(ctx, snapshot, c, trig, state)
		l.setLastLayer(state)
		return state
	}

	// The shield ledger's views (the rules' no-shield filter) use this cycle's
	// live shield limits (UC-040 A33).
	l.charges.setLimits(limitsOf(snapshot.Policy))

	// Objective layer: publish the per-cycle objectives flag into the rules
	// engine. The engine reads them inside Evaluate; engines that ignore
	// objectives (Noop/Probe) skip this.
	rules := l.rulesFor(snapshot)
	if pub, ok := rules.(objectivePublisher); ok {
		pub.SetObjectives(snapshot.Objectives)
	}
	// Objective layer (fitness half, #731): publish the per-cycle fitness
	// thresholds so the engine's fitness functions score against the live flag
	// values. Per-cycle publication is what makes a mid-session flag change take
	// effect on the very next decision.
	if pub, ok := rules.(fitnessPublisher); ok {
		pub.SetFitness(rulesThresholds(snapshot))
	}

	// The evaluate log line is throttled to at most one per throttle interval.
	if l.shouldLog() {
		l.Log.Info("agent.evaluate",
			"session_id", c.SessionID,
			"player_count", len(c.Players),
			"game_mode", derefStr(c.Session.GameMode),
			"duration", derefFloat(c.Session.DurationSeconds),
			"decision_backend", requestedBackend(snapshot),
			"battery_threshold", snapshot.Policy.BatteryThreshold,
			"max_per_minute", snapshot.Policy.MaxInterventionsPerMinute,
		)
	}

	// #2479: early-decided predictions and the due base-rate control of this game,
	// and the game's goal sample (every game, whatever predictions_enabled says).
	l.observePredictions(snapshot, c)
	l.sampleGoal(c)

	decisions, decideRec := l.decide(ctx, snapshot, rules, c, trig)

	// Lift the cycle-level fitness evaluation the engine just computed onto the
	// LayerState so it rides the decision span as fitness.evaluated (#731). Read
	// after decide so it reflects this cycle's thresholds and live context. This
	// happens even on an empty-decision cycle so a future cycle-scoped consumer
	// still sees the evaluation, but it only reaches a span when a decision emits.
	if rd, ok := rules.(fitnessReader); ok {
		if vals := rd.LastFitness().Evaluated(); len(vals) > 0 {
			state.FitnessEvaluated = vals
		}
	}

	// Bump the per-cycle liveness counter BEFORE the lazy-trace branch so an idle
	// cycle (no rule fired, hence no span) is still counted (#1053). outcome=idle
	// on a zero-decision cycle, decided otherwise; signal is the OTLP signal that
	// triggered this cycle (metrics|traces). This is what makes "metrics are
	// arriving but nothing fires" distinguishable from "metrics aren't arriving".
	if len(decisions) == 0 {
		l.recordEvaluation(ctx, trig.Signal, "idle")
		// A configuration fallback is still recorded when the rules then emit
		// nothing (UC-040 A3, decide contract §7), throttled; otherwise no
		// decision, no trace.
		if decideRec != nil && isConfigFallback(decideRec.fallbackReason) {
			l.emitQuietFallback(ctx, decideRec, c, trig)
		}
		l.setLastLayer(state)
		return state
	}
	l.recordEvaluation(ctx, trig.Signal, "decided")

	// The root span wraps the inbound OTLP Export with rpc.* semconv attrs and
	// kind SERVER. There is no otelgrpc interceptor on this server today, so
	// this is the only span representing the Export. CAVEAT: if
	// otelgrpc.NewServerHandler is ever added to the agent's gRPC server,
	// demote this span to SpanKindInternal and drop the rpc.* attributes to
	// avoid a duplicate server span for the same RPC.
	rootCtx, root := l.startSignalReceivedSpan(ctx, c, trig)
	defer root.End()

	// #1226: the decide-contract record of this cycle's rules ranking (the answers
	// from the SAME ranking that produced these decisions), as an agent.decide
	// sibling of the agent.decision spans. Nil when the engine has no decide seam.
	// #1295: the span is emitted AFTER the decisions ran (it is backdated to the
	// ranking, so the timeline is unchanged) so it can report what the permission
	// chain did with the decision its answers describe (decide.blocked).
	if decideRec != nil {
		state.DecideBackend = decideRec.used // rides every agent.decision span
	}
	decisions = l.stampShieldSeconds(rootCtx, snapshot, decisions, decideRec)
	before := len(state.Outcomes)
	for _, d := range decisions {
		l.runDecision(rootCtx, snapshot, c, d, now(), &state)
	}
	if decideRec != nil {
		decideRec.notePermission(&state, before)
		l.emitDecideSpan(rootCtx, decideRec, c)
	}

	l.setLastLayer(state)
	return state
}

// LastLayerState returns the LayerState from the most recent OnEvaluate call.
// Issue #729 reads it to populate the decision span.
func (l *Loop) LastLayerState() LayerState {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.lastLayer
}

// setLastLayer stores the cycle's LayerState under the loop mutex (OnEvaluate
// runs concurrently from the trace and metrics Export handler goroutines).
func (l *Loop) setLastLayer(state LayerState) {
	l.mu.Lock()
	l.lastLayer = state
	l.mu.Unlock()
}

// startSignalReceivedSpan opens the per-cycle agent.signal_received root span: the
// SERVER-kind span that wraps the inbound OTLP Export with rpc.* semconv attrs,
// anchored at the trigger's T0 and tagged with session.id + the game.id alias
// (#845). It is the single shared shape for every site that needs the cycle root —
// the sync decided path (OnEvaluate), the kill-switch path (emitDisabledSpan), and
// the async-fire anchor (#1140 Slice A) — so the attribute set never drifts between
// them. The caller owns ending the returned span. CAVEAT (carried from the original
// site): if otelgrpc.NewServerHandler is ever added to the agent's gRPC server,
// demote this to SpanKindInternal and drop the rpc.* attributes to avoid a duplicate
// server span for the same RPC.
func (l *Loop) startSignalReceivedSpan(ctx context.Context, c gamecontext.GameContext, trig EvalTrigger) (context.Context, trace.Span) {
	startOpts := []trace.SpanStartOption{
		trace.WithTimestamp(trig.T0),
		trace.WithSpanKind(trace.SpanKindServer),
		trace.WithAttributes(
			semconv.RPCSystemGRPC,
			semconv.RPCService(trig.RPCService),
			semconv.RPCMethod("Export"),
			attribute.String("otlp.signal", trig.Signal),
			attribute.String(AttrSessionID, c.SessionID),
			// game.id alias (#845): session.id IS the game_id since PR A; the alias
			// keeps Jaeger queries by game.id symmetrical with the coordinator spans
			// and makes two concurrent games' traces independently attributable.
			attribute.String(AttrGameID, c.SessionID),
		),
	}
	// In-game re-parenting (#1325, refining #1195/#1178/#1174; UC-040 BR-013): the
	// agent's in-game decisions/actions belong to the running game CYCLE, so this
	// cycle-root span is started as a remote CHILD of the coordinator's game_cycle span
	// instead of LINKED to the game. Because it is the parent of the whole sync
	// decision->action subtree AND the #1140 async fire anchor (both call this shared
	// helper), that whole subtree is pulled INSIDE the game trace, nested under
	// game_cycle. The parent is remote (we hold only its span_id from the
	// game_trace_correlation gauge); a remote child can be started under it regardless
	// of its liveness, and inherits the game trace's sampling fate. Fallback
	// (inGameRemoteParent): game_cycle -> gameplay_phase -> game ROOT (#1187) -> own
	// root, so correlation is never lost. The game trace ids are still stamped as
	// searchable attributes.
	if parentCtx, ok := inGameRemoteParent(ctx, c.GameTraceID, c.GameTraceSpanID, c.GameplayPhaseSpanID, c.GameCycleSpanID); ok {
		ctx = parentCtx
	}
	if c.GameTraceID != "" {
		startOpts = append(startOpts, trace.WithAttributes(attribute.String(AttrGameTraceID, c.GameTraceID)))
	}
	if c.GameTraceSpanID != "" {
		startOpts = append(startOpts, trace.WithAttributes(attribute.String(AttrGameTraceSpanID, c.GameTraceSpanID)))
	}
	return l.Tracer.Start(ctx, SignalReceived, startOpts...)
}

// decide runs this cycle's decision path with the engine rulesFor resolved once
// for the cycle (rules), so a pattern registration or flag flip between two
// look-ups cannot mix engines inside one cycle. decision.backend selects it (#1226):
// rules (the default) runs the rules engine through rulesDecide, which returns
// Evaluate's decisions unchanged plus the agent.decide record from the same
// ranking; any other value hands the decision to that remote backend
// (decideRemote, see decide_hook.go), which falls back to rules when it cannot
// decide. The legacy mode=llm path (#739/#741/#847/#917/#1207) was retired in #1234.
func (l *Loop) decide(ctx context.Context, snapshot flags.Snapshot, rules RulesEngine, c gamecontext.GameContext, trig EvalTrigger) ([]Decision, *decideRecord) {
	if !snapshot.Decision.IsRules() {
		// UC-046 A7: a remote backend may decide in a pattern game, which makes it non-exact.
		l.markPatternRemote()
		return l.decideRemote(ctx, snapshot, rules, c, trig)
	}
	// The remote call gate is not reached on rules: leave the empty-allow-list
	// state so its "entering" debug line fires again on a later remote cycle (#1298).
	l.llmGate.Lock()
	l.noteNoInterventions(c, false, false)
	l.noteMisconfigured(c, flags.DecisionBackendRules, "")
	l.llmGate.Unlock()
	return l.rulesDecide(ctx, snapshot, rules, c, flags.DecisionBackendRules, "")
}

// recordGated increments the agent_llm_gated_total counter for one gated remote
// decide cycle, labeled by the gate reason (#847). The counter is never nil
// (NewLoop seeds a no-op), so this is always safe to call.
func (l *Loop) recordGated(ctx context.Context, reason string) {
	l.llmGated.Add(ctx, 1, otelmetric.WithAttributes(attribute.String("reason", reason)))
}

// nowOrDefault returns the loop's injected clock, or time.Now when unset.
func (l *Loop) nowOrDefault() func() time.Time {
	if l.now != nil {
		return l.now
	}
	return time.Now
}

// runDecision emits one agent.decision span (full #724 schema) and its
// agent.action child, runs the permission chain, records the outcome in state,
// and applies the decision only if it passes every gate. Blocked decisions carry
// decision.blocked=true (and the BlockReason) on both spans and in the
// LayerState — recorded, never silently dropped.
func (l *Loop) runDecision(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext, d Decision, now time.Time, state *LayerState) {
	d = l.withShieldSeconds(ctx, snapshot, d)
	cost := interventionCost(d.Intervention)
	perm := l.evaluatePermission(ctx, snapshot, c, d, now, cost, state)
	reason, blocked := perm.reason, perm.blocked
	// UC-040 A31 step 1, A33: a shield reserved by the permission chain is kept only
	// when the write is applied; every other way out of this function, a later
	// refusal in the chain included, releases it.
	applied := false
	defer func() {
		// A nil interface (no ledger step ran) holds nothing to finish.
		if perm.shield != nil {
			perm.shield.finish(&applied)
		}
	}()
	if blocked {
		if repeatReason, repeat := l.repeatRefusalReason(perm, d, now); repeat {
			// A repeated refusal is counted, but emits no span, warning or timeline entry.
			l.recordRepeatRefusal(ctx, c.GameKind, d, cost, state, repeatReason)
			return
		}
	}

	startOpts, target := l.decisionSpanOptions(c, d, state, blocked, reason)
	dCtx, dSpan := l.Tracer.Start(ctx, SpanDecision, startOpts...)
	defer dSpan.End()
	// UC-046 step 7 / NFR-042: record the decision in the pattern game's sequence.
	l.recordPatternDecision(dSpan, snapshot, d)
	if len(d.TargetSerials) > 0 {
		dSpan.SetAttributes(groupAttributes(d, perm.refusedSerial)...)
	}

	// The interventions.allowed evaluation as a feature_flag.* span event
	// (semconv) — same shape the openfeature OTel hooks emit in the Python
	// services. With #727 wired, the provider is the real flagd provider.
	// NOTE: the bare event name "feature_flag" is intentional — it matches the
	// openfeature contrib TracingHook used by the Python services (current
	// semconv drafts call the log event "feature_flag.evaluation"); do not
	// "fix" this without also migrating the Python side, or the traces diverge.
	dSpan.AddEvent("feature_flag", trace.WithAttributes(
		semconv.FeatureFlagKey(interventionsAllowedFlagKey),
		semconv.FeatureFlagProviderName(flagProviderName),
		semconv.FeatureFlagResultVariant(allowedSummary(snapshot.InterventionsAllowed)),
	))

	_, aSpan := l.Tracer.Start(dCtx, SpanAction, trace.WithAttributes(
		attribute.String(AttrDecisionAction, d.Intervention),
		attribute.Bool(AttrDecisionBlocked, blocked),
		// session.id + game.id (#1088): the action span acts on a specific game, so
		// it carries the same game.id as its parent decision span for correlation.
		attribute.String(AttrSessionID, c.SessionID),
		attribute.String(AttrGameID, c.SessionID),
	), trace.WithAttributes(target...))
	defer aSpan.End()

	// Count EVERY decision the engine returned (#790), applied or blocked, before
	// branching — block_reason is empty when applied, the BlockReason when blocked —
	// so the demo dashboard can split applied vs blocked by reason. Emitted alongside
	// the existing audit span/log, not in place of it.
	l.recordDecision(ctx, c.GameKind, d.Intervention, blocked, reason)

	if blocked {
		l.recordBlockedDecision(d, reason, cost, snapshot.InterventionsAllowed, state)
		return
	}
	l.dispatchDecision(ctx, dCtx, snapshot, c, d, cost, state, dSpan, aSpan, &applied)
	if applied {
		state.appliedOutcomes = append(state.appliedOutcomes, len(state.Outcomes)-1)
	}
}

// repeatRefusalReason reports the reason of a blocked decision that repeats an
// earlier refusal, to be counted without a span, warning or timeline entry.
// Checked in order: UC-040 A21 (budget unchanged since the refusal), A40 step 6
// (same experiment flag already blocked this lever in this game), A35 (the same
// value already suppressed against this entry), A31 step 1 / A33 (the shield
// ledger already refused a grant with this reason).
func (l *Loop) repeatRefusalReason(perm permission, d Decision, now time.Time) (BlockReason, bool) {
	switch {
	case perm.reason == ReasonRateLimit && l.repeatRefusal(d, now):
		return ReasonRateLimit, true
	case perm.repeatExperimentFlag:
		return ReasonExperimentFlag, true
	case perm.repeatApplied:
		return ReasonAlreadyApplied, true
	case perm.repeatShield:
		return perm.reason, true
	}
	return "", false
}

// decisionSpanOptions builds the agent.decision start options and returns the
// target (player.*) attributes the agent.action span reuses.
func (l *Loop) decisionSpanOptions(c gamecontext.GameContext, d Decision, state *LayerState, blocked bool, reason BlockReason) ([]trace.SpanStartOption, []attribute.KeyValue) {
	// Trace correlation Phase 2 (#1133): when the game's root trace_id is known, add
	// an OTel span LINK to it (a Link, not parent-child: the relationship is async /
	// cross-trace). gameTraceLink returns no options when the id is absent or invalid.
	target := l.targetAttributes(d.TargetSerial, c.GameKind)
	startOpts := []trace.SpanStartOption{
		trace.WithAttributes(decisionAttributes(state, d, blocked, reason, c.GameKind, c.SessionID)...),
		// player.serial / player.name of a targeted decision, blocked or applied (#1295).
		trace.WithAttributes(target...),
	}
	startOpts = append(startOpts, gameTraceLink(c.GameTraceID, c.GameTraceSpanID)...)
	if c.GameTraceID != "" {
		// Also a searchable span ATTRIBUTE, for backends that don't surface Link
		// attributes in search (#1133 review).
		startOpts = append(startOpts, trace.WithAttributes(attribute.String(AttrGameTraceID, c.GameTraceID)))
	}
	if c.GameTraceSpanID != "" {
		// Likewise the originating span_id (#1157).
		startOpts = append(startOpts, trace.WithAttributes(attribute.String(AttrGameTraceSpanID, c.GameTraceSpanID)))
	}
	return startOpts, target
}

// recordBlockedDecision records a decision a gate refused (or, UC-040 A35, one
// whose value is already in place) in state and the #916 narrative timeline.
func (l *Loop) recordBlockedDecision(d Decision, reason BlockReason, cost float64, allowed []string, state *LayerState) {
	state.recordBlocked(d, reason, cost)
	if reason == ReasonAlreadyApplied {
		// UC-040 A35: not a refusal by a gate. Info, not the agent.decision_blocked
		// warning, and no timeline entry, so the narrative does not read as the agent
		// being stopped.
		l.recordAlreadyApplied(d.Intervention, d.TargetSerial)
		l.Log.Info("agent.decision_already_applied",
			"intervention", d.Intervention,
			"target_serial", d.TargetSerial,
			"decision_reason", d.Reason)
		return
	}
	// Feed the BLOCKED outcome into this game's #916 rolling narrative timeline
	// (#945): blocked interventions are recorded, never silently dropped.
	l.recordIntervention(d.Intervention, d.TargetSerial, true)
	l.Log.Warn("agent.decision_blocked",
		"blocked", true,
		"reason", string(reason),
		"intervention", d.Intervention,
		"target_serial", d.TargetSerial,
		"decision_reason", d.Reason,
		"allowed", allowed)
}

// dispatchDecision records and writes a decision that passed every gate, stamps
// the outcome on both spans and schedules the effect sample. It sets *applied
// as soon as the write is known to have gone out (right after the applied span
// stamps), so a panic in the later hooks still leaves the permission shield kept.
func (l *Loop) dispatchDecision(ctx, dCtx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext, d Decision, cost float64, state *LayerState, dSpan, aSpan trace.Span, applied *bool) {
	state.recordDispatched(d, cost)
	// Feed the DISPATCHED outcome into this game's #916 rolling narrative timeline
	// (#945): recorded BEFORE the action sink applies it (it passed every gate, so it
	// IS dispatched regardless of a downstream apply error — the timeline tracks the
	// agent's INTENT, mirroring recordDispatched). The recorder is the per-game seam
	// from SetInterventionRecorder, closing over this loop's game_id so the event
	// lands in the timeline of the game it targeted (#845/#917 isolation). This single
	// hook also covers the async apply path (#917), since applyOneAsync /
	// fallbackToRules both dispatch through runDecision.
	l.recordIntervention(d.Intervention, d.TargetSerial, false)
	// Address the write to the game this loop decided for (UC-040 BR-002,
	// BR-011): the sink needs it so end_game can never end another game (#1310).
	addressed := d
	addressed.GameID = c.SessionID
	addressed.GameKind = c.GameKind
	// UC-040 A35: an applied state or level value is recorded only after the write
	// went out; a failed write or an overlapping one deletes the entry.
	flight := l.beginApplied(d)
	wrote, err := applyAction(dCtx, l.Actions, addressed)
	flight.end(wrote, err, l.nowOrDefault()())
	if err != nil {
		failed := []attribute.KeyValue{
			attribute.Bool(AttrDecisionApplied, false),
			attribute.String(AttrDecisionNotAppliedReason, NotAppliedWriteFailed),
		}
		dSpan.SetAttributes(failed...)
		aSpan.SetAttributes(failed...)
		aSpan.RecordError(err)
		// semconv.ErrorType derives the low-cardinality error class from the
		// Go error's dynamic type, per the error.type convention.
		aSpan.SetAttributes(semconv.ErrorType(err))
		aSpan.SetStatus(codes.Error, err.Error())
		l.Log.Error("agent.apply_failed",
			"error", err, "intervention", d.Intervention)
		l.recordApplyOutcome(d.Intervention, gamecontext.ApplyOutcomeWriteFailed)
		return
	}
	// Apply succeeded: count the intervention as applied (#790) — the numerator for
	// the dashboard's applied-rate panel. Only a clean Apply that wrote something
	// increments this: an apply error returns above, and a gated no-op
	// (interventions_enabled off, UC-040 A11) is not counted (#1681).
	if !wrote {
		// #2093: the write never left the agent, so the spans must not read as an
		// effective decision. Both spans, so a Jaeger search on either finds it.
		notApplied := []attribute.KeyValue{
			attribute.Bool(AttrDecisionApplied, false),
			attribute.String(AttrDecisionNotAppliedReason, NotAppliedWriteGateOff),
		}
		dSpan.SetAttributes(notApplied...)
		aSpan.SetAttributes(notApplied...)
		l.recordApplyOutcome(d.Intervention, gamecontext.ApplyOutcomeGatedOff)
		return
	}
	dSpan.SetAttributes(attribute.Bool(AttrDecisionApplied, true))
	aSpan.SetAttributes(attribute.Bool(AttrDecisionApplied, true))
	*applied = true
	l.recordApplyOutcome(d.Intervention, gamecontext.ApplyOutcomeApplied)
	l.recordInterventionApplied(ctx, d.Intervention)
	// Intervention-effect MEASUREMENT (#918): stamp a baseline of this game's
	// fitness/objective signals NOW (the same fitness.evaluated quantities the decision
	// span carries) and schedule a follow-up sample after the window to measure the
	// delta, attributed to a dispatch-unique intervention id. This closes the game-path
	// measurement loop the infra path already has (fitness->rollback). It is MEASUREMENT
	// ONLY — it never reverts the intervention. Scheduled only for WRITTEN decisions:
	// a blocked one, a gated no-op (interventions_enabled off) and a failed write
	// never moved the game, so they get no sample and no id (#1728 review). The decision
	// span is still open here, so the id lands on it. The id is stamped on this decision span so a
	// Jaeger query joins decision->effect; "" means no sample was scheduled (effect
	// measurement un-wired, no baseline signals this cycle, or the pending cap reached) —
	// then the decision span simply carries no effect id. Reuses state.FitnessEvaluated,
	// which OnEvaluate lifted from this cycle's engine evaluation, and the live
	// thresholds from this cycle's snapshot, so baseline and follow-up sample the SAME
	// fitness functions against the SAME thresholds. Off the hot path: a cheap map copy
	// plus a goroutine + timer (bounded, leak-safe via effectWG + rootCtx).
	if id := l.scheduleEffectSample(dCtx, c, d, state.FitnessEvaluated, fitnessThresholds(snapshot.Fitness)); id != "" {
		dSpan.SetAttributes(attribute.String(AttrDecisionInterventionID, id))
	}
}

// applyAction applies d through sink and reports whether anything was written. A
// sink without the ActionApplier seam always writes on a nil error.
func applyAction(ctx context.Context, sink ActionSink, d Decision) (applied bool, err error) {
	if ap, ok := sink.(ActionApplier); ok {
		return ap.ApplyReported(ctx, d)
	}
	return true, sink.Apply(ctx, d)
}

// emitDisabledSpan emits the kill-switch trace for a cycle where the existence
// layer reported the agent off (enabled=false): a root agent.signal_received with
// a single agent.decision child (no action child — nothing was decided) carrying
// the existence + capability + permission attribution lifted from the disabled
// LayerState. This makes "agent off, here are the flags that were in effect"
// visible in a Jaeger trace (#729). It is throttled to one per the configured
// throttle interval (shared with the evaluate log) so a disabled agent under heavy signal load
// does not flood the trace backend — the steady-state disabled agent is silent.
func (l *Loop) emitDisabledSpan(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext, trig EvalTrigger, state LayerState) {
	if !l.shouldLog() {
		return
	}
	rootCtx, root := l.startSignalReceivedSpan(ctx, c, trig)
	defer root.End()

	// One agent.disabled span carrying the full flag attribution; it is named
	// distinctly (not agent.decision) so kill-switch traces are trivial to find
	// in Jaeger. There is no action child because the kill switch decided
	// nothing. AttrDecisionAction is empty (no action) and AttrDecisionBlocked is
	// false (no decision was blocked by a permission gate — the loop never ran).
	_, dSpan := l.Tracer.Start(rootCtx, SpanDisabled,
		trace.WithAttributes(decisionAttributes(&state, Decision{}, false, "", c.GameKind, c.SessionID)...))
	dSpan.End()
}

// permission is the verdict of the permission chain for one decision.
type permission struct {
	reason  BlockReason
	blocked bool
	// shield is the shield-ledger reservation this decision holds (UC-040 A31, A33;
	// a group shield's targets, A47), nil when the ledger does not apply or refused.
	// It is returned on a later refusal too: the caller's deferred finish releases it.
	shield shieldHold
	// refusedSerial is the group target the shield ledger refused (#2467, A48), ""
	// otherwise.
	refusedSerial string
	// repeatShield marks a shield-ledger refusal (already_charged,
	// shield_cap_reached, shield_cooldown) that repeats an earlier one with the same
	// reason against the same entry (quiet, as A21).
	repeatShield bool
	// repeatApplied marks an already_applied suppression that repeats an earlier one
	// against the same entry (quiet, UC-040 A35).
	repeatApplied bool
	// repeatExperimentFlag marks an experiment_flag block of a lever already
	// blocked on the same flag in this game (quiet, UC-040 A40 step 6).
	repeatExperimentFlag bool
}

// evaluatePermission runs the permission chain over one decision in order:
// allow-list gate (#727), battery threshold, the experiment-flag check (UC-040
// A40), the already-applied check (UC-040
// A35), the shield ledger (UC-040 A31, A33), then the weighted rate limit (#728). It returns the verdict. The
// rate limit is charged ONLY when the decision passes the allow-list and
// battery gates AND fits the budget — so the budget is spent on decisions the
// agent actually dispatches, the unification chosen over #726's
// charge-every-emitted-decision model now that the loop can see permissions.
func (l *Loop) evaluatePermission(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext, d Decision, now time.Time, cost float64, state *LayerState) permission {
	// 1. Allow-list gate (#727): not in interventions_allowed.
	// UC-045 BR-001: interventions_allowed is the permission gate of the three.
	// UC-040 BR-004: the agent's half of the double enforcement; the coordinator
	// checks the allow-list again (interventions.py _check_chain).
	if !snapshot.Permits(d.Intervention) {
		return permission{reason: ReasonNotAllowed, blocked: true}
	}
	// 2. Battery threshold: block player-targeted interventions when the target's
	// battery is below the threshold. Session-scoped decisions (empty
	// TargetSerial) are unaffected; missing battery is treated as unknown (does
	// not block, but is noted).
	// 1a. Group gate and set (#2467, UC-040 A47, A48, BR-033): a grant_group_shield
	// needs the open gate and 2..max_targets distinct alive players. Check-only.
	if d.Intervention == InterventionGrantGroupShield {
		if r := groupSetRefusal(snapshot.Policy, c, d); r != "" {
			return permission{reason: r, blocked: true}
		}
	}
	if l.batteryBlocks(snapshot, c, d) {
		return permission{reason: ReasonBatteryThreshold, blocked: true}
	}
	// 2a. Experiment flag (UC-040 A40, BR-028): in a game of an experiment, either
	// arm, a lever of the flag under test is blocked experiment_flag. Check-only and
	// before the rate limit, so it charges no budget and does not end rank-order
	// admission.
	// A repeat of a block already emitted in this game is quiet (A40 step 6).
	if flag := l.experimentFlagBlocks(c, d); flag != "" {
		return permission{reason: ReasonExperimentFlag, blocked: true,
			repeatExperimentFlag: !l.experimentFlagSeen.first(d.Intervention, flag)}
	}
	// 3. Already applied (UC-040 A35, BR-023): a state or level decision that
	// repeats the value this loop last wrote for its key is blocked already_applied.
	// Check-only, ahead of every step that reserves, so its refusal leaks nothing;
	// before the rate limit, so it charges no budget and does not end rank-order
	// admission.
	if match, first := l.alreadyApplied(c, d, now); match {
		return permission{reason: ReasonAlreadyApplied, blocked: true, repeatApplied: !first}
	}
	// 4. Shield ledger (UC-040 A31 step 1, BR-020; A33, BR-022): a targeted
	// grant_shield is refused already_charged (trigger mode), shield_cap_reached or
	// shield_cooldown, else reserved. Before the rate limit, so a refusal charges no
	// budget and does not end rank-order admission.
	var shield shieldHold
	if d.Intervention == InterventionGrantGroupShield {
		// A group reserves every target and a group slot, all or nothing (A47, A48).
		res, refused, serial, firstRefusal := l.reserveGroupShield(ctx, snapshot, c, d, now)
		if refused != "" {
			return permission{reason: refused, blocked: true, repeatShield: !firstRefusal, refusedSerial: serial}
		}
		shield = res
	} else {
		res, refused, firstRefusal := l.reserveShield(ctx, snapshot, c, d, now)
		if refused != "" {
			return permission{reason: refused, blocked: true, repeatShield: !firstRefusal}
		}
		shield = res
	}
	// 5. Weighted rate limit: block when the per-minute budget is exhausted. The
	// limiter charges the cost only when it admits, so blocked decisions above do
	// not draw down the budget. Budget-aware (UC-040 BR-014/BR-015, budget.go):
	// rank order is kept within a cycle and nudges leave a protective reserve.
	// admitOrHold charges the budget on admit and may set the cycle's hold. A
	// block returns the shield reservation, and the caller's finish releases it.
	if !l.admitOrHold(snapshot, d, now, cost, state) {
		return permission{reason: ReasonRateLimit, blocked: true, shield: shield}
	}
	return permission{shield: shield}
}

// batteryBlocks reports whether a player-targeted decision must be blocked
// because the target's battery is below policy.battery_threshold. A threshold of
// 0 disables the check. Session-scoped decisions and unknown battery never block;
// unknown battery is logged so the gap is visible (and recorded for #729).
func (l *Loop) batteryBlocks(snapshot flags.Snapshot, c gamecontext.GameContext, d Decision) bool {
	if snapshot.Policy.BatteryThreshold <= 0 {
		return false
	}
	// A group shield is blocked when any of its targets is low (#2467, A48).
	for _, serial := range d.TargetSerials {
		if p := c.Players[serial]; p != nil && p.BatteryPct != nil && *p.BatteryPct < float64(snapshot.Policy.BatteryThreshold) {
			return true
		}
	}
	if d.TargetSerial == "" {
		return false
	}
	player := c.Players[d.TargetSerial]
	if player == nil || player.BatteryPct == nil {
		// Unknown battery: do not block, but make the gap visible.
		l.Log.Debug("agent.battery_unknown",
			"target_serial", d.TargetSerial,
			"intervention", d.Intervention,
			"threshold", snapshot.Policy.BatteryThreshold,
		)
		return false
	}
	return *player.BatteryPct < float64(snapshot.Policy.BatteryThreshold)
}

// shouldLog rate-limits the evaluate log line to once per the configured
// throttle interval (decision.throttle_seconds, default 1s). Mutex-guarded:
// OnEvaluate runs concurrently from the trace and metrics Export handler
// goroutines.
func (l *Loop) shouldLog() bool {
	return l.throttleAllows(&l.lastLog)
}

// throttleAllows reports whether the throttle window since *last has elapsed
// and, if so, moves *last to now. last must be a field guarded by l.mu.
func (l *Loop) throttleAllows(last *time.Time) bool {
	now := l.now
	if now == nil {
		now = time.Now
	}
	// Prefer the LIVE throttle source (#927) so a config-change to
	// decision.throttle_seconds is honored on the very next cycle; fall back to the
	// static read-at-startup value (and then the default) when no source is wired or
	// it reports a non-positive duration.
	throttle := l.throttle
	if l.throttleSource != nil {
		if d := l.throttleSource(); d > 0 {
			throttle = d
		}
	}
	if throttle <= 0 {
		throttle = DefaultThrottleInterval
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	t := now()
	if t.Sub(*last) < throttle {
		return false
	}
	*last = t
	return true
}

// gameTraceLink builds the trace-correlation span option for #1133/#1157: when
// gameTraceID is a valid hex trace_id AND gameTraceSpanID is a valid hex span_id
// (both ingested into GameContext from the game_trace_correlation signal), it
// returns a single trace.WithLinks option whose Link targets a remote SpanContext
// reconstructed from BOTH ids. That makes the agent.decision span LINK to the
// originating game's root span (not merely its trace), so Jaeger's uiFind
// highlights the actual game-start span (#1157) instead of an all-zero span id
// under the trace. The SpanContext is marked Remote + Sampled.
//
// An empty/unparseable trace_id OR span_id yields NO option, so the span is created
// exactly as before: graceful fallback, no Link, no error. Pre-#1157 emitters (or
// an unsampled game span) carry an empty span_id and therefore produce no Link —
// the same as a missing trace_id. Both ids are also stamped as plain attributes on
// the Link; the caller additionally stamps them as span attributes (see runDecision)
// so they stay queryable as span tags on backends that don't surface Link attributes.
func gameTraceLink(gameTraceID, gameTraceSpanID string) []trace.SpanStartOption {
	// Delegates to the single primitive in gamecontext (#1174): the implementation moved
	// there so the cycle-independent game-end emitters (gamesummary, which decision
	// imports and so cannot import back) share the EXACT same Link logic instead of a
	// replica. The canonical Link attribute keys (AttrGameTraceID / AttrGameTraceSpanID)
	// are passed through, so the on-wire attributes are unchanged.
	return gamecontext.TraceLink(gameTraceID, gameTraceSpanID, AttrGameTraceID, AttrGameTraceSpanID)
}

// gameRemoteParent is the #1178 sibling of gameTraceLink: instead of LINKING a span to
// the game trace it returns a context that makes the span a remote CHILD of the game
// span. When BOTH gameTraceID and gameTraceSpanID are valid hex ids it installs the SAME
// remote game SpanContext gameTraceLink targets as the remote parent of ctx and returns
// ok=true; a span started from the returned ctx joins the game trace under the game span
// (regardless of the game span's liveness — the remote-parent rule is liveness-free) and
// inherits the game trace's sampling fate. When either id is empty/unparseable/all-zero
// it returns the UNCHANGED ctx and ok=false, so the caller starts the span exactly as
// before (own root): graceful fallback, never breaking span emission. Delegates to the
// single primitive in gamecontext so the SpanContext construction is shared with the Link.
func gameRemoteParent(ctx context.Context, gameTraceID, gameTraceSpanID string) (context.Context, bool) {
	return gamecontext.RemoteParent(ctx, gameTraceID, gameTraceSpanID)
}

// inGameRemoteParent re-parents the agent's IN-GAME decision chain (#1325, refining
// #1195/#1187; UC-040 BR-013). It is the target-swap entry point: the in-game cycle
// root (agent.signal_received) and the async remote agent.decide landing parent under
// the coordinator's game_cycle span — the running game cycle — so a reader sees
// interventions nested where they actually occurred.
//
// Fallback order (the safety property — correlation is never lost):
//  1. game_cycle span_id (gameCycleSpanID), if a valid hex id -> child of game_cycle.
//  2. else gameplay_phase span_id (gameplayPhaseSpanID) -> child of gameplay_phase
//     (the #1195 target) — used until game_cycle opens / when it is unsampled.
//  3. else the game ROOT span_id (gameTraceSpanID) -> child of the game root (the
//     #1187 behavior) — modes with their own run() publish neither phase id.
//  4. else (all empty/invalid) -> ctx unchanged, ok=false: own-root span. The
//     agent.decision spans keep their game-trace Link (gameTraceLink) regardless.
//
// gameTraceID (the trace_id) is identical for all three spans (same trace), so the
// same id selects any parent. Delegates to gameRemoteParent for the actual remote
// SpanContext construction + validation (gameSpanContext rejects empty/invalid ids,
// which is what drives the fallback to the next tier). The POST-game spans
// (retro/summary/experiment.*) deliberately do NOT use this — they keep targeting
// the game root via their existing Links/experiment-parenting.
func inGameRemoteParent(ctx context.Context, gameTraceID, gameTraceSpanID, gameplayPhaseSpanID, gameCycleSpanID string) (context.Context, bool) {
	for _, spanID := range []string{gameCycleSpanID, gameplayPhaseSpanID, gameTraceSpanID} {
		if parentCtx, ok := gameRemoteParent(ctx, gameTraceID, spanID); ok {
			return parentCtx, true
		}
	}
	return ctx, false
}

// GameTraceLink is the exported entry point to the gameTraceLink primitive so other
// agent packages (e.g. the experiment loop, #1140 Slice C) can build the SAME
// game-trace-correlation span Link the agent.decision / agent.llm.retro spans use,
// instead of maintaining a divergent replica. It carries identical semantics: when
// BOTH gameTraceID and gameTraceSpanID are valid hex ids it returns one trace.WithLinks
// option targeting the game's root span (so Jaeger highlights the game-start span,
// #1157); an empty/unparseable/all-zero trace_id OR span_id yields NO option (graceful
// fallback, no Link, no error). Sharing this avoids the #1157 span_id=0 bug recurring
// in a replica.
func GameTraceLink(gameTraceID, gameTraceSpanID string) []trace.SpanStartOption {
	return gameTraceLink(gameTraceID, gameTraceSpanID)
}

// attrLinkKind labels a span Link with what the link points at, so a reader who lands
// on the link target knows WHY it is there. #1140 (Slice A) stamps it
// linkKindFireCycle on the async agent.decide root's Link so a reader of the async
// trace knows the link navigates back to the originating fire cycle's
// agent.signal_received trace. Defined locally here (not in telemetry.go) to keep the
// new attribute owned by Slice A.
const (
	attrLinkKind      = "link.kind"
	attrLinkTraceID   = "link.trace_id"
	linkKindFireCycle = "fire_cycle"
)

// fireCycleLink builds the #1140 (Slice A) trace-continuity span option: when the
// captured fire-cycle SpanContext is valid, it returns a single trace.WithLinks option
// whose Link targets that SpanContext — so the async agent.decide root (its own
// root by the non-blocking design) LINKs back
// to the fire cycle's originating agent.signal_received trace and are navigable in
// Jaeger as one logical decision. The Link carries link.kind=fire_cycle plus the fire
// trace_id as queryable attributes. An invalid/zero SpanContext (the production default,
// where no span is active at fire time) yields NO option, so the span is created exactly
// as before: graceful fallback, no Link, no error — mirroring gameTraceLink's contract.
func fireCycleLink(sc trace.SpanContext) []trace.SpanStartOption {
	if !sc.IsValid() {
		return nil
	}
	return []trace.SpanStartOption{
		trace.WithLinks(trace.Link{
			SpanContext: sc,
			Attributes: []attribute.KeyValue{
				attribute.String(attrLinkKind, linkKindFireCycle),
				attribute.String(attrLinkTraceID, sc.TraceID().String()),
			},
		}),
	}
}

// decisionAttributes builds the complete #724 + #729 decision-span schema for
// one decision. It lifts every evaluated flag from the cycle's LayerState
// verbatim (cycle-level: existence/capability/permission/policy), the
// per-decision outcome (action,
// reason, objective served, fitness, blocked + reason). Every span carries every
// attribute; subsystems that do not exist yet contribute explicit placeholder
// values (see telemetry.go). The block reason (#728) is carried only when a
// decision is blocked.
func decisionAttributes(state *LayerState, d Decision, blocked bool, reason BlockReason, gameKind, gameID string) []attribute.KeyValue {
	objective := d.ObjectiveServed
	if objective == "" {
		objective = DefaultObjectives
	}
	attrs := []attribute.KeyValue{
		semconv.GenAIAgentName(AgentName),
		// game.kind (#845): schema-complete — empty string when unknown.
		attribute.String(AttrGameKind, gameKind),
		// session.id + game.id alias (#1088): the child agent.decision / agent.disabled
		// spans carry the SAME game.id as their agent.signal_received root, so a Jaeger
		// query by game.id matches every per-game span — not just the root — and
		// correlates the agent's orphan trace with the coordinator's per-game trace.
		// game.id IS the SessionID (= the real game_id since #845 PR A); empty when
		// unknown (schema-complete).
		attribute.String(AttrSessionID, gameID),
		attribute.String(AttrGameID, gameID),
	}
	// Cycle-level flag attribution: lift the whole LayerState onto the span so a
	// single trace answers "which flags were in effect" (#729). agent.objectives
	// is recorded there from the cycle's evaluated weights; the rules engine
	// stamps the same normalized weights onto each Decision, so the per-decision
	// view (d.Objectives) is identical and not re-emitted.
	attrs = append(attrs, layerStateAttributes(state)...)
	// Per-decision attribution: the chosen action and why.
	attrs = append(attrs,
		attribute.String(AttrDecisionAction, d.Intervention),
		attribute.String(AttrDecisionReason, d.Reason),
		attribute.String(AttrDecisionObjective, objective),
		attribute.Bool(AttrDecisionBlocked, blocked),
	)
	// fitness.evaluated (#731): the cycle-level fitness evaluation (dotted
	// per-objective keys) is authoritative and was already emitted by
	// layerStateAttributes when populated. Only fall back to the per-decision
	// rule fitness (the pre-#731 view, e.g. the rule's own session_duration) when
	// the cycle-level evaluation is absent — engines without a fitness function
	// (Probe/Noop) still surface what the rule recorded, and the attribute is
	// always present on the span.
	if len(state.FitnessEvaluated) == 0 {
		attrs = append(attrs, attribute.StringSlice(AttrFitnessEvaluated, renderFitness(d.Fitness)))
	}
	if blocked {
		attrs = append(attrs, attribute.String(AttrDecisionBlockReason, string(reason)))
	}
	return attrs
}

// layerStateAttributes lifts the cycle's evaluated flag set from the LayerState
// onto a span verbatim — the heart of #729. It is used by both the live decision
// span and the disabled kill-switch span. #1234 removed the legacy capability /
// inference attribution (agent.mode, agent.model, agent.prompt_variant,
// inference.*): decide.backend says who decided.
func layerStateAttributes(state *LayerState) []attribute.KeyValue {
	attrs := []attribute.KeyValue{
		// Existence layer.
		attribute.Bool(AttrEnabled, state.Enabled),
		// Objective layer (cycle-wide weights as evaluated this cycle).
		attribute.String(AttrObjectives, summarizeObjectives(state.Objectives)),
		// Permission layer.
		attribute.String(AttrInterventionsAllowed, allowedSummary(state.InterventionsAllowed)),
		attribute.Int(AttrPolicyBatteryThreshold, state.PolicyBatteryThreshold),
		attribute.Int(AttrPolicyMaxPerMinute, state.PolicyMaxPerMinute),
	}
	// Decision backend (#1226): only present when the decide hook ran, so spans of
	// engines without the decide seam keep their exact pre-#1226 attribute set.
	if state.DecideBackend != "" {
		attrs = append(attrs, attribute.String(AttrDecideBackend, state.DecideBackend))
	}
	// Fitness layer (#731 hook): only present when populated, so the attribute
	// stays absent until the fitness engine lands rather than carrying an
	// empty-slice placeholder that looks like "evaluated nothing".
	if len(state.FitnessEvaluated) > 0 {
		attrs = append(attrs, attribute.StringSlice(AttrFitnessEvaluated, renderFitness(state.FitnessEvaluated)))
	}
	return attrs
}

// summarizeObjectives renders the objective weights as a stable sorted "k=v"
// summary (e.g. "balanced=0.3,endurance=0.7"); nil/empty keeps the "unset"
// placeholder for engines that carry no weights.
func summarizeObjectives(weights map[string]float64) string {
	if len(weights) == 0 {
		return DefaultObjectives
	}
	parts := make([]string, 0, len(weights))
	for k, v := range weights {
		parts = append(parts, k+"="+strconv.FormatFloat(v, 'g', -1, 64))
	}
	sort.Strings(parts)
	return strings.Join(parts, ",")
}

// renderFitness renders the evaluated fitness values as sorted "k=v" strings
// for the fitness.evaluated attribute; an empty map renders the empty slice
// (the schema attribute is always present).
func renderFitness(fitness map[string]float64) []string {
	out := make([]string, 0, len(fitness))
	for k, v := range fitness {
		out = append(out, k+"="+strconv.FormatFloat(v, 'g', -1, 64))
	}
	sort.Strings(out)
	return out
}

// allowedSummary renders the permission list for the interventions.allowed
// attribute and the feature_flag.result.variant event attribute. An empty
// allow-list (the fail-closed default) renders the explicit "none" marker —
// distinct from the "unrestricted" placeholder the #724 stub carried before
// flagd was wired.
func allowedSummary(allowed []string) string {
	if len(allowed) == 0 {
		return AllowedNone
	}
	return strings.Join(allowed, ",")
}

// disabledFlags is the fallback FlagSource: it always reports the agent disabled
// with empty permissions, so a nil flag source can never dispatch interventions.
type disabledFlags struct{}

func (disabledFlags) Evaluate(context.Context) flags.Snapshot {
	return flags.Snapshot{Enabled: flags.DefaultEnabled}
}

// fitnessThresholds converts the flag-layer fitness snapshot into the engine's
// FitnessThresholds (the flag carries seconds as ints; the engine works in
// float seconds). #731.
// FitnessThresholdsFromFlags converts the evaluated fitness.* flags into the
// thresholds the fitness evaluator scores with (the experiment loop's live
// scoring, #1740, uses the same conversion as the decision loop).
func FitnessThresholdsFromFlags(f flags.Fitness) FitnessThresholds { return fitnessThresholds(f) }

func fitnessThresholds(f flags.Fitness) FitnessThresholds {
	return FitnessThresholds{
		EnduranceMinSessionSeconds:     float64(f.EnduranceMinSessionSeconds),
		BalancedMaxSkillGap:            f.BalancedMaxSkillGap,
		BalancedSpikeSurvivalThreshold: f.BalancedSpikeSurvivalThreshold,
		AccelerateTargetSessionSeconds: float64(f.AccelerateTargetSessionSeconds),
	}
}

// rulesThresholds is what the loop publishes into the rules engine each cycle: the
// fitness thresholds plus R4's danger-shield policy (UC-040 A43, BR-032), which
// the engine clamps at use time.
func rulesThresholds(s flags.Snapshot) FitnessThresholds {
	t := fitnessThresholds(s.Fitness)
	t.ShieldDangerRatio = s.Policy.ShieldDangerRatio
	t.ShieldDangerSamples = s.Policy.ShieldDangerSamples
	t.ShieldDangerOff = s.Policy.ShieldDangerOff
	return t
}

func derefStr(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func derefFloat(p *float64) float64 {
	if p == nil {
		return 0
	}
	return *p
}

// noRules is NewLoop's default rules engine: it never proposes an intervention.
// main.go replaces it with the real engine; tests rely on it staying inert.
type noRules struct{}

// Evaluate always returns nil (no decisions).
func (noRules) Evaluate(context.Context, gamecontext.GameContext) []Decision { return nil }

// discardActions is NewLoop's default action sink: it discards decisions. The
// real sink (actions.GatedActionSink over actions.Writer) writes game-scoped
// values to the `interventions` flag set through the flag service (UC-053).
type discardActions struct{}

// Apply discards the decision and never errors.
func (discardActions) Apply(context.Context, Decision) error { return nil }
