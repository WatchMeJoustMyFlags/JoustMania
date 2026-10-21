package gamerunner

import (
	"context"
	"fmt"
	"testing"
	"time"

	gcpb "github.com/joustmania/agent/gen/game_coordinator"
	"github.com/joustmania/agent/patterngame"
)

// terminalRun is what one experiment pattern game reported when it ended.
type terminalRun struct {
	outcome  Outcome            // the TerminalCallback outcome (drives release vs sample)
	record   patterngame.Record // the replay record (pattern.outcome)
	elapsed  time.Duration      // spawn to terminal callback
	forceEnd []string           // ForceEndGame calls the runner made
	reasons  []string           // the reasons of those calls
}

// endedEv builds a game_ended event with the given payload (games/ffa.py).
func endedEv(data map[string]string) *gcpb.GameEvent {
	e := ev(eventGameEnded)
	e.Data = data
	return e
}

// forceEndedEv builds a game_force_ended event with a reason (game_session.py).
func forceEndedEv(reason string) *gcpb.GameEvent {
	e := ev(eventGameForceEnded)
	e.Data = map[string]string{"reason": reason}
	return e
}

// runTerminal spawns one experiment pattern game against coord and waits for its
// terminal callback. setup may adjust the runner; act runs once the game is
// registered (e.g. to fire the kill switch).
func runTerminal(t *testing.T, coord *fakeCoord, maxGameSeconds int, setup func(*Runner), act func(tracker *patterngame.Tracker, gameID string)) terminalRun {
	t.Helper()
	a := testArchetype(t)
	h := newHarness(t, patternMock(a, map[int]uint64{0: 300, 1: 300, 2: 300}, -1), coord)
	t.Cleanup(h.stop)
	tracker := patterngame.NewTracker()
	h.runner.SetTracker(tracker)
	if setup != nil {
		setup(h.runner)
	}
	records := make(chan patterngame.Record, 1)
	outcomes := make(chan Outcome, 1)
	h.runner.SetPatternCallback(func(rec patterngame.Record) { records <- rec })
	h.runner.SetTerminalCallback(func(_ string, o Outcome) { outcomes <- o })

	spec := patternSpec(a)
	spec.ExperimentID, spec.Arm, spec.PairIndex = "exp_1403", "control", 0
	spec.MaxGameSeconds = maxGameSeconds
	start := time.Now()
	gameID, err := h.runner.StartExperimentGame(context.Background(), context.Background(), spec)
	if err != nil {
		t.Fatalf("StartExperimentGame: %v", err)
	}
	if act != nil {
		deadline := time.After(2 * time.Second)
		for !tracker.Active(gameID) {
			select {
			case <-deadline:
				t.Fatal("game never registered")
			case <-time.After(time.Millisecond):
			}
		}
		act(tracker, gameID)
	}
	var run terminalRun
	select {
	case run.record = <-records:
	case <-time.After(10 * time.Second):
		t.Fatal("no replay record")
	}
	run.outcome = <-outcomes
	run.elapsed = time.Since(start)
	// The runner reports one terminal outcome per game (UC-046 BR-016): the agent's
	// end join (#1405) folds or releases on it exactly once.
	select {
	case again := <-outcomes:
		t.Fatalf("second terminal callback %s after %s", again, run.outcome)
	case <-time.After(20 * time.Millisecond):
	}
	run.forceEnd = coord.forceEndGameIDs()
	run.reasons = coord.forceEndReasons()
	return run
}

