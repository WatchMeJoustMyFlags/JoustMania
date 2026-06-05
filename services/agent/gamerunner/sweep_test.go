package gamerunner

import (
	"context"
	"testing"

	mockpb "github.com/joustmania/agent/gen/controller_manager_mock"
	gcpb "github.com/joustmania/agent/gen/game_coordinator"
)

func TestSweepOrphans_RemovesUnreferencedReserved(t *testing.T) {
	// Three reserved+agent-tagged controllers exist; one (live-1) is still
	// referenced by a live game, the other two are orphans. A fourth controller
	// is reserved under a DIFFERENT prefix and must be left untouched.
	mock := &fakeMock{
		list: func() (*mockpb.ListResponse, error) {
			return &mockpb.ListResponse{
				Controllers: []*mockpb.MockControllerInfo{
					{Serial: "orphan-1", Reserved: true, Tag: "agent:dead-run"},
					{Serial: "orphan-2", Reserved: true, Tag: "agent:dead-run"},
					{Serial: "live-1", Reserved: true, Tag: "agent:live-run"},
					{Serial: "other-1", Reserved: true, Tag: "tester:foo"},
					{Serial: "lobby-1", Reserved: false, Tag: ""},
				},
			}, nil
		},
	}
	coord := &fakeCoord{
		listGames: func() (*gcpb.ListGamesResponse, error) {
			return &gcpb.ListGamesResponse{
				Success: true,
				Games: []*gcpb.GameInfo{
					{GameId: "g1", Players: []*gcpb.PlayerInfo{{Serial: "live-1"}}},
				},
			}, nil
		},
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	res, err := h.runner.SweepOrphans(context.Background(), "agent:")
	if err != nil {
		t.Fatalf("SweepOrphans error: %v", err)
	}

	if !contains(res.Removed, "orphan-1") || !contains(res.Removed, "orphan-2") {
		t.Errorf("expected orphan-1 and orphan-2 removed, got %v", res.Removed)
	}
	if len(res.Removed) != 2 {
		t.Errorf("removed %v, want exactly [orphan-1 orphan-2]", res.Removed)
	}
	if !contains(res.Live, "live-1") {
		t.Errorf("live-1 should be protected, Live=%v", res.Live)
	}

	_, removed, _, _ := mock.snapshot()
	if !contains(removed, "orphan-1") || !contains(removed, "orphan-2") {
		t.Errorf("RemoveController not called for orphans: %v", removed)
	}
	// Differently-prefixed and live/unreserved controllers must not be removed.
	for _, protected := range []string{"live-1", "other-1", "lobby-1"} {
		if contains(removed, protected) {
			t.Errorf("protected controller %q was removed", protected)
		}
	}
}

func TestSweepOrphans_RejectsEmptyPrefix(t *testing.T) {
	mock := &fakeMock{}
	coord := &fakeCoord{}
	h := newHarness(t, mock, coord)
	defer h.stop()

	if _, err := h.runner.SweepOrphans(context.Background(), ""); err == nil {
		t.Fatal("expected error for empty tagPrefix")
	}
}

func TestSweepOrphans_NoOrphans(t *testing.T) {
	mock := &fakeMock{
		list: func() (*mockpb.ListResponse, error) {
			return &mockpb.ListResponse{}, nil
		},
	}
	coord := &fakeCoord{}
	h := newHarness(t, mock, coord)
	defer h.stop()

	res, err := h.runner.SweepOrphans(context.Background(), "agent:")
	if err != nil {
		t.Fatalf("SweepOrphans error: %v", err)
	}
	if len(res.Removed) != 0 {
		t.Errorf("expected no removals, got %v", res.Removed)
	}
}
