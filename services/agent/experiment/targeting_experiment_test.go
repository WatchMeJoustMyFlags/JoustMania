package experiment

import (
	"bytes"
	"context"
	"encoding/json"
	"testing"

	jsonlogic "github.com/diegoholiveira/jsonlogic/v3"
)

// resolveExperiment resolves a flag for a full experiment eval-context
// (game_kind/experiment_id/arm) using the SAME jsonlogic engine flagd uses — an
// independent re-implementation of resolution in the test, so it does not just
// trust resolveFlag. ctx keys absent from a real game (no experiment_id) model a
// real game by construction.
func resolveExperiment(t *testing.T, path, flagKey string, ctx map[string]any) (string, any) {
	t.Helper()
	flagRaw := readFlagRaw(t, path, flagKey)
	var flag struct {
		Variants       map[string]any  `json:"variants"`
		DefaultVariant string          `json:"defaultVariant"`
		Targeting      json.RawMessage `json:"targeting"`
	}
	if err := json.Unmarshal(flagRaw, &flag); err != nil {
		t.Fatalf("decode flag: %v", err)
	}
	variant := flag.DefaultVariant
	if len(flag.Targeting) > 0 {
		data, _ := json.Marshal(ctx)
		var out bytes.Buffer
		if err := jsonlogic.Apply(bytes.NewReader(flag.Targeting), bytes.NewReader(data), &out); err != nil {
			t.Fatalf("jsonlogic apply: %v", err)
		}
		var sel any
		_ = json.Unmarshal(out.Bytes(), &sel)
		if name, ok := sel.(string); ok {
			if _, known := flag.Variants[name]; known {
				variant = name
			}
		}
	}
	return variant, flag.Variants[variant]
}

// realCtx / experimentalCtx / controlCtx build the three eval contexts a game
// presents. A real game omits experiment_id entirely (API-level default).
func realCtx() map[string]any { return map[string]any{GameKindVar: GameKindReal} }
func experimentalCtx(id string) map[string]any {
	return map[string]any{GameKindVar: GameKindShadowForTest, ExperimentIDVar: id, ArmVar: ArmExperimental}
}
func controlCtx(id string) map[string]any {
	return map[string]any{GameKindVar: GameKindShadowForTest, ExperimentIDVar: id, ArmVar: "control"}
}

// GameKindShadowForTest mirrors the shadow game_kind value (#975 GAME_KIND_SHADOW
// = "shadow"). Experiment targeting does NOT key on it; it is set only so the
// eval contexts look like real shadow games.
const GameKindShadowForTest = "shadow"

// ---- Single experiment ------------------------------------------------------

// A single experiment-scoped proposal: the experimental arm resolves V; the
// control arm AND a real game both resolve the pre-experiment default.
func TestReview_SingleExperimentScoped(t *testing.T) {
	g, path, _ := gateWithRecorder(t)
	const flagKey = "invincibility_seconds"
	const expID = "exp_alpha"

	beforeVar, beforeVal := resolveExperiment(t, path, flagKey, realCtx())

	if err := g.Review(context.Background(), Proposal{
		FlagKey:           flagKey,
		ExperimentID:      expID,
		ExperimentalValue: 6.0,
		Rationale:         "longer invincibility for cohort alpha",
	}); err != nil {
		t.Fatalf("experiment proposal rejected: %v", err)
	}

	// Experimental arm resolves the experiment-scoped variant + value.
	expVar, expVal := resolveExperiment(t, path, flagKey, experimentalCtx(expID))
	if want := experimentVariantName(expID); expVar != want {
		t.Fatalf("experimental arm variant = %q, want %q", expVar, want)
	}
	if expVal != 6.0 {
		t.Fatalf("experimental arm value = %v, want 6.0", expVal)
	}

	// Control arm falls through to the pre-experiment default (no separate control
	// variant — control IS the else branch).
	ctlVar, ctlVal := resolveExperiment(t, path, flagKey, controlCtx(expID))
	if ctlVar != beforeVar || ctlVal != beforeVal {
		t.Fatalf("control arm drifted: before %s=%v, control %s=%v", beforeVar, beforeVal, ctlVar, ctlVal)
	}

	// Real game (no experiment_id) — THE INVARIANT.
	realVar, realVal := resolveExperiment(t, path, flagKey, realCtx())
	if realVar != beforeVar || realVal != beforeVal {
		t.Fatalf("real resolution changed: before %s=%v, after %s=%v", beforeVar, beforeVal, realVar, realVal)
	}
}