// UC: UC-046 main A8 A9 A3 A12 A14 BR-013 BR-014 BR-015 BR-016
func TestAwaitTerminal_OutcomeClassification(t *testing.T) {
	t.Run("UC-046/A9 an operator force-end the agent did not request is released without a sample and recorded aborted", func(t *testing.T) {
		coord := &fakeCoord{gameID: "game_op", events: []*gcpb.GameEvent{
			ev("game_starting"), death("MOCK0002"),
			endedEv(map[string]string{"game_id": "game_op", "outcome": "force_ended", "reason": "admin_request"}),
			forceEndedEv("admin_request"),
		}}
		run := runTerminal(t, coord, 100, nil, nil)
		if run.outcome != OutcomeForceEnded {
			t.Fatalf("terminal outcome = %s, want force_ended (released, pair reruns on the 3-rerun budget)", run.outcome)
		}
		if run.record.Outcome != patterngame.OutcomeAborted || run.record.WinnerSlot != -1 {
			t.Errorf("record = %s/winner %d, want aborted/-1", run.record.Outcome, run.record.WinnerSlot)
		}
		if len(run.forceEnd) != 0 {
			t.Errorf("the agent force-ended an operator-ended game: %v", run.forceEnd)
		}
	})

	t.Run("UC-046/A8 preempted: game_ended force_ended then game_force_ended is released without a sample", func(t *testing.T) {
		coord := &fakeCoord{gameID: "game_pre", events: []*gcpb.GameEvent{
			ev("game_starting"), death("MOCK0002"),
			endedEv(map[string]string{"game_id": "game_pre", "outcome": "force_ended", "reason": "preempted_by_real_game"}),
			forceEndedEv("preempted_by_real_game"),
		}}
		run := runTerminal(t, coord, 100, nil, nil)
		if run.outcome != OutcomePreempted {
			t.Fatalf("terminal outcome = %s, want preempted (released, no sample, pair reruns off-budget)", run.outcome)
		}
		if run.record.Outcome != patterngame.OutcomeAborted || run.record.WinnerSlot != -1 {
			t.Errorf("record = %s/winner %d, want aborted/-1", run.record.Outcome, run.record.WinnerSlot)
		}
		if len(run.forceEnd) != 0 {
			t.Errorf("the agent force-ended a preempted game: %v", run.forceEnd)
		}
	})

	t.Run("UC-046/A8 game_ended without outcome followed by game_force_ended is released without a sample", func(t *testing.T) {
		coord := &fakeCoord{gameID: "game_old", events: []*gcpb.GameEvent{
			ev("game_starting"),
			endedEv(map[string]string{"game_id": "game_old", "duration": "4.2"}),
			forceEndedEv("preempted_by_real_game"),
		}}
		run := runTerminal(t, coord, 100, nil, nil)
		if run.outcome != OutcomePreempted {
			t.Fatalf("terminal outcome = %s, want preempted", run.outcome)
		}
		if run.record.Outcome != patterngame.OutcomeAborted {
			t.Errorf("record outcome = %s, want aborted", run.record.Outcome)
		}
	})

	t.Run("UC-046/BR-016 game_ended without outcome and no game_force_ended completes after the grace", func(t *testing.T) {
		winner := ev("game_winner")
		winner.Data = map[string]string{"serial": "MOCK0000"}
		coord := &fakeCoord{gameID: "game_old_win", events: []*gcpb.GameEvent{
			ev("game_starting"), death("MOCK0002"), death("MOCK0001"), winner,
			endedEv(map[string]string{"game_id": "game_old_win"}),
		}}
		run := runTerminal(t, coord, 100, nil, nil)
		if run.outcome != OutcomeCompleted {
			t.Fatalf("terminal outcome = %s, want completed", run.outcome)
		}
		if run.record.Outcome != patterngame.OutcomeWinner || run.record.WinnerSlot != 0 {
			t.Errorf("record = %s/winner %d, want winner/0", run.record.Outcome, run.record.WinnerSlot)
		}
		if run.elapsed < 200*time.Millisecond {
			t.Errorf("classified after %s, want the %s wait for a game_force_ended", run.elapsed, 200*time.Millisecond)
		}
	})

	t.Run("UC-046/A12 agent-requested force-end (kill switch) stays aborted", func(t *testing.T) {
		coord := &fakeCoord{gameID: "game_kill", events: []*gcpb.GameEvent{ev("game_starting")},
			forceEndEvents: []*gcpb.GameEvent{
				endedEv(map[string]string{"game_id": "game_kill", "outcome": "force_ended", "reason": killSwitchForceEndReason}),
				forceEndedEv(killSwitchForceEndReason),
			}}
		run := runTerminal(t, coord, 100, nil, func(tr *patterngame.Tracker, id string) {
			tr.Abort(id, patterngame.ReasonKillSwitch)
		})
		if run.outcome != OutcomeForceEnded || run.record.Outcome != patterngame.OutcomeAborted {
			t.Fatalf("outcome = %s / record %s, want force_ended / aborted", run.outcome, run.record.Outcome)
		}
		if len(run.forceEnd) != 1 {
			t.Errorf("ForceEndGame calls = %v, want exactly one (the kill switch)", run.forceEnd)
		}
		if len(run.reasons) != 1 || run.reasons[0] != "pattern_kill_switch" {
			t.Errorf("ForceEndGame reasons = %v, want [pattern_kill_switch] (not the cap's shadow_game_timeout)", run.reasons)
		}
	})

	t.Run("UC-046/A3 cap timeout: game_ended force_ended with the agent's reason is a timeout sample", func(t *testing.T) {
		coord := &fakeCoord{gameID: "game_cap", events: []*gcpb.GameEvent{ev("game_starting"), death("MOCK0002")},
			forceEndEvents: []*gcpb.GameEvent{
				endedEv(map[string]string{"game_id": "game_cap", "outcome": "force_ended", "reason": agentForceEndReason}),
				forceEndedEv(agentForceEndReason),
			}}
		run := runTerminal(t, coord, 30, nil, nil) // 30 units = 600 ms
		if run.outcome != OutcomeTimeout {
			t.Fatalf("terminal outcome = %s, want timeout (BR-013: a valid, censored sample)", run.outcome)
		}
		if run.record.Outcome != patterngame.OutcomeTimeout || !run.record.Censored || run.record.WinnerSlot != -1 {
			t.Errorf("record = %s censored=%v winner %d, want timeout/true/-1", run.record.Outcome, run.record.Censored, run.record.WinnerSlot)
		}
		if len(run.reasons) != 1 || run.reasons[0] != "shadow_game_timeout" {
			t.Errorf("ForceEndGame reasons = %v, want [shadow_game_timeout]", run.reasons)
		}
	})

	t.Run("UC-046/A14 the agent's own end_game is a censored sample, not an abort", func(t *testing.T) {
		coord := &fakeCoord{gameID: "game_end", events: []*gcpb.GameEvent{
			ev("game_starting"), death("MOCK0002"),
			endedEv(map[string]string{"game_id": "game_end", "outcome": "force_ended", "reason": "agent_intervention"}),
			forceEndedEv("agent_intervention"),
		}}
		run := runTerminal(t, coord, 100, nil, nil)
		if run.outcome != OutcomeAgentEnded {
			t.Fatalf("terminal outcome = %s, want agent_ended (kept as a sample)", run.outcome)
		}
		if run.record.Outcome != patterngame.OutcomeAgentEnded || !run.record.Censored || run.record.WinnerSlot != -1 {
			t.Errorf("record = %s censored=%v winner %d, want agent_ended/true/-1", run.record.Outcome, run.record.Censored, run.record.WinnerSlot)
		}
		if fmt.Sprint(run.record.EliminationOrder) != "[2]" {
			t.Errorf("elimination order = %v, want [2] (the players who died before the end)", run.record.EliminationOrder)
		}
		if len(run.forceEnd) != 0 {
			t.Errorf("the runner force-ended a game the agent's end_game ended: %v", run.forceEnd)
		}
	})

	t.Run("UC-046/A14 an outcome-less game_ended followed by the agent's end_game force-end is a censored sample", func(t *testing.T) {
		coord := &fakeCoord{gameID: "game_end_old", events: []*gcpb.GameEvent{
			ev("game_starting"),
			endedEv(map[string]string{"game_id": "game_end_old"}),
			forceEndedEv("agent_intervention"),
		}}
		run := runTerminal(t, coord, 100, nil, nil)
		if run.outcome != OutcomeAgentEnded || !run.record.Censored {
			t.Fatalf("outcome = %s censored=%v, want agent_ended/true", run.outcome, run.record.Censored)
		}
	})

	t.Run("UC-046/main game_ended with the base outcome completed is a sample, classified at once", func(t *testing.T) {
		coord := &fakeCoord{gameID: "game_done", events: []*gcpb.GameEvent{
			ev("game_starting"), death("MOCK0002"),
			endedEv(map[string]string{"game_id": "game_done", "outcome": "completed"}),
		}}
		run := runTerminal(t, coord, 100, func(r *Runner) { r.endedGrace = time.Minute }, nil)
		if run.outcome != OutcomeCompleted {
			t.Fatalf("terminal outcome = %s, want completed", run.outcome)
		}
		if run.record.Censored || run.record.Outcome != patterngame.OutcomeWinner {
			t.Errorf("record = %s censored=%v, want winner/false", run.record.Outcome, run.record.Censored)
		}
		if run.elapsed > 5*time.Second {
			t.Errorf("classified after %s: an outcome-bearing game_ended must not wait", run.elapsed)
		}
		if len(run.forceEnd) != 0 {
			t.Errorf("ForceEndGame calls = %v, want none", run.forceEnd)
		}
	})

	t.Run("UC-046/A8 cap force-end that lost the race to a preemption is released without a sample", func(t *testing.T) {
		coord := &fakeCoord{gameID: "game_race", events: []*gcpb.GameEvent{ev("game_starting")},
			forceEndEvents: []*gcpb.GameEvent{
				endedEv(map[string]string{"game_id": "game_race", "outcome": "force_ended", "reason": "preempted_by_real_game"}),
			}}
		run := runTerminal(t, coord, 30, nil, nil)
		if run.outcome != OutcomePreempted {
			t.Fatalf("terminal outcome = %s, want preempted (the coordinator names a foreign reason)", run.outcome)
		}
	})

	t.Run("UC-046/main natural winner is a sample even when a preemption follows the frozen outcome", func(t *testing.T) {
		winner := ev("game_winner")
		winner.Data = map[string]string{"serial": "MOCK0000"}
		coord := &fakeCoord{gameID: "game_win", events: []*gcpb.GameEvent{
			ev("game_starting"), death("MOCK0002"), death("MOCK0001"), winner,
			endedEv(map[string]string{"game_id": "game_win", "outcome": "winner"}),
			forceEndedEv("preempted_by_real_game"),
		}}
		// A huge grace proves an outcome-bearing game_ended is classified at once.
		run := runTerminal(t, coord, 100, func(r *Runner) { r.endedGrace = time.Minute }, nil)
		if run.outcome != OutcomeCompleted {
			t.Fatalf("terminal outcome = %s, want completed", run.outcome)
		}
		if run.record.Outcome != patterngame.OutcomeWinner || run.record.WinnerSlot != 0 {
			t.Errorf("record = %s/winner %d, want winner/0", run.record.Outcome, run.record.WinnerSlot)
		}
		if run.elapsed > 5*time.Second {
			t.Errorf("classified after %s: an outcome-bearing game_ended must not wait", run.elapsed)
		}
	})

	t.Run("UC-046/BR-014 tie is a sample with no winner slot", func(t *testing.T) {
		coord := &fakeCoord{gameID: "game_tie", events: []*gcpb.GameEvent{
			ev("game_starting"), death("MOCK0002"), death("MOCK0000"), death("MOCK0001"), ev("game_tie"),
			endedEv(map[string]string{"game_id": "game_tie", "outcome": "tie"}),
		}}
		run := runTerminal(t, coord, 100, func(r *Runner) { r.endedGrace = time.Minute }, nil)
		if run.outcome != OutcomeCompleted {
			t.Fatalf("terminal outcome = %s, want completed", run.outcome)
		}
		if run.record.Outcome != patterngame.OutcomeTie || run.record.WinnerSlot != -1 {
			t.Errorf("record = %s/winner %d, want tie/-1", run.record.Outcome, run.record.WinnerSlot)
		}
	})
}

