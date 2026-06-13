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

// TestStartExperimentGame_BindsCohortAndReturnsID covers the #976/#991 async
// experiment spawn: StartExperimentGame returns the game_id promptly, binds
// (experiment_id, arm) and origin=AGENT onto the StartGameConfig, and the
// background drive/cleanup runs to completion (controllers removed) when the game
// ends — all bounded by driveCtx.
func TestStartExperimentGame_BindsCohortAndReturnsID(t *testing.T) {
	mock := &fakeMock{}
	coord := &fakeCoord{
		gameID: "game_exp_001",
		events: []*gcpb.GameEvent{
			ev("game_starting"),
			ev("game_ended"),
		},
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	spec := Spec{
		RunID: "run-exp", GameName: "JoustFFA", Players: 2, Sensitivity: 2,
		ExperimentID: "exp_abc123def456", Arm: "experimental",
	}

	driveCtx, cancelDrive := context.WithCancel(context.Background())
	defer cancelDrive()

	gameID, err := h.runner.StartExperimentGame(context.Background(), driveCtx, spec)
	if err != nil {
		t.Fatalf("StartExperimentGame error: %v", err)
	}
	if gameID != "game_exp_001" {
		t.Fatalf("game_id = %q, want game_exp_001", gameID)
	}

	// The start config must carry the cohort binding + AGENT origin (the #976
	// contract: an experiment game is always a shadow game).
	cfg := coord.capturedStartConfig()
	if cfg == nil {
		t.Fatal("StartGameConfig was not sent")
	}
	if cfg.GetExperimentId() != "exp_abc123def456" {
		t.Errorf("experiment_id = %q, want exp_abc123def456", cfg.GetExperimentId())
	}
	if cfg.GetArm() != "experimental" {
		t.Errorf("arm = %q, want experimental", cfg.GetArm())
	}
	if cfg.GetOrigin() != gcpb.GameOrigin_GAME_ORIGIN_AGENT {
		t.Errorf("origin = %v, want GAME_ORIGIN_AGENT", cfg.GetOrigin())
	}

	// The background drive should reach the terminal event and remove the reserved
	// controllers. Poll for cleanup (the goroutine runs after Start returns).
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		_, removed, _, _ := mock.snapshot()
		if len(removed) == spec.Players {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	_, removed, _, _ := mock.snapshot()
	t.Fatalf("background drive did not remove reserved controllers: removed %d, want %d", len(removed), spec.Players)
}

// TestStartExperimentGame_ValidatesSpec confirms the async start rejects an invalid
// spec without spawning a goroutine or leaving controllers reserved.
func TestStartExperimentGame_ValidatesSpec(t *testing.T) {
	mock := &fakeMock{}
	coord := &fakeCoord{gameID: "g"}
	h := newHarness(t, mock, coord)
	defer h.stop()

	_, err := h.runner.StartExperimentGame(context.Background(), context.Background(),
		Spec{RunID: "r", GameName: "JoustFFA", Players: 1})
	if err == nil {
		t.Fatal("expected a spec-validation error for Players < 2")
	}
	if added, _, _, _ := mock.snapshot(); len(added) != 0 {
		t.Fatalf("invalid spec reserved %d controllers, want 0", len(added))
	}
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
