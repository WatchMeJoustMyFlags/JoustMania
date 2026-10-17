package promote

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/joustmania/agent/flagservice/fake"
)

// realdefault_test.go exercises the autonomous REAL-default writer against an
// in-memory fake of the flag service (no network): the separate,
// higher-privilege write the safety rail isolates from the shadow Writer. A
// promotion is PUT /game/{flag} with scope.game_kind=real; a revert is
// DELETE ?game_kind=real (W5, epic #1407).

const promotedFlag = "death_grace_period_seconds"

func newPromotionService(t *testing.T) *fake.Service {
	t.Helper()
	path := filepath.Join(t.TempDir(), "game.json")
	doc := `{"flags":{"` + promotedFlag + `":{"state":"ENABLED","variants":{"default":2.0,"long":3.0},"defaultVariant":"default"}}}`
	if err := os.WriteFile(path, []byte(doc), 0o644); err != nil {
		t.Fatal(err)
	}
	svc := fake.New()
	if err := svc.LoadSet("game", path, map[string][]string{promotedFlag: {"experiment+arm", "real"}}); err != nil {
		t.Fatal(err)
	}
	return svc
}

func writerForTest(t *testing.T, svc *fake.Service, storeDir string) *serviceRealDefaultWriter {
	t.Helper()
	snaps, err := newSnapStore(storeDir)
	if err != nil {
		t.Fatalf("snap store: %v", err)
	}
	return newServiceRealDefaultWriter(svc, snaps, discardLogger())
}

// effective returns the value real games resolve (the effective default).
func effective(t *testing.T, svc *fake.Service) string {
	t.Helper()
	v, err := svc.GetFlag(context.Background(), "game", promotedFlag)
	if err != nil {
		t.Fatal(err)
	}
	if raw, ok := svc.RealValue("game", promotedFlag); ok && v.EffectiveDefault == v.RealOverlay.Variant {
		return string(raw)
	}
	return string(v.VariantValues[v.EffectiveDefault])
}

func TestRealDefaultWriterPromotesThroughTheRealScope(t *testing.T) {
	svc := newPromotionService(t)
	w := writerForTest(t, svc, t.TempDir())
	if err := w.SetRealDefault(context.Background(), promotedFlag, 3.0); err != nil {
		t.Fatalf("SetRealDefault: %v", err)
	}
	if got := effective(t, svc); got != "3" {
		t.Errorf("real default resolves %s, want 3", got)
	}
	if svc.Calls[0] != "PUT game/"+promotedFlag+" real" {
		t.Errorf("write = %v, want a real-scoped PUT", svc.Calls)
	}
}

// Defence in depth (#936): a value of another JSON kind is refused before any write.
func TestRealDefaultWriterRejectsTypeMismatch(t *testing.T) {
	svc := newPromotionService(t)
	w := writerForTest(t, svc, t.TempDir())
	if err := w.SetRealDefault(context.Background(), promotedFlag, "banana"); err == nil {
		t.Fatal("SetRealDefault accepted a string value on a numeric flag")
	}
	if len(svc.Calls) != 0 {
		t.Errorf("writes despite the type mismatch: %v", svc.Calls)
	}
	if err := w.SetRealDefault(context.Background(), promotedFlag, 3); err != nil {
		t.Errorf("same-kind numeric value rejected: %v", err)
	}
}

// The registry bounds the promoted value: a refused value is an error, nothing stored.
func TestRealDefaultWriterSurfacesARegistryRefusal(t *testing.T) {
	svc := newPromotionService(t)
	w := writerForTest(t, svc, t.TempDir())
	if err := w.SetRealDefault(context.Background(), promotedFlag, 99.0); err == nil || !strings.Contains(err.Error(), "out_of_bounds") {
		t.Fatalf("err = %v, want out_of_bounds", err)
	}
	if _, ok := svc.RealValue("game", promotedFlag); ok {
		t.Error("refused value was stored")
	}
}

