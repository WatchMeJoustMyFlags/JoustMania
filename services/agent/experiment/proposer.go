package experiment

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/llm"
)

// proposer.go is M7-4 (#931): the agent turns its rolling game NARRATIVE (#916/
// #929) + fitness signals into a structured flag-experiment Proposal and submits
// it through Gate.Review — shadow-scoped, safe by construction (#932).
//
// THE RESCOPED DESIGN (#931/#932 "Refined design 2026-06-12", encoded in
// proposal.go + the testing-strategy doc docs/research/m7-4-testing-strategy.md):
// the original "remote coding agent edits SOURCE" is superseded. There is NO
// source diff. The agent emits a structured {flag, value, rationale} over the
// EXISTING Backend.Infer + defensive-decode seam (#739) — a full coding agent is
// overkill for flag deltas. The Proposer is a THIN wrapper over that seam:
//
//	narrative + fitness  --Build prompt-->  Backend.Infer  --decodeProposal-->
//	    Proposal  --Gate.Review-->  experiment-scoped write to the game flag set (or blocked span)
//
// SAFETY (the whole point of routing through the Gate, never writing the game flag set
// directly): the Gate enforces THE INVARIANT — a write can never change what a
// game_kind="real" game resolves — and the type guard (#955). So even a malicious
// or hallucinated model reply can, at worst, write a well-typed shadow-only
// variant; it can NEVER affect real players. The decoder is the first line
// (unparseable/out-of-vocab => no proposal at all); the Gate is the structural
// backstop.
//
// DEGRADES TO NOTHING WITHOUT A REAL BACKEND: main.go wires the direct Claude text
// client (inference.Claude) when inference.backend=claude, else an inert stub.
// A backend error (the stub, a transport failure, a timeout) is recorded and
// produces NO proposal and NO error to the caller — the agent simply does not
// propose this cycle. (agent.code_improvement.engine, once recorded on the span,
// was retired in #1810.)
//
// GATED LIKE EVERY OTHER AGENT LLM PATH (#1270): a cycle runs only while the
// `enabled` kill switch, `experiments_enabled` and the proposer gate (#1810) are on (ProposerConfig.Permit,
// read at use time and re-checked before the Gate write), and a real model call
// first passes the shared LLM call gate (SetLLMAdmission: llm.eligible_game_kinds +
// the global llm.max_requests_per_minute budget the decide calls and the retro use).

// Backend is the inference seam the Proposer needs: send a constrained prompt,
// get the model's RAW response text back. It is deliberately a NARROW local
// interface (just Infer) — Go's structural typing means inference.Claude satisfies
// it for free, and keeping it local keeps this package free of the inference and
// decision packages. The Proposer reuses llm.Prompt as the prompt carrier, the same
// carrier the retro uses, so one text backend drives both.
type Backend interface {
	// Infer sends prompt to the resolved inference tier and returns the model's
	// raw response text, decoded DEFENSIVELY by decodeProposal (unparseable/
	// out-of-vocab never proposes). An error (the sentinel stub, transport
	// failure, timeout) makes the Proposer degrade to no-proposal SAFELY. ctx
	// bounds the call (timeout/cancel).
	Infer(ctx context.Context, prompt llm.Prompt) (string, error)
}

// Narrative is the per-cycle input the Proposer reasons over: the rolling
// cross-game narrative block (#916/#929, already assembled by the decision side
// via gamewindow.Render) plus the current fitness signals. It is passed in
// PRE-ASSEMBLED so this package stays free of the gamecontext/gamewindow/flags
// dependencies and the prompt build is a PURE function of its inputs (same
// "caller owns context assembly, package owns logic" split llm.BuildInput uses).
type Narrative struct {
	// ContextBlock is the rendered cross-game narrative (gamewindow.Render output):
	// the last N game summaries the agent has memory of. Empty = no prior-games
	// section (the model then has only the current fitness to reason from).
	ContextBlock string
	// FitnessSignals are the current fitness/objective readings that motivate an
	// experiment (e.g. {"endurance": 0.42, "balance": 0.81}). Rendered sorted for a
	// deterministic prompt. Empty = no explicit fitness section.
	FitnessSignals map[string]float64
	// GameMode is the mode the recent games were (e.g. "joust"), for grounding the
	// model's flag choice. Optional.
	GameMode string
}

