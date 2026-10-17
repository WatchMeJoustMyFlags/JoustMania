package experiment

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/flagservice"
)

// Reason is a typed rejection cause emitted on the code_improvement.proposed
// span (AttrReason) when the Gate blocks a proposal. The set is closed so the
// audit trail / dashboards can enumerate every way an experiment can be denied.
type Reason string

const (
	// ReasonNonGameTarget — the proposal's flag key names a path or another flag
	// set (e.g. the agent set). HARDCODED deny; the agent has no self-modify path.
	ReasonNonGameTarget Reason = "non_game_target"
	// ReasonUnknownFlag — FlagKey is not in the game flag set. The agent
	// experiments on the existing parameterized surface only; inventing flags is
	// a no-op (nothing reads them) and unbounded key creation is a smell.
	ReasonUnknownFlag Reason = "unknown_flag"
	// ReasonDefaultChanged — the write would change defaultVariant. That is what
	// real games resolve when targeting falls through → forbidden.
	ReasonDefaultChanged Reason = "default_variant_changed"
	// ReasonExistingVariantChanged — the write would mutate or drop a
	// pre-existing variant's value. A real game can resolve any existing variant
	// (via pre-existing targeting), so existing variants are immutable.
	ReasonExistingVariantChanged Reason = "existing_variant_changed"
	// ReasonTargetingNotShadowScoped — the resulting targeting could select the
	// experimental variant (or otherwise differ) for a real context. The only
	// sanctioned targeting delta is an added `game_kind != "real"` branch.
	ReasonTargetingNotShadowScoped Reason = "targeting_not_shadow_scoped"
	// ReasonRealResolutionChanged — the belt-and-suspenders eval found the
	// real-context resolution differs before vs after. The invariant's last line
	// of defence.
	ReasonRealResolutionChanged Reason = "real_resolution_changed"
	// ReasonReadError / ReasonWriteSimulationError — I/O or modelling failure
	// while validating (file unreadable, malformed flag). Fail closed.
	ReasonReadError            Reason = "read_error"
	ReasonWriteSimulationError Reason = "write_simulation_error"
	// ReasonTypeMismatch — ExperimentalValue's JSON kind (number/string/bool/
	// array/object) does not match the flag's EXISTING variant values. The
	// invariant is structurally safe regardless (shadow-only), but a game-side
	// reader of the flag expects a stable type, so a string on a numeric flag would
	// crash/misbehave the reader in a SHADOW game once an LLM emits a mistyped
	// value. Reject before the write so only well-typed experiments reach shadow.
	ReasonTypeMismatch Reason = "type_mismatch"
	// ReasonNotExperimentable — the flag-service registry does not allow an
	// experiment-arm write on this flag (it is not in the experiment catalog,
	// flags.md §13.3), or the proposal carries no experiment id (FR-128).
	ReasonNotExperimentable Reason = "not_experimentable"
	// ReasonOutOfBounds — the registry refused the value (not a declared
	// variant's value, or outside the flag's range).
	ReasonOutOfBounds Reason = "out_of_bounds"
)

// RejectError is the typed error a blocked Review returns. It carries the Reason
// so callers can branch / surface it; the Gate has already emitted the span.
type RejectError struct {
	Reason Reason
	Detail string
}

func (e *RejectError) Error() string {
	if e.Detail == "" {
		return fmt.Sprintf("experiment proposal blocked: %s", e.Reason)
	}
	return fmt.Sprintf("experiment proposal blocked: %s (%s)", e.Reason, e.Detail)
}

// Gate validates a Proposal against THE INVARIANT: an agent write must NEVER
// change what a game_kind="real" evaluation resolves to, for any flag — and the
// agent may ONLY write the game flag set. It is the single sanctioned entry
// point to the Writer (Review applies the write itself on accept), so no caller
// can skip validation.
//
// With the flag service (#1415) the guarantee is primarily STRUCTURAL at the
// source: the service renders an experiment value as a rung only a non-real
// game of that experiment and arm resolves (FR-128), and its registry decides
// which flags are experimentable and within which bounds (C-032). The Gate is the
// second check: it reads the rendered flag before and after the write and
// rejects (and rolls back) a write that changed the default, an existing
// variant, or what a real context resolves (JSONLogic eval, the engine flagd uses).
type Gate struct {
	w      *Writer
	tracer trace.Tracer
	log    *slog.Logger
}

