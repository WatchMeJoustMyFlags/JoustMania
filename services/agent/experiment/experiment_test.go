package experiment

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/url"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	jsonlogic "github.com/diegoholiveira/jsonlogic/v3"
	"go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/flagservice/fake"
)

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// registryPath is the flag service's registry: the experiment catalog (the
// game flags whose row allows experiment+arm, flags.md §13.3) and its bounds.
var registryPath = filepath.Join("..", "..", "flag-service", "registry.yaml")

// newGameService serves testdata/game.json as the game flag set of a fake flag
// service with the real registry's game policies.
func newGameService(t *testing.T) *fake.Service {
	t.Helper()
	policies, err := fake.RegistryPolicies(registryPath, GameFlagSet)
	if err != nil {
		t.Fatalf("read registry: %v", err)
	}
	svc := fake.New()
	if err := svc.LoadSetWithPolicies(GameFlagSet, filepath.Join("testdata", "game.json"), policies); err != nil {
		t.Fatalf("load fixture: %v", err)
	}
	return svc
}

// gateWithRecorder builds a Gate over a fake flag service with an in-memory
// span recorder so tests can assert on the emitted spans.
func gateWithRecorder(t *testing.T) (*Gate, *fake.Service, *tracetest.SpanRecorder) {
	t.Helper()
	svc := newGameService(t)
	rec := tracetest.NewSpanRecorder()
	tp := trace.NewTracerProvider(trace.WithSpanProcessor(rec))
	g := NewGate(NewWriter(svc, discardLogger()), tp.Tracer("test"), discardLogger())
	return g, svc, rec
}