// ProposerConfig is the live, per-cycle flag configuration the Proposer reads
// (#931 flags), resolved fresh each cycle by the caller (flags side) so a retune
// takes effect on the next cycle — same never-cached contract as the #847 llm.*
// gate flags and the #935 validation flags. The experiment package stays free of
// an OpenFeature dependency: the caller resolves these and passes the value in.
// (Named ProposerConfig, not Config — the validate stage already owns Config.)
type ProposerConfig struct {
	// MinInterval is agent.code_improvement.min_interval_seconds — the per-Proposer
	// cadence floor (#847 gating concept). Proposing is expensive (an LLM call), so
	// at most one proposal attempt per this interval; cycles inside the window are
	// gated with no Infer call. <= 0 disables the floor (propose every cycle the
	// caller invokes us).
	MinInterval time.Duration
	// Permit is the kill-switch gate (#1270): the caller's USE-TIME read of the
	// agent `enabled` kill switch and the `experiments_enabled` gate (the same two
	// gates the experiment loop obeys, FR-066). It returns ok=false plus a skip
	// reason (SkipReasonAgentDisabled / SkipReasonExperimentsDisabled) when either
	// is off. ProposeOnce consults it FIRST — before the cadence floor, the LLM
	// admission and any Infer call — and AGAIN right before Gate.Review, so an
	// operator who flips the agent off while a model call is in flight still gets
	// no experiment write. A nil Permit FAILS CLOSED (SkipReasonNoPermit): a caller
	// that forgets to wire the gate gets no proposals, never ungated ones.
	Permit func(ctx context.Context) (ok bool, reason string)
	// GameKind is the kind of the game whose end triggered this cycle ("real",
	// "shadow", ...). It is handed to the LLM admission (SetLLMAdmission) for the
	// llm.eligible_game_kinds check, exactly as the retro checks its game's kind.
	GameKind string
}

// Skip reasons a Permit returns (#1270), carried on Result.Reason for a
// ProposeDisabled cycle.
const (
	// SkipReasonAgentDisabled: the agent `enabled` kill switch is off (or flagd is
	// unreachable — the kill switch fails closed).
	SkipReasonAgentDisabled = "agent_disabled"
	// SkipReasonExperimentsDisabled: the `experiments_enabled` gate is off.
	SkipReasonExperimentsDisabled = "experiments_disabled"
	// SkipReasonNoPermit: ProposerConfig.Permit was nil (fail-closed wiring guard).
	SkipReasonNoPermit = "no_permit"
	// SkipReasonProposerDisabled: the proposer gate is closed (#1810, C-034, FR-152
	// d): AGENT_EXPERIMENT_PROPOSER_ENABLED is not true, or experiment_proposer_enabled
	// is off or cannot be resolved.
	SkipReasonProposerDisabled = "proposer_disabled"
)

// LLMAdmission is the shared LLM call gate the Proposer consults before a model
// call (#1270): eligibility of the triggering game kind (llm.eligible_game_kinds)
// and one slot of the GLOBAL per-minute request budget (llm.max_requests_per_minute,
// capped by the AGENT_LLM_MAX_REQUESTS_PER_MINUTE ceiling) that the in-game decide calls
// and the retro also draw from. Admit returns ok=false with the gate reason when
// the call must not be made, and records the gated metric itself. A local
// interface keeps this package free of the decision package; main.go wires
// decision.LLMAdmission. It is always installed and consulted only for a real
// cycle, so a stub propose cycle (no model call) never charges the budget.
type LLMAdmission interface {
	Admit(ctx context.Context, gameKind string) (ok bool, reason string)
}

// ProposeOutcome is what one ProposeOnce cycle did, returned for telemetry/
// branching by the caller. The set is closed so dashboards can enumerate every
// result. (Named ProposeOutcome, not Outcome — the validate stage already owns
// the Outcome type for its promote/discard/revert verdict; both ride the shared
// AttrOutcome span key, distinguished by the span name.)
type ProposeOutcome string

