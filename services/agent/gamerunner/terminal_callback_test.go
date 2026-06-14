package gamerunner

import (
	"context"
	"sync"
	"testing"
	"time"

	gcpb "github.com/joustmania/agent/gen/game_coordinator"
)

// captureTerminal records the (gameID, outcome) the runner reports.
type captureTerminal struct {
	mu     sync.Mutex
	gameID string
	out    Outcome
	fired  bool
}

func (c *captureTerminal) cb(gameID string, outcome Outcome) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.gameID = gameID
	c.out = outcome
	c.fired = true
}

func (c *captureTerminal) snapshot() (string, Outcome, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.gameID, c.out, c.fired
}

// TestStartExperimentGame_TerminalCallbackFiresOnError covers the #1014 backstop:
// a game that ends in game_error must invoke the terminal callback with the
// errored game's id and OutcomeError, so the registry can release the in-flight
// slot directly (the telemetry-independent deadlock backstop).
func TestStartExperimentGame_TerminalCallbackFiresOnError(t *testing.T) {
	mock := &fakeMock{}
	coord := &fakeCoord{
		gameID: "game_err_001",
		events: []*gcpb.GameEvent{ev("game_starting"), ev("game_error")},
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	cap := &captureTerminal{}
	h.runner.SetTerminalCallback(cap.cb)

	driveCtx, cancel := context.WithCancel(context.Background())
	defer cancel()

	spec := Spec{
		RunID: "r", GameName: "JoustFFA", Players: 2, Sensitivity: 2,
		ExperimentID: "exp_x", Arm: "experimental",
	}
	gameID, err := h.runner.StartExperimentGame(context.Background(), driveCtx, spec)
	if err != nil {
		t.Fatalf("StartExperimentGame error: %v", err)
	}

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if _, _, fired := cap.snapshot(); fired {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	gotID, gotOut, fired := cap.snapshot()
	if !fired {
		t.Fatal("terminal callback never fired for a game_error game")
	}
	if gotID != gameID {
		t.Errorf("callback game_id = %q, want %q", gotID, gameID)
	}
	if gotOut != OutcomeError {
		t.Errorf("callback outcome = %q, want %q", gotOut, OutcomeError)
	}
}

// TestStartExperimentGame_TerminalCallbackFiresOnComplete confirms the callback
// ALSO fires for a naturally completed game (with OutcomeCompleted) — the loop
// uses the outcome to decide release-vs-conclude, so it must always be informed.
func TestStartExperimentGame_TerminalCallbackFiresOnComplete(t *testing.T) {
	mock := &fakeMock{}
	coord := &fakeCoord{
		gameID: "game_ok_001",
		events: []*gcpb.GameEvent{ev("game_starting"), ev("game_ended")},
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	cap := &captureTerminal{}
	h.runner.SetTerminalCallback(cap.cb)

	driveCtx, cancel := context.WithCancel(context.Background())
	defer cancel()

	spec := Spec{
		RunID: "r", GameName: "JoustFFA", Players: 2, Sensitivity: 2,
		ExperimentID: "exp_x", Arm: "control",
	}
	if _, err := h.runner.StartExperimentGame(context.Background(), driveCtx, spec); err != nil {
		t.Fatalf("StartExperimentGame error: %v", err)
	}

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if _, _, fired := cap.snapshot(); fired {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	_, gotOut, fired := cap.snapshot()
	if !fired {
		t.Fatal("terminal callback never fired for a completed game")
	}
	if gotOut != OutcomeCompleted {
		t.Errorf("callback outcome = %q, want %q", gotOut, OutcomeCompleted)
	}
}
