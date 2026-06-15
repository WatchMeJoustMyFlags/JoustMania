package gamecontext

import (
	"testing"
	"time"
)

func t0() time.Time { return time.Date(2026, time.June, 15, 12, 0, 0, 0, time.UTC) }

func fp(v float64) *float64 { return &v }

// TestPlayerHistoryBucketDedup: within one bucket-second the tail is overwritten
// (latest value wins); a new second appends. This is the memory bound's linchpin.
func TestPlayerHistoryBucketDedup(t *testing.T) {
	var h playerHistory
	base := t0()
	// Three samples in the SAME second -> one entry holding the last value.
	h.record(MovementSample{At: base.Add(10 * time.Millisecond), Intensity: 0.1})
	h.record(MovementSample{At: base.Add(500 * time.Millisecond), Intensity: 0.2})
	h.record(MovementSample{At: base.Add(900 * time.Millisecond), Intensity: 0.3})
	if got := h.samples(); len(got) != 1 || got[0].Intensity != 0.3 {
		t.Fatalf("same-bucket coalesce failed: %+v", got)
	}
	// A new second appends.
	h.record(MovementSample{At: base.Add(1100 * time.Millisecond), Intensity: 0.9})
	if got := h.samples(); len(got) != 2 || got[1].Intensity != 0.9 {
		t.Fatalf("new-bucket append failed: %+v", got)
	}
}

// TestPlayerHistoryRingBound: the ring never exceeds playerHistoryCap and keeps
// the most-recent window (oldest entries evicted).
func TestPlayerHistoryRingBound(t *testing.T) {
	var h playerHistory
	base := t0()
	total := playerHistoryCap + 20
	for i := 0; i < total; i++ {
		h.record(MovementSample{At: base.Add(time.Duration(i) * time.Second), Intensity: float64(i)})
	}
	got := h.samples()
	if len(got) != playerHistoryCap {
		t.Fatalf("ring not bounded: len=%d want %d", len(got), playerHistoryCap)
	}
	// Oldest retained is total-cap; newest is total-1.
	if got[0].Intensity != float64(total-playerHistoryCap) {
		t.Errorf("oldest = %v, want %v", got[0].Intensity, float64(total-playerHistoryCap))
	}
	if got[len(got)-1].Intensity != float64(total-1) {
		t.Errorf("newest = %v, want %v", got[len(got)-1].Intensity, float64(total-1))
	}
}

// TestSparklineScaling: a value at the threshold renders the top block; idle
// renders the idle glyph; an empty series renders "".
func TestRenderSparkline(t *testing.T) {
	base := t0()
	samples := []MovementSample{
		{At: base, Intensity: 0.0},                  // idle -> '_'
		{At: base.Add(time.Second), Intensity: 0.9}, // mid
		{At: base.Add(2 * time.Second), Intensity: 1.8},
	}
	thr := fp(1.8)
	got := RenderSparkline(samples, thr)
	r := []rune(got)
	if len(r) != 3 {
		t.Fatalf("sparkline len = %d, want 3 (%q)", len(r), got)
	}
	if r[0] != idleGlyph {
		t.Errorf("idle bucket = %q, want %q", r[0], idleGlyph)
	}
	if r[2] != sparkGlyphs[len(sparkGlyphs)-1] {
		t.Errorf("at-threshold bucket = %q, want top block", r[2])
	}
	if RenderSparkline(nil, thr) != "" {
		t.Error("empty series must render empty string")
	}
	// nil threshold -> auto-scale to peak; the peak renders the top block.
	auto := []rune(RenderSparkline(samples, nil))
	if auto[2] != sparkGlyphs[len(sparkGlyphs)-1] {
		t.Errorf("auto-scaled peak = %q, want top block", auto[2])
	}
}

