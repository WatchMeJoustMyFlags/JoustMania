package decision

import (
	"sync/atomic"
	"testing"
	"time"
)

// effect_window_source_test.go covers the #1788 use-time read of the effect window
// (effect.window_seconds, FR-150): each dispatched intervention schedules its
// follow-up with the window in force at that moment, not one read at startup.
func TestEffect_WindowSourceReadPerIntervention(t *testing.T) {
	provider := newFakeContextProvider()
	const gameID = "game_window"
	provider.set(gameID, durationContext(gameID, 90))
	l, _, _, _ := effectLoop(t, provider, gameID)

	var windows []time.Duration
	l.afterFunc = func(d time.Duration, _ func()) func() bool {
		windows = append(windows, d)
		return func() bool { return true }
	}
	var flagSeconds atomic.Int64
	flagSeconds.Store(10)
	l.SetEffectWindow(40 * time.Second) // a startup value the source must override
	l.SetEffectWindowSource(func() time.Duration { return time.Duration(flagSeconds.Load()) * time.Second })

	l.schedule(shieldDecision(), enduranceBaseline(), defaultFit())
	flagSeconds.Store(25) // the operator changes the flag mid-run
	l.schedule(shieldDecision(), enduranceBaseline(), defaultFit())
	flagSeconds.Store(0) // non-positive: the default
	l.schedule(shieldDecision(), enduranceBaseline(), defaultFit())

	want := []time.Duration{10 * time.Second, 25 * time.Second, DefaultEffectWindow}
	if len(windows) != len(want) {
		t.Fatalf("scheduled %d samples, want %d", len(windows), len(want))
	}
	for i := range want {
		if windows[i] != want[i] {
			t.Errorf("sample %d window = %v, want %v", i+1, windows[i], want[i])
		}
	}
}
