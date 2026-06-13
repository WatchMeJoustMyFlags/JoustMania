package gamecontext

import (
	"testing"

	"go.opentelemetry.io/collector/pdata/ptrace"
)

// spanWith builds a ptrace.Traces with one span, given attrs and event names.
func spanWith(attrs map[string]string, events []string) ptrace.Traces {
	td := ptrace.NewTraces()
	span := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	span.SetName("player_lifecycle")
	for k, v := range attrs {
		span.Attributes().PutStr(k, v)
	}
	for _, name := range events {
		span.Events().AppendEmpty().SetName(name)
	}
	return td
}

func TestApplySpans_Enrichment(t *testing.T) {
	s := newTestStore()
	td := spanWith(map[string]string{
		"player.serial": "A",
		"game.mode":     "joust",
		"game.id":       "game-7",
	}, nil)
	if !s.ApplySpans(td) {
		t.Fatal("expected span to be recognized")
	}
	snap := s.Snapshot()
	if snap.Players["A"] == nil {
		t.Fatal("span should enrich player identity")
	}
	if snap.Session.GameMode == nil || *snap.Session.GameMode != "joust" {
		t.Fatalf("game mode = %v, want joust", snap.Session.GameMode)
	}
	if snap.SessionID != "game-7" {
		t.Fatalf("SessionID = %q, want game-7", snap.SessionID)
	}
}

// TestApplySpans_AdoptsGameKind verifies the player_lifecycle / game-session span
// now carries game.kind (#845) and the store records it alongside game.id.
func TestApplySpans_AdoptsGameKind(t *testing.T) {
	s := newTestStore()
	td := spanWith(map[string]string{
		"player.serial": "A",
		"game.id":       "game-9",
		"game.kind":     "real",
	}, nil)
	if !s.ApplySpans(td) {
		t.Fatal("expected span to be recognized")
	}
	snap := s.Snapshot()
	if snap.SessionID != "game-9" {
		t.Fatalf("SessionID = %q, want game-9", snap.SessionID)
	}
	if snap.GameKind != "real" {
		t.Fatalf("GameKind = %q, want real (adopted from game.kind span attr)", snap.GameKind)
	}
}

// TestApplySpans_GameKindAttrAloneIsRecognized verifies a span carrying only
// game.kind (no serial/mode/id) is still recognized and records the kind.
func TestApplySpans_GameKindAttrAloneIsRecognized(t *testing.T) {
	s := newTestStore()
	if !s.ApplySpans(spanWith(map[string]string{"game.kind": "shadow"}, nil)) {
		t.Fatal("span with only game.kind should be recognized")
	}
	if k := s.Snapshot().GameKind; k != "shadow" {
		t.Fatalf("GameKind = %q, want shadow", k)
	}
}

func TestApplySpans_DeathEventAppendsElimination(t *testing.T) {
	s := newTestStore()
	td := spanWith(map[string]string{"player.serial": "A"}, []string{"player_death"})
	s.ApplySpans(td)
	if seq := s.Snapshot().Session.EliminationSequence; len(seq) != 1 || seq[0] != "A" {
		t.Fatalf("elimination seq = %v, want [A]", seq)
	}
}

func TestApplySpans_DedupeAgainstMetricPath(t *testing.T) {
	s := newTestStore()
	// Metric-path elimination first.
	s.ApplyMetrics(metricsWith(metricDeathsTotal, 0, serial("A")))
	s.ApplyMetrics(metricsWith(metricDeathsTotal, 1, serial("A")))
	// Span confirmation for same serial must not double-append.
	s.ApplySpans(spanWith(map[string]string{"player.serial": "A"}, []string{"player_death"}))
	if seq := s.Snapshot().Session.EliminationSequence; len(seq) != 1 {
		t.Fatalf("elimination seq = %v, want 1 (deduped)", seq)
	}
}

func TestApplySpans_Unrecognized(t *testing.T) {
	s := newTestStore()
	if s.ApplySpans(spanWith(nil, nil)) {
		t.Fatal("span with no recognized attrs/events should return false")
	}
}

func TestApplySpans_SkipsOwnService(t *testing.T) {
	s := newTestStore()
	s.SetOwnService("agent")

	td := spanWith(map[string]string{"player.serial": "A"}, []string{"player_death"})
	td.ResourceSpans().At(0).Resource().Attributes().PutStr("service.name", "agent")
	if s.ApplySpans(td) {
		t.Fatal("own-service spans must be skipped (self-ingestion loop defense)")
	}
	if s.Snapshot().Players["A"] != nil {
		t.Fatal("own-service span must not create players")
	}

	// Other services still apply.
	td = spanWith(map[string]string{"player.serial": "A"}, nil)
	td.ResourceSpans().At(0).Resource().Attributes().PutStr("service.name", "game-coordinator")
	if !s.ApplySpans(td) {
		t.Fatal("non-own service spans must still apply")
	}
}

// TestApplySpans_AdoptsExperimentAttribution verifies the game span's
// experiment.id / experiment.arm attributes (the PRIMARY attribution channel,
// #975) enrich the GameContext snapshot.
func TestApplySpans_AdoptsExperimentAttribution(t *testing.T) {
	s := newTestStore()
	td := spanWith(map[string]string{
		"game.id":        "game-7",
		"game.kind":      "shadow",
		"experiment.id":  "exp_abc123",
		"experiment.arm": "experimental",
	}, nil)
	if !s.ApplySpans(td) {
		t.Fatal("expected span to be recognized")
	}
	snap := s.Snapshot()
	if snap.ExperimentID != "exp_abc123" {
		t.Fatalf("ExperimentID = %q, want exp_abc123 (from experiment.id span attr)", snap.ExperimentID)
	}
	if snap.Arm != "experimental" {
		t.Fatalf("Arm = %q, want experimental (from experiment.arm span attr)", snap.Arm)
	}
}

// TestApplySpans_NoExperimentAttrsLeavesAttributionEmpty verifies a span without
// experiment attributes leaves attribution empty (an unlabeled signal is a
// no-op for experiment id / arm, #975).
func TestApplySpans_NoExperimentAttrsLeavesAttributionEmpty(t *testing.T) {
	s := newTestStore()
	td := spanWith(map[string]string{
		"game.id":   "game-7",
		"game.kind": "real",
	}, nil)
	s.ApplySpans(td)
	snap := s.Snapshot()
	if snap.ExperimentID != "" {
		t.Fatalf("ExperimentID = %q, want empty", snap.ExperimentID)
	}
	if snap.Arm != "" {
		t.Fatalf("Arm = %q, want empty", snap.Arm)
	}
}