const (
	// ProposeProposed — the model returned a valid in-vocab proposal AND the Gate
	// ACCEPTED it: the shadow-scoped experiment was written to the game flag set. This is
	// the only outcome that mutates the (shadow) flag surface.
	ProposeProposed ProposeOutcome = "proposed"
	// ProposeGated — the cadence floor (MinInterval) suppressed this cycle; no
	// Infer call was made. The cheap, common "not yet" outcome.
	ProposeGated ProposeOutcome = "gated"
	// ProposeNoBackend — Backend.Infer failed (the sentinel stub, transport error,
	// timeout). Degrades to NO proposal, NO error to the caller — the agent does
	// not propose this cycle. This is the steady state until #738/#742 wire a real
	// backend.
	ProposeNoBackend ProposeOutcome = "no_backend"
	// ProposeUndecodable — the model replied but the response was unparseable,
	// incomplete, or named an out-of-vocab flag (decodeProposal rejected it). No
	// proposal, recorded — we never fabricate one from a broken reply (#739 rule).
	ProposeUndecodable ProposeOutcome = "undecodable"
	// ProposeBlocked — the decoded proposal reached Gate.Review and the Gate
	// REJECTED it (invariant/type guard). The Gate already emitted its
	// code_improvement.proposed blocked span; the Proposer records the reason and
	// does NOT retry blindly.
	ProposeBlocked ProposeOutcome = "blocked"
	// ProposeDisabled — the kill switch or experiments_enabled is off (#1270), so
	// the cycle did nothing: no Infer call, no budget slot, no experiment write.
	// Result.Reason carries the SkipReason*. When the gate flips off WHILE a model
	// call is in flight, the cycle also ends here (after Infer, before Gate.Review),
	// with the reason on the attempt span.
	ProposeDisabled ProposeOutcome = "disabled"
	// ProposeLLMGated — the shared LLM call gate refused the model call (#1270):
	// the triggering game kind is not in llm.eligible_game_kinds (llm_not_eligible)
	// or the global per-minute budget is full (llm_budget_exhausted). No Infer
	// call; the gate recorded agent_llm_gated_total{reason}. Result.Reason carries
	// the gate reason. The cadence window is NOT consumed, so the next game end may
	// try again.
	ProposeLLMGated ProposeOutcome = "llm_gated"
)

// Result is the full outcome of one ProposeOnce cycle. Proposal is non-nil only
// on ProposeProposed (or ProposeBlocked, carrying what was rejected, for the
// caller's telemetry). Err is the underlying cause for the non-proposing outcomes
// (already recorded on the span); the Proposer NEVER returns an error from
// ProposeOnce itself — a failed cycle is a normal, recorded no-op, matching the
// #739 degrade-to-rules contract.
type Result struct {
	Outcome  ProposeOutcome
	Proposal *Proposal // set on ProposeProposed / ProposeBlocked, and on a ProposeDisabled cycle dropped after inference
	Err      error     // set on ProposeNoBackend / ProposeUndecodable / ProposeBlocked
	// Reason is the skip reason on ProposeDisabled (SkipReason*) and the gate
	// reason on ProposeLLMGated (llm_not_eligible / llm_budget_exhausted).
	Reason string
}

// Proposer turns a Narrative into a shadow-scoped flag experiment and submits it
// through the Gate. It is the M7-4 propose-side seam. Safe for concurrent use:
// the cadence state is mutex-guarded (the agent may run per-game loops that all
// share one Proposer, like the shared llmBudget).
type Proposer struct {
	backend Backend
	// sink receives decoded proposals: a RegistrySink in production (see ProposalSink).
	sink ProposalSink
	// vocab returns the experiment catalog of the game flag set the model is constrained
	// to. Injected as a func (not a static set) because the flag surface is read
	// from the game flag file each cycle — the decoder rejects any flag not in it,
	// and the prompt lists it so the model picks an existing flag. Returning a
	// fresh map each call keeps the Proposer free of a file dependency (the caller/
	// Gate's Writer owns the file).
	vocab func() map[string]flagservice.FlagView
	// admission is the shared LLM call gate (#1270), consulted after the cadence
	// floor and before Infer, only when the cycle's backend is real (a stub cycle
	// makes no model call and must not charge the budget). nil = ungated.
	admission LLMAdmission
	// source, when set, picks the backend for ONE ProposeOnce (inference.backend,
	// #1810): it is resolved once per cycle, after the Permit gate, so a flag flip
	// mid-cycle never splits a cycle across two backends. nil = the fixed backend.
	source BackendSource
	tracer trace.Tracer
	log    *slog.Logger

	// clock is injected for deterministic cadence tests (no wall-clock read on the
	// hot path otherwise). Defaults to time.Now.
	clock func() time.Time

	mu          sync.Mutex
	lastAttempt time.Time // last time the cadence floor ADMITTED a proposal attempt
}

