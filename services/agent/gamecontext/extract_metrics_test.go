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
		t.Fatal("game_current_mode with value 0 should be ignored")
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

// TestApplyMetrics_AdoptsGameLabels verifies that a live session signal carrying
// game_id + game_kind labels adopts both into the context (the early-adoption
// path #845 adds, much earlier than the pre-#845 end-of-game peak_accel path).
func TestApplyMetrics_AdoptsGameLabels(t *testing.T) {
	s := newTestStore()
	s.ApplyMetrics(metricsWith(metricGameActive, 1, map[string]string{
		attrGameID:   "game-42",
		attrGameKind: "shadow",
	}))
	snap := s.Snapshot()
	if snap.SessionID != "game-42" {
		t.Fatalf("SessionID = %q, want game-42 (adopted from game_id)", snap.SessionID)
	}
	if snap.GameKind != "shadow" {
		t.Fatalf("GameKind = %q, want shadow (adopted from game_kind)", snap.GameKind)
	}
}

// TestApplyMetrics_PerPlayerAdoptsGameID verifies a per-player signal with a
// game_id label adopts the session id (no game_kind on these signals).
func TestApplyMetrics_PerPlayerAdoptsGameID(t *testing.T) {
	s := newTestStore()
	s.ApplyMetrics(metricsWith(metricGameAlive, 1, map[string]string{
		attrSerial: "A",
		attrGameID: "game-7",
	}))
	snap := s.Snapshot()
	if snap.SessionID != "game-7" {
		t.Fatalf("SessionID = %q, want game-7", snap.SessionID)
	}
	if snap.GameKind != "" {
		t.Fatalf("GameKind = %q, want empty (per-player signals carry no game_kind)", snap.GameKind)
	}
}

// TestApplyMetrics_NoGameLabelsLeavesIdentityUnchanged verifies that an
// unlabeled session signal does not clobber a synthetic SessionID or an
// already-observed GameKind — single-game behavior is preserved (no dropping).
func TestApplyMetrics_NoGameLabelsLeavesIdentityUnchanged(t *testing.T) {
	s := newTestStore()
	// First a labeled signal establishes identity.
	s.ApplyMetrics(metricsWith(metricGameActive, 1, map[string]string{
		attrGameID:   "game-1",
		attrGameKind: "real",
	}))
	// Then an unlabeled live signal arrives (the legacy/primary-context path).
	if !s.ApplyMetrics(metricsWith(metricDuration, 30, nil)) {
		t.Fatal("duration without labels should still apply its value")
	}
	snap := s.Snapshot()
	if snap.SessionID != "game-1" {
		t.Fatalf("SessionID = %q, want game-1 (unlabeled signal must not clear it)", snap.SessionID)
	}
	if snap.GameKind != "real" {
		t.Fatalf("GameKind = %q, want real (unlabeled signal must not clear it)", snap.GameKind)
	}
	if snap.Session.DurationSeconds == nil || *snap.Session.DurationSeconds != 30 {
		t.Fatalf("duration not applied: %v", snap.Session.DurationSeconds)
	}
}

// TestApplyMetrics_GameModeNeverCarriesGameID documents the contract: the
// primary/legacy game_current_mode signal never carries game_id, so it must not
// adopt a session id even if one is somehow present.
func TestApplyMetrics_GameModeNeverAdopts(t *testing.T) {
	s := newTestStore()
	s.ApplyMetrics(metricsWith(metricGameMode, 1, map[string]string{
		"mode":     "joust",
		attrGameID: "should-be-ignored",
	}))
	if id := s.Snapshot().SessionID; id != "" {
		t.Fatalf("SessionID = %q, want empty (game_current_mode must not adopt game_id)", id)
	}
}

func TestApplyMetrics_SkipsOwnService(t *testing.T) {
	s := newTestStore()
	s.SetOwnService("agent")

	md := metricsWith(metricGameAlive, 1, serial("A"))
	md.ResourceMetrics().At(0).Resource().Attributes().PutStr("service.name", "agent")
	if s.ApplyMetrics(md) {
		t.Fatal("own-service metrics must be skipped (self-ingestion loop defense)")
	}
	if s.Snapshot().Players["A"] != nil {
		t.Fatal("own-service metrics must not create players")
	}

	// Other services still apply.
	md = metricsWith(metricGameAlive, 1, serial("A"))
	md.ResourceMetrics().At(0).Resource().Attributes().PutStr("service.name", "controller-manager")
	if !s.ApplyMetrics(md) {
		t.Fatal("non-own service metrics must still apply")
	}
}