// resolve resolves the rendered flag for an eval context with the jsonlogic
// engine flagd uses (independent of resolveFlag).
func resolve(t *testing.T, svc *fake.Service, flagKey string, evalCtx map[string]any) (string, any) {
	t.Helper()
	var flag struct {
		Variants       map[string]any  `json:"variants"`
		DefaultVariant string          `json:"defaultVariant"`
		Targeting      json.RawMessage `json:"targeting"`
	}
	if err := json.Unmarshal(svc.Rendered(GameFlagSet, flagKey), &flag); err != nil {
		t.Fatalf("decode rendered flag: %v", err)
	}
	variant := flag.DefaultVariant
	if len(flag.Targeting) > 0 {
		data, _ := json.Marshal(evalCtx)
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

func realCtx() map[string]any { return map[string]any{GameKindVar: GameKindReal} }
func experimentalCtx(id string) map[string]any {
	return map[string]any{GameKindVar: "shadow", ExperimentIDVar: id, ArmVar: ArmExperimental}
}
func controlCtx(id string) map[string]any {
	return map[string]any{GameKindVar: "shadow", ExperimentIDVar: id, ArmVar: "control"}
}

// ---- Happy path --------------------------------------------------------------

// UC: UC-042 main
// UC: UC-043 main BR-001 Post-F-1
func TestReview_ExperimentScopedProposal(t *testing.T) {
	g, svc, rec := gateWithRecorder(t)
	const flagKey, expID = "nonstop.respawn_seconds", "exp_alpha"
	beforeVar, beforeVal := resolve(t, svc, flagKey, realCtx())

	if err := g.Review(context.Background(), Proposal{
		FlagKey: flagKey, ExperimentID: expID, ExperimentalValue: 6.0,
		Rationale: "a longer respawn wait may reduce early-game frustration",
	}); err != nil {
		t.Fatalf("valid proposal rejected: %v", err)
	}

	if v, val := resolve(t, svc, flagKey, realCtx()); v != beforeVar || val != beforeVal {
		t.Fatalf("real resolution changed: before %s=%v, after %s=%v", beforeVar, beforeVal, v, val)
	}
	if _, val := resolve(t, svc, flagKey, experimentalCtx(expID)); val != 6.0 {
		t.Fatalf("experimental arm value = %v, want 6.0", val)
	}
	if v, val := resolve(t, svc, flagKey, controlCtx(expID)); v != beforeVar || val != beforeVal {
		t.Fatalf("control arm drifted: %s=%v", v, val)
	}
	// A shadow game of ANOTHER experiment is not affected either.
	if v, _ := resolve(t, svc, flagKey, experimentalCtx("exp_other")); v != beforeVar {
		t.Fatalf("other experiment resolves %s", v)
	}
	// Check before write: one dry run, then the one write (PR #1417 review B5).
	if got := svc.Calls; len(got) != 2 || got[0] != "DRYRUN game/nonstop.respawn_seconds experiment+arm" || got[1] != "PUT game/nonstop.respawn_seconds experiment+arm" {
		t.Fatalf("calls = %v, want a dry run then one experiment+arm PUT", got)
	}
	assertSpan(t, rec, SpanProposed, false, "")
}

// A flag with baseline targeting (sensitivity: Werewolf → fast) keeps its real
// behaviour; the experiment rung sits in front of it.
func TestReview_PreservesExistingTargetingForReal(t *testing.T) {
	g, svc, _ := gateWithRecorder(t)
	const flagKey, expID = "sensitivity", "exp_sens"
	realWerewolf := map[string]any{GameKindVar: GameKindReal, "game_mode": "Werewolf"}
	if v, _ := resolve(t, svc, flagKey, realWerewolf); v != "fast" {
		t.Fatalf("precondition: real+Werewolf should select fast, got %q", v)
	}
	if err := g.Review(context.Background(), Proposal{FlagKey: flagKey, ExperimentID: expID, ExperimentalValue: 1}); err != nil {
		t.Fatalf("rejected: %v", err)
	}
	if v, _ := resolve(t, svc, flagKey, realWerewolf); v != "fast" {
		t.Fatalf("existing real targeting broke: %s", v)
	}
	if _, val := resolve(t, svc, flagKey, experimentalCtx(expID)); val != 1.0 {
		t.Fatalf("experimental arm value = %v, want 1", val)
	}
}

// UC: UC-042 main
func TestReview_TwoExperimentsSameFlagNonBleed(t *testing.T) {
	g, svc, _ := gateWithRecorder(t)
	const flagKey = "nonstop.respawn_seconds"
	ctx := context.Background()
	if err := g.Review(ctx, Proposal{FlagKey: flagKey, ExperimentID: "exp_a", ExperimentalValue: 6.0}); err != nil {
		t.Fatal(err)
	}
	if err := g.Review(ctx, Proposal{FlagKey: flagKey, ExperimentID: "exp_b", ExperimentalValue: 2.0}); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		ctx  map[string]any
		want any
	}{
		{experimentalCtx("exp_a"), 6.0},
		{experimentalCtx("exp_b"), 2.0},
		{controlCtx("exp_a"), 3.0},
		{controlCtx("exp_b"), 3.0},
		{realCtx(), 3.0},
	} {
		if _, val := resolve(t, svc, flagKey, c.ctx); val != c.want {
			t.Fatalf("ctx %v resolves %v, want %v", c.ctx, val, c.want)
		}
	}
}

// Re-experimenting with the same id replaces the value (no second rung).
func TestReview_ReExperimentReplacesValue(t *testing.T) {
	g, svc, _ := gateWithRecorder(t)
	const flagKey, expID = "nonstop.respawn_seconds", "exp_re"
	ctx := context.Background()
	if err := g.Review(ctx, Proposal{FlagKey: flagKey, ExperimentID: expID, ExperimentalValue: 6.0}); err != nil {
		t.Fatal(err)
	}
	if err := g.Review(ctx, Proposal{FlagKey: flagKey, ExperimentID: expID, ExperimentalValue: 7.0}); err != nil {
		t.Fatal(err)
	}
	if arms := svc.ArmValues(GameFlagSet, flagKey); len(arms) != 1 {
		t.Fatalf("arms = %v, want one", arms)
	}
	if _, val := resolve(t, svc, flagKey, experimentalCtx(expID)); val != 7.0 {
		t.Fatalf("experimental arm = %v, want 7.0 (latest)", val)
	}
	if _, val := resolve(t, svc, flagKey, realCtx()); val != 3.0 {
		t.Fatalf("real drifted: %v", val)
	}
}

// ---- Blocked -------------------------------------------------------------------

// UC: UC-042 A4
func TestReview_BlocksUnknownFlag(t *testing.T) {
	g, svc, rec := gateWithRecorder(t)
	err := g.Review(context.Background(), Proposal{FlagKey: "no_such_flag", ExperimentID: "exp_x", ExperimentalValue: 1})
	assertBlocked(t, err, ReasonUnknownFlag)
	assertNoWrites(t, svc)
	assertSpan(t, rec, SpanProposed, true, ReasonUnknownFlag)
}

// UC: UC-042 A4
func TestReview_BlocksPathOrOtherSetInFlagKey(t *testing.T) {
	g, svc, _ := gateWithRecorder(t)
	for _, key := range []string{"../agent/enabled", "foo/bar", "agent.json", "x?persist=true", ""} {
		err := g.Review(context.Background(), Proposal{FlagKey: key, ExperimentID: "exp_x", ExperimentalValue: 1})
		assertBlocked(t, err, ReasonNonGameTarget)
	}
	assertNoWrites(t, svc)
}

// FR-138: an experiment value without an experiment id is refused.
func TestReview_BlocksUnscopedProposal(t *testing.T) {
	g, svc, rec := gateWithRecorder(t)
	err := g.Review(context.Background(), Proposal{FlagKey: "nonstop.respawn_seconds", ExperimentalValue: 6.0})
	assertBlocked(t, err, ReasonNotExperimentable)
	assertNoWrites(t, svc)
	assertSpan(t, rec, SpanProposed, true, ReasonNotExperimentable)
}

// A flag outside the experiment catalog (registry) is refused before any write.
// UC: UC-043 main
func TestReview_BlocksFlagOutsideCatalog(t *testing.T) {
	g, svc, _ := gateWithRecorder(t)
	for _, key := range []string{"num_teams", "random_assignment"} {
		err := g.Review(context.Background(), Proposal{FlagKey: key, ExperimentID: "exp_x", ExperimentalValue: 4})
		assertBlocked(t, err, ReasonNotExperimentable)
	}
	assertNoWrites(t, svc)
}

// The registry bounds a catalog value to a declared variant's value.
func TestReview_BlocksOutOfBoundsValue(t *testing.T) {
	g, svc, _ := gateWithRecorder(t)
	err := g.Review(context.Background(), Proposal{FlagKey: "nonstop.respawn_seconds", ExperimentID: "exp_x", ExperimentalValue: 99.0})
	assertBlocked(t, err, ReasonOutOfBounds)
	if arms := svc.ArmValues(GameFlagSet, "nonstop.respawn_seconds"); len(arms) != 0 {
		t.Fatalf("refused value was stored: %v", arms)
	}
}

func TestReview_BlocksStringOnNumericFlag(t *testing.T) {
	g, svc, rec := gateWithRecorder(t)
	err := g.Review(context.Background(), Proposal{FlagKey: "nonstop.respawn_seconds", ExperimentID: "exp_x", ExperimentalValue: "six"})
	assertBlocked(t, err, ReasonTypeMismatch)
	assertNoWrites(t, svc)
	assertSpan(t, rec, SpanProposed, true, ReasonTypeMismatch)
}

// An int on a float-variant flag and a float on an int-variant flag are one JSON
// number kind: accepted.
func TestReview_AcceptsAnyNumberKind(t *testing.T) {
	g, _, _ := gateWithRecorder(t)
	if err := g.Review(context.Background(), Proposal{FlagKey: "nonstop.respawn_seconds", ExperimentID: "exp_i", ExperimentalValue: 6}); err != nil {
		t.Fatalf("int on float flag rejected: %v", err)
	}
	if err := g.Review(context.Background(), Proposal{FlagKey: "fight_club.round_seconds", ExperimentID: "exp_f", ExperimentalValue: 15.0}); err != nil {
		t.Fatalf("float on int flag rejected: %v", err)
	}
	// A value inside the catalog range that is no declared variant (the dry-run seed).
	if err := g.Review(context.Background(), Proposal{FlagKey: "nonstop.respawn_seconds", ExperimentID: "exp_r", ExperimentalValue: 4.5}); err != nil {
		t.Fatalf("in-range value rejected: %v", err)
	}
}

// A service read or write failure fails closed.
func TestReview_ServiceErrorsFailClosed(t *testing.T) {
	g, svc, _ := gateWithRecorder(t)
	svc.FailPut = &flagservice.Error{Status: 503, Reason: "no_sync_stream"}
	err := g.Review(context.Background(), Proposal{FlagKey: "nonstop.respawn_seconds", ExperimentID: "exp_x", ExperimentalValue: 6.0})
	assertBlocked(t, err, ReasonWriteSimulationError)

	broken := NewGate(NewWriter(failingService{}, discardLogger()), nil, discardLogger())
	err = broken.Review(context.Background(), Proposal{FlagKey: "nonstop.respawn_seconds", ExperimentID: "exp_x", ExperimentalValue: 6.0})
	assertBlocked(t, err, ReasonReadError)
}

// The second check runs BEFORE the write: a dry run that renders as something
// that changes the real resolution is refused and nothing is written (PR #1417
// review B5).
// UC: UC-043 main Post-F-1
func TestReview_DryRunCatchesARealLeakBeforeWriting(t *testing.T) {
	svc := newGameService(t)
	leaky := &leakingService{Service: svc}
	g := NewGate(NewWriter(leaky, discardLogger()), nil, discardLogger())
	err := g.Review(context.Background(), Proposal{FlagKey: "nonstop.respawn_seconds", ExperimentID: "exp_x", ExperimentalValue: 6.0})
	assertBlocked(t, err, ReasonDefaultChanged)
	assertNoWrites(t, svc)
	if arms := svc.ArmValues(GameFlagSet, "nonstop.respawn_seconds"); len(arms) != 0 {
		t.Fatalf("a refused proposal left arm values: %v", arms)
	}
}

// ---- checkRenderedFlag unit checks -------------------------------------------

// UC: UC-042 A4
// UC: UC-043 main
func TestCheckRenderedFlag(t *testing.T) {
	before := json.RawMessage(`{"variants":{"a":1,"b":2},"defaultVariant":"a"}`)
	cases := []struct {
		name  string
		after string
		want  Reason
	}{
		{"default changed", `{"variants":{"a":1,"b":2,"r1":9},"defaultVariant":"b"}`, ReasonDefaultChanged},
		{"existing variant mutated", `{"variants":{"a":1,"b":99,"r1":9},"defaultVariant":"a"}`, ReasonExistingVariantChanged},
		{"unexpected variant", `{"variants":{"a":1,"b":2,"r1":9,"zz":3},"defaultVariant":"a"}`, ReasonTargetingNotShadowScoped},
		{"real leak", `{"variants":{"a":1,"b":2,"r1":9},"defaultVariant":"a","targeting":{"if":[{"==":[{"var":"game_kind"},"real"]},"r1",null]}}`, ReasonRealResolutionChanged},
		{"scoped rung", `{"variants":{"a":1,"b":2,"r1":9},"defaultVariant":"a","targeting":{"if":[{"and":[{"!=":[{"var":"game_kind"},"real"]},{"==":[{"var":"experiment_id"},"e"]},{"==":[{"var":"arm"},"experimental"]}]},"r1",null]}}`, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rej := checkRenderedFlag(before, json.RawMessage(c.after), "r1")
			switch {
			case c.want == "" && rej != nil:
				t.Fatalf("unexpected reject %v", rej)
			case c.want != "" && (rej == nil || rej.Reason != c.want):
				t.Fatalf("got %v, want %s", rej, c.want)
			}
		})
	}
}

