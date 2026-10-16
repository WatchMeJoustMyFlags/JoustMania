package gamerunner

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	mockpb "github.com/joustmania/agent/gen/controller_manager_mock"
	gcpb "github.com/joustmania/agent/gen/game_coordinator"
	"github.com/joustmania/agent/patterngame"
)

func testArchetype(t *testing.T) patterngame.Archetype {
	t.Helper()
	a, errs := patterngame.NewArchetype("unit_cascade", map[string]any{
		"players": 3.0, "stream_rate_hz": 30.0, "base": 1.0, "drift": 0.01, "amp": 0.1,
		"period_s": 3.0, "phase": 0.0, "burst_h": 0.0, "burst_period_s": 0.0,
		"d_base": 0.0, "d_drift": 0.01, "d_amp": 0.0, "d_phase": 0.25,
	})
	if len(errs) > 0 {
		t.Fatalf("archetype: %v", errs)
	}
	return a
}

// patternMock answers AddControllers with the archetype echo and RemoveController
// with a replay record whose frame counts give the elimination order 2, 1, winner 0.
func patternMock(a patterngame.Archetype, frames map[int]uint64, overrunSlot int) *fakeMock {
	m := &fakeMock{}
	var mu sync.Mutex
	slotOf := map[string]int{}
	m.addControllers = func(req *mockpb.AddControllersRequest) (*mockpb.AddControllersResponse, error) {
		serials := make([]string, req.GetCount())
		mu.Lock()
		for i := range serials {
			serials[i] = fmt.Sprintf("MOCK%04d", i)
			slotOf[serials[i]] = i
		}
		mu.Unlock()
		m.mu.Lock()
		m.added = append(m.added, serials...)
		m.mu.Unlock()
		return &mockpb.AddControllersResponse{
			Success: true, Serials: serials, Archetype: req.GetArchetype(),
			StreamRateHz: int32(a.StreamRateHz), Players: int32(a.Players),
		}, nil
	}
	m.removeResponse = func(serial string) *mockpb.RemoveControllerResponse {
		mu.Lock()
		slot := slotOf[serial]
		mu.Unlock()
		overruns := uint32(0)
		if slot == overrunSlot {
			overruns = 2
		}
		return &mockpb.RemoveControllerResponse{
			Success: true, Archetype: a.Name, PatternSlot: int32(slot),
			FramesDelivered: frames[slot], SignalHash: uint64(1000 + slot),
			SignalCheckpoints: []string{fmt.Sprintf("cp%d", slot)}, FrameOverruns: overruns,
		}
	}
	return m
}

func patternSpec(a patterngame.Archetype) Spec {
	return Spec{
		RunID: "run-pat", GameName: "JoustFFA", Players: 2, Sensitivity: 2,
		Pattern: a, MaxGameSeconds: 60, Seed: 42, FlagDigest: "digest", PairIndex: -1,
	}
}

// UC: UC-046 main BR-004 BR-006 BR-014
func TestRunShadowGame_PatternGameEndsWithWinner(t *testing.T) {
	t.Run("UC-046/main archetype passed, no scripted drive, replay record built", func(t *testing.T) {
		a := testArchetype(t)
		mock := patternMock(a, map[int]uint64{0: 900, 1: 600, 2: 300}, -1)
		// The winner event names slot 0; slot 1 died last and ties it on frames.
		mock = patternMock(a, map[int]uint64{0: 900, 1: 900, 2: 300}, -1)
		winnerEv := ev("game_winner")
		winnerEv.Data = map[string]string{"serial": "MOCK0000"}
		coord := &fakeCoord{gameID: "game_pat_1", events: []*gcpb.GameEvent{ev("game_starting"), winnerEv, ev("game_ended")}}
		h := newHarness(t, mock, coord)
		defer h.stop()
		tracker := patterngame.NewTracker()
		h.runner.SetTracker(tracker)
		var started patterngame.Info
		h.runner.SetPatternStartCallback(func(info patterngame.Info) { started = info })

		res, err := h.runner.RunShadowGame(context.Background(), patternSpec(a))
		if err != nil {
			t.Fatalf("RunShadowGame: %v", err)
		}
		mock.mu.Lock()
		add := mock.lastAdd
		mock.mu.Unlock()
		if add.GetArchetype() != a.Name || add.GetArchetypeParams() != a.ParamsJSON || add.GetCount() != 3 {
			t.Fatalf("AddControllers = %q/%q/%d, want archetype, params, players 3", add.GetArchetype(), add.GetArchetypeParams(), add.GetCount())
		}
		_, removed, deaths, movements := mock.snapshot()
		if len(deaths) != 0 || movements != 0 {
			t.Fatalf("pattern game was driven: deaths=%v movements=%d (UC-046 BR-004)", deaths, movements)
		}
		if len(removed) != 3 {
			t.Fatalf("removed %v, want 3 controllers", removed)
		}
		if got := coord.capturedStartConfig().GetRngSeed(); got != 42 {
			t.Errorf("one-shot pattern game rng_seed = %d, want 42", got)
		}
		if started.GameID != "game_pat_1" || started.Archetype.Name != a.Name {
			t.Errorf("start callback = %+v", started)
		}
		rec := res.Pattern
		if rec == nil {
			t.Fatal("no replay record")
		}
		if rec.Outcome != patterngame.OutcomeWinner || rec.WinnerSlot != 0 {
			t.Errorf("outcome/winner = %s/%d, want winner/0", rec.Outcome, rec.WinnerSlot)
		}
		if fmt.Sprint(rec.EliminationOrder) != "[2 1]" {
			t.Errorf("elimination order = %v, want [2 1] (winner from game_winner, BR-014)", rec.EliminationOrder)
		}
		if !rec.Exact() {
			t.Errorf("non-exact %v, want exact", rec.NonExactReasons)
		}
		if tracker.Active("game_pat_1") {
			t.Error("game still tracked after finish")
		}
	})
}

