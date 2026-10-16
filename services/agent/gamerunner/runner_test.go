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
	spec = testPatternSpec(t, spec)

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
// UC: UC-046 main BR-004
func TestStartExperimentGame_ValidatesSpec(t *testing.T) {
	t.Run("UC-046/BR-004 a shadow game without an archetype is refused and reserves nothing", func(t *testing.T) {
		mock := &fakeMock{}
		coord := &fakeCoord{gameID: "g"}
		h := newHarness(t, mock, coord)
		defer h.stop()

		_, err := h.runner.StartExperimentGame(context.Background(), context.Background(),
			Spec{RunID: "r", GameName: "JoustFFA", Players: 4})
		if !errors.Is(err, errNotPattern) {
			t.Fatalf("err = %v, want errNotPattern", err)
		}
		if added, _, _, _ := mock.snapshot(); len(added) != 0 {
			t.Fatalf("refused spec reserved %d controllers, want 0", len(added))
		}
	})
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

	spec := testPatternSpec(t, Spec{RunID: "run1", Sensitivity: 2})
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
	added, removed, deaths, movements := mock.snapshot()
	if len(added) != 2 || len(removed) != 2 {
		t.Fatalf("added %v removed %v, want the archetype's 2 players reserved and removed", added, removed)
	}
	if len(deaths) != 0 || movements != 0 {
		t.Errorf("scripted motion sent: deaths=%v movements=%d (UC-046 BR-004)", deaths, movements)
	}
	if mock.reservedTag != "agent:run1" {
		t.Errorf("reservation tag = %q, want agent:run1", mock.reservedTag)
	}
}

func TestRunShadowGame_TimeoutForceEnds(t *testing.T) {
	mock := &fakeMock{}
	coord := &fakeCoord{
		gameID:                 "game_timeout",
		events:                 []*gcpb.GameEvent{ev("game_started")},
		emitTerminalOnForceEnd: eventGameForceEnded,
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	spec := testPatternSpec(t, Spec{RunID: "runTO", MaxGameSeconds: 30}) // 30 units = 600 ms
	res, err := h.runner.RunShadowGame(context.Background(), spec)
	if err != nil {
		t.Fatalf("RunShadowGame error: %v", err)
	}
	if res.Outcome != OutcomeTimeout {
		t.Errorf("Outcome = %q, want timeout", res.Outcome)
	}
	if forceEnds := coord.forceEndGameIDs(); len(forceEnds) == 0 || forceEnds[0] != "game_timeout" {
		t.Fatalf("ForceEndGame calls = %v, want game_timeout", forceEnds)
	}
	added, removed, _, _ := mock.snapshot()
	if len(removed) != len(added) || len(added) == 0 {
		t.Errorf("cleanup mismatch: added=%v removed=%v", added, removed)
	}
}

func TestRunShadowGame_CleanupOnStreamError(t *testing.T) {
	mock := &fakeMock{}
	coord := &fakeCoord{
		gameID:    "game_err",
		events:    []*gcpb.GameEvent{ev("game_started")},
		streamErr: status.Error(codes.Unavailable, "boom"),
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	res, _ := h.runner.RunShadowGame(context.Background(), testPatternSpec(t, Spec{RunID: "runErr"}))
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
	coord := &fakeCoord{events: []*gcpb.GameEvent{ev("game_start_error")}}
	h := newHarness(t, mock, coord)
	defer h.stop()

	if _, err := h.runner.RunShadowGame(context.Background(), testPatternSpec(t, Spec{RunID: "runReject"})); err == nil {
		t.Fatal("expected error on rejected start")
	}
	added, removed, _, _ := mock.snapshot()
	if len(removed) != len(added) || len(added) == 0 {
		t.Errorf("cleanup on rejected start failed: added=%v removed=%v", added, removed)
	}
}

// UC: UC-046 A8
func TestRunShadowGame_PreemptedIsAborted(t *testing.T) {
	t.Run("UC-046/A8 a force-end the agent did not request is an abort, not a completed game", func(t *testing.T) {
		mock := &fakeMock{}
		preempt := ev(eventGameForceEnded)
		preempt.Data = map[string]string{"reason": "preempted_by_real_game"}
		coord := &fakeCoord{gameID: "game_pre", events: []*gcpb.GameEvent{ev("game_started"), preempt}}
		h := newHarness(t, mock, coord)
		defer h.stop()
		outcomes := make(chan Outcome, 1)
		h.runner.SetTerminalCallback(func(_ string, o Outcome) { outcomes <- o })

		spec := testPatternSpec(t, Spec{RunID: "runPre", ExperimentID: "exp", Arm: "control"})
		if _, err := h.runner.StartExperimentGame(context.Background(), context.Background(), spec); err != nil {
			t.Fatal(err)
		}
		if o := <-outcomes; o != OutcomeForceEnded {
			t.Fatalf("outcome = %s, want force_ended (released without a sample)", o)
		}
		if calls := coord.forceEndGameIDs(); len(calls) != 0 {
			t.Errorf("the agent force-ended a preempted game: %v", calls)
		}
	})
}

func TestRunShadowGame_ValidatesSpec(t *testing.T) {
	r := New(Config{}, nil)
	base := testPatternSpec(t, Spec{RunID: "x"})
	noName := base
	noName.RunID = ""
	noMode := base
	noMode.GameName = ""
	noPattern := Spec{RunID: "x", GameName: "JoustFFA", Players: 2}
	for i, spec := range []Spec{noName, noMode, noPattern} {
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