// ---- Two experiments on the SAME flag: non-bleed ---------------------------

// The headline #977 test: two concurrent experiments on one flag coexist as
// chained ifs. Each experiment's experimental arm resolves ONLY its own value;
// the other experiment's games (both arms) and real games are unaffected. Mirrors
// the fail-safe style of lib/tests/test_feature_flags.py (real-by-default + each
// scope isolated).
func TestReview_TwoExperimentsSameFlagNonBleed(t *testing.T) {
	g, path, _ := gateWithRecorder(t)
	const flagKey = "invincibility_seconds"
	const expA, expB = "exp_alpha", "exp_beta"

	_, realValBefore := resolveExperiment(t, path, flagKey, realCtx())

	if err := g.Review(context.Background(), Proposal{
		FlagKey: flagKey, ExperimentID: expA, ExperimentalValue: 6.0,
	}); err != nil {
		t.Fatalf("experiment A rejected: %v", err)
	}
	if err := g.Review(context.Background(), Proposal{
		FlagKey: flagKey, ExperimentID: expB, ExperimentalValue: 8.0,
	}); err != nil {
		t.Fatalf("experiment B rejected: %v", err)
	}

	// A's experimental arm resolves ONLY A's value.
	if v, val := resolveExperiment(t, path, flagKey, experimentalCtx(expA)); v != experimentVariantName(expA) || val != 6.0 {
		t.Fatalf("A experimental = %s/%v, want %s/6.0", v, val, experimentVariantName(expA))
	}
	// B's experimental arm resolves ONLY B's value (A did NOT clobber it; B's
	// write did NOT bleed into A).
	if v, val := resolveExperiment(t, path, flagKey, experimentalCtx(expB)); v != experimentVariantName(expB) || val != 8.0 {
		t.Fatalf("B experimental = %s/%v, want %s/8.0", v, val, experimentVariantName(expB))
	}

	// Each control arm and BOTH real games resolve the original default — unbled.
	for name, ctx := range map[string]map[string]any{
		"A control": controlCtx(expA),
		"B control": controlCtx(expB),
		"real":      realCtx(),
		// A game in experiment A's experimental arm must NOT pick up B's value and
		// vice versa — covered above; here ensure an unrelated experiment id falls
		// through too.
		"other experiment": experimentalCtx("exp_gamma"),
	} {
		if _, val := resolveExperiment(t, path, flagKey, ctx); val != realValBefore {
			t.Fatalf("%s bled: resolved %v, want original default %v", name, val, realValBefore)
		}
	}

	// Both experiment variants exist; the two original game variants survive.
	flag := readFlagRaw(t, path, flagKey)
	var f struct {
		Variants map[string]json.RawMessage `json:"variants"`
	}
	_ = json.Unmarshal(flag, &f)
	for _, want := range []string{experimentVariantName(expA), experimentVariantName(expB), "4s", "6s"} {
		if _, ok := f.Variants[want]; !ok {
			t.Fatalf("variant %q missing after two experiments", want)
		}
	}
}

