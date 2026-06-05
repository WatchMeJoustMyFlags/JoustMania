package gamerunner

import (
	"context"
	"errors"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	gcpb "github.com/joustmania/agent/gen/game_coordinator"
)

// ev is a small helper to build a scripted GameEvent.
func ev(eventType string) *gcpb.GameEvent {
	return &gcpb.GameEvent{EventType: eventType, Timestamp: time.Now().UnixMilli()}
}

func TestRunShadowGame_HappyPath(t *testing.T) {
	mock := &fakeMock{}
	coord := &fakeCoord{
		gameID: "game_abc123",
		events: []*gcpb.GameEvent{
			ev("game_starting"),
			ev("game_started"),
			ev("player_death"),
			ev("game_ended"),
		},
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	spec := Spec{RunID: "run1", GameName: "JoustFFA", Players: 3, Sensitivity: 2}
	res, err := h.runner.RunShadowGame(context.Background(), spec)
	if err != nil {
		t.Fatalf("RunShadowGame error: %v", err)
	}

	if res.GameID != "game_abc123" {
		t.Errorf("GameID = %q, want game_abc123", res.GameID)
	}
	if res.Outcome != OutcomeCompleted {
		t.Errorf("Outcome = %q, want completed", res.Outcome)
	}
	if res.TerminalEvent != eventGameEnded {
		t.Errorf("TerminalEvent = %q, want game_ended", res.TerminalEvent)
	}
	// The capture loop returns after the first game_id-stamped event
	// (game_starting); the await loop then sees the remaining three.
	if res.EventsSeen != 3 {
		t.Errorf("EventsSeen = %d, want 3", res.EventsSeen)
	}
	if res.Duration <= 0 {
		t.Errorf("Duration not recorded: %v", res.Duration)
	}

	added, removed, _, _ := mock.snapshot()
	if len(added) != 3 {
		t.Fatalf("added %d controllers, want 3", len(added))
	}
	// Cleanup must remove exactly the reserved controllers it created.
	if len(removed) != 3 {
		t.Errorf("removed %d controllers, want 3 (added=%v removed=%v)", len(removed), added, removed)
	}
	for _, s := range added {
		if !contains(removed, s) {
			t.Errorf("reserved controller %q was not removed", s)
		}
	}
	if mock.reservedTag != "agent:run1" {
		t.Errorf("reservation tag = %q, want agent:run1", mock.reservedTag)
	}
}

func TestRunShadowGame_TimeoutForceEnds(t *testing.T) {
	mock := &fakeMock{}
	// Stream emits a start event (with game_id) then blocks forever — no
	// terminal event arrives, so the runner must force-end. ForceEnd pushes the
	// terminal event back onto the stream.
	coord := &fakeCoord{
		gameID:                 "game_timeout",
		events:                 []*gcpb.GameEvent{ev("game_started")},
		emitTerminalOnForceEnd: eventGameForceEnded,
	}
	h := newHarness(t, mock, coord)
	h.runner.cfg.GameTimeout = 80 * time.Millisecond
	defer h.stop()

	spec := Spec{RunID: "runTO", GameName: "JoustFFA", Players: 2}
	res, err := h.runner.RunShadowGame(context.Background(), spec)
	if err != nil {
		t.Fatalf("RunShadowGame error: %v", err)
	}

	if res.Outcome != OutcomeForceEnded {
		t.Errorf("Outcome = %q, want force_ended", res.Outcome)
	}
	forceEnds := coord.forceEndGameIDs()
	if len(forceEnds) == 0 {
		t.Fatal("ForceEndGame was never called")
	}
	if forceEnds[0] != "game_timeout" {
		t.Errorf("ForceEndGame game_id = %q, want game_timeout", forceEnds[0])
	}

	// Controllers cleaned up even though the game timed out.
	added, removed, _, _ := mock.snapshot()
	if len(removed) != len(added) || len(added) == 0 {
		t.Errorf("cleanup mismatch: added=%v removed=%v", added, removed)
	}
}

func TestRunShadowGame_CleanupOnStreamError(t *testing.T) {
	mock := &fakeMock{}
	// Stream sends the start event (so game_id is captured) then errors out.
	coord := &fakeCoord{
		gameID:    "game_err",
		events:    []*gcpb.GameEvent{ev("game_started")},
		streamErr: status.Error(codes.Unavailable, "boom"),
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	spec := Spec{RunID: "runErr", GameName: "JoustFFA", Players: 2}
	res, _ := h.runner.RunShadowGame(context.Background(), spec)

	// Even though the stream errored, the reserved controllers must be removed.
	added, removed, _, _ := mock.snapshot()
	if len(added) == 0 {
		t.Fatal("no controllers were reserved")
	}
	if len(removed) != len(added) {
		t.Errorf("cleanup-on-error failed: added=%v removed=%v", added, removed)
	}
	if res.Outcome != OutcomeError {
		t.Errorf("Outcome = %q, want error", res.Outcome)
	}
}

func TestRunShadowGame_StartRejected(t *testing.T) {
	mock := &fakeMock{}
	// Coordinator rejects the start: emits game_start_error and no game_id.
	coord := &fakeCoord{
		events: []*gcpb.GameEvent{ev("game_start_error")},
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	spec := Spec{RunID: "runReject", GameName: "BogusMode", Players: 2}
	_, err := h.runner.RunShadowGame(context.Background(), spec)
	if err == nil {
		t.Fatal("expected error on rejected start")
	}

	// Controllers reserved before the start must still be cleaned up.
	added, removed, _, _ := mock.snapshot()
	if len(removed) != len(added) || len(added) == 0 {
		t.Errorf("cleanup on rejected start failed: added=%v removed=%v", added, removed)
	}
}

func TestRunShadowGame_DrivesKills(t *testing.T) {
	mock := &fakeMock{}
	// Block after start so the drive loop runs long enough to issue kills, then
	// rely on the timeout force-end to terminate.
	coord := &fakeCoord{
		gameID:                 "game_kills",
		events:                 []*gcpb.GameEvent{ev("game_started")},
		emitTerminalOnForceEnd: eventGameForceEnded,
	}
	h := newHarness(t, mock, coord)
	h.runner.cfg.GameTimeout = 120 * time.Millisecond
	h.runner.cfg.KillInterval = 15 * time.Millisecond
	defer h.stop()

	spec := Spec{RunID: "runKills", GameName: "JoustFFA", Players: 3}
	_, err := h.runner.RunShadowGame(context.Background(), spec)
	if err != nil {
		t.Fatalf("RunShadowGame error: %v", err)
	}

	_, _, deaths, movements := mock.snapshot()
	// With 3 players the drive loop kills at most 2 (leaves a survivor).
	if len(deaths) == 0 {
		t.Error("expected at least one SimulateDeath call")
	}
	if len(deaths) > 2 {
		t.Errorf("killed %d players, want <= 2 (one survivor)", len(deaths))
	}
	if movements == 0 {
		t.Error("expected at least one SimulateMovement liveliness call")
	}
}

func TestRunShadowGame_ValidatesSpec(t *testing.T) {
	r := New(Config{}, nil)
	cases := []Spec{
		{RunID: "", GameName: "JoustFFA", Players: 2},
		{RunID: "x", GameName: "", Players: 2},
		{RunID: "x", GameName: "JoustFFA", Players: 1},
	}
	for i, spec := range cases {
		if _, err := r.RunShadowGame(context.Background(), spec); err == nil {
			t.Errorf("case %d: expected validation error for spec %+v", i, spec)
		}
	}
}

func contains(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

// sentinel to keep the errors import if other tests are trimmed.
var _ = errors.New
