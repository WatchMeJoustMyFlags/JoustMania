package main

import (
	"context"
	"log/slog"
	"os"
	"strings"
	"sync"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/inference"
)

// inference_backends.go selects the text backend of the OFFLINE LLM seams (the
// post-game retro and the shadow-experiment proposer) at USE time (#1810, C-035,
// FR-150, C-035). In-game decisions never use it (decision.backend, decide_backends.go).
//
// The choice is the inference.backend flag ("stub" | "claude", tuning: the egress
// ceilings below stay). The flag always wins; only when flagd cannot resolve it the
// backend is "stub" (#1907: the env fallback is removed). The ceilings stay env-only: the Claude client exists only with ANTHROPIC_API_KEY set and a valid
// AGENT_INFERENCE_BASE_URL (a typo or a missing key fails safe to the stub, whatever
// the flag says), and the model-call budget is min(env ceiling, llm flag).

// envFallback is the backend while the inference.backend flag cannot be resolved.
const envFallback = "stub"

// inferenceBackendFlag is the slice of *flags.Flags the selection reads.
type inferenceBackendFlag interface {
	InferenceBackend(ctx context.Context, def string) string
}

// textBackends holds the Claude client (built once, when the key and the base URL are
// valid) and picks between it and the stub for each use.
type textBackends struct {
	claude      *inference.Claude // nil: no usable key / base URL, so only the stub
	whyNoClaude string            // why claude is nil, for the WARN
	src         inferenceBackendFlag
	logger      *slog.Logger

	mu   sync.Mutex
	last string // last logged "requested/effective", so a WARN is once per change
}

// newTextBackends reads the env ceilings once and builds the Claude client when the
// key and the base URL are valid. tuning, when non-nil, resolves model / effort /
// timeout on every Infer call (#1788); src nil = the stub (tests).
func newTextBackends(logger *slog.Logger, tuning *agentTuning, src inferenceBackendFlag) *textBackends {
	if logger == nil {
		logger = slog.Default()
	}
	tb := &textBackends{
		src:    src,
		logger: logger,
	}
	key := strings.TrimSpace(os.Getenv("ANTHROPIC_API_KEY"))
	if key == "" {
		tb.whyNoClaude = "ANTHROPIC_API_KEY is empty"
		return tb
	}
	baseURL, ok := inferenceBaseURL(logger)
	if !ok {
		tb.whyNoClaude = "AGENT_INFERENCE_BASE_URL rejected"
		return tb
	}
	cfg := inference.Config{
		APIKey:  key,
		BaseURL: baseURL,
		Model:   inference.DefaultModel,
		Effort:  inference.DefaultEffort,
		Timeout: inference.DefaultTimeout,
	}
	if tuning != nil {
		cfg.Tuning = tuning.Inference
	}
	tb.claude = inference.NewClaude(cfg)
	return tb
}

// normalizeInferenceBackend lower-cases and trims; only "claude" is a real backend,
// any other value (a typo, the retired "openai") is the stub.
func normalizeInferenceBackend(v string) string {
	return strings.ToLower(strings.TrimSpace(v))
}

// Select resolves the backend for ONE use: the Claude client or nil (the stub), and
// the effective backend name. It reads the flag every call. A request for claude that
// the env ceilings do not allow (no key, bad base URL) is the stub. The WARN for a
// degraded claude request is logged once per change of the choice, not per call.
func (b *textBackends) Select(ctx context.Context) (*inference.Claude, string) {
	requested := envFallback
	if b.src != nil {
		requested = b.src.InferenceBackend(ctx, envFallback)
	}
	requested = normalizeInferenceBackend(requested)
	effective := "stub"
	var client *inference.Claude
	if requested == inference.Name && b.claude != nil {
		client, effective = b.claude, inference.Name
	}
	b.noteChange(requested, effective)
	return client, effective
}

// noteChange logs the choice when it differs from the last one logged: a WARN when
// claude was requested but is not usable (the stub serves), else an INFO.
func (b *textBackends) noteChange(requested, effective string) {
	key := requested + "/" + effective
	b.mu.Lock()
	changed := key != b.last
	b.last = key
	b.mu.Unlock()
	if !changed {
		return
	}
	if requested == inference.Name && effective != inference.Name {
		b.logger.Warn("inference.backend=claude but "+b.whyNoClaude+"; proposer/retro stay on the stub",
			"requested", requested)
		return
	}
	b.logger.Info("inference.backend in effect (proposer/retro text backend)", "backend", effective)
}

// RetroBackend is the retro's per-retro source (decision.RetroCoordinator.SetBackendSource):
// nil (an untyped interface nil) for the stub.
func (b *textBackends) RetroBackend(ctx context.Context) decision.TextBackend {
	if client, _ := b.Select(ctx); client != nil {
		return client
	}
	return nil
}

// ProposerBackend is the proposer's per-cycle source (experiment.Proposer.SetBackendSource):
// the Claude client with real=true, or the inert stub with real=false so the shared
// budget is never charged for a stub cycle.
func (b *textBackends) ProposerBackend(ctx context.Context) (experiment.Backend, bool) {
	if client, _ := b.Select(ctx); client != nil {
		return inferBackend{infer: client.Infer}, true
	}
	return proposeBackend{}, false
}

// newWiredProposer builds the M7-4 proposer with the production wiring (#1810): the
// inert stub as the fixed backend, the per-cycle backend source (inference.backend)
// and the shared LLM admission, always installed and consulted only for a real
// cycle. main.go and the proposer wiring tests both use it.
func newWiredProposer(source experiment.BackendSource, sink experiment.ProposalSink, vocab func() map[string]flagservice.FlagView,
	admission experiment.LLMAdmission, logger *slog.Logger) *experiment.Proposer {
	p := experiment.NewProposer(proposeBackend{}, sink, vocab, nil, logger)
	p.SetLLMAdmission(admission)
	p.SetBackendSource(source)
	return p
}
