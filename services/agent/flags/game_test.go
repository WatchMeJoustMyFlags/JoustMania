package flags

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"math"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
)

// game_test.go covers the agent's read-only game domain (UC-040 A31 step 1, #1837):
// ShieldCharge mirrors the coordinator's resolve_shield_activation /
// resolve_charge_expiry, evaluates with the coordinator's context, fails open, and
// warns at most once per 30 s when the game set cannot be read.

// ctxRecorder wraps a stub evaluator and records each evaluation context.
type ctxRecorder struct {
	stubEvaluator
	mu   sync.Mutex
	seen []openfeature.EvaluationContext
}

func (r *ctxRecorder) record(c openfeature.EvaluationContext) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.seen = append(r.seen, c)
}

func (r *ctxRecorder) StringValue(ctx context.Context, flag, def string, ec openfeature.EvaluationContext, o ...openfeature.Option) (string, error) {
	r.record(ec)
	return r.stubEvaluator.StringValue(ctx, flag, def, ec, o...)
}

func (r *ctxRecorder) FloatValue(ctx context.Context, flag string, def float64, ec openfeature.EvaluationContext, o ...openfeature.Option) (float64, error) {
	r.record(ec)
	return r.stubEvaluator.FloatValue(ctx, flag, def, ec, o...)
}

func gameFlags(game Evaluator, log *slog.Logger) *Flags {
	f := New(stubEvaluator{strings: map[string]string{"shield.activation": "trigger"}}, log)
	f.game = game
	return f
}

// UC: UC-040 A31 BR-019
func TestShieldCharge_Resolution(t *testing.T) {
	for _, tc := range []struct {
		name        string
		mode        string
		expiry      *float64
		errs        map[string]error
		wantTrigger bool
		wantExpiry  time.Duration
	}{
		{"UC-040/A31 trigger with the default expiry", "trigger", nil, nil, true, 20 * time.Second},
		{"UC-040/A31 instant", "instant", nil, nil, false, 20 * time.Second},
		{"UC-040/A31 trigger trimmed and lower-cased", " TRIGGER ", nil, nil, true, 20 * time.Second},
		{"UC-040/A31 a malformed mode is instant", "garbage", nil, nil, false, 20 * time.Second},
		{"UC-040/A31 expiry 10", "trigger", fp(10), nil, true, 10 * time.Second},
		{"UC-040/A31 expiry 0 means never", "trigger", fp(0), nil, true, 0},
		{"UC-040/A31 NaN expiry gives 20", "trigger", fp(math.NaN()), nil, true, 20 * time.Second},
		{"UC-040/A31 negative expiry gives 20", "trigger", fp(-1), nil, true, 20 * time.Second},
		{"UC-040/A31 expiry clamped to 120", "trigger", fp(200), nil, true, 120 * time.Second},
		{"UC-040/A31 unreadable mode fails open", "trigger", nil, map[string]error{"shield.activation": errors.New("PROVIDER_NOT_READY")}, false, 20 * time.Second},
		{"UC-040/A31 unreadable expiry gives 20", "trigger", nil, map[string]error{"shield.charge_expiry_seconds": errors.New("TYPE_MISMATCH")}, true, 20 * time.Second},
	} {
		t.Run(tc.name, func(t *testing.T) {
			stub := stubEvaluator{strings: map[string]string{"shield.activation": tc.mode}, floats: map[string]float64{}, errs: tc.errs}
			if tc.expiry != nil {
				stub.floats["shield.charge_expiry_seconds"] = *tc.expiry
			}
			trigger, expiry := gameFlags(stub, nil).ShieldCharge(context.Background(), "g-1")
			if trigger != tc.wantTrigger || expiry != tc.wantExpiry {
				t.Fatalf("ShieldCharge = (%v, %s), want (%v, %s)", trigger, expiry, tc.wantTrigger, tc.wantExpiry)
			}
		})
	}

	t.Run("UC-040/A31 a nil game client returns the defaults, never a read through the agent client", func(t *testing.T) {
		f := gameFlags(nil, nil) // the agent client WOULD answer trigger
		if trigger, expiry := f.ShieldCharge(context.Background(), "g-1"); trigger || expiry != 20*time.Second {
			t.Fatalf("ShieldCharge = (%v, %s), want (false, 20s)", trigger, expiry)
		}
		var nilFlags *Flags
		if trigger, _ := nilFlags.ShieldCharge(context.Background(), "g-1"); trigger {
			t.Fatal("a nil *Flags read trigger")
		}
	})
}

func fp(v float64) *float64 { return &v }

// UC: UC-040 A31
func TestShieldCharge_CoordinatorContext(t *testing.T) {
	// The coordinator's grant read carries the game's context too (#1970); the shield
	// flags' targeting may use only gameId and game_kind, so this subset resolves the same.
	t.Run("UC-040/A31 both reads use game_kind=real and gameId, no targetingKey or game_mode", func(t *testing.T) {
		rec := &ctxRecorder{stubEvaluator: stubEvaluator{strings: map[string]string{"shield.activation": "trigger"}}}
		_, _ = gameFlags(rec, nil).ShieldCharge(context.Background(), "g-42")
		if len(rec.seen) != 2 {
			t.Fatalf("evaluations = %d, want 2 (activation, expiry)", len(rec.seen))
		}
		for i, ec := range rec.seen {
			attrs := ec.Attributes()
			if ec.TargetingKey() != "" || len(attrs) != 2 || attrs["game_kind"] != "real" || attrs["gameId"] != "g-42" {
				t.Fatalf("evaluation %d context = key %q attrs %v, want game_kind=real and gameId only", i, ec.TargetingKey(), attrs)
			}
		}
	})
}

