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