// NewProposer builds a Proposer over the inference backend, the experiment Gate
// (the ONLY sanctioned write path — the Proposer never writes the game flag set
// directly), and a vocab provider (the live game-flag key set). tracer nil uses
// the global tracer; log nil uses slog.Default(). The clock defaults to time.Now;
// tests inject a fake via SetClock.
func NewProposer(backend Backend, sink ProposalSink, vocab func() map[string]flagservice.FlagView, tracer trace.Tracer, log *slog.Logger) *Proposer {
	if tracer == nil {
		tracer = otel.Tracer(instrumentationName)
	}
	if log == nil {
		log = slog.Default()
	}
	if vocab == nil {
		// Fail-closed: no vocabulary means nothing is proposable (decodeProposal
		// rejects every flag), so a nil provider degrades to "never propose".
		vocab = func() map[string]flagservice.FlagView { return nil }
	}
	return &Proposer{
		backend: backend,
		sink:    sink,
		vocab:   vocab,
		tracer:  tracer,
		log:     log,
		clock:   time.Now,
	}
}

// SetClock overrides the clock for deterministic cadence tests. Production never
// calls it (defaults to time.Now).
func (p *Proposer) SetClock(clock func() time.Time) { p.clock = clock }

// BackendSource resolves the proposer's backend for one cycle (#1810, C-035): the
// backend to call and whether it is a REAL one (a model call that is charged to the
// shared LLM budget). real=false is the inert stub: it makes no model call, so the
// admission is skipped and no budget slot is charged.
type BackendSource func(ctx context.Context) (backend Backend, real bool)

// SetBackendSource installs the per-cycle backend resolver (inference.backend). It
// replaces the fixed backend given to NewProposer. Not safe to call concurrently
// with ProposeOnce: set it during construction.
func (p *Proposer) SetBackendSource(src BackendSource) { p.source = src }

// resolveBackend returns the backend and the real flag for one cycle. Without a
// source it is the fixed backend, real exactly when an admission is installed
// (the pre-#1810 wiring: the admission was installed only for a real backend).
func (p *Proposer) resolveBackend(ctx context.Context) (Backend, bool) {
	if p.source != nil {
		b, real := p.source(ctx)
		if b == nil {
			return p.backend, false // never charge the budget for the fixed stub
		}
		return b, real
	}
	return p.backend, p.admission != nil
}

// SetLLMAdmission installs the shared LLM call gate (#1270). main.go always installs
// it (inference.backend can flip at run time, #1810); ProposeOnce consults it only
// when the cycle's backend is real, so each real model call draws one slot from the
// global budget and a stub cycle draws none. Not safe to call concurrently with ProposeOnce; wire it at startup.
func (p *Proposer) SetLLMAdmission(a LLMAdmission) { p.admission = a }

// permit evaluates the caller's kill-switch gate, failing closed on a nil Permit.
func permit(ctx context.Context, cfg ProposerConfig) (bool, string) {
	if cfg.Permit == nil {
		return false, SkipReasonNoPermit
	}
	return cfg.Permit(ctx)
}