// TestResolveFlag_UnknownOperatorFailsClosed guards resolve.go: a targeting
// operator the jsonlogic engine does not implement (fractional) must ERROR, so
// the Gate rejects instead of mis-evaluating the real branch.
func TestResolveFlag_UnknownOperatorFailsClosed(t *testing.T) {
	flagRaw := json.RawMessage(`{
		"variants": {"a": 1, "b": 2},
		"defaultVariant": "a",
		"targeting": {"fractional": [{"var": "game_id"}, ["a", 50], ["b", 50]]}
	}`)
	if _, err := resolveFlag(flagRaw, map[string]any{"game_kind": "real", "game_id": "g1"}); err == nil {
		t.Fatal("resolveFlag must ERROR on an unknown flagd operator (fail-closed)")
	}
}

func TestCheckTypeCompatible_KindMatching(t *testing.T) {
	num := map[string]json.RawMessage{"a": json.RawMessage(`1.0`), "b": json.RawMessage(`2`)}
	if rej := checkTypeCompatible(num, 5); rej != nil {
		t.Fatalf("int on numeric flag rejected: %v", rej)
	}
	if rej := checkTypeCompatible(num, "x"); rej == nil || rej.Reason != ReasonTypeMismatch {
		t.Fatalf("string on numeric flag: got %v", rej)
	}
	boolean := map[string]json.RawMessage{"on": json.RawMessage(`true`), "off": json.RawMessage(`false`)}
	if rej := checkTypeCompatible(boolean, true); rej != nil {
		t.Fatalf("bool on bool flag rejected: %v", rej)
	}
	if rej := checkTypeCompatible(boolean, 3); rej == nil || rej.Reason != ReasonTypeMismatch {
		t.Fatalf("number on bool flag: got %v", rej)
	}
}