func TestRealDefaultWriterRequiresBothOptIns(t *testing.T) {
	t.Setenv("AGENT_CODE_IMPROVEMENT_ENABLED", "true")
	t.Setenv("AGENT_AUTONOMOUS_ENABLED", "")
	if w, err := NewRealDefaultWriterFromEnv(discardLogger()); w != nil || err != nil {
		t.Errorf("writer built without AGENT_AUTONOMOUS_ENABLED: (%v, %v)", w, err)
	}
	t.Setenv("AGENT_CODE_IMPROVEMENT_ENABLED", "")
	t.Setenv("AGENT_AUTONOMOUS_ENABLED", "true")
	if w, err := NewRealDefaultWriterFromEnv(discardLogger()); w != nil || err != nil {
		t.Errorf("writer built without AGENT_CODE_IMPROVEMENT_ENABLED: (%v, %v)", w, err)
	}
	t.Setenv("AGENT_CODE_IMPROVEMENT_ENABLED", "true")
	t.Setenv("AGENT_EXPERIMENT_DIR", t.TempDir())
	if w, err := NewRealDefaultWriterFromEnv(discardLogger()); w == nil || err != nil {
		t.Errorf("writer not built with both opt-ins: (%v, %v)", w, err)
	}
}

// #1065: a revert restores exactly the pre-promotion state (the overlay is
// removed; the baseline is untouched), and re-promotion then revert still lands
// on the baseline.
func TestRevertRestoresThePrePromotionState(t *testing.T) {
	svc := newPromotionService(t)
	w := writerForTest(t, svc, t.TempDir())
	ctx := context.Background()
	if err := w.SetRealDefault(ctx, promotedFlag, 3.0); err != nil {
		t.Fatal(err)
	}
	if err := w.SetRealDefault(ctx, promotedFlag, 2.0); err != nil {
		t.Fatal(err)
	}
	if err := w.RevertRealDefault(ctx, promotedFlag); err != nil {
		t.Fatalf("RevertRealDefault: %v", err)
	}
	if got := effective(t, svc); got != "2.0" {
		t.Errorf("after revert real resolves %s, want the baseline 2.0", got)
	}
	last := svc.Calls[len(svc.Calls)-1]
	if !strings.HasPrefix(last, "DELETE game/"+promotedFlag) || !strings.Contains(last, "game_kind=real") {
		t.Errorf("revert call = %s", last)
	}
}

// A revert with no promotion overlay is refused: it never fabricates a prior state.
func TestRevertWithoutPromotionIsRefused(t *testing.T) {
	svc := newPromotionService(t)
	w := writerForTest(t, svc, t.TempDir())
	if err := w.RevertRealDefault(context.Background(), promotedFlag); err == nil {
		t.Fatal("revert without a promotion overlay must be refused")
	}
	if len(svc.Calls) != 0 {
		t.Errorf("unexpected writes: %v", svc.Calls)
	}
}

// #1076 item 3: the post-revert cooldown stamp survives a restart (a new writer
// over the same store directory).
func TestRevertCooldownSurvivesRestart(t *testing.T) {
	svc := newPromotionService(t)
	storeDir := t.TempDir()
	ctx := context.Background()
	w1 := writerForTest(t, svc, storeDir)
	if err := w1.SetRealDefault(ctx, promotedFlag, 3.0); err != nil {
		t.Fatal(err)
	}
	// The promotion lives in the flag service, so a restarted agent can revert it.
	w2 := writerForTest(t, svc, storeDir)
	if err := w2.RevertRealDefault(ctx, promotedFlag); err != nil {
		t.Fatalf("post-restart revert: %v", err)
	}
	w3 := writerForTest(t, svc, storeDir)
	if _, ok := w3.PersistedRevertTimes()[promotedFlag]; !ok {
		t.Error("post-revert cooldown stamp did not survive restart")
	}
}

// A cooldown-stamp failure is logged, not returned: the revert itself succeeded.
func TestRevertSucceedsWhenTheCooldownStampFails(t *testing.T) {
	svc := newPromotionService(t)
	storeDir := filepath.Join(t.TempDir(), "store")
	w := writerForTest(t, svc, storeDir)
	ctx := context.Background()
	if err := w.SetRealDefault(ctx, promotedFlag, 3.0); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(storeDir, []byte("not a dir"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := w.RevertRealDefault(ctx, promotedFlag); err != nil {
		t.Fatalf("revert failed because of the cooldown stamp: %v", err)
	}
	if _, ok := svc.RealValue("game", promotedFlag); ok {
		t.Error("overlay still present after revert")
	}
}

// The SAME concrete writer backs both seams.
func TestWriterIsAlsoReverter(t *testing.T) {
	var _ RealDefaultReverter = (*serviceRealDefaultWriter)(nil)
	var _ RealDefaultWriter = (*serviceRealDefaultWriter)(nil)
}