// TestClassifyActivity covers idle / active / erratic / unknown.
func TestClassifyActivity(t *testing.T) {
	base := t0()
	mk := func(vals ...float64) []MovementSample {
		out := make([]MovementSample, len(vals))
		for i, v := range vals {
			out[i] = MovementSample{At: base.Add(time.Duration(i) * time.Second), Intensity: v}
		}
		return out
	}
	if got := ClassifyActivity(nil); got != ActivityUnknown {
		t.Errorf("empty = %q, want unknown", got)
	}
	if got := ClassifyActivity(mk(0.0, 0.05, 0.1, 0.02)); got != ActivityIdle {
		t.Errorf("idle = %q, want idle", got)
	}
	if got := ClassifyActivity(mk(1.0, 1.05, 0.98, 1.02)); got != ActivityActive {
		t.Errorf("steady = %q, want active", got)
	}
	if got := ClassifyActivity(mk(0.2, 1.8, 0.3, 1.7)); got != ActivityErratic {
		t.Errorf("bursty = %q, want erratic", got)
	}
	// #1085: a smooth monotonic fade-out (high → settled-to-still) must read idle —
	// the player's CURRENT state — not erratic. The std-vs-mean test alone fires on
	// the decline because the early high samples inflate both mean and std.
	if got := ClassifyActivity(mk(0.90, 0.50, 0.10, 0.05, 0.02, 0.00)); got != ActivityIdle {
		t.Errorf("monotonic fade = %q, want idle", got)
	}
	// Guard the other direction: a genuinely bursty tail must stay erratic even
	// though its window mean is comparable to the fade above.
	if got := ClassifyActivity(mk(0.1, 0.9, 0.1, 0.8, 0.1, 0.9)); got != ActivityErratic {
		t.Errorf("sustained bursty = %q, want erratic", got)
	}
	// Steady high keeps reading active; steady low keeps reading idle.
	if got := ClassifyActivity(mk(0.9, 0.95, 0.92, 0.9, 0.93, 0.91)); got != ActivityActive {
		t.Errorf("steady high = %q, want active", got)
	}
	if got := ClassifyActivity(mk(0.05, 0.1, 0.08, 0.02, 0.0, 0.03)); got != ActivityIdle {
		t.Errorf("steady low = %q, want idle", got)
	}
}

// TestComputeProximity: the closest call uses the threshold IN FORCE at each
// sample's time, marks Crossed when intensity meets the threshold, and reports
// unobserved when there is no threshold to compare against.
func TestComputeProximity(t *testing.T) {
	base := t0()
	samples := []MovementSample{
		{At: base.Add(0 * time.Second), Intensity: 1.0},
		{At: base.Add(10 * time.Second), Intensity: 1.9}, // crosses at the fast threshold
		{At: base.Add(20 * time.Second), Intensity: 1.2},
	}
	track := []SpeedSample{
		{At: base.Add(0 * time.Second), DeathThreshold: fp(2.0)},
		{At: base.Add(10 * time.Second), DeathThreshold: fp(1.8)},
	}
	p := ComputeProximity(samples, track, nil)
	if !p.Observed {
		t.Fatal("expected observed proximity")
	}
	// At +10s: 1.9/1.8 = 1.056 -> crossed, the closest call.
	if !p.Crossed {
		t.Errorf("expected crossed; ratio=%.3f", p.Ratio)
	}
	if p.At != base.Add(10*time.Second) {
		t.Errorf("closest-call time = %v, want +10s", p.At)
	}
	// No threshold anywhere -> unobserved.
	if got := ComputeProximity(samples, nil, nil); got.Observed {
		t.Error("no threshold must yield unobserved proximity")
	}
	// Fallback scalar threshold when track empty.
	if got := ComputeProximity(samples, nil, fp(2.0)); !got.Observed {
		t.Error("fallback threshold should yield observed proximity")
	}
}

// TestRenderProximity formats the readout and the unobserved sentinel.
func TestRenderProximity(t *testing.T) {
	now := t0()
	p := Proximity{Observed: true, Ratio: 0.92, At: now.Add(-3 * time.Second), Crossed: false}
	if got := RenderProximity(p, now, false); got != "near-death@-3s(0.92×thr)" {
		t.Errorf("proximity = %q", got)
	}
	crossed := Proximity{Observed: true, Ratio: 1.05, At: now.Add(-5 * time.Second), Crossed: true}
	if got := RenderProximity(crossed, now, true); got != "near-death@-5s(1.05×thr)→elim" {
		t.Errorf("crossed proximity = %q", got)
	}
	if got := RenderProximity(Proximity{}, now, false); got != "near-death=unknown" {
		t.Errorf("unobserved = %q, want near-death=unknown", got)
	}
}

