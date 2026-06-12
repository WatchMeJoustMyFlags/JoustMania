package experiment

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"testing"

	jsonlogic "github.com/diegoholiveira/jsonlogic/v3"
	"go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// newGameFile copies the game.json fixture into a t.TempDir and returns its path
// — tests never touch the real flag file.
func newGameFile(t *testing.T) string {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("testdata", "game.json"))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "game.json")
	if err := os.WriteFile(path, src, 0o644); err != nil {
		t.Fatalf("write temp fixture: %v", err)
	}
	return path
}

// gateWithRecorder builds a Gate over a temp game.json with an in-memory span
// recorder so tests can assert on the emitted spans.
func gateWithRecorder(t *testing.T) (*Gate, string, *tracetest.SpanRecorder) {
	t.Helper()
	path := newGameFile(t)
	rec := tracetest.NewSpanRecorder()
	tp := trace.NewTracerProvider(trace.WithSpanProcessor(rec))
	w := NewWriter(path, discardLogger())
	g := NewGate(w, tp.Tracer("test"), discardLogger())
	return g, path, rec
}

// readFlagRaw returns the raw JSON of a flag from the file at path.
func readFlagRaw(t *testing.T, path, flagKey string) json.RawMessage {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read file: %v", err)
	}
	var doc struct {
		Flags map[string]json.RawMessage `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("file is invalid JSON: %v", err)
	}
	fr, ok := doc.Flags[flagKey]
	if !ok {
		t.Fatalf("flag %q missing", flagKey)
	}
	return fr
}

// resolveVariant resolves a flag for a game_kind context using the SAME
// jsonlogic engine flagd uses (an independent re-implementation of resolution in
// the test, so it does not just trust resolveFlag).
func resolveVariant(t *testing.T, path, flagKey, gameKind string) (string, any) {
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
		data, _ := json.Marshal(map[string]any{GameKindVar: gameKind})
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

// ---- Happy path: a valid shadow experiment ---------------------------------

func TestReview_ValidShadowProposal(t *testing.T) {
	g, path, rec := gateWithRecorder(t)

	// invincibility_seconds is a plain numeric flag (no pre-existing targeting),
	// default "4s" => 4.0. Experiment: try 6.0 in shadow games.
	const flagKey = "invincibility_seconds"
	beforeDV, beforeVal := resolveVariant(t, path, flagKey, "real")

	err := g.Review(context.Background(), Proposal{
		FlagKey:           flagKey,
		ExperimentalValue: 6.0,
		Rationale:         "longer invincibility may reduce early-game frustration",
	})
	if err != nil {
		t.Fatalf("valid proposal rejected: %v", err)
	}

	// Real games: SAME variant + value as before (THE INVARIANT).
	afterDVReal, afterValReal := resolveVariant(t, path, flagKey, "real")
	if afterDVReal != beforeDV || afterValReal != beforeVal {
		t.Fatalf("real resolution changed: before %s=%v, after %s=%v",
			beforeDV, beforeVal, afterDVReal, afterValReal)
	}

	// Shadow games: resolve the experimental variant + value.
	shadowVar, shadowVal := resolveVariant(t, path, flagKey, "shadow")
	if shadowVar != ExperimentVariant {
		t.Fatalf("shadow variant = %q, want %q", shadowVar, ExperimentVariant)
	}
	if shadowVal != 6.0 {
		t.Fatalf("shadow value = %v, want 6.0", shadowVal)
	}

	// defaultVariant and the existing "4s" variant are byte-unchanged.
	flag := readFlagRaw(t, path, flagKey)
	var f struct {
		Variants       map[string]json.RawMessage `json:"variants"`
		DefaultVariant string                     `json:"defaultVariant"`
	}
	_ = json.Unmarshal(flag, &f)
	if f.DefaultVariant != "4s" {
		t.Fatalf("defaultVariant changed to %q", f.DefaultVariant)
	}
	if string(f.Variants["4s"]) != "4.0" && string(f.Variants["4s"]) != "4" {
		t.Fatalf("existing variant 4s mutated: %s", f.Variants["4s"])
	}
	if _, ok := f.Variants[ExperimentVariant]; !ok {
		t.Fatalf("experimental variant not added")
	}

	// Span: code_improvement.proposed with blocked=false.
	assertSpan(t, rec, SpanProposed, false, "")
}

// A flag that ALREADY has targeting (sensitivity targets game_mode=Werewolf)
// must keep its real-game behaviour while gaining the shadow branch.
func TestReview_PreservesExistingTargetingForReal(t *testing.T) {
	g, path, _ := gateWithRecorder(t)

	const flagKey = "sensitivity"
	// Real + Werewolf: existing targeting selects "fast" (=3). Must persist.
	realWerewolf := func() (string, any) {
		flagRaw := readFlagRaw(t, path, flagKey)
		var flag struct {
			Variants       map[string]any  `json:"variants"`
			DefaultVariant string          `json:"defaultVariant"`
			Targeting      json.RawMessage `json:"targeting"`
		}
		_ = json.Unmarshal(flagRaw, &flag)
		data, _ := json.Marshal(map[string]any{GameKindVar: "real", "game_mode": "Werewolf"})
		var out bytes.Buffer
		if err := jsonlogic.Apply(bytes.NewReader(flag.Targeting), bytes.NewReader(data), &out); err != nil {
			t.Fatalf("apply: %v", err)
		}
		var sel any
		_ = json.Unmarshal(out.Bytes(), &sel)
		name, _ := sel.(string)
		return name, flag.Variants[name]
	}

	beforeVar, beforeVal := realWerewolf()
	if beforeVar != "fast" {
		t.Fatalf("precondition: real+Werewolf should select fast, got %q", beforeVar)
	}

	err := g.Review(context.Background(), Proposal{
		FlagKey:           flagKey,
		ExperimentalValue: 1,
		Rationale:         "try slow sensitivity in shadow",
	})
	if err != nil {
		t.Fatalf("rejected: %v", err)
	}

	afterVar, afterVal := realWerewolf()
	if afterVar != beforeVar || afterVal != beforeVal {
		t.Fatalf("existing real targeting broke: before %s=%v, after %s=%v",
			beforeVar, beforeVal, afterVar, afterVal)
	}
	// Shadow still gets the experiment.
	sv, _ := resolveVariant(t, path, flagKey, "shadow")
	if sv != ExperimentVariant {
		t.Fatalf("shadow variant = %q, want experimental", sv)
	}
}

// ---- Blocked: proposals that would change real resolution ------------------

func TestReview_BlocksUnknownFlag(t *testing.T) {
	g, path, rec := gateWithRecorder(t)
	before := mustRead(t, path)

	err := g.Review(context.Background(), Proposal{FlagKey: "no_such_flag", ExperimentalValue: 1})
	assertBlocked(t, err, ReasonUnknownFlag)
	assertUnchanged(t, path, before)
	assertSpan(t, rec, SpanProposed, true, ReasonUnknownFlag)
}

func TestReview_BlocksNonGamePath(t *testing.T) {
	// A Writer mispointed at agent.json must be denied by the Gate (belt to the
	// :ro mount), and nothing written.
	dir := t.TempDir()
	agentPath := filepath.Join(dir, "agent.json")
	if err := os.WriteFile(agentPath, []byte(`{"flags":{"enabled":{"variants":{"on":true},"defaultVariant":"on"}}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	before := mustRead(t, agentPath)

	rec := tracetest.NewSpanRecorder()
	tp := trace.NewTracerProvider(trace.WithSpanProcessor(rec))
	g := NewGate(NewWriter(agentPath, discardLogger()), tp.Tracer("test"), discardLogger())

	err := g.Review(context.Background(), Proposal{FlagKey: "enabled", ExperimentalValue: false})
	assertBlocked(t, err, ReasonNonGameTarget)
	assertUnchanged(t, agentPath, before)
	assertSpan(t, rec, SpanProposed, true, ReasonNonGameTarget)
}

// A FlagKey that smuggles a path reference is denied even on a game.json Writer.
func TestReview_BlocksPathInFlagKey(t *testing.T) {
	g, path, _ := gateWithRecorder(t)
	before := mustRead(t, path)
	for _, key := range []string{"../agent.json", "foo/bar", "agent.json"} {
		err := g.Review(context.Background(), Proposal{FlagKey: key, ExperimentalValue: 1})
		assertBlocked(t, err, ReasonNonGameTarget)
	}
	assertUnchanged(t, path, before)
}

// The Gate must reject any directly-constructed write that would change a real
// resolution. We can't go through Review for these (the Writer can't produce
// them), so we drive the structural/eval checks via validate-equivalent paths:
// a proposal whose applied result mutates default/existing is impossible through
// applyProposal — so we assert the checks themselves catch a hand-built bad
// after-state.

func TestCheckVariantsAndDefault_DetectsDefaultChange(t *testing.T) {
	before := mustFlag(t, `{"variants":{"a":1,"b":2},"defaultVariant":"a"}`)
	after := mustFlag(t, `{"variants":{"a":1,"b":2,"agent_experiment":9},"defaultVariant":"b"}`)
	if rej := checkVariantsAndDefault(before, after); rej == nil || rej.Reason != ReasonDefaultChanged {
		t.Fatalf("expected default_variant_changed, got %v", rej)
	}
}

func TestCheckVariantsAndDefault_DetectsExistingVariantMutation(t *testing.T) {
	before := mustFlag(t, `{"variants":{"a":1,"b":2},"defaultVariant":"a"}`)
	after := mustFlag(t, `{"variants":{"a":1,"b":99,"agent_experiment":9},"defaultVariant":"a"}`)
	if rej := checkVariantsAndDefault(before, after); rej == nil || rej.Reason != ReasonExistingVariantChanged {
		t.Fatalf("expected existing_variant_changed, got %v", rej)
	}
}

func TestCheckTargeting_DetectsNonShadowScope(t *testing.T) {
	// A targeting that selects the experimental variant for everyone (no shadow
	// condition) must be rejected.
	after := mustFlag(t, `{"variants":{"a":1,"agent_experiment":9},"defaultVariant":"a","targeting":"agent_experiment"}`)
	if rej := checkTargetingShadowScoped(after); rej == nil || rej.Reason != ReasonTargetingNotShadowScoped {
		t.Fatalf("expected targeting_not_shadow_scoped, got %v", rej)
	}
}

func TestCheckTargeting_DetectsRealLeakInElse(t *testing.T) {
	// Correct shadow condition but the else (real) branch also selects the
	// experimental variant — a real leak.
	after := mustFlag(t, `{"variants":{"a":1,"agent_experiment":9},"defaultVariant":"a","targeting":{"if":[{"!=":[{"var":"game_kind"},"real"]},"agent_experiment","agent_experiment"]}}`)
	if rej := checkTargetingShadowScoped(after); rej == nil || rej.Reason != ReasonTargetingNotShadowScoped {
		t.Fatalf("expected targeting_not_shadow_scoped (real leak), got %v", rej)
	}
}

func TestCheckRealResolution_DetectsChange(t *testing.T) {
	// before: real => a(=1). after: a non-shadow targeting flips real to b(=2).
	before := mustFlag(t, `{"variants":{"a":1,"b":2},"defaultVariant":"a"}`)
	after := mustFlag(t, `{"variants":{"a":1,"b":2},"defaultVariant":"a","targeting":{"if":[{"==":[{"var":"game_kind"},"real"]},"b","a"]}}`)
	if rej := checkRealResolutionUnchanged(before, after); rej == nil || rej.Reason != ReasonRealResolutionChanged {
		t.Fatalf("expected real_resolution_changed, got %v", rej)
	}
}

// ---- Idempotent re-experimentation -----------------------------------------

func TestReview_ReExperimentIsIdempotent(t *testing.T) {
	g, path, _ := gateWithRecorder(t)
	const flagKey = "invincibility_seconds"

	if err := g.Review(context.Background(), Proposal{FlagKey: flagKey, ExperimentalValue: 6.0}); err != nil {
		t.Fatalf("first: %v", err)
	}
	if err := g.Review(context.Background(), Proposal{FlagKey: flagKey, ExperimentalValue: 7.0}); err != nil {
		t.Fatalf("second: %v", err)
	}

	// Real unchanged; shadow now resolves the latest value; targeting did not
	// nest a second shadow branch.
	dvReal, valReal := resolveVariant(t, path, flagKey, "real")
	if dvReal != "4s" || valReal != 4.0 {
		t.Fatalf("real drifted after re-experiment: %s=%v", dvReal, valReal)
	}
	_, shadowVal := resolveVariant(t, path, flagKey, "shadow")
	if shadowVal != 7.0 {
		t.Fatalf("shadow value = %v, want 7.0 (latest)", shadowVal)
	}
	// Exactly one experimental variant, one shadow branch.
	flag := readFlagRaw(t, path, flagKey)
	var f struct {
		Targeting map[string]json.RawMessage `json:"targeting"`
	}
	_ = json.Unmarshal(flag, &f)
	var arms []json.RawMessage
	_ = json.Unmarshal(f.Targeting["if"], &arms)
	if len(arms) != 3 || string(arms[2]) != "null" {
		t.Fatalf("re-experiment nested the shadow branch: else=%s", arms[2])
	}
}

// ---- Atomicity / concurrency ----------------------------------------------

func TestApply_AtomicAndConcurrent(t *testing.T) {
	path := newGameFile(t)
	w := NewWriter(path, discardLogger())

	var wg sync.WaitGroup
	flags := []string{"invincibility_seconds", "num_teams", "nonstop.respawn_seconds"}
	for i := 0; i < 30; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_ = w.Apply(Proposal{FlagKey: flags[i%len(flags)], ExperimentalValue: float64(i)})
		}(i)
	}
	wg.Wait()

	// File is valid JSON (no partial write) after the concurrent storm.
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("concurrent writes corrupted the file: %v", err)
	}
}