// NewGate builds a Gate over the given Writer. tracer nil uses the global
// tracer; log nil uses slog.Default().
func NewGate(w *Writer, tracer trace.Tracer, log *slog.Logger) *Gate {
	if tracer == nil {
		tracer = otel.Tracer(instrumentationName)
	}
	if log == nil {
		log = slog.Default()
	}
	return &Gate{w: w, tracer: tracer, log: log}
}

// Review validates p, applies it on accept and checks the rendered result. On
// accept it emits code_improvement.proposed (blocked=false) and returns nil. On
// reject it emits the same span with blocked=true + the reason, leaves nothing
// written (a write that failed the after-check is rolled back) and returns a
// *RejectError.
func (g *Gate) Review(ctx context.Context, p Proposal) error {
	ctx, span := g.tracer.Start(ctx, SpanProposed, trace.WithAttributes(
		attribute.String(AttrFlagKey, p.FlagKey),
		attribute.String(AttrRationale, p.Rationale),
	))
	defer span.End()

	rej := g.review(ctx, p)
	if rej != nil {
		span.SetAttributes(
			attribute.Bool(AttrBlocked, true),
			attribute.String(AttrReason, string(rej.Reason)),
		)
		g.log.Warn("code_improvement.blocked",
			"blocked", true,
			"reason", string(rej.Reason),
			"flag_key", p.FlagKey,
			"detail", rej.Detail)
		return rej
	}
	span.SetAttributes(attribute.Bool(AttrBlocked, false))
	g.log.Info("code_improvement.proposed",
		"flag_key", p.FlagKey,
		"experiment_id", p.ExperimentID)
	return nil
}

func (g *Gate) review(ctx context.Context, p Proposal) *RejectError {
	// Defence in depth against a FlagKey that smuggles a path or another set.
	if p.FlagKey == "" || strings.ContainsAny(p.FlagKey, "/\\?#") || strings.Contains(p.FlagKey, "agent.json") {
		return &RejectError{Reason: ReasonNonGameTarget, Detail: p.FlagKey}
	}
	if p.ExperimentID == "" {
		return &RejectError{Reason: ReasonNotExperimentable, Detail: "no experiment id"}
	}
	svc := g.w.Service()
	before, err := svc.GetFlag(ctx, GameFlagSet, p.FlagKey)
	if flagservice.IsReason(err, "unknown_flag") {
		return &RejectError{Reason: ReasonUnknownFlag, Detail: p.FlagKey}
	}
	if err != nil {
		return &RejectError{Reason: ReasonReadError, Detail: err.Error()}
	}
	if !before.Policy.AllowsScope("experiment+arm") {
		return &RejectError{Reason: ReasonNotExperimentable, Detail: p.FlagKey + " is not in the experiment catalog"}
	}
	if rej := checkTypeCompatible(before.VariantValues, p.ExperimentalValue); rej != nil {
		return rej
	}

	res, err := g.w.Apply(ctx, p)
	switch {
	case flagservice.IsReason(err, "out_of_bounds"):
		return &RejectError{Reason: ReasonOutOfBounds, Detail: err.Error()}
	case flagservice.IsReason(err, "flag_read_only"), flagservice.IsReason(err, "malformed"):
		return &RejectError{Reason: ReasonNotExperimentable, Detail: err.Error()}
	case err != nil:
		return &RejectError{Reason: ReasonWriteSimulationError, Detail: err.Error()}
	}

	// The second check, on what flagd now serves for this flag.
	after, err := svc.GetFlag(ctx, GameFlagSet, p.FlagKey)
	if err != nil {
		g.rollback(ctx, p)
		return &RejectError{Reason: ReasonReadError, Detail: "read after write: " + err.Error()}
	}
	if rej := checkRenderedFlag(before.Rendered, after.Rendered, res.Variant); rej != nil {
		g.rollback(ctx, p)
		return rej
	}
	return nil
}

func (g *Gate) rollback(ctx context.Context, p Proposal) {
	if err := g.w.Strip(ctx, p.FlagKey, p.ExperimentID); err != nil {
		g.log.Error("code_improvement: rollback of a rejected experiment write failed", "flag_key", p.FlagKey, "error", err)
	}
}

// renderedFlag is the part of a rendered flagd flag the checks compare.
type renderedFlag struct {
	DefaultVariant string                     `json:"defaultVariant"`
	Variants       map[string]json.RawMessage `json:"variants"`
}

