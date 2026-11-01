package jev

import (
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// stale_uc040_test.go covers #2514 in jev: a player marked readings_stale is no
// target candidate of a lever that needs fresh readings, still one of every other
// player lever, and is described as stale (UC-040 A50, BR-039).

// staleRequest is the lopsided fixture with MOCK0003's only reading 10 s old.
func staleRequest() decide.Request {
	c := lopsidedContext()
	c.Players["MOCK0003"].History = []gamecontext.MovementSample{{At: c.UpdatedAt.Add(-10 * time.Second), Intensity: 2.2}}
	st := decide.BuildState(c)
	return decide.Request{State: st, Questions: decide.FFAQuestions(st, fullInterventions)}
}

// targetOptions is the details-stage target question's options for iv (nil when
// the target is answered locally).
func targetOptions(req decide.Request, iv string) []string {
	for _, q := range detailsRequest(req, iv).ask.Questions {
		if q.ID == decide.QuestionFFATarget {
			return q.Options
		}
	}
	return nil
}

// UC: UC-040 A50 BR-039
func TestPlanDetails_ReadingsStale(t *testing.T) {
	req := staleRequest()
	t.Run("UC-040/BR-039 a fresh-readings lever does not offer the stale player", func(t *testing.T) {
		for _, iv := range decide.FreshReadingsLevers() {
			if got, want := targetOptions(req, iv), []string{"MOCK0002", "MOCK0004"}; !reflect.DeepEqual(got, want) {
				t.Errorf("%s target options = %v, want %v", iv, got, want)
			}
		}
	})
	t.Run("UC-040/BR-039 another player lever still offers it", func(t *testing.T) {
		if got, want := targetOptions(req, decide.IntvEliminatePlayer), []string{"MOCK0002", "MOCK0003", "MOCK0004"}; !reflect.DeepEqual(got, want) {
			t.Errorf("eliminate_player target options = %v, want %v", got, want)
		}
	})
	t.Run("UC-040/A50 the stale player is described as stale", func(t *testing.T) {
		d := describePlayers(req.State)["MOCK0003"]
		if !strings.Contains(d, "Player MOCK0003, still in the game. Its movement readings are stale") || strings.Contains(d, "Skill level") {
			t.Errorf("stale player description = %q", d)
		}
		if c := describeCandidatesFor(req.State, decide.IntvEliminatePlayer, decide.PromptProfileNeutral)["MOCK0003"]; !strings.Contains(c, "Its movement readings are stale") {
			t.Errorf("stale candidate description = %q", c)
		}
	})
}

// stage1Options is the options of stage 1's ffa.intervention question of req.
func stage1Options(t *testing.T, req decide.Request) []string {
	t.Helper()
	for _, q := range splitStages(req).first.Questions {
		if q.ID == decide.QuestionFFAIntervention {
			return q.Options
		}
	}
	t.Fatal("stage 1 has no ffa.intervention question")
	return nil
}

// UC: UC-040 A50 BR-039
func TestSplitStages_NoFreshPlayer(t *testing.T) {
	allStale := func() decide.Request {
		c := lopsidedContext()
		for _, p := range c.Players {
			p.History = nil
		}
		st := decide.BuildState(c)
		return decide.Request{State: st, Questions: decide.FFAQuestions(st, fullInterventions)}
	}
	t.Run("UC-040/BR-039 with no fresh alive player stage 1 offers no fresh-readings lever", func(t *testing.T) {
		got := stage1Options(t, allStale())
		for _, iv := range decide.FreshReadingsLevers() {
			if contains(got, iv) {
				t.Errorf("stage 1 offers %s with no fresh player: %v", iv, got)
			}
		}
		if !contains(got, decide.IntvEliminatePlayer) || !contains(got, decide.IntvAdjustMusicTempo) {
			t.Errorf("stage 1 lost a lever that needs no fresh readings: %v", got)
		}
	})
	t.Run("UC-040/BR-039 the caller's question keeps every option", func(t *testing.T) {
		req := allStale()
		if g := splitStages(req).gated; g == nil || !reflect.DeepEqual(g.Options, req.Questions[0].Options) {
			t.Errorf("gated question = %+v, want the caller's options %v", g, req.Questions[0].Options)
		}
	})
	t.Run("UC-040/BR-039 a fresh-readings lever that is the only one left is still offered", func(t *testing.T) {
		req := allStale()
		req.Questions = decide.FFAQuestions(req.State, []string{decide.IntvGrantShield})
		if got := stage1Options(t, req); !reflect.DeepEqual(got, []string{decide.IntvGrantShield}) {
			t.Errorf("stage 1 options = %v, want [grant_shield] (nothing but noop would be left)", got)
		}
	})
	t.Run("UC-040/BR-039 one fresh alive player keeps the levers", func(t *testing.T) {
		got := stage1Options(t, staleRequest())
		for _, iv := range decide.FreshReadingsLevers() {
			if !contains(got, iv) {
				t.Errorf("stage 1 lacks %s although players are fresh: %v", iv, got)
			}
		}
	})
}
