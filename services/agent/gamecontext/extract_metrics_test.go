package gamecontext

import (
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric"
)

// newStore makes a Store with generous timeouts and a fixed clock for tests.
func newTestStore() *Store {
	return NewStore(time.Hour, time.Hour, func() time.Time {
		return time.Unix(1000, 0)
	})
}

// metricsWith builds a pmetric.Metrics with a single gauge data point.
func metricsWith(name string, value float64, attrs map[string]string) pmetric.Metrics {
	md := pmetric.NewMetrics()
	m := md.ResourceMetrics().AppendEmpty().ScopeMetrics().AppendEmpty().Metrics().AppendEmpty()
	m.SetName(name)
	dp := m.SetEmptyGauge().DataPoints().AppendEmpty()
	dp.SetDoubleValue(value)
	for k, v := range attrs {
		dp.Attributes().PutStr(k, v)
	}
	return md
}

func serial(s string) map[string]string { return map[string]string{"serial": s} }

func TestApplyMetrics_BatteryFallbackThenPreferred(t *testing.T) {
	s := newTestStore()

	// Fallback level 4 -> 80%.
	if !s.ApplyMetrics(metricsWith(metricBatteryLevel, 4, serial("A"))) {
		t.Fatal("expected battery level update")
	}
	if got := *s.Snapshot().Players["A"].BatteryPct; got != 80 {
		t.Fatalf("battery = %v, want 80", got)
	}

	// Preferred 77 wins.
	s.ApplyMetrics(metricsWith(metricBatteryPct, 77, serial("A")))
	if got := *s.Snapshot().Players["A"].BatteryPct; got != 77 {
		t.Fatalf("battery = %v, want 77", got)
	}

	// Later fallback is ignored.
	s.ApplyMetrics(metricsWith(metricBatteryLevel, 1, serial("A")))
	if got := *s.Snapshot().Players["A"].BatteryPct; got != 77 {
		t.Fatalf("battery = %v, want 77 (fallback ignored)", got)
	}
}

func TestApplyMetrics_SkillPlaystyleThenPreferred(t *testing.T) {
	s := newTestStore()

	s.ApplyMetrics(metricsWith(metricPlaystyle, 3, serial("A")))
	if got := *s.Snapshot().Players["A"].SkillLevel; got != 1.0 {
		t.Fatalf("skill = %v, want 1.0", got)
	}

	s.ApplyMetrics(metricsWith(metricSkillLevel, 0.42, serial("A")))
	if got := *s.Snapshot().Players["A"].SkillLevel; got != 0.42 {
		t.Fatalf("skill = %v, want 0.42", got)
	}

	s.ApplyMetrics(metricsWith(metricPlaystyle, 0, serial("A")))
	if got := *s.Snapshot().Players["A"].SkillLevel; got != 0.42 {
		t.Fatalf("skill = %v, want 0.42 (fallback ignored)", got)
	}
}

func TestApplyMetrics_Alive(t *testing.T) {
	s := newTestStore()
	s.ApplyMetrics(metricsWith(metricGameAlive, 1, serial("A")))
	p := s.Snapshot().Players["A"]
	if p == nil || p.Active == nil || !*p.Active {
		t.Fatalf("expected player A active")
	}
}

func TestApplyMetrics_ConnectedFalseDoesNotCreate(t *testing.T) {
	s := newTestStore()

	// connected=false for an unknown player must not create it.
	s.ApplyMetrics(metricsWith(metricConnected, 0, serial("ghost")))
	if _, ok := s.Snapshot().Players["ghost"]; ok {
		t.Fatal("connected=false should not create a player")
	}

	// connected=true creates; connected=false then updates the existing one.
	s.ApplyMetrics(metricsWith(metricConnected, 1, serial("A")))
	if s.Snapshot().Players["A"] == nil {
		t.Fatal("connected=true should create player A")
	}
	s.ApplyMetrics(metricsWith(metricConnected, 0, serial("A")))
	p := s.Snapshot().Players["A"]
	if p == nil || p.Active == nil || *p.Active {
		t.Fatal("connected=false should update existing player A to inactive")
	}
}

func TestApplyMetrics_UnknownMetric(t *testing.T) {
	s := newTestStore()
	if s.ApplyMetrics(metricsWith("some_unknown_metric", 1, serial("A"))) {
		t.Fatal("unknown metric should not report an update")
	}
	if len(s.Snapshot().Players) != 0 {
		t.Fatal("unknown metric should not create players")
	}
}

