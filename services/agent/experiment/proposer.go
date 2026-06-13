package experiment

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

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
//	    Proposal  --Gate.Review-->  shadow-scoped game.json write (or blocked span)
//
// SAFETY (the whole point of routing through the Gate, never writing game.json
// directly): the Gate enforces THE INVARIANT — a write can never change what a
// game_kind="real" game resolves — and the type guard (#955). So even a malicious
// or hallucinated model reply can, at worst, write a well-typed shadow-only
// variant; it can NEVER affect real players. The decoder is the first line
// (unparseable/out-of-vocab => no proposal at all); the Gate is the structural
// backstop.
//
// DEGRADES TO NOTHING WITHOUT A REAL BACKEND: the production Backend
// (decision.endpointBackend.Infer) is a sentinel stub until #738/#742 wire a real
// Ollama/cloud transport. A backend error (the stub, a transport failure, a
// timeout) is recorded and produces NO proposal and NO error to the caller — the
// agent simply does not propose this cycle, exactly like the #739 llm path
// degrades to rules. The engine is flag-selectable via agent.code_improvement.engine
// (#931 AC); main.go reads that flag and selects the backend (today it only
// selects the stub).

// Backend is the inference seam the Proposer needs: send a constrained prompt,
// get the model's RAW response text back. It is deliberately a NARROW local
// interface (just Infer) rather than an import of decision.Backend — Go's
// structural typing means decision's backends (endpointBackend, the resolver's
// chosen tier) satisfy it for free, and keeping it local avoids an
// experiment->decision import (decision already does not import experiment; this
// keeps it that way). The Proposer reuses llm.Prompt as the prompt carrier so the
// same Backend implementations the #739 path drives also drive proposals.
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
	// Engine is agent.code_improvement.engine — the flag-selectable backend
	// identifier (#931 AC). The Proposer records it on the span for attribution;
	// the actual backend selection happens in main.go (today every value selects
	// the stub backend, so a non-stub value still degrades to no-proposal until a
	// real transport lands). Recorded, not branched on, here.
	Engine string
	// MinInterval is agent.code_improvement.min_interval_seconds — the per-Proposer
	// cadence floor (#847 gating concept). Proposing is expensive (an LLM call), so
	// at most one proposal attempt per this interval; cycles inside the window are
	// gated with no Infer call. <= 0 disables the floor (propose every cycle the
	// caller invokes us).
	MinInterval time.Duration
}

// ProposeOutcome is what one ProposeOnce cycle did, returned for telemetry/
// branching by the caller. The set is closed so dashboards can enumerate every
// result. (Named ProposeOutcome, not Outcome — the validate stage already owns
// the Outcome type for its promote/discard/revert verdict; both ride the shared
// AttrOutcome span key, distinguished by the span name.)
type ProposeOutcome string

const (
	// ProposeProposed — the model returned a valid in-vocab proposal AND the Gate
	// ACCEPTED it: the shadow-scoped experiment was written to game.json. This is
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
)

// Result is the full outcome of one ProposeOnce cycle. Proposal is non-nil only
// on ProposeProposed (or ProposeBlocked, carrying what was rejected, for the
// caller's telemetry). Err is the underlying cause for the non-proposing outcomes
// (already recorded on the span); the Proposer NEVER returns an error from
// ProposeOnce itself — a failed cycle is a normal, recorded no-op, matching the
// #739 degrade-to-rules contract.
type Result struct {
	Outcome  ProposeOutcome
	Proposal *Proposal // set on ProposeProposed / ProposeBlocked
	Err      error     // set on ProposeNoBackend / ProposeUndecodable / ProposeBlocked
}

// Proposer turns a Narrative into a shadow-scoped flag experiment and submits it
// through the Gate. It is the M7-4 propose-side seam. Safe for concurrent use:
// the cadence state is mutex-guarded (the agent may run per-game loops that all
// share one Proposer, like the shared Resolver/llmBudget).
type Proposer struct {
	backend Backend
	gate    *Gate
	// vocab returns the LIVE set of game.json flag keys the model is constrained
	// to. Injected as a func (not a static set) because the flag surface is read
	// from the game flag file each cycle — the decoder rejects any flag not in it,
	// and the prompt lists it so the model picks an existing flag. Returning a
	// fresh map each call keeps the Proposer free of a file dependency (the caller/
	// Gate's Writer owns the file).
	vocab  func() map[string]struct{}
	tracer trace.Tracer
	log    *slog.Logger

	// clock is injected for deterministic cadence tests (no wall-clock read on the
	// hot path otherwise). Defaults to time.Now.
	clock func() time.Time

	mu          sync.Mutex
	lastAttempt time.Time // last time the cadence floor ADMITTED a proposal attempt
}