// ---- Writer -------------------------------------------------------------------

func TestWriter_ApplyRefusesUnscoped(t *testing.T) {
	w := NewWriter(newGameService(t), discardLogger())
	if _, err := w.Apply(context.Background(), Proposal{FlagKey: "nonstop.respawn_seconds", ExperimentalValue: 6.0}); !errors.Is(err, ErrUnscoped) {
		t.Fatalf("err = %v, want ErrUnscoped", err)
	}
}

func TestWriter_ConcurrentApplies(t *testing.T) {
	svc := newGameService(t)
	w := NewWriter(svc, discardLogger())
	flags := []string{"tournament.match_seconds", "fight_club.round_seconds", "nonstop.respawn_seconds"}
	values := []any{6.0, 15, 1.5}
	var wg sync.WaitGroup
	for i := 0; i < 30; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, _ = w.Apply(context.Background(), Proposal{FlagKey: flags[i%3], ExperimentID: "exp_" + string(rune('a'+i)), ExperimentalValue: values[i%3]})
		}(i)
	}
	wg.Wait()
	total := 0
	for _, f := range flags {
		total += len(svc.ArmValues(GameFlagSet, f))
	}
	if total != 30 {
		t.Fatalf("stored %d arm values, want 30", total)
	}
}

// Strip restores the pre-experiment resolution, leaves other experiments and the
// baseline targeting alone, and is idempotent; an unknown flag is a no-op.
// UC: UC-053 A3
func TestWriter_Strip(t *testing.T) {
	t.Run("UC-053/A3 a teardown strip clears that experiment's arm value and nothing else", func(t *testing.T) {
		svc := newGameService(t)
		w := NewWriter(svc, discardLogger())
		g := NewGate(w, nil, discardLogger())
		tw := NewGateTargetingWriter(g, w)
		ctx := context.Background()
		const flagKey = "sensitivity"
		if err := tw.Apply(ctx, Proposal{FlagKey: flagKey, ExperimentID: "exp_a", ExperimentalValue: 0}); err != nil {
			t.Fatal(err)
		}
		if err := tw.Apply(ctx, Proposal{FlagKey: flagKey, ExperimentID: "exp_b", ExperimentalValue: 4}); err != nil {
			t.Fatal(err)
		}
		for i := 0; i < 2; i++ {
			if err := tw.Strip(ctx, flagKey, "exp_a"); err != nil {
				t.Fatalf("strip %d: %v", i, err)
			}
		}
		if v, _ := resolve(t, svc, flagKey, experimentalCtx("exp_a")); v != "medium" {
			t.Fatalf("stripped experiment still resolves %s", v)
		}
		if _, val := resolve(t, svc, flagKey, experimentalCtx("exp_b")); val != 4.0 {
			t.Fatalf("other experiment lost: %v", val)
		}
		if v, _ := resolve(t, svc, flagKey, map[string]any{GameKindVar: GameKindReal, "game_mode": "Werewolf"}); v != "fast" {
			t.Fatalf("baseline targeting lost: %s", v)
		}
		if err := w.Strip(ctx, "flag_that_does_not_exist", "exp_z"); err != nil {
			t.Fatalf("strip of an unknown flag should be a no-op: %v", err)
		}
	})
}