// TestRenderSpeedTrack: a ramp renders rising; a flat tempo renders a mid bar;
// empty renders "".
func TestRenderSpeedTrack(t *testing.T) {
	base := t0()
	ramp := []SpeedSample{
		{At: base, Tempo: fp(1.0)},
		{At: base.Add(time.Second), Tempo: fp(1.15)},
		{At: base.Add(2 * time.Second), Tempo: fp(1.3)},
	}
	got := []rune(RenderSpeedTrack(ramp))
	if len(got) != 3 || got[0] != sparkGlyphs[0] || got[2] != sparkGlyphs[len(sparkGlyphs)-1] {
		t.Errorf("ramp = %q, want rising lowest..highest", string(got))
	}
	if RenderSpeedTrack(nil) != "" {
		t.Error("empty track must render empty string")
	}
	// Threshold-only samples (no tempo) render nothing.
	if RenderSpeedTrack([]SpeedSample{{At: base, DeathThreshold: fp(1.5)}}) != "" {
		t.Error("threshold-only track must render empty (no tempo dimension)")
	}
}

// TestStoreRecordsPlayerSamples: SetPlayerIntensity feeds the per-player ring and
// the snapshot carries it; SetMusicTempo/SetDeathThreshold feed the speed track.
func TestStoreRecordsPlayerSamples(t *testing.T) {
	clock := t0()
	now := func() time.Time { return clock }
	s := NewStore(time.Minute, time.Minute, now)

	s.SetMusicTempo(1.0)
	s.SetDeathThreshold(1.5)
	clock = clock.Add(time.Second)
	s.SetPlayerIntensity("AA:11", 0.5)
	clock = clock.Add(time.Second)
	s.SetMusicTempo(1.3)
	s.SetPlayerIntensity("AA:11", 1.4)

	snap := s.Snapshot()
	if snap.Session.MusicTempo == nil || *snap.Session.MusicTempo != 1.3 {
		t.Errorf("music tempo = %v, want 1.3", snap.Session.MusicTempo)
	}
	if snap.Session.DeathThreshold == nil || *snap.Session.DeathThreshold != 1.5 {
		t.Errorf("death threshold = %v, want 1.5", snap.Session.DeathThreshold)
	}
	p := snap.Players["AA:11"]
	if p == nil || len(p.History) != 2 {
		t.Fatalf("player history = %+v, want 2 samples", p)
	}
	if p.History[0].Intensity != 0.5 || p.History[1].Intensity != 1.4 {
		t.Errorf("history values = %+v", p.History)
	}
	if len(snap.SpeedTrack) < 1 {
		t.Errorf("speed track empty: %+v", snap.SpeedTrack)
	}
}

// TestStoreSnapshotHistoryIsolation: a snapshot's history shares no backing array
// with the live ring — a later record never mutates a handed-out snapshot.
func TestStoreSnapshotHistoryIsolation(t *testing.T) {
	clock := t0()
	now := func() time.Time { return clock }
	s := NewStore(time.Minute, time.Minute, now)
	s.SetPlayerIntensity("AA:11", 0.5)
	snap := s.Snapshot()
	clock = clock.Add(time.Second)
	s.SetPlayerIntensity("AA:11", 9.9)
	if got := len(snap.Players["AA:11"].History); got != 1 {
		t.Errorf("snapshot history mutated after later record: len=%d, want 1", got)
	}
}

// TestStoreResetClearsHistory: a new session (and grace exit) drops the per-player
// history and the speed track so a back-to-back game starts clean.
func TestStoreResetClearsHistory(t *testing.T) {
	clock := t0()
	now := func() time.Time { return clock }
	s := NewStore(time.Minute, time.Minute, now)
	s.SetGameActive(true)
	s.SetPlayerIntensity("AA:11", 1.0)
	s.SetMusicTempo(1.2)
	// New session: reset.
	s.SetGameActive(false)
	s.SetGameActive(true)
	snap := s.Snapshot()
	if p := snap.Players["AA:11"]; p != nil && len(p.History) != 0 {
		t.Errorf("history not reset on new session: %+v", p.History)
	}
	if len(snap.SpeedTrack) != 0 {
		t.Errorf("speed track not reset on new session: %+v", snap.SpeedTrack)
	}
}

// TestStoreEvictDropsHistory: TTL-evicting a player drops its movement ring.
func TestStoreEvictDropsHistory(t *testing.T) {
	clock := t0()
	now := func() time.Time { return clock }
	s := NewStore(time.Second, time.Minute, now)
	s.SetPlayerIntensity("AA:11", 1.0)
	clock = clock.Add(10 * time.Second) // past the 1s TTL
	s.EvictStale()
	if _, ok := s.playerHistory["AA:11"]; ok {
		t.Error("evicted player's history ring not dropped")
	}
}
