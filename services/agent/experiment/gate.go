package experiment

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// Reason is a typed rejection cause emitted on the code_improvement.proposed
// span (AttrReason) when the Gate blocks a proposal. The set is closed so the
// audit trail / dashboards can enumerate every way an experiment can be denied.
type Reason string

const (
	// ReasonNonGameTarget — the proposal would write a file other than the game
	// flagset (e.g. agent.json). HARDCODED deny; the agent has no self-modify
	// path. (Belt to the `:ro` mount suspenders.)
	ReasonNonGameTarget Reason = "non_game_target"
	// ReasonUnknownFlag — FlagKey is not present in game.json. The agent
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
)

// gameFlagFileName is the only basename the Gate will validate a write against.
// The Writer is already hardcoded to the game flagset; this is the independent
// app-level confirmation of the same invariant ("only game.json"), so a bug that
// pointed a Writer at agent.json is caught here even before the `:ro` mount
// rejects the syscall.
const gameFlagFileName = "game.json"

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

// Gate validates a Proposal against THE INVARIANT before the Writer applies it:
// an agent write must NEVER change what a game_kind="real" evaluation resolves
// to, for any flag — and the agent may ONLY write the game flagset. It is the
// single sanctioned entry point to the Writer (Review applies the write itself
// on accept), so no caller can skip validation.
//
// The guarantee is primarily STRUCTURAL (easy to prove + test): only the
// reserved experimental variant is added, defaultVariant and every existing
// variant are byte-unchanged, and the only targeting delta is an added shadow
// (game_kind != "real") branch. A belt-and-suspenders EVAL of the resulting flag
// for a synthetic {game_kind:"real"} context (before vs after) backs the
// structural checks with flagd's actual JSONLogic engine.
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

