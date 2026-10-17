package gamecontext

import "testing"

// UC: UC-041 main
// UC: UC-035 BR-002
func TestNormalizeGameKind(t *testing.T) {
	cases := map[string]string{
		"primary": "real",   // coordinator telemetry label for a menu game
		"real":    "real",   // already flag vocabulary
		"shadow":  "shadow", // same in both vocabularies
		"":        "",       // unknown stays unknown (never assumed real)
		"weird":   "weird",  // unrecognised passes through (fails closed downstream)
	}
	for in, want := range cases {
		if got := NormalizeGameKind(in); got != want {
			t.Errorf("NormalizeGameKind(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestApplyMetrics_PrimaryGameKindNormalisedToReal: the coordinator labels a menu
// game game_kind="primary"; the agent must record it as "real" (#1249).
// UC: UC-041 main
// UC: UC-035 BR-002
func TestApplyMetrics_PrimaryGameKindNormalisedToReal(t *testing.T) {
	s := newTestStore()
	s.ApplyMetrics(metricsWith(metricGameActive, 1, map[string]string{
		attrGameID:   "game_primary",
		attrGameKind: "primary",
	}))
	if k := s.Snapshot().GameKind; k != GameKindReal {
		t.Fatalf("GameKind = %q, want real (normalised from telemetry primary)", k)
	}
}

// TestApplySpans_PrimaryGameKindNormalisedToReal: same for the game.kind span attr.
// UC: UC-041 main
func TestApplySpans_PrimaryGameKindNormalisedToReal(t *testing.T) {
	s := newTestStore()
	if !s.ApplySpans(spanWith(map[string]string{"game.id": "game_p", "game.kind": "primary"}, nil)) {
		t.Fatal("expected span to be recognized")
	}
	if k := s.Snapshot().GameKind; k != GameKindReal {
		t.Fatalf("GameKind = %q, want real (normalised from telemetry primary)", k)
	}
}

// TestApplyMetrics_UnlabeledSignalDoesNotMakeShadowReal: the empty-kind fail-safe.
// An unlabeled signal (no game_kind) must neither set "real" on a fresh store nor
// clobber an observed "shadow".
func TestApplyMetrics_UnlabeledSignalDoesNotMakeShadowReal(t *testing.T) {
	s := newTestStore()
	s.ApplyMetrics(metricsWith(metricGameActive, 1, map[string]string{attrGameID: "game_s"}))
	if k := s.Snapshot().GameKind; k != "" {
		t.Fatalf("GameKind = %q after unlabeled signal, want empty (unknown)", k)
	}
	s.ApplyMetrics(metricsWith(metricGameActive, 1, map[string]string{attrGameID: "game_s", attrGameKind: "shadow"}))
	s.ApplyMetrics(metricsWith(metricGameActive, 1, map[string]string{attrGameID: "game_s"}))
	if k := s.Snapshot().GameKind; k != GameKindShadow {
		t.Fatalf("GameKind = %q, want shadow (unlabeled signal must not clobber)", k)
	}
}