// The registry seam's Apply is gated: a mistyped value writes nothing.
func TestGateTargetingWriter_ApplyIsGated(t *testing.T) {
	svc := newGameService(t)
	w := NewWriter(svc, discardLogger())
	tw := NewGateTargetingWriter(NewGate(w, nil, discardLogger()), w)
	err := tw.Apply(context.Background(), Proposal{FlagKey: "nonstop.respawn_seconds", ExperimentID: "exp_f", ExperimentalValue: "not-a-number"})
	if rej, ok := AsReject(err); !ok || rej.Reason != ReasonTypeMismatch {
		t.Fatalf("expected ReasonTypeMismatch, got %v", err)
	}
	assertNoWrites(t, svc)
}

// ---- helpers ------------------------------------------------------------------

// failingService fails every read.
type failingService struct{}

func (failingService) Put(context.Context, string, string, flagservice.Write) (flagservice.Result, error) {
	return flagservice.Result{}, errors.New("down")
}
func (failingService) Delete(context.Context, string, string, url.Values) error {
	return errors.New("down")
}
func (failingService) GetFlag(context.Context, string, string) (flagservice.FlagView, error) {
	return flagservice.FlagView{}, errors.New("down")
}
func (failingService) GetSet(context.Context, string) (flagservice.SetView, error) {
	return flagservice.SetView{}, errors.New("down")
}

