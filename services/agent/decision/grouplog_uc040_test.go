package decision

import (
	"context"
	"strings"
	"testing"

	"github.com/joustmania/agent/flags"
)

// UC: UC-040 A47 BR-033
func TestGroupLog_NotesRescueAndBlindRationale(t *testing.T) {
	ctx := context.Background()
	run := func(t *testing.T, nearThreshold bool, mutate func(p *flags.Policy), reason string) []GroupRecord {
		l, _, clock, _ := budgetLoop(t, 100)
		log := NewGroupLog()
		l.SetGroupLog(log)
		c := groupCtx("shadow", "A", "B", "C")
		threshold, calm, hot := 10.0, 1.0, 9.0
		c.Session.DeathThreshold = &threshold
		for _, p := range c.Players {
			p.MovementIntensity = &calm
		}
		if nearThreshold {
			c.Players["A"].MovementIntensity = &hot
		}
		p := groupPolicy()
		mutate(&p)
		d := groupDecision("A", "B")
		d.Reason = reason
		var st LayerState
		st.DecideBackend = "jev"
		l.runDecision(ctx, groupSnapshot(p), c, d, *clock, &st)
		return log.Take(c.SessionID)
	}
	t.Run("UC-040/A47 an applied group with nobody in danger is a non-rescue record", func(t *testing.T) {
		got := run(t, false, func(*flags.Policy) {}, "  rally the field ")
		if len(got) != 1 || got[0].Outcome != GroupOutcomeApplied || got[0].Rescue || got[0].Rationale != "rally the field" {
			t.Fatalf("records = %+v", got)
		}
	})
	t.Run("UC-040/A47 a target near the threshold makes the group a rescue", func(t *testing.T) {
		got := run(t, true, func(*flags.Policy) {}, "save A")
		if len(got) != 1 || !got[0].Rescue {
			t.Fatalf("records = %+v", got)
		}
	})
	t.Run("UC-040/A48 a refused group covering a rescue is noted blocked", func(t *testing.T) {
		got := run(t, true, func(p *flags.Policy) { p.MaxInterventionsPerMinute = 0 }, "save A")
		if len(got) != 1 || got[0].Outcome != GroupOutcomeBlocked || !got[0].Rescue {
			t.Fatalf("records = %+v", got)
		}
	})
	t.Run("UC-040/A47 the record names no backend or arm", func(t *testing.T) {
		got := run(t, false, func(*flags.Policy) {}, "rally")
		if strings.Contains(strings.ToLower(got[0].Rationale), "jev") || len(got) != 1 {
			t.Fatalf("records = %+v", got)
		}
	})
	t.Run("UC-040/A47 Take forgets the game", func(t *testing.T) {
		log := NewGroupLog()
		log.note("g", GroupRecord{Outcome: GroupOutcomeApplied})
		if len(log.Take("g")) != 1 || log.Take("g") != nil {
			t.Fatal("Take must return once")
		}
	})
}