// ProposeOnce runs one propose cycle: kill-switch gate, cadence-gate, LLM
// admission (eligibility + shared budget, real backend only), build the prompt, infer,
// defensively decode, and submit through Gate.Review. It NEVER returns an error —
// every failure mode is a recorded, normal no-op (Result.Outcome + Result.Err),
// because a missing/failing backend or a garbage reply must degrade the agent to
// "did not propose this cycle", never crash a decision loop (the #739/#741
// degrade-to-rules discipline). The only state mutation on the real flag surface
// is a SHADOW-SCOPED write the Gate accepted.
func (p *Proposer) ProposeOnce(ctx context.Context, n Narrative, cfg ProposerConfig) Result {
	// Kill switch FIRST (#1270): when the agent `enabled` kill switch or
	// experiments_enabled is off, the cycle does nothing at all — no cadence
	// advance, no budget slot, no model call, no experiment write. "Agent off" is
	// total. No span is emitted (same as the cadence gate), so a switched-off agent
	// stays quiet in Jaeger.
	if ok, reason := permit(ctx, cfg); !ok {
		p.log.Debug("code_improvement.propose_skipped", "reason", reason)
		return Result{Outcome: ProposeDisabled, Reason: reason}
	}
	// inference.backend (#1810): resolved ONCE for the whole cycle, after the gate.
	backend, real := p.resolveBackend(ctx)

	// Cadence floor (cheapest remaining gate): suppress cycles inside MinInterval
	// with no Infer call, so proposing — an expensive LLM round-trip — happens at
	// most occasionally, not every decision cycle (#847 gating concept; mirrors the
	// per-game decision.min_interval_ms floor in decide_hook.go). Charged only
	// on admission, so a gated cycle does not advance the window.
	now := p.clock()
	prevAttempt, admitted := p.admit(now, cfg.MinInterval)
	if !admitted {
		p.log.Debug("code_improvement gated by cadence", "min_interval", cfg.MinInterval)
		return Result{Outcome: ProposeGated}
	}

	// Shared LLM call gate (#1270): eligibility of the triggering game kind and one
	// slot of the global per-minute budget, AFTER the cadence floor so a
	// cadence-gated cycle never spends a budget slot. Always installed;
	// consulted only for a real cycle (a stub cycle makes no model call). On refusal the cadence
	// admission is rolled back so the next game end can try again, and nothing is
	// captured (no span), exactly like a budget-gated retro.
	if real && p.admission != nil {
		if ok, reason := p.admission.Admit(ctx, cfg.GameKind); !ok {
			p.rollback(now, prevAttempt)
			p.log.Info("code_improvement.propose_gated", "reason", reason, "game_kind", cfg.GameKind)
			return Result{Outcome: ProposeLLMGated, Reason: reason}
		}
	}

	ctx, span := p.tracer.Start(ctx, SpanProposeAttempt)
	defer span.End()

	// Build the constrained prompt: ask for ONE {flag, value, rationale} over the
	// live flag vocabulary. The vocabulary is read ONCE here and reused for the
	// decoder so the prompt and the validation agree on the surface.
	vocab := p.vocab()
	prompt := buildProposalPrompt(n, vocab)

	raw, err := backend.Infer(ctx, prompt)
	if err != nil {
		// DEGRADES TO NOTHING: the sentinel stub (no real backend yet), a transport
		// error, or a timeout. Record and no-op — no proposal, no error to caller.
		span.SetAttributes(attribute.String(AttrOutcome, string(ProposeNoBackend)))
		p.log.Debug("code_improvement: backend unavailable, no proposal", "error", err)
		return Result{Outcome: ProposeNoBackend, Err: err}
	}

	proposal, err := decodeProposal(raw, vocabKeys(vocab))
	if err != nil {
		// Untrusted-output rule (#739): unparseable / incomplete / out-of-vocab =>
		// NO proposal, never fabricated. Recorded; no Gate.Review.
		span.SetAttributes(attribute.String(AttrOutcome, string(ProposeUndecodable)))
		p.log.Info("code_improvement: model reply not a valid proposal, no dispatch", "error", err)
		return Result{Outcome: ProposeUndecodable, Err: err}
	}

	// Record the model's reasoning on the attempt span BEFORE submitting (the AC's
	// code_improvement.proposed_diff_summary — here the "diff summary" is the
	// structured experiment, since the rescope has no source diff). Gate.Review
	// emits its OWN code_improvement.proposed span with the blocked/reason verdict.
	span.SetAttributes(
		attribute.String(AttrFlagKey, proposal.FlagKey),
		attribute.String(AttrRationale, proposal.Rationale),
		attribute.String(AttrProposedDiffSummary, summarizeProposal(proposal)),
	)

	// Re-check the kill switch before the WRITE (#1270): the model call can take
	// seconds, and an operator who switched the agent off meanwhile must not see a
	// new experiment land afterwards.
	if ok, reason := permit(ctx, cfg); !ok {
		span.SetAttributes(
			attribute.String(AttrOutcome, string(ProposeDisabled)),
			attribute.String(AttrReason, reason),
		)
		p.log.Info("code_improvement.propose_skipped: switched off during inference, no write", "reason", reason, "flag", proposal.FlagKey)
		pr := proposal
		return Result{Outcome: ProposeDisabled, Proposal: &pr, Reason: reason}
	}

	// Submit to the sink: the experiment registry declares the proposal as an
	// experiment (its own id and arms, FR-138) and starts it, which writes the arm
	// value through the Gate (shadow-scoping invariant + type guard). No sink, no
	// write. We never call the Writer directly.
	sink := p.sink
	if sink == nil {
		sink = &LateSink{}
	}
	if err := sink.Review(ctx, proposal); err != nil {
		// A Gate rejection (RejectError) is RECORDED, not retried blindly. The Gate
		// already emitted its blocked span with the typed reason.
		span.SetAttributes(attribute.String(AttrOutcome, string(ProposeBlocked)))
		if rej, ok := AsReject(err); ok {
			span.SetAttributes(attribute.String(AttrReason, string(rej.Reason)))
			p.log.Info("code_improvement: proposal blocked by gate", "reason", string(rej.Reason), "flag", proposal.FlagKey)
		} else {
			p.log.Warn("code_improvement: proposal apply failed", "error", err, "flag", proposal.FlagKey)
		}
		pr := proposal
		return Result{Outcome: ProposeBlocked, Proposal: &pr, Err: err}
	}

	span.SetAttributes(attribute.String(AttrOutcome, string(ProposeProposed)))
	p.log.Info("code_improvement: shadow experiment proposed", "flag", proposal.FlagKey, "rationale", proposal.Rationale)
	pr := proposal
	return Result{Outcome: ProposeProposed, Proposal: &pr}
}

