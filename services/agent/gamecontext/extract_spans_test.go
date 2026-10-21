package gamecontext

import (
	"testing"
	"time"

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

// UC: UC-041 main
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

// UC: UC-041 main BR-001
func TestApplySpans_DeathEventAppendsElimination(t *testing.T) {
	s := newTestStore()
	td := spanWith(map[string]string{"player.serial": "A"}, []string{"player_death"})
	s.ApplySpans(td)
	if seq := s.Snapshot().Session.EliminationSequence; len(seq) != 1 || seq[0] != "A" {
		t.Fatalf("elimination seq = %v, want [A]", seq)
	}
}

// UC: UC-041 BR-001
func TestApplySpans_DedupeAgainstMetricPath(t *testing.T) {
	s := newTestStore()
	// Metric-path elimination first.
	s.ApplyMetrics(metricsWith(metricEliminationOrder, 1, serial("A")))
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

// shieldChargeSpan is the coordinator's game.shield_charge span at its end (UC-040
// A32: outcome death), which reaches the agent after the player died.
func shieldChargeSpan(serial, gameID string) ptrace.Traces {
	td := spanWith(map[string]string{
		"player.serial":         serial,
		"game.id":               gameID,
		"game.mode":             "FFA",
		"shield.charge_outcome": "death",
	}, nil)
	td.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).SetName("game.shield_charge")
	return td
}

// UC: UC-041 main
// UC: UC-040 A32
func TestApplySpans_ShieldChargeSpanIsIgnored(t *testing.T) {
	t.Run("UC-040/A32 a charge span never creates a player or adopts its game", func(t *testing.T) {
		s := newTestStore()
		if s.ApplySpans(shieldChargeSpan("DEAD", "game-9")) {
			t.Fatal("game.shield_charge reported as recognized")
		}
		snap := s.Snapshot()
		if snap.Players["DEAD"] != nil {
			t.Fatal("game.shield_charge re-created the player")
		}
		if snap.SessionID != "" || snap.Session.GameMode != nil {
			t.Fatalf("game.shield_charge set identity: session %q mode %v", snap.SessionID, snap.Session.GameMode)
		}
	})

	t.Run("UC-040/A32 a charge span never refreshes a player's last update", func(t *testing.T) {
		now := time.Unix(1000, 0)
		s := NewStore(time.Hour, time.Hour, func() time.Time { return now })
		s.ApplySpans(spanWith(map[string]string{"player.serial": "DEAD", "game.id": "game-9"}, nil))
		before := s.Snapshot().Players["DEAD"].LastUpdate
		now = now.Add(30 * time.Second)
		s.ApplySpans(shieldChargeSpan("DEAD", "game-9"))
		if got := s.Snapshot().Players["DEAD"].LastUpdate; !got.Equal(before) {
			t.Fatalf("LastUpdate moved to %s, want %s (the span must not keep a dead player fresh)", got, before)
		}
	})

	t.Run("UC-041/main a charge span never creates a partition", func(t *testing.T) {
		m := NewMultiplexer(func(string) *Store { return newTestStore() })
		if touched := m.ApplySpans(shieldChargeSpan("DEAD", "game-evicted")); len(touched) != 0 {
			t.Fatalf("touched partitions = %v, want none", touched)
		}
		if parts := m.Partitions(); len(parts) != 0 {
			t.Fatalf("partitions = %v, want none", parts)
		}
	})
}