// leakingService renders an accepted write with a changed defaultVariant.
// A write by someone else between the Gate's read and its write is no reason to
// reject: the dry run renders before and after under one lock (review B5).
func TestReview_ConcurrentWriteIsNoFalseRejection(t *testing.T) {
	svc := newGameService(t)
	racing := &racingService{Service: svc}
	g := NewGate(NewWriter(racing, discardLogger()), nil, discardLogger())
	if err := g.Review(context.Background(), Proposal{FlagKey: "nonstop.respawn_seconds", ExperimentID: "exp_x", ExperimentalValue: 6.0}); err != nil {
		t.Fatalf("rejected because of another writer: %v", err)
	}
}

// racingService lands another experiment's write right after every GetFlag.
type racingService struct {
	*fake.Service
	n int
}

func (r *racingService) GetFlag(ctx context.Context, set, flag string) (flagservice.FlagView, error) {
	v, err := r.Service.GetFlag(ctx, set, flag)
	r.n++
	_, _ = r.Service.Put(ctx, set, flag, flagservice.Write{Value: 3.0, Scope: flagservice.Scope{ExperimentID: fmt.Sprintf("exp_other%d", r.n), Arm: ArmExperimental}})
	return v, err
}

// leakingService renders every dry run as a write that moves the default (what a
// broken renderer would serve to real games).
type leakingService struct {
	*fake.Service
}

func (l *leakingService) Put(ctx context.Context, set, flag string, w flagservice.Write) (flagservice.Result, error) {
	res, err := l.Service.Put(ctx, set, flag, w)
	if err != nil || !w.DryRun {
		return res, err
	}
	var doc map[string]any
	_ = json.Unmarshal(res.Rendered, &doc)
	doc["defaultVariant"] = "slow"
	res.Rendered, _ = json.Marshal(doc)
	return res, nil
}

func assertNoWrites(t *testing.T, svc *fake.Service) {
	t.Helper()
	for _, c := range svc.Calls {
		if strings.HasPrefix(c, "PUT") {
			t.Fatalf("unexpected write: %v", svc.Calls)
		}
	}
}

func assertBlocked(t *testing.T, err error, want Reason) {
	t.Helper()
	rej, ok := AsReject(err)
	if !ok {
		t.Fatalf("expected RejectError, got %v", err)
	}
	if rej.Reason != want {
		t.Fatalf("reason = %q, want %q (%s)", rej.Reason, want, rej.Detail)
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