func TestApplyMetrics_GameActiveSessionTransition(t *testing.T) {
	s := newTestStore()

	s.ApplyMetrics(metricsWith(metricGameActive, 1, nil))
	first := s.Snapshot().SessionID
	if first != "session-1" {
		t.Fatalf("SessionID = %q, want session-1", first)
	}

	// Record an elimination via deaths fallback (baseline then increment).
	s.ApplyMetrics(metricsWith(metricDeathsTotal, 0, serial("A")))
	s.ApplyMetrics(metricsWith(metricDeathsTotal, 1, serial("A")))
	if seq := s.Snapshot().Session.EliminationSequence; len(seq) != 1 {
		t.Fatalf("elimination seq = %v, want 1 entry", seq)
	}

	// End and restart -> new session, cleared elimination sequence.
	s.ApplyMetrics(metricsWith(metricGameActive, 0, nil))
	s.ApplyMetrics(metricsWith(metricGameActive, 1, nil))
	snap := s.Snapshot()
	if snap.SessionID != "session-2" {
		t.Fatalf("SessionID = %q, want session-2", snap.SessionID)
	}
	if len(snap.Session.EliminationSequence) != 0 {
		t.Fatalf("elimination seq should reset, got %v", snap.Session.EliminationSequence)
	}
}

func TestApplyMetrics_SessionScalars(t *testing.T) {
	s := newTestStore()
	s.ApplyMetrics(metricsWith(metricDuration, 12.5, nil))
	s.ApplyMetrics(metricsWith(metricActivePlayers, 3, nil))
	s.ApplyMetrics(metricsWith(metricGameMode, 1, map[string]string{"mode": "joust"}))
	snap := s.Snapshot()
	if snap.Session.DurationSeconds == nil || *snap.Session.DurationSeconds != 12.5 {
		t.Fatalf("duration wrong: %v", snap.Session.DurationSeconds)
	}
	if snap.Session.ActivePlayerCount == nil || *snap.Session.ActivePlayerCount != 3 {
		t.Fatalf("active players wrong: %v", snap.Session.ActivePlayerCount)
	}
	if snap.Session.GameMode == nil || *snap.Session.GameMode != "joust" {
		t.Fatalf("game mode wrong: %v", snap.Session.GameMode)
	}

	// mode with value 0 must be ignored.
	s2 := newTestStore()
	if s2.ApplyMetrics(metricsWith(metricGameMode, 0, map[string]string{"mode": "off"})) {
		t.Fatal("current_game_mode with value 0 should be ignored")
	}
}

func TestApplyMetrics_DeathsBaselineThenIncrementDedupe(t *testing.T) {
	s := newTestStore()
	s.ApplyMetrics(metricsWith(metricDeathsTotal, 0, serial("A"))) // baseline
	if len(s.Snapshot().Session.EliminationSequence) != 0 {
		t.Fatal("baseline should not append")
	}
	s.ApplyMetrics(metricsWith(metricDeathsTotal, 1, serial("A"))) // increment
	s.ApplyMetrics(metricsWith(metricDeathsTotal, 2, serial("A"))) // again (dedupe)
	if seq := s.Snapshot().Session.EliminationSequence; len(seq) != 1 || seq[0] != "A" {
		t.Fatalf("elimination seq = %v, want [A] once", seq)
	}
}

func TestApplyMetrics_EliminationOrderWinsOverDeaths(t *testing.T) {
	s := newTestStore()

	// Preferred elimination order arrives + adopts game_id.
	s.ApplyMetrics(metricsWith(metricEliminationOrder, 1, map[string]string{"serial": "B", "game_id": "game-xyz"}))
	s.ApplyMetrics(metricsWith(metricEliminationOrder, 0, map[string]string{"serial": "A", "game_id": "game-xyz"}))
	snap := s.Snapshot()
	if snap.SessionID != "game-xyz" {
		t.Fatalf("SessionID = %q, want game-xyz", snap.SessionID)
	}
	want := []string{"A", "B"}
	if got := snap.Session.EliminationSequence; len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("elimination seq = %v, want %v (sorted by order)", got, want)
	}

	// deaths_total fallback must now be suppressed.
	s.ApplyMetrics(metricsWith(metricDeathsTotal, 0, serial("C")))
	s.ApplyMetrics(metricsWith(metricDeathsTotal, 5, serial("C")))
	if got := s.Snapshot().Session.EliminationSequence; len(got) != 2 {
		t.Fatalf("deaths fallback should be suppressed, seq = %v", got)
	}
}

func TestApplyMetrics_VarianceNilWhenAbsent(t *testing.T) {
	s := newTestStore()
	// Only a "today" metric arrives; proposed variance never does.
	s.ApplyMetrics(metricsWith(metricAccelMagnitude, 1.2, serial("A")))
	if v := s.Snapshot().Players["A"].MovementVariance; v != nil {
		t.Fatalf("variance should be nil, got %v", *v)
	}
}

func TestApplyMetrics_MissingSerialSkipped(t *testing.T) {
	s := newTestStore()
	if s.ApplyMetrics(metricsWith(metricAccelMagnitude, 1.2, nil)) {
		t.Fatal("data point without serial should be skipped")
	}
}