// NewProposer builds a Proposer over the inference backend, the experiment Gate
// (the ONLY sanctioned write path — the Proposer never touches game.json
// directly), and a vocab provider (the live game-flag key set). tracer nil uses
// the global tracer; log nil uses slog.Default(). The clock defaults to time.Now;
// tests inject a fake via SetClock.
func NewProposer(backend Backend, gate *Gate, vocab func() map[string]struct{}, tracer trace.Tracer, log *slog.Logger) *Proposer {
	if tracer == nil {
		tracer = otel.Tracer(instrumentationName)
	}
	if log == nil {
		log = slog.Default()
	}
	if vocab == nil {
		// Fail-closed: no vocabulary means nothing is proposable (decodeProposal
		// rejects every flag), so a nil provider degrades to "never propose".
		vocab = func() map[string]struct{} { return nil }
	}
	return &Proposer{
		backend: backend,
		gate:    gate,
		vocab:   vocab,
		tracer:  tracer,
		log:     log,
		clock:   time.Now,
	}
}

// SetClock overrides the clock for deterministic cadence tests. Production never
// calls it (defaults to time.Now).
func (p *Proposer) SetClock(clock func() time.Time) { p.clock = clock }

// ProposeOnce runs one propose cycle: cadence-gate, build the prompt, infer,
// defensively decode, and submit through Gate.Review. It NEVER returns an error —
// every failure mode is a recorded, normal no-op (Result.Outcome + Result.Err),
// because a missing/failing backend or a garbage reply must degrade the agent to
// "did not propose this cycle", never crash a decision loop (the #739/#741
// degrade-to-rules discipline). The only state mutation on the real flag surface
// is a SHADOW-SCOPED write the Gate accepted.
func (p *Proposer) ProposeOnce(ctx context.Context, n Narrative, cfg ProposerConfig) Result {
	// Cadence floor FIRST (cheapest gate): suppress cycles inside MinInterval with
	// no Infer call, so proposing — an expensive LLM round-trip — happens at most
	// occasionally, not every decision cycle (#847 gating concept; mirrors the
	// per-game llm.min_decision_interval_seconds floor in decision.go). Charged only
	// on admission, so a gated cycle does not advance the window.
	now := p.clock()
	if !p.admit(now, cfg.MinInterval) {
		p.log.Debug("code_improvement gated by cadence", "min_interval", cfg.MinInterval)
		return Result{Outcome: ProposeGated}
	}

	ctx, span := p.tracer.Start(ctx, SpanProposeAttempt, trace.WithAttributes(
		attribute.String(AttrEngine, cfg.Engine),
	))
	defer span.End()

	// Build the constrained prompt: ask for ONE {flag, value, rationale} over the
	// live flag vocabulary. The vocabulary is read ONCE here and reused for the
	// decoder so the prompt and the validation agree on the surface.
	vocab := p.vocab()
	prompt := buildProposalPrompt(n, vocab)

	raw, err := p.backend.Infer(ctx, prompt)
	if err != nil {
		// DEGRADES TO NOTHING: the sentinel stub (no real backend yet), a transport
		// error, or a timeout. Record and no-op — no proposal, no error to caller.
		span.SetAttributes(attribute.String(AttrOutcome, string(ProposeNoBackend)))
		p.log.Debug("code_improvement: backend unavailable, no proposal", "error", err, "engine", cfg.Engine)
		return Result{Outcome: ProposeNoBackend, Err: err}
	}

	proposal, err := decodeProposal(raw, vocab)
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

	// Submit through the Gate — the ONLY sanctioned write path. The Gate enforces
	// the shadow-scoping invariant + type guard and writes game.json on accept. We
	// never call the Writer directly.
	if err := p.gate.Review(ctx, proposal); err != nil {
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
// llm.min_decision_interval_seconds check in decision.go, charged on admission.
func (p *Proposer) admit(now time.Time, interval time.Duration) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if interval > 0 && !p.lastAttempt.IsZero() && now.Sub(p.lastAttempt) < interval {
		return false
	}
	p.lastAttempt = now
	return true
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
func buildProposalPrompt(n Narrative, vocab map[string]struct{}) llm.Prompt {
	var sys strings.Builder
	sys.WriteString("You are the JoustMania game-balance agent. From the game narrative and ")
	sys.WriteString("fitness signals below, propose EXACTLY ONE experiment that tunes a single ")
	sys.WriteString("game flag to a new value, with a one-sentence hypothesis.\n\n")

	sys.WriteString("You MUST choose a flag from this list (any other flag is rejected):\n")
	for _, k := range sortedKeys(vocab) {
		sys.WriteString("  - ")
		sys.WriteString(k)
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
