package gamerunner

import (
	"context"
	"errors"
	"testing"
	"time"

	gcpb "github.com/joustmania/agent/gen/game_coordinator"
)

// backpressure_test.go covers the #998 mapping: the game-coordinator's
// single-game cap rejects a concurrent start with a game_start_error event
// carrying "Game already in progress". gamerunner must surface that as
// ErrGameInProgress (errors.Is) so the experiment registry treats it as
// backpressure rather than a hard spawn failure.

// errEvent builds a game_start_error event with the given data map.
func errEvent(data map[string]string) *gcpb.GameEvent {
	return &gcpb.GameEvent{
		EventType: "game_start_error",
		Timestamp: time.Now().UnixMilli(),
		Data:      data,
	}
}

// A "Game already in progress" rejection maps to ErrGameInProgress so the caller
// can branch on backpressure.
func TestStartExperimentGame_InProgressRejectionIsBackpressure(t *testing.T) {
	mock := &fakeMock{}
	coord := &fakeCoord{
		// No gameID: the start-error event is read before any game_id is assigned.
		events: []*gcpb.GameEvent{
			errEvent(map[string]string{"error": "Game already in progress"}),
		},
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	spec := Spec{
		RunID: "run-bp", GameName: "JoustFFA", Players: 2, Sensitivity: 2,
		ExperimentID: "exp_bp", Arm: "experimental",
	}

	_, err := h.runner.StartExperimentGame(context.Background(), context.Background(), spec)
	if err == nil {
		t.Fatal("expected an error when the coordinator rejects with 'Game already in progress'")
	}
	if !errors.Is(err, ErrGameInProgress) {
		t.Fatalf("error %v should wrap ErrGameInProgress (backpressure)", err)
	}

	// No controller leak: the reserved controllers are removed on the rejection.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		_, removed, _, _ := mock.snapshot()
		if len(removed) == spec.Players {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if _, removed, _, _ := mock.snapshot(); len(removed) != spec.Players {
		t.Fatalf("in-progress rejection leaked controllers: removed %d, want %d", len(removed), spec.Players)
	}
}

// A genuine (non-in-progress) start rejection is NOT classified as backpressure —
// it stays a plain error so the registry still WARNs on it.
func TestStartExperimentGame_OtherRejectionIsNotBackpressure(t *testing.T) {
	mock := &fakeMock{}
	coord := &fakeCoord{
		events: []*gcpb.GameEvent{
			errEvent(map[string]string{"error": "Need at least 2 players"}),
		},
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	spec := Spec{RunID: "run-other", GameName: "JoustFFA", Players: 2}

	_, err := h.runner.StartExperimentGame(context.Background(), context.Background(), spec)
	if err == nil {
		t.Fatal("expected an error on a coordinator start rejection")
	}
	if errors.Is(err, ErrGameInProgress) {
		t.Fatalf("a non-in-progress rejection (%v) must NOT be classified as backpressure", err)
	}
}

// startRejectedInProgress matches the coordinator's phrase case-insensitively and
// across any data key/value, and rejects unrelated data.
func TestStartRejectedInProgress(t *testing.T) {
	cases := []struct {
		name string
		data map[string]string
		want bool
	}{
		{"canonical error key", map[string]string{"error": "Game already in progress"}, true},
		{"case insensitive", map[string]string{"error": "GAME ALREADY IN PROGRESS"}, true},
		{"other key", map[string]string{"reason": "game already in progress"}, true},
		{"unrelated", map[string]string{"error": "Need at least 2 players"}, false},
		{"empty", map[string]string{}, false},
		{"nil", nil, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := startRejectedInProgress(tc.data); got != tc.want {
				t.Fatalf("startRejectedInProgress(%v) = %v, want %v", tc.data, got, tc.want)
			}
		})
	}
}
