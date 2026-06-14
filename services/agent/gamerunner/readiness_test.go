package gamerunner

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	mockpb "github.com/joustmania/agent/gen/controller_manager_mock"
	gcpb "github.com/joustmania/agent/gen/game_coordinator"
)

// TestReserveControllers_WaitsForDelayedRegistration covers the #1013 readiness
// gate: AddControllers returns serials, but the controllers only become VISIBLE
// to ListMockControllers after a delay. reserveControllers (via the start path)
// must WAIT until all spec.Players serials are present before the game starts, so
// the spawned game reliably fields all spec.Players controllers and FFA never
// aborts with "Need at least 2 players".
func TestReserveControllers_WaitsForDelayedRegistration(t *testing.T) {
	const want = 2
	serials := []string{"MOCK0000", "MOCK0001"}

	var mu sync.Mutex
	visible := map[string]bool{} // serials registration has propagated for

	mock := &fakeMock{
		addControllers: func(_ *mockpb.AddControllersRequest) (*mockpb.AddControllersResponse, error) {
			// Reservation succeeds immediately, but only the FIRST serial is visible
			// right away; the second propagates after a short delay (simulating the
			// controller-manager registration lag #1013 describes).
			mu.Lock()
			visible[serials[0]] = true
			mu.Unlock()
			go func() {
				time.Sleep(120 * time.Millisecond)
				mu.Lock()
				visible[serials[1]] = true
				mu.Unlock()
			}()
			return &mockpb.AddControllersResponse{Success: true, Serials: serials}, nil
		},
		list: func() (*mockpb.ListResponse, error) {
			mu.Lock()
			defer mu.Unlock()
			var present []string
			for _, s := range serials {
				if visible[s] {
					present = append(present, s)
				}
			}
			return &mockpb.ListResponse{Serials: present, Count: int32(len(present))}, nil
		},
	}
	coord := &fakeCoord{
		gameID: "game_ready_001",
		events: []*gcpb.GameEvent{ev("game_starting"), ev("game_ended")},
	}
	h := newHarness(t, mock, coord)
	defer h.stop()
	h.runner.cfg.ReadyTimeout = 2 * time.Second
	h.runner.cfg.ReadyPollInterval = 20 * time.Millisecond

	spec := Spec{RunID: "r", GameName: "JoustFFA", Players: want, Sensitivity: 2}

	cl, err := h.runner.connect()
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer cl.close()

	start := time.Now()
	got, err := h.runner.reserveControllers(context.Background(), cl, spec)
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("reserveControllers error: %v", err)
	}
	if len(got) != want {
		t.Fatalf("reserved %d serials, want %d", len(got), want)
	}
	// It must have BLOCKED for the propagation delay rather than starting early.
	if elapsed < 100*time.Millisecond {
		t.Fatalf("reserveControllers returned in %s — it did not wait for delayed registration", elapsed)
	}
	// And by the time it returns, every reserved serial must be visible.
	missing := h.runner.missingControllers(context.Background(), cl, got)
	if len(missing) != 0 {
		t.Fatalf("controllers still not visible after reserve: %v", missing)
	}
}

// TestReserveControllers_RejectsDuplicateSerials covers the #1013 root-cause
// guard: when AddControllers hands back a DUPLICATE serial (the len()-based mock
// naming colliding with a prior game's not-yet-removed controller), the runner
// must refuse to start an under-populated game rather than let the serial-keyed
// roster collapse two players into one (the FFA "got 1" abort).
func TestReserveControllers_RejectsDuplicateSerials(t *testing.T) {
	mock := &fakeMock{
		addControllers: func(_ *mockpb.AddControllersRequest) (*mockpb.AddControllersResponse, error) {
			// count=2 but both serials identical (the collision bug).
			return &mockpb.AddControllersResponse{
				Success: true,
				Serials: []string{"MOCK0001", "MOCK0001"},
			}, nil
		},
	}
	coord := &fakeCoord{gameID: "g"}
	h := newHarness(t, mock, coord)
	defer h.stop()

	cl, err := h.runner.connect()
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer cl.close()

	_, err = h.runner.reserveControllers(context.Background(), cl,
		Spec{RunID: "r", GameName: "JoustFFA", Players: 2})
	if err == nil {
		t.Fatal("expected an error for duplicate reserved serials, got nil")
	}
	if !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("error = %q, want it to mention the duplicate serial", err)
	}
}

// TestReserveControllers_ReadyTimeout covers the failure path: if the reserved
// controllers never become visible, reserveControllers fails (rather than starting
// an under-populated game) once ReadyTimeout elapses.
func TestReserveControllers_ReadyTimeout(t *testing.T) {
	mock := &fakeMock{
		addControllers: func(_ *mockpb.AddControllersRequest) (*mockpb.AddControllersResponse, error) {
			return &mockpb.AddControllersResponse{Success: true, Serials: []string{"A", "B"}}, nil
		},
		// Never reports the reserved controllers as present.
		list: func() (*mockpb.ListResponse, error) {
			return &mockpb.ListResponse{}, nil
		},
	}
	coord := &fakeCoord{gameID: "g"}
	h := newHarness(t, mock, coord)
	defer h.stop()
	h.runner.cfg.ReadyTimeout = 80 * time.Millisecond
	h.runner.cfg.ReadyPollInterval = 20 * time.Millisecond

	cl, err := h.runner.connect()
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer cl.close()

	_, err = h.runner.reserveControllers(context.Background(), cl,
		Spec{RunID: "r", GameName: "JoustFFA", Players: 2})
	if err == nil {
		t.Fatal("expected a readiness-timeout error, got nil")
	}
	if !strings.Contains(err.Error(), "not ready") {
		t.Fatalf("error = %q, want a readiness-timeout error", err)
	}
}