// admit applies the cadence floor under the mutex: returns true (and advances
// lastAttempt) when this cycle may attempt a proposal, false when it is inside the
// MinInterval window. A non-positive interval disables the floor (always admit). A
// zero lastAttempt (first ever call) always admits. Mirrors the
// decision.min_interval_ms check in decide_hook.go, charged on admission. The
// previous lastAttempt is returned so a later gate can roll the admission back.
func (p *Proposer) admit(now time.Time, interval time.Duration) (time.Time, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	prev := p.lastAttempt
	if interval > 0 && !prev.IsZero() && now.Sub(prev) < interval {
		return prev, false
	}
	p.lastAttempt = now
	return prev, true
}

// rollback undoes a cadence admission made at now (restoring prev) when a later
// gate refused the cycle. It only restores when no other cycle has been admitted
// since, so a concurrent admission is never clobbered.
func (p *Proposer) rollback(now, prev time.Time) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.lastAttempt.Equal(now) {
		p.lastAttempt = prev
	}
}

// summarizeProposal renders the structured experiment as a compact human-readable
// "diff summary" for the span (AttrProposedDiffSummary, #931 AC). Since the
// rescope has no source diff, the "diff" IS the shadow-scoped flag change:
// flag=value (shadow only).
func summarizeProposal(p Proposal) string {
	return fmt.Sprintf("set %s=%v for shadow games (game_kind != real); rationale: %s",
		p.FlagKey, p.ExperimentalValue, p.Rationale)
}

// buildProposalPrompt renders the System/User prompt pair asking the model for
// exactly ONE flag experiment over the supplied flag vocabulary. It is PURE
// (deterministic given its inputs — vocabulary and fitness signals are sorted) so
// the prompt is stable for golden/round-trip tests, matching llm.Build's
// determinism contract. It reuses llm.Prompt as the carrier so the same Backend
// implementations drive both the #739 intervention path and the proposal path.
func buildProposalPrompt(n Narrative, vocab map[string]flagservice.FlagView) llm.Prompt {
	var sys strings.Builder
	sys.WriteString("You are the JoustMania game-balance agent. From the game narrative and ")
	sys.WriteString("fitness signals below, propose EXACTLY ONE experiment that tunes a single ")
	sys.WriteString("game flag to a new value, with a one-sentence hypothesis.\n\n")

	sys.WriteString("You MUST choose a flag from this list (any other flag is rejected). Each line gives ")
	sys.WriteString("what the flag does in the game, its declared variants (name=value) and its allowed range:\n")
	for _, k := range sortedKeys(vocabKeys(vocab)) {
		sys.WriteString(catalogLine(k, vocab[k]))
		sys.WriteString("\n")
	}
	sys.WriteString("\n")

	// RESPONSE CONTRACT — mirrors decodeProposal's schema. Reply with EXACTLY ONE
	// JSON object and nothing else (the decoder tolerates surrounding prose/fences
	// but the contract asks for clean JSON). The value's type must match the flag's
	// existing values (the Gate rejects a mistyped value).
	sys.WriteString("Respond with EXACTLY ONE JSON object and no other text:\n")
	sys.WriteString(`{"flag":"<one of the flags above>","value":<the experimental value>,"rationale":"<one sentence>"}` + "\n")
	sys.WriteString("The value's JSON type MUST match the flag's existing values. Propose only ONE flag.\n")

	var usr strings.Builder
	if n.GameMode != "" {
		fmt.Fprintf(&usr, "Game mode: %s\n\n", n.GameMode)
	}
	if n.ContextBlock != "" {
		usr.WriteString(n.ContextBlock)
		usr.WriteString("\n\n")
	}
	if len(n.FitnessSignals) > 0 {
		usr.WriteString("Current fitness signals:\n")
		// Sort keys for a deterministic prompt (no map-iteration-order dependence).
		keys := make([]string, 0, len(n.FitnessSignals))
		for k := range n.FitnessSignals {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			fmt.Fprintf(&usr, "  %s = %.3f\n", k, n.FitnessSignals[k])
		}
	}

	return llm.Prompt{System: sys.String(), User: usr.String()}
}