// Re-running the SAME experiment id is idempotent (replace its value) and does
// NOT disturb the OTHER experiment chained on the same flag.
func TestReview_ReExperimentSameIDPreservesOther(t *testing.T) {
	g, path, _ := gateWithRecorder(t)
	const flagKey = "invincibility_seconds"
	const expA, expB = "exp_alpha", "exp_beta"

	for _, p := range []Proposal{
		{FlagKey: flagKey, ExperimentID: expA, ExperimentalValue: 6.0},
		{FlagKey: flagKey, ExperimentID: expB, ExperimentalValue: 8.0},
		{FlagKey: flagKey, ExperimentID: expA, ExperimentalValue: 7.0}, // re-run A
	} {
		if err := g.Review(context.Background(), p); err != nil {
			t.Fatalf("review %+v: %v", p, err)
		}
	}

	if _, val := resolveExperiment(t, path, flagKey, experimentalCtx(expA)); val != 7.0 {
		t.Fatalf("A after re-run = %v, want 7.0 (latest)", val)
	}
	if _, val := resolveExperiment(t, path, flagKey, experimentalCtx(expB)); val != 8.0 {
		t.Fatalf("B after A re-run = %v, want 8.0 (undisturbed)", val)
	}
	// Exactly one variant per experiment (no accumulation).
	flag := readFlagRaw(t, path, flagKey)
	var f struct {
		Variants map[string]json.RawMessage `json:"variants"`
	}
	_ = json.Unmarshal(flag, &f)
	count := 0
	for name := range f.Variants {
		if isAnyExperimentVariant(name) {
			count++
		}
	}
	if count != 2 {
		t.Fatalf("expected 2 experiment variants (A,B), got %d", count)
	}
}

// Two experiments on DIFFERENT flags are fully independent (K independent rules).
func TestReview_TwoExperimentsDifferentFlags(t *testing.T) {
	g, path, _ := gateWithRecorder(t)

	_, numBefore := resolveExperiment(t, path, "num_teams", realCtx())

	if err := g.Review(context.Background(), Proposal{
		FlagKey: "invincibility_seconds", ExperimentID: "exp_a", ExperimentalValue: 6.0,
	}); err != nil {
		t.Fatalf("A: %v", err)
	}
	if err := g.Review(context.Background(), Proposal{
		FlagKey: "num_teams", ExperimentID: "exp_b", ExperimentalValue: 4,
	}); err != nil {
		t.Fatalf("B: %v", err)
	}

	if _, val := resolveExperiment(t, path, "num_teams", realCtx()); val != numBefore {
		t.Fatalf("num_teams real drifted: %v want %v", val, numBefore)
	}
	if _, val := resolveExperiment(t, path, "num_teams", experimentalCtx("exp_b")); val != float64(4) {
		t.Fatalf("num_teams exp_b experimental = %v, want 4", val)
	}
}

// ---- Gate verifies the invariant across ALL branches -----------------------

// A hand-built after-state with TWO experiment branches whose TERMINAL else
// leaks an experiment variant to the real/control path must be rejected by
// checkTargetingShadowScoped (the chain walk reaches all branches).
func TestCheckTargeting_ChainTerminalElseLeak(t *testing.T) {
	after := mustFlag(t, `{
		"variants":{"a":1,"agent_experiment__x":9,"agent_experiment__y":7},
		"defaultVariant":"a",
		"targeting":{"if":[
			{"and":[{"==":[{"var":"experiment_id"},"x"]},{"==":[{"var":"arm"},"experimental"]}]},
			"agent_experiment__x",
			{"if":[
				{"and":[{"==":[{"var":"experiment_id"},"y"]},{"==":[{"var":"arm"},"experimental"]}]},
				"agent_experiment__y",
				"agent_experiment__x"
			]}
		]}
	}`)
	if rej := checkTargetingShadowScoped(after); rej == nil || rej.Reason != ReasonTargetingNotShadowScoped {
		t.Fatalf("expected targeting_not_shadow_scoped for terminal-else leak, got %v", rej)
	}
}

// A chain whose branch then-arm selects the WRONG experiment's variant (a
// mismatched cond/variant pairing) is rejected.
func TestCheckTargeting_ChainBranchVariantMismatch(t *testing.T) {
	after := mustFlag(t, `{
		"variants":{"a":1,"agent_experiment__x":9,"agent_experiment__y":7},
		"defaultVariant":"a",
		"targeting":{"if":[
			{"and":[{"==":[{"var":"experiment_id"},"x"]},{"==":[{"var":"arm"},"experimental"]}]},
			"agent_experiment__y",
			null
		]}
	}`)
	if rej := checkTargetingShadowScoped(after); rej == nil || rej.Reason != ReasonTargetingNotShadowScoped {
		t.Fatalf("expected targeting_not_shadow_scoped for branch/variant mismatch, got %v", rej)
	}
}