// UC: UC-043 A17
func TestShieldCharge_ExperimentArmContext(t *testing.T) {
	// #2096: shield.activation has an experiment rung since FR-162, which matches only
	// game_kind != real with experiment_id and arm; the coordinator reads it in the
	// game's flag context, so the agent's ledger must read the same arm.
	t.Run("UC-043/A17 a shadow arm game reads with its kind, experiment_id and arm", func(t *testing.T) {
		rec := &ctxRecorder{stubEvaluator: stubEvaluator{strings: map[string]string{"shield.activation": "trigger"}}}
		f := gameFlags(rec, nil)
		f.SetGameAttrs(func(gameID string) GameAttrs {
			if gameID != "g-7" {
				t.Fatalf("lookup for %q", gameID)
			}
			return GameAttrs{GameKind: "shadow", ExperimentID: "exp_1", Arm: "experimental"}
		})
		_, _ = f.ShieldCharge(context.Background(), "g-7")
		f.LatchShieldMode(context.Background(), "g-7")
		if len(rec.seen) != 3 {
			t.Fatalf("evaluations = %d, want 3", len(rec.seen))
		}
		for i, ec := range rec.seen {
			attrs := ec.Attributes()
			if len(attrs) != 4 || attrs["game_kind"] != "shadow" || attrs["experiment_id"] != "exp_1" ||
				attrs["arm"] != "experimental" || attrs["gameId"] != "g-7" {
				t.Fatalf("evaluation %d attrs %v, want shadow arm context", i, attrs)
			}
		}
	})
	t.Run("UC-043/A17 an unseen kind reads as real", func(t *testing.T) {
		rec := &ctxRecorder{stubEvaluator: stubEvaluator{strings: map[string]string{"shield.activation": "trigger"}}}
		f := gameFlags(rec, nil)
		f.SetGameAttrs(func(string) GameAttrs { return GameAttrs{} })
		_, _ = f.ShieldCharge(context.Background(), "g-8")
		if attrs := rec.seen[0].Attributes(); len(attrs) != 2 || attrs["game_kind"] != "real" {
			t.Fatalf("attrs %v, want game_kind=real and gameId", attrs)
		}
	})
}

// UC: UC-040 A31
func TestShieldCharge_UnreadableWarnsThrottled(t *testing.T) {
	t.Run("UC-040/A31 an unready game set warns once per 30 s, not on every check", func(t *testing.T) {
		var buf bytes.Buffer
		log := slog.New(slog.NewTextHandler(&buf, nil))
		down := stubEvaluator{errs: map[string]error{"shield.activation": errors.New("PROVIDER_NOT_READY")}}
		f := gameFlags(down, log)
		for i := 0; i < 5; i++ {
			_, _ = f.ShieldCharge(context.Background(), "g-1")
		}
		if n := strings.Count(buf.String(), "agent.shield_charge_mode_unreadable"); n != 1 {
			t.Fatalf("unreadable warnings = %d, want 1:\n%s", n, buf.String())
		}
		if !strings.Contains(buf.String(), "level=WARN") {
			t.Fatalf("the warning is not at WARN level:\n%s", buf.String())
		}
		f.gameWarn.last = time.Now().Add(-31 * time.Second) // the window passed
		_, _ = f.ShieldCharge(context.Background(), "g-1")
		if n := strings.Count(buf.String(), "agent.shield_charge_mode_unreadable"); n != 2 {
			t.Fatalf("unreadable warnings after 30 s = %d, want 2", n)
		}
	})
}

// UC: UC-040 A31
// UC: UC-045 A3
func TestSetupFlagd_GameDomain(t *testing.T) {
	t.Run("UC-040/A31 the game set is its own domain: default before its sync, served after", func(t *testing.T) {
		agentSrc, rolloutSrc, gameSrc := newFakeFlagdSource("on"), newFakeFlagdSource("on"), newFakeFlagdSource("on")
		agentSrc.set(true, "on")
		rolloutSrc.set(true, "on")
		ctx, cancel := context.WithCancel(context.Background())
		f, _, shutdown, err := setupFlagd(ctx, fakeFactory(agentSrc), fakeFactory(rolloutSrc), fakeFactory(gameSrc), nil, testSyncTiming, nil)
		if err != nil {
			t.Fatalf("setupFlagd: %v", err)
		}
		t.Cleanup(func() { cancel(); shutdown() })
		read := func() bool {
			trigger, _ := f.ShieldCharge(context.Background(), "g-1")
			return trigger
		}
		// The agent domain syncs, the game domain does not yet: the game read fails open.
		waitBool(t, "enabled (agent domain)", func() bool { return f.Evaluate(context.Background()).Enabled }, true, 5*time.Second)
		if read() {
			t.Fatal("shield.activation read trigger before the game domain synced")
		}
		gameSrc.set(true, "on")
		waitBool(t, "shield.activation (game domain)", read, true, 5*time.Second)
		if _, expiry := f.ShieldCharge(context.Background(), "g-1"); expiry != 10*time.Second {
			t.Fatalf("expiry = %s, want 10s from the game set", expiry)
		}
		gameSrc.set(true, "off")
		waitBool(t, "shield.activation flipped to instant", read, false, 5*time.Second)
	})
}