// Prompt bounds for one catalog line (#2345): the flag service caps a
// description at 120 characters at load; the agent applies its own cap too, so
// an older or misconfigured service can never grow the prompt unboundedly.
const (
	maxPromptDescription  = 120
	maxPromptVariants     = 10
	maxPromptVariantValue = 24
)

// catalogLine renders one catalog flag for the proposer prompt: key, the
// registry description, the allowed declared variants with their values and
// the numeric range (flags.md §13, UC-042 BR-008). A flag without a policy or
// a description renders what it has: the key alone at least.
func catalogLine(key string, f flagservice.FlagView) string {
	var b strings.Builder
	b.WriteString("  - ")
	b.WriteString(key)
	var desc string
	var bounds flagservice.Bounds
	if f.Policy != nil {
		desc = oneLine(f.Policy.Description, maxPromptDescription)
		bounds = f.Policy.Bounds
	}
	if desc != "" {
		b.WriteString(": ")
		b.WriteString(desc)
	}
	var details []string
	names := bounds.Variants
	if len(names) == 0 {
		names = f.Variants
	}
	if len(names) > 0 {
		shown := names
		if len(shown) > maxPromptVariants {
			shown = shown[:maxPromptVariants]
		}
		parts := make([]string, 0, len(shown)+1)
		for _, name := range shown {
			parts = append(parts, oneLine(name, maxPromptVariantValue)+"="+variantValue(f.VariantValues[name]))
		}
		if len(names) > len(shown) {
			parts = append(parts, "...")
		}
		details = append(details, "variants "+strings.Join(parts, ", "))
	}
	if r := bounds.Range; len(r) == 2 {
		rng := "range " + strconv.FormatFloat(r[0], 'g', -1, 64) + " to " + strconv.FormatFloat(r[1], 'g', -1, 64)
		if bounds.Integer {
			rng += " (whole numbers)"
		}
		details = append(details, rng)
	}
	if len(details) > 0 {
		b.WriteString(" (")
		b.WriteString(strings.Join(details, "; "))
		b.WriteString(")")
	}
	return b.String()
}

// variantValue renders a declared variant's value compactly: a scalar as its
// JSON (capped), an object or array by its kind only.
func variantValue(raw json.RawMessage) string {
	raw = json.RawMessage(strings.TrimSpace(string(raw)))
	if len(raw) == 0 {
		return "?"
	}
	switch raw[0] {
	case '{':
		return "object"
	case '[':
		return "array"
	}
	return oneLine(string(raw), maxPromptVariantValue)
}

// oneLine folds line breaks into spaces and caps s at max runes.
func oneLine(s string, max int) string {
	s = strings.Join(strings.Fields(s), " ")
	if r := []rune(s); len(r) > max {
		s = string(r[:max-3]) + "..."
	}
	return s
}

// vocabKeys returns the flag keys of the catalog as a set (the decoder's input).
func vocabKeys(vocab map[string]flagservice.FlagView) map[string]struct{} {
	if vocab == nil {
		return nil
	}
	set := make(map[string]struct{}, len(vocab))
	for k := range vocab {
		set[k] = struct{}{}
	}
	return set
}

// sortedKeys returns the keys of a set in a stable order for a deterministic
// prompt.
func sortedKeys(set map[string]struct{}) []string {
	keys := make([]string, 0, len(set))
	for k := range set {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