// UC: UC-046 A8 A9 A12 A14 BR-016
func TestClassifyTerminal(t *testing.T) {
	cases := []struct {
		name            string
		event           *gcpb.GameEvent
		forced, aborted bool
		want            Outcome
		wait            bool
	}{
		{"preempted game_ended", endedEv(map[string]string{"outcome": "force_ended", "reason": "preempted_by_real_game"}), false, false, OutcomePreempted, false},
		{"foreign game_ended, any reason", endedEv(map[string]string{"outcome": "force_ended", "reason": "shadow_game_timeout"}), false, false, OutcomeForceEnded, false},
		{"foreign operator force-end", forceEndedEv("admin_request"), false, false, OutcomeForceEnded, false},
		{"preempted game_force_ended", forceEndedEv("preempted_by_real_game"), false, false, OutcomePreempted, false},
		{"own cap force-end", endedEv(map[string]string{"outcome": "force_ended", "reason": agentForceEndReason}), true, false, OutcomeTimeout, false},
		{"own kill-switch force-end", forceEndedEv("pattern_kill_switch"), true, true, OutcomeForceEnded, false},
		{"own kill-switch force-end lost to a preemption", forceEndedEv("preempted_by_real_game"), true, true, OutcomeForceEnded, false},
		{"own kill-switch force-end lost to the agent's end_game", forceEndedEv("agent_intervention"), true, true, OutcomeForceEnded, false},
		{"own force-end without reason", ev(eventGameForceEnded), true, false, OutcomeTimeout, false},
		{"own cap request lost to a preemption", endedEv(map[string]string{"outcome": "force_ended", "reason": "preempted_by_real_game"}), true, false, OutcomePreempted, false},
		{"agent end_game game_ended", endedEv(map[string]string{"outcome": "force_ended", "reason": "agent_intervention"}), false, false, OutcomeAgentEnded, false},
		{"agent end_game game_force_ended", forceEndedEv("agent_intervention"), false, false, OutcomeAgentEnded, false},
		{"own cap request lost to the agent's end_game", endedEv(map[string]string{"outcome": "force_ended", "reason": "agent_intervention"}), true, false, OutcomeAgentEnded, false},
		{"winner", endedEv(map[string]string{"outcome": "winner"}), false, false, OutcomeCompleted, false},
		{"tie", endedEv(map[string]string{"outcome": "tie"}), false, false, OutcomeCompleted, false},
		{"base completed", endedEv(map[string]string{"outcome": "completed"}), false, false, OutcomeCompleted, false},
		{"no outcome waits", ev(eventGameEnded), false, false, OutcomeCompleted, true},
		{"error", ev(eventGameError), false, false, OutcomeError, false},
	}
	for _, c := range cases {
		t.Run("UC-046/BR-016 "+c.name, func(t *testing.T) {
			got, wait := classifyTerminal(c.event, c.forced, c.aborted)
			if got != c.want || wait != c.wait {
				t.Errorf("classifyTerminal = %s/wait=%v, want %s/wait=%v", got, wait, c.want, c.wait)
			}
		})
	}
}