// ---- Untouched flags round-trip byte-for-byte ------------------------------

func TestApply_LeavesOtherFlagsByteIdentical(t *testing.T) {
	path := newGameFile(t)
	w := NewWriter(path, discardLogger())

	otherBefore := readFlagRaw(t, path, "num_teams")
	if err := w.Apply(Proposal{FlagKey: "invincibility_seconds", ExperimentalValue: 6.0}); err != nil {
		t.Fatal(err)
	}
	otherAfter := readFlagRaw(t, path, "num_teams")
	if !bytes.Equal(otherBefore, otherAfter) {
		t.Fatalf("unrelated flag num_teams changed:\nbefore %s\nafter  %s", otherBefore, otherAfter)
	}
}

// ---- helpers ---------------------------------------------------------------

func mustRead(t *testing.T, path string) []byte {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func assertUnchanged(t *testing.T, path string, before []byte) {
	t.Helper()
	after := mustRead(t, path)
	if !bytes.Equal(before, after) {
		t.Fatalf("file changed despite blocked proposal:\nbefore %s\nafter  %s", before, after)
	}
}

func mustFlag(t *testing.T, raw string) *flagObj {
	t.Helper()
	keys, vals, err := decodeOrdered([]byte(raw))
	if err != nil {
		t.Fatalf("decode flag fixture: %v", err)
	}
	return &flagObj{keys: keys, values: vals}
}

func assertBlocked(t *testing.T, err error, want Reason) {
	t.Helper()
	rej, ok := AsReject(err)
	if !ok {
		t.Fatalf("expected RejectError, got %v", err)
	}
	if rej.Reason != want {
		t.Fatalf("reason = %q, want %q", rej.Reason, want)
	}
}

func assertSpan(t *testing.T, rec *tracetest.SpanRecorder, name string, blocked bool, reason Reason) {
	t.Helper()
	for _, s := range rec.Ended() {
		if s.Name() != name {
			continue
		}
		var gotBlocked bool
		var gotReason string
		for _, a := range s.Attributes() {
			switch string(a.Key) {
			case AttrBlocked:
				gotBlocked = a.Value.AsBool()
			case AttrReason:
				gotReason = a.Value.AsString()
			}
		}
		if gotBlocked != blocked {
			t.Fatalf("span %s blocked=%v, want %v", name, gotBlocked, blocked)
		}
		if blocked && gotReason != string(reason) {
			t.Fatalf("span %s reason=%q, want %q", name, gotReason, reason)
		}
		return
	}
	t.Fatalf("span %q not found", name)
}