// A well-formed two-experiment chain (the exact shape applyProposal builds) is
// ACCEPTED by the chain walk.
func TestCheckTargeting_ValidChainAccepted(t *testing.T) {
	after := mustFlag(t, `{
		"variants":{"a":1,"agent_experiment__x":9,"agent_experiment__y":7},
		"defaultVariant":"a",
		"targeting":{"if":[
			{"and":[{"==":[{"var":"experiment_id"},"y"]},{"==":[{"var":"arm"},"experimental"]}]},
			"agent_experiment__y",
			{"if":[
				{"and":[{"==":[{"var":"experiment_id"},"x"]},{"==":[{"var":"arm"},"experimental"]}]},
				"agent_experiment__x",
				null
			]}
		]}
	}`)
	if rej := checkTargetingShadowScoped(after); rej != nil {
		t.Fatalf("valid two-experiment chain rejected: %v", rej)
	}
}

// The real-resolution eval over the full chain catches a proposal that would
// change real resolution: a chain whose terminal else flips real to b.
func TestCheckRealResolution_ChainAcrossBranches(t *testing.T) {
	before := mustFlag(t, `{"variants":{"a":1,"b":2},"defaultVariant":"a"}`)
	after := mustFlag(t, `{"variants":{"a":1,"b":2,"agent_experiment__x":9},"defaultVariant":"a",
		"targeting":{"if":[
			{"and":[{"==":[{"var":"experiment_id"},"x"]},{"==":[{"var":"arm"},"experimental"]}]},
			"agent_experiment__x",
			{"if":[{"==":[{"var":"game_kind"},"real"]},"b","a"]}
		]}}`)
	if rej := checkRealResolutionUnchanged(before, after); rej == nil || rej.Reason != ReasonRealResolutionChanged {
		t.Fatalf("expected real_resolution_changed across chain, got %v", rej)
	}
}

// ---- Regression: empty ExperimentID round-trips the legacy rule byte-for-byte
// (the pre-#977 binary game_kind!=real rule). -------------------------------

func TestBuildShadowTargeting_LegacyByteForByte(t *testing.T) {
	got, err := buildShadowTargeting("", json.RawMessage("null"))
	if err != nil {
		t.Fatal(err)
	}
	const want = `{"if":[{"!=":[{"var":"game_kind"},"real"]},"agent_experiment",null]}`
	if string(got) != want {
		t.Fatalf("legacy targeting drifted:\n got %s\nwant %s", got, want)
	}
}

// experimentVariantName / experimentIDFromCondition round-trip.
func TestExperimentVariantNameRoundTrip(t *testing.T) {
	if experimentVariantName("") != ExperimentVariant {
		t.Fatalf("empty id should yield legacy variant")
	}
	if got := experimentVariantName("exp_42"); got != "agent_experiment__exp_42" {
		t.Fatalf("scoped variant = %q", got)
	}
	cond, _ := json.Marshal(shadowCondition("exp_42"))
	id, ok := experimentIDFromCondition(cond)
	if !ok || id != "exp_42" {
		t.Fatalf("condition decode = %q,%v", id, ok)
	}
	// The legacy condition is not an experiment condition.
	legacy, _ := json.Marshal(shadowCondition(""))
	if _, ok := experimentIDFromCondition(legacy); ok {
		t.Fatalf("legacy condition mis-decoded as experiment")
	}
	// A control-arm condition must NOT decode (arm != experimental).
	ctlCond, _ := json.Marshal(map[string]any{"and": []any{
		map[string]any{"==": []any{map[string]any{"var": ExperimentIDVar}, "exp_42"}},
		map[string]any{"==": []any{map[string]any{"var": ArmVar}, "control"}},
	}})
	if _, ok := experimentIDFromCondition(ctlCond); ok {
		t.Fatalf("control-arm condition mis-decoded as experimental")
	}
}