// checkRenderedFlag enforces, on the rendered flag before and after the write:
// the defaultVariant is unchanged; every pre-existing variant is unchanged (an
// earlier value of THIS experiment is replaced by the flag service, so a variant
// may disappear only if it is not the default); the only new variant is the one
// the write generated; and a real context resolves the same variant and value.
func checkRenderedFlag(beforeRaw, afterRaw json.RawMessage, written string) *RejectError {
	var before, after renderedFlag
	if err := json.Unmarshal(beforeRaw, &before); err != nil {
		return &RejectError{Reason: ReasonReadError, Detail: "rendered before: " + err.Error()}
	}
	if err := json.Unmarshal(afterRaw, &after); err != nil {
		return &RejectError{Reason: ReasonReadError, Detail: "rendered after: " + err.Error()}
	}
	if before.DefaultVariant != after.DefaultVariant {
		return &RejectError{Reason: ReasonDefaultChanged}
	}
	for name, bv := range before.Variants {
		av, ok := after.Variants[name]
		if !ok {
			continue // a replaced earlier value of a scoped write (opaque name)
		}
		if !jsonEqual(bv, av) {
			return &RejectError{Reason: ReasonExistingVariantChanged, Detail: "changed variant " + name}
		}
	}
	for name := range after.Variants {
		if _, existed := before.Variants[name]; !existed && name != written {
			return &RejectError{Reason: ReasonTargetingNotShadowScoped, Detail: "unexpected new variant " + name}
		}
	}
	realCtx := map[string]any{GameKindVar: GameKindReal}
	bRes, err := resolveFlag(beforeRaw, realCtx)
	if err != nil {
		return &RejectError{Reason: ReasonWriteSimulationError, Detail: "resolve before: " + err.Error()}
	}
	aRes, err := resolveFlag(afterRaw, realCtx)
	if err != nil {
		return &RejectError{Reason: ReasonWriteSimulationError, Detail: "resolve after: " + err.Error()}
	}
	if bRes.Variant != aRes.Variant || !jsonEqual(bRes.Value, aRes.Value) {
		return &RejectError{
			Reason: ReasonRealResolutionChanged,
			Detail: fmt.Sprintf("real resolves %s=%s before, %s=%s after", bRes.Variant, bRes.Value, aRes.Variant, aRes.Value),
		}
	}
	return nil
}

// jsonKind classifies a raw JSON value into one of the five JSON kinds the type
// guard distinguishes: "number", "string", "bool", "array", "object". JSON null
// and unrecognised bytes return "" (no kind). Crucially ALL numerics share the
// "number" kind — JSON has one number type, so int-vs-float (6 vs 4.0) are
// compatible and never rejected; flagd/Go decode both to float64 anyway.
func jsonKind(raw json.RawMessage) string {
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return ""
	}
	switch v.(type) {
	case float64:
		return "number"
	case string:
		return "string"
	case bool:
		return "bool"
	case []any:
		return "array"
	case map[string]any:
		return "object"
	default: // nil (JSON null) — kindless; nothing to match against.
		return ""
	}
}

// checkTypeCompatible rejects an ExperimentalValue whose JSON kind differs from
// the flag's declared variant values. The reader of a game flag expects one
// stable type, so a string on a numeric flag would crash/misbehave in a shadow
// game once an LLM emits a mistyped value. Kindless values impose no constraint.
func checkTypeCompatible(variants map[string]json.RawMessage, experimental any) *RejectError {
	expRaw, err := json.Marshal(experimental)
	if err != nil {
		return &RejectError{Reason: ReasonTypeMismatch, Detail: "experimental value is not JSON-encodable"}
	}
	expKind := jsonKind(expRaw)
	if expKind == "" {
		return nil
	}
	for name, vRaw := range variants {
		k := jsonKind(vRaw)
		if k == "" {
			continue
		}
		if k != expKind {
			return &RejectError{
				Reason: ReasonTypeMismatch,
				Detail: fmt.Sprintf("experimental value is %s but variant %q is %s", expKind, name, k),
			}
		}
	}
	return nil
}

// AsReject extracts a *RejectError from an error returned by Review, if it is
// one. Convenience for callers branching on the typed reason.
func AsReject(err error) (*RejectError, bool) {
	var re *RejectError
	if errors.As(err, &re) {
		return re, true
	}
	return nil, false
}