// UC: UC-046 A3 BR-013
func TestRunShadowGame_PatternSafetyCapIsTimeout(t *testing.T) {
	t.Run("UC-046/A3 cap reached gives a valid timeout, survivors left out, still exact", func(t *testing.T) {
		a := testArchetype(t)
		// slot 2 died early; slots 0 and 1 were alive when the cap force-ended the game.
		mock := patternMock(a, map[int]uint64{0: 1800, 1: 1795, 2: 400}, -1)
		coord := &fakeCoord{gameID: "game_pat_2", events: []*gcpb.GameEvent{ev("game_starting")}, emitTerminalOnForceEnd: "game_force_ended"}
		h := newHarness(t, mock, coord)
		defer h.stop()
		h.runner.SetTracker(patterngame.NewTracker())
		h.runner.capUnit = time.Millisecond // 60 "s" cap = 60 ms

		res, err := h.runner.RunShadowGame(context.Background(), patternSpec(a))
		if err != nil {
			t.Fatalf("RunShadowGame: %v", err)
		}
		if res.Outcome != OutcomeTimeout {
			t.Fatalf("outcome = %s, want timeout", res.Outcome)
		}
		if ids := coord.forceEndGameIDs(); len(ids) != 1 {
			t.Fatalf("force-end calls = %v, want one", ids)
		}
		rec := res.Pattern
		if rec.Outcome != patterngame.OutcomeTimeout || rec.WinnerSlot != -1 {
			t.Errorf("outcome/winner = %s/%d, want timeout/-1", rec.Outcome, rec.WinnerSlot)
		}
		if fmt.Sprint(rec.EliminationOrder) != "[2]" {
			t.Errorf("elimination order = %v, want [2] (survivors left out)", rec.EliminationOrder)
		}
		if !rec.Exact() {
			t.Errorf("a timeout alone made the game non-exact: %v", rec.NonExactReasons)
		}
	})
}

// UC: UC-046 A10 A12 BR-008
func TestStartExperimentGame_PatternAbortAndOverrun(t *testing.T) {
	t.Run("UC-046/A12 kill switch force-ends and marks kill_switch; A10 overrun marks frame_overrun", func(t *testing.T) {
		a := testArchetype(t)
		mock := patternMock(a, map[int]uint64{0: 300, 1: 300, 2: 300}, 1)
		coord := &fakeCoord{gameID: "game_pat_3", events: []*gcpb.GameEvent{ev("game_starting")}, emitTerminalOnForceEnd: "game_force_ended"}
		h := newHarness(t, mock, coord)
		defer h.stop()
		tracker := patterngame.NewTracker()
		h.runner.SetTracker(tracker)
		done := make(chan patterngame.Record, 1)
		outcomes := make(chan Outcome, 1)
		h.runner.SetPatternCallback(func(rec patterngame.Record) { done <- rec })
		h.runner.SetTerminalCallback(func(_ string, o Outcome) { outcomes <- o })

		spec := patternSpec(a)
		spec.ExperimentID, spec.Arm, spec.PairIndex = "exp_x", "control", 0
		gameID, err := h.runner.StartExperimentGame(context.Background(), context.Background(), spec)
		if err != nil {
			t.Fatalf("StartExperimentGame: %v", err)
		}
		deadline := time.After(2 * time.Second)
		for !tracker.Active(gameID) {
			select {
			case <-deadline:
				t.Fatal("game never registered")
			case <-time.After(time.Millisecond):
			}
		}
		tracker.Abort(gameID, patterngame.ReasonKillSwitch)
		rec := <-done
		if o := <-outcomes; o != OutcomeForceEnded {
			t.Errorf("terminal outcome = %s, want force_ended (aborted)", o)
		}
		if rec.Outcome != patterngame.OutcomeAborted {
			t.Errorf("record outcome = %s, want aborted", rec.Outcome)
		}
		got := strings.Join(rec.NonExactReasons, ",")
		if got != "frame_overrun,kill_switch" {
			t.Errorf("non-exact reasons = %q, want frame_overrun,kill_switch", got)
		}
		if rec.ExperimentID != "exp_x" || rec.PairIndex != 0 {
			t.Errorf("record experiment = %s/%d", rec.ExperimentID, rec.PairIndex)
		}
	})
}

// UC: UC-046 A13
func TestRunShadowGame_PatternRejectsUnsupportedMode(t *testing.T) {
	t.Run("UC-046/A13 a pattern game in a mode other than JoustFFA is refused", func(t *testing.T) {
		a := testArchetype(t)
		h := newHarness(t, patternMock(a, nil, -1), &fakeCoord{gameID: "g"})
		defer h.stop()
		spec := patternSpec(a)
		spec.GameName = "JoustTeams"
		_, err := h.runner.RunShadowGame(context.Background(), spec)
		if err == nil || !strings.Contains(err.Error(), patterngame.DiscardModeUnsupported) {
			t.Fatalf("err = %v, want %s", err, patterngame.DiscardModeUnsupported)
		}
	})
}
