package gamecontext

import (
	"testing"
	"time"
)

// Tests for the #916 rolling-narrative ring buffer: accumulation (order),
// capacity (oldest evicted at cap), the snapshot's shared-nothing copy, reset,
// and deterministic rendering. These exercise the buffer directly; the
// Store-level lifecycle (per-partition isolation, session/grace eviction) is
// covered in store_test.go and multiplexer_test.go.

// kinds extracts the event kinds in order — a compact way to assert accumulation.
func kinds(events []TimelineEvent) []TimelineEventKind {
	out := make([]TimelineEventKind, len(events))
	for i, e := range events {
		out[i] = e.Kind
	}
	return out
}

func TestTimeline_AppendsInOrder(t *testing.T) {
	tl := newTimeline()
	base := time.Unix(0, 0)
	tl.append(TimelineEvent{At: base, Kind: EventPhase, Detail: "start"})
	tl.append(TimelineEvent{At: base.Add(time.Second), Kind: EventStateDelta})
	tl.append(TimelineEvent{At: base.Add(2 * time.Second), Kind: EventElimination, Serial: "A", Order: 1})

	got := tl.events()
	if len(got) != 3 {
		t.Fatalf("len = %d, want 3", len(got))
	}
	want := []TimelineEventKind{EventPhase, EventStateDelta, EventElimination}
	for i := range want {
		if got[i].Kind != want[i] {
			t.Errorf("event[%d].Kind = %q, want %q", i, got[i].Kind, want[i])
		}
	}
	// Append order is oldest-first.
	if !got[0].At.Before(got[2].At) {
		t.Errorf("events not in oldest-first order")
	}
}

func TestTimeline_CapacityEvictsOldest(t *testing.T) {
	tl := newTimeline()
	base := time.Unix(0, 0)
	// Append exactly 2x capacity; the first timelineCap entries must be evicted.
	total := timelineCap * 2
	for i := 0; i < total; i++ {
		tl.append(TimelineEvent{At: base.Add(time.Duration(i) * time.Second), Kind: EventStateDelta, Order: i})
	}

	got := tl.events()
	if len(got) != timelineCap {
		t.Fatalf("len = %d, want capped at %d", len(got), timelineCap)
	}
	// The oldest surviving entry is index (total - cap); the newest is total-1.
	if got[0].Order != total-timelineCap {
		t.Errorf("oldest survivor Order = %d, want %d", got[0].Order, total-timelineCap)
	}
	if got[len(got)-1].Order != total-1 {
		t.Errorf("newest Order = %d, want %d", got[len(got)-1].Order, total-1)
	}
	// Ordering across the wrap is still strictly increasing.
	for i := 1; i < len(got); i++ {
		if got[i].Order != got[i-1].Order+1 {
			t.Fatalf("non-contiguous after wrap at %d: %d then %d", i, got[i-1].Order, got[i].Order)
		}
	}
}

func TestTimeline_AtExactlyCapacity(t *testing.T) {
	tl := newTimeline()
	base := time.Unix(0, 0)
	for i := 0; i < timelineCap; i++ {
		tl.append(TimelineEvent{At: base, Kind: EventStateDelta, Order: i})
	}
	got := tl.events()
	if len(got) != timelineCap {
		t.Fatalf("len = %d, want %d (nothing evicted at exactly cap)", len(got), timelineCap)
	}
	if got[0].Order != 0 {
		t.Errorf("oldest Order = %d, want 0 (no eviction yet)", got[0].Order)
	}
}

func TestTimeline_EmptyEventsIsNil(t *testing.T) {
	tl := newTimeline()
	if got := tl.events(); got != nil {
		t.Errorf("empty timeline events() = %v, want nil", got)
	}
}

func TestTimeline_EventsCopyIsIndependent(t *testing.T) {
	tl := newTimeline()
	base := time.Unix(0, 0)
	tl.append(TimelineEvent{At: base, Kind: EventPhase, Detail: "start"})

	snap := tl.events()
	// A later append must not mutate the previously returned slice.
	tl.append(TimelineEvent{At: base.Add(time.Second), Kind: EventStateDelta})
	if len(snap) != 1 {
		t.Fatalf("handed-out snapshot grew to %d after a later append", len(snap))
	}
	if snap[0].Kind != EventPhase {
		t.Errorf("handed-out snapshot mutated: kind = %q", snap[0].Kind)
	}
}

func TestTimeline_ResetClearsBufferAndDedupe(t *testing.T) {
	tl := newTimeline()
	tl.append(TimelineEvent{At: time.Unix(0, 0), Kind: EventStateDelta})
	tl.lastActivePlayers = ptr(3)
	tl.reset()
	if got := tl.events(); got != nil {
		t.Errorf("after reset events() = %v, want nil", got)
	}
	if tl.lastActivePlayers != nil {
		t.Errorf("reset did not clear state-delta dedupe memory")
	}
}

func TestRenderTimeline_Deterministic(t *testing.T) {
	now := time.Date(2026, time.June, 5, 12, 30, 0, 0, time.UTC)
	events := []TimelineEvent{
		{At: now.Add(-90 * time.Second), Kind: EventPhase, Detail: "start"},
		{At: now.Add(-60 * time.Second), Kind: EventStateDelta, ActivePlayers: ptr(3), MeanIntensity: ptr(1.25)},
		{At: now.Add(-30 * time.Second), Kind: EventElimination, Serial: "AA:11", Order: 1},
		{At: now.Add(-10 * time.Second), Kind: EventIntervention, Detail: "grant_shield", Serial: "BB:22", Blocked: true},
		{At: now.Add(-5 * time.Second), Kind: EventIntervention, Detail: "set_music_tempo"},
	}
	want := []string{
		"-90s phase: start",
		"-60s state: active_players=3 mean_intensity=1.25",
		"-30s elimination: AA:11 (#1)",
		"-10s intervention: grant_shield blocked -> BB:22",
		"-5s intervention: set_music_tempo dispatched",
	}
	got := RenderTimeline(events, now)
	if len(got) != len(want) {
		t.Fatalf("len = %d, want %d: %v", len(got), len(want), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("line[%d] = %q, want %q", i, got[i], want[i])
		}
	}
	// Re-render is byte-identical (determinism contract).
	again := RenderTimeline(events, now)
	for i := range got {
		if got[i] != again[i] {
			t.Errorf("non-deterministic render at %d: %q vs %q", i, got[i], again[i])
		}
	}
}

func TestRenderTimeline_StateDeltaOmitsNilAggregates(t *testing.T) {
	now := time.Unix(100, 0)
	// Only ActivePlayers set; MeanIntensity nil must be omitted, not rendered 0.
	got := RenderTimeline([]TimelineEvent{
		{At: now.Add(-10 * time.Second), Kind: EventStateDelta, ActivePlayers: ptr(2)},
	}, now)
	if len(got) != 1 || got[0] != "-10s state: active_players=2" {
		t.Errorf("got %v, want a single line omitting the nil mean_intensity", got)
	}
}

func TestRenderTimeline_EmptyIsEmptySlice(t *testing.T) {
	if got := RenderTimeline(nil, time.Now()); len(got) != 0 {
		t.Errorf("RenderTimeline(nil) = %v, want empty", got)
	}
}
