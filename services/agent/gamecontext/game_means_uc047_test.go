package gamecontext

import (
	"math"
	"testing"
	"time"
)

// UC: UC-047 main step 1 BR-009 BR-002
func TestGameMeansCoverTheWholeGame(t *testing.T) {
	t.Run("UC-047/BR-009 whole-game means outlive the ring", func(t *testing.T) {
		var h playerHistory
		base := t0()
		total := playerHistoryCap + 20
		for i := 0; i < total; i++ {
			v := float64(i)
			// Two samples per second: the bucket keeps its last value, as the ring does.
			h.record(MovementSample{At: base.Add(time.Duration(i) * time.Second), Intensity: -1, Variance: fp(-1)})
			h.record(MovementSample{At: base.Add(time.Duration(i)*time.Second + 500*time.Millisecond), Intensity: v, Variance: fp(2 * v)})
		}
		m := h.stats()
		if m.IntensitySamples != total || m.VarianceSamples != total {
			t.Fatalf("samples = %d/%d, want %d each", m.IntensitySamples, m.VarianceSamples, total)
		}
		wantMean := float64(total-1) / 2
		if got, ok := m.MeanIntensity(); !ok || math.Abs(got-wantMean) > 1e-9 {
			t.Errorf("MeanIntensity = %v, %v; want %v", got, ok, wantMean)
		}
		if got, ok := m.MeanVariance(); !ok || math.Abs(got-2*wantMean) > 1e-9 {
			t.Errorf("MeanVariance = %v, %v; want %v", got, ok, 2*wantMean)
		}
	})
	t.Run("UC-047/BR-009 a sample without variance and a non-finite value are skipped", func(t *testing.T) {
		var h playerHistory
		base := t0()
		h.record(MovementSample{At: base, Intensity: 1})
		h.record(MovementSample{At: base.Add(time.Second), Intensity: math.NaN(), Variance: fp(4)})
		m := h.stats()
		if m.IntensitySamples != 1 || m.VarianceSamples != 1 {
			t.Fatalf("samples = %d/%d, want 1/1", m.IntensitySamples, m.VarianceSamples)
		}
		if _, ok := (GameMeans{}).MeanVariance(); ok {
			t.Error("an empty GameMeans must have no mean")
		}
	})
	t.Run("UC-047/BR-009 the snapshot carries the whole-game means", func(t *testing.T) {
		now := t0()
		s := NewStore(time.Hour, time.Hour, func() time.Time { return now })
		s.SetGameActive(true)
		for i := 0; i < playerHistoryCap+8; i++ {
			s.SetPlayerVariance("a", 0.5)
			s.SetPlayerIntensity("a", 1)
			now = now.Add(time.Second)
		}
		p := s.Snapshot().Players["a"]
		if p == nil || p.Game.IntensitySamples != playerHistoryCap+8 {
			t.Fatalf("snapshot Game = %+v, want %d intensity samples", p, playerHistoryCap+8)
		}
		if got, _ := p.Game.MeanVariance(); got != 0.5 {
			t.Errorf("MeanVariance = %v, want 0.5", got)
		}
	})
}

// playSeconds feeds n seconds of intensity/variance for serial, advancing *now.
func playSeconds(s *Store, now *time.Time, serial string, n int, intensity, variance float64) {
	for i := 0; i < n; i++ {
		s.SetPlayerVariance(serial, variance)
		s.SetPlayerIntensity(serial, intensity)
		*now = now.Add(time.Second)
	}
}

// UC: UC-047 BR-009 BR-002
func TestGameMeansSurviveEviction(t *testing.T) {
	newStore := func() (*Store, *time.Time) {
		now := t0()
		s := NewStore(5*time.Second, time.Hour, func() time.Time { return now })
		s.SetGameActive(true)
		return s, &now
	}
	t.Run("UC-047/BR-009 a player who comes back keeps the sums", func(t *testing.T) {
		s, now := newStore()
		playSeconds(s, now, "a", 10, 1, 0.02)
		playSeconds(s, now, "b", 1, 1, 0.02) // keeps the game alive
		*now = now.Add(7 * time.Second)      // "a" silent past the 5 s TTL
		s.EvictStale()
		if p := s.Snapshot().Players["a"]; p != nil {
			t.Fatal("precondition: a should be evicted")
		}
		playSeconds(s, now, "a", 2, 1, 0.02)
		p := s.Snapshot().Players["a"]
		if p == nil || p.Game.IntensitySamples != 12 || p.Game.VarianceSamples != 12 {
			t.Fatalf("after return Game = %+v, want 12 samples each", p)
		}
	})
	t.Run("UC-047/BR-009 a player still evicted at the end gets real means", func(t *testing.T) {
		var got GameContext
		s, now := newStore()
		s.OnGameEnd = func(c GameContext) { got = c }
		playSeconds(s, now, "a", 10, 1, 0.03)
		*now = now.Add(7 * time.Second)
		s.EvictStale()
		s.SetGameActive(false)
		p := got.Players["a"]
		if p == nil || !p.Waiting {
			t.Fatalf("end snapshot player a = %+v, want a waiting player", p)
		}
		if v, ok := p.Game.MeanVariance(); !ok || math.Abs(v-0.03) > 1e-12 || p.Game.IntensitySamples != 10 {
			t.Fatalf("waiting player Game = %+v, want 10 samples with mean variance 0.03", p.Game)
		}
	})
	t.Run("UC-047/BR-009 seconds after the elimination are left out", func(t *testing.T) {
		s, now := newStore()
		playSeconds(s, now, "a", 10, 1, 0.04)
		s.RecordElimination("a")
		*now = now.Add(time.Second)
		playSeconds(s, now, "a", 5, 9, 9) // a coordinator still exporting the dead player
		p := s.Snapshot().Players["a"]
		if v, _ := p.Game.MeanVariance(); p.Game.VarianceSamples != 10 || math.Abs(v-0.04) > 1e-12 {
			t.Fatalf("Game = %+v, want the 10 pre-elimination samples only", p.Game)
		}
		if len(p.History) != 15 {
			t.Errorf("History len = %d, want 15 (the ring keeps recording)", len(p.History))
		}
	})
}