// Review validates p and, if it upholds the invariant, applies it via the
// Writer. On accept it emits code_improvement.proposed (blocked=false) and
// returns nil. On reject it emits the same span with blocked=true + the reason,
// does NOT write, and returns a *RejectError. Any non-reject error (I/O) is also
// returned without writing.
func (g *Gate) Review(ctx context.Context, p Proposal) error {
	_, span := g.tracer.Start(ctx, SpanProposed, trace.WithAttributes(
		attribute.String(AttrFlagKey, p.FlagKey),
		attribute.String(AttrRationale, p.Rationale),
	))
	defer span.End()

	if rej := g.validate(p); rej != nil {
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

	if err := g.w.Apply(p); err != nil {
		// The validation already simulated the write against the same file, so a
		// failure here is genuine I/O. Record it as blocked (nothing changed for
		// real players) and fail closed.
		span.SetAttributes(
			attribute.Bool(AttrBlocked, true),
			attribute.String(AttrReason, string(ReasonWriteSimulationError)),
		)
		return fmt.Errorf("apply accepted proposal: %w", err)
	}

	span.SetAttributes(attribute.Bool(AttrBlocked, false))
	g.log.Info("code_improvement.proposed",
		"flag_key", p.FlagKey,
		"experiment_id", p.ExperimentID,
		"variant", experimentVariantName(p.ExperimentID))
	return nil
}

// validate runs every invariant check and returns a *RejectError on the first
// violation, or nil if the proposal is safe to apply. It performs NO writes — it
// reads the current file and simulates the mutation in memory.
func (g *Gate) validate(p Proposal) *RejectError {
	// HARDCODED deny: the write target must be the game flagset. The Writer is
	// already pinned to it, but checking the basename here catches a misbuilt
	// Writer (e.g. GAME_FLAG_PATH=.../agent.json) before any syscall.
	if !isGameFlagPath(g.w.Path()) {
		return &RejectError{Reason: ReasonNonGameTarget, Detail: g.w.Path()}
	}
	// Defence in depth against a FlagKey that smuggles a path/flagset reference.
	if strings.ContainsAny(p.FlagKey, "/\\") || strings.Contains(p.FlagKey, "agent.json") {
		return &RejectError{Reason: ReasonNonGameTarget, Detail: p.FlagKey}
	}

	raw, err := os.ReadFile(g.w.Path())
	if err != nil {
		return &RejectError{Reason: ReasonReadError, Detail: err.Error()}
	}
	before, err := parseDoc(raw)
	if err != nil {
		return &RejectError{Reason: ReasonReadError, Detail: err.Error()}
	}
	if !before.hasFlag(p.FlagKey) {
		return &RejectError{Reason: ReasonUnknownFlag, Detail: p.FlagKey}
	}

	// Snapshot the pre-write flag, then simulate the mutation on a fresh parse of
	// the SAME bytes so before/after compare apples to apples.
	beforeFlag, err := before.flag(p.FlagKey)
	if err != nil {
		return &RejectError{Reason: ReasonReadError, Detail: err.Error()}
	}

	// Type-compatibility: the experimental value must be the same JSON kind as the
	// flag's existing variants. The invariant is upheld either way (the experiment
	// is shadow-scoped), but a mistyped value would surface to a SHADOW game's
	// reader that expects a consistent type — reject before the write so only
	// well-typed experiments reach shadow. Runs before the structural simulation:
	// it's a property of the proposal vs the current variants, not of the write.
	if rej := checkTypeCompatible(beforeFlag, p.ExperimentalValue); rej != nil {
		return rej
	}

	after, err := parseDoc(raw)
	if err != nil {
		return &RejectError{Reason: ReasonReadError, Detail: err.Error()}
	}
	if err := applyProposal(after, p); err != nil {
		return &RejectError{Reason: ReasonWriteSimulationError, Detail: err.Error()}
	}
	afterFlag, err := after.flag(p.FlagKey)
	if err != nil {
		return &RejectError{Reason: ReasonWriteSimulationError, Detail: err.Error()}
	}

	if rej := checkVariantsAndDefault(beforeFlag, afterFlag, experimentVariantName(p.ExperimentID)); rej != nil {
		return rej
	}
	if rej := checkTargetingShadowScoped(afterFlag); rej != nil {
		return rej
	}
	if rej := checkRealResolutionUnchanged(beforeFlag, afterFlag); rej != nil {
		return rej
	}
	return nil
}

// checkVariantsAndDefault enforces: defaultVariant byte-unchanged; every
// pre-existing variant byte-unchanged (including OTHER experiments' variants);
// the ONLY added/overwritten variant is THIS proposal's reserved experiment
// variant (writtenVariant). With K experiments on one flag, each carries its own
// agent_experiment__<id> variant; a single write may replace only its own.
func checkVariantsAndDefault(before, after *flagObj, writtenVariant string) *RejectError {
	bDV, _ := before.raw("defaultVariant")
	aDV, _ := after.raw("defaultVariant")
	if !jsonEqual(bDV, aDV) {
		return &RejectError{Reason: ReasonDefaultChanged}
	}

	bVars := map[string]json.RawMessage{}
	aVars := map[string]json.RawMessage{}
	if _, err := before.get("variants", &bVars); err != nil {
		return &RejectError{Reason: ReasonWriteSimulationError, Detail: err.Error()}
	}
	if _, err := after.get("variants", &aVars); err != nil {
		return &RejectError{Reason: ReasonWriteSimulationError, Detail: err.Error()}
	}
	// Every pre-existing variant must survive byte-for-byte — game-authored AND
	// other experiments' agent_experiment__<id> variants. The ONLY exemption is
	// the variant THIS proposal writes (writtenVariant): it is never resolved by a
	// real context (its shadow branch guarantees that), so overwriting it on
	// re-experimentation is safe and keeps re-runs idempotent (replace, not stack).
	for name, bv := range bVars {
		if name == writtenVariant {
			continue
		}
		av, ok := aVars[name]
		if !ok {
			return &RejectError{Reason: ReasonExistingVariantChanged, Detail: "dropped variant " + name}
		}
		if !jsonEqual(bv, av) {
			return &RejectError{Reason: ReasonExistingVariantChanged, Detail: "changed variant " + name}
		}
	}
	// Any newly added variant must be exactly THIS proposal's reserved experiment
	// variant. (Other experiments' variants are pre-existing, handled above.)
	for name := range aVars {
		if _, existed := bVars[name]; existed {
			continue
		}
		if name != writtenVariant {
			return &RejectError{Reason: ReasonTargetingNotShadowScoped, Detail: "unexpected new variant " + name}
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
// the flag's EXISTING (non-experimental) variant values. The reader of a game
// flag expects one stable type, so a string on a numeric flag would
// crash/misbehave in a shadow game once an LLM emits a mistyped value. We compare
// against the existing variants (not defaultVariant alone) so a flag whose
// variants are uniformly numbers requires a number, etc. The reserved
// experimental variant is skipped — it is the value we are (re)writing, not a
// game-authored type witness. Empty/kindless existing variants (e.g. all-null)
// impose no constraint; a kindless experimental value matches anything.
func checkTypeCompatible(flag *flagObj, experimental any) *RejectError {
	expRaw, err := json.Marshal(experimental)
	if err != nil {
		return &RejectError{Reason: ReasonTypeMismatch, Detail: "experimental value is not JSON-encodable"}
	}
	expKind := jsonKind(expRaw)
	if expKind == "" {
		// A null/kindless experimental value can sit alongside any-typed variants;
		// the structural checks still guard the invariant.
		return nil
	}

	var variants map[string]json.RawMessage
	if _, err := flag.get("variants", &variants); err != nil {
		return &RejectError{Reason: ReasonReadError, Detail: err.Error()}
	}
	for name, vRaw := range variants {
		if name == ExperimentVariant {
			continue
		}
		k := jsonKind(vRaw)
		if k == "" {
			continue // kindless existing variant imposes no type constraint.
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

// checkTargetingShadowScoped enforces that the resulting targeting is a CHAIN of
// shadow-scoped wrappers: each link is {"if":[shadowCond, <its experiment
// variant>, <next link>]} where shadowCond is either the legacy game_kind!=real
// condition (selecting the unscoped agent_experiment variant) or an experiment-
// scoped experiment_id==id AND arm==experimental condition (selecting
// agent_experiment__id). The chain bottoms out in a TERMINAL else that mentions
// NO experiment variant — that is the real / control / other-game path, and it
// is what a real context resolves. This is the structural proof, now across ALL
// branches, that no experiment variant is reachable for a real context.
func checkTargetingShadowScoped(after *flagObj) *RejectError {
	t, ok := after.raw("targeting")
	if !ok {
		// No targeting at all means the experimental variant is selectable by no
		// one — but applyProposal always writes one, so absence is a bug.
		return &RejectError{Reason: ReasonTargetingNotShadowScoped, Detail: "missing targeting"}
	}
	return walkShadowChain(t)
}

// walkShadowChain validates one link of the targeting chain and recurses into
// its else. A node that is NOT a single-key if is the terminal else: it must not
// mention any experiment variant (or it could leak one to the real/control
// path). A node that IS an if must be a recognised shadow link (cond + matching
// experiment variant) before we descend; an if we don't recognise as ours is
// treated as a terminal else and held to the same no-experiment-variant rule.
func walkShadowChain(node json.RawMessage) *RejectError {
	arms, ok := decodeIfArms(node)
	if !ok {
		// Terminal else (real/control path): may be anything that does NOT itself
		// select an experiment variant.
		if mentionsExperimentVariant(node) {
			return &RejectError{Reason: ReasonTargetingNotShadowScoped, Detail: "else-branch can select an experiment variant"}
		}
		return nil
	}

	cond, then, elseBranch := arms[0], arms[1], arms[2]

	// Identify which experiment this link is scoped to and confirm its then-branch
	// selects exactly that experiment's variant. Legacy (game_kind!=real) link
	// selects the unscoped ExperimentVariant; an experiment-scoped link selects
	// agent_experiment__<id>.
	var wantVariant string
	switch {
	case isShadowCondition(cond):
		wantVariant = ExperimentVariant
	default:
		id, ok := experimentIDFromCondition(cond)
		if !ok {
			// An if we don't recognise as a shadow link. It could be a game-authored
			// targeting (e.g. game_mode==Werewolf). It is part of the real/control
			// path, so it must not select any experiment variant — treat as terminal.
			if mentionsExperimentVariant(node) {
				return &RejectError{Reason: ReasonTargetingNotShadowScoped, Detail: "unrecognised if-condition reaches an experiment variant"}
			}
			return nil
		}
		wantVariant = experimentVariantName(id)
	}

	if !variantNameIs(then, wantVariant) {
		return &RejectError{Reason: ReasonTargetingNotShadowScoped, Detail: "then-branch is not this link's experiment variant"}
	}
	// The then-branch is a bare variant string; descend the chain via the else.
	return walkShadowChain(elseBranch)
}

// mentionsExperimentVariant reports whether raw references ANY reserved
// experiment variant name as a JSON string — the legacy agent_experiment or any
// scoped agent_experiment__<id>. A conservative substring check on the raw bytes:
// no experiment variant may appear in a terminal (real/control) branch. Because
// the scoped names are prefixed by the legacy name, a single substring test on
// the prefix catches both.
func mentionsExperimentVariant(raw json.RawMessage) bool {
	return strings.Contains(string(raw), `"`+ExperimentVariant)
}

// checkRealResolutionUnchanged is the belt-and-suspenders EVAL: resolve the flag
// for a synthetic {game_kind:"real"} context before and after; the selected
// variant AND its value must be identical. This backs the structural checks with
// flagd's actual JSONLogic engine (diegoholiveira/jsonlogic, the same lib the
// agent uses elsewhere).
func checkRealResolutionUnchanged(before, after *flagObj) *RejectError {
	realCtx := map[string]any{GameKindVar: GameKindReal}

	bRaw, err := before.marshal()
	if err != nil {
		return &RejectError{Reason: ReasonWriteSimulationError, Detail: err.Error()}
	}
	aRaw, err := after.marshal()
	if err != nil {
		return &RejectError{Reason: ReasonWriteSimulationError, Detail: err.Error()}
	}
	bRes, err := resolveFlag(bRaw, realCtx)
	if err != nil {
		return &RejectError{Reason: ReasonWriteSimulationError, Detail: "resolve before: " + err.Error()}
	}
	aRes, err := resolveFlag(aRaw, realCtx)
	if err != nil {
		return &RejectError{Reason: ReasonWriteSimulationError, Detail: "resolve after: " + err.Error()}
	}
	if bRes.Variant != aRes.Variant || !jsonEqual(bRes.Value, aRes.Value) {
		return &RejectError{
			Reason: ReasonRealResolutionChanged,
			Detail: fmt.Sprintf("real resolves %s=%s before, %s=%s after",
				bRes.Variant, bRes.Value, aRes.Variant, aRes.Value),
		}
	}
	return nil
}

// isGameFlagPath reports whether path is the game flagset file. The agent's write
// surface is hardcoded to this one file, so this is a security boundary, not a
// convenience check — a basename compare ALONE is not enough: a symlink named
// game.json pointing at agent.json has basename "game.json" yet a write through it
// lands in the agent's own governance file. So we ALSO reject a path whose final
// component is a symlink (e.g. game.json -> agent.json). The Writer additionally
// opens with O_NOFOLLOW (writeInPlace), so the kernel refuses to follow a symlink
// even if one is swapped in after this check (TOCTOU) — this check is the
// fail-fast with a clean ReasonNonGameTarget, O_NOFOLLOW is the hard backstop.
func isGameFlagPath(path string) bool {
	if filepath.Base(path) != gameFlagFileName {
		return false
	}
	// Lstat (does NOT follow the final symlink): reject if the game flag path is
	// itself a symlink. A not-yet-existing path is fine — the first write creates a
	// regular file via O_CREATE|O_NOFOLLOW (which cannot create through a symlink).
	if fi, err := os.Lstat(path); err == nil && fi.Mode()&os.ModeSymlink != 0 {
		return false
	}
	return true
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
