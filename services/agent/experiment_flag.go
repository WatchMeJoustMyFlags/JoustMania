package main

import (
	"sync/atomic"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/gamecontext"
)

// experimentFlagLookup is the decision loops' view of the flag under test in a game
// (UC-040 A40, BR-028; #1975). The loops are built before the experiment registry,
// so the registry's lookup is installed late; until then, and for a game the
// registry does not know, it answers "" and nothing is skipped.
type experimentFlagLookup struct {
	fn atomic.Pointer[func(gameID, experimentID string) (string, bool)]
}

// Set installs the registry lookup. A nil fn is ignored.
func (l *experimentFlagLookup) Set(fn func(gameID, experimentID string) (string, bool)) {
	if fn != nil {
		l.fn.Store(&fn)
	}
}

// FlagFor returns the flag under test in c's game: the experiment bound to the game
// id at spawn, else the one its experiment_id names; "" when there is none.
func (l *experimentFlagLookup) FlagFor(c gamecontext.GameContext) string {
	p := l.fn.Load()
	if p == nil || (c.SessionID == "" && c.ExperimentID == "") {
		return ""
	}
	flag, ok := (*p)(c.SessionID, c.ExperimentID)
	if !ok {
		return ""
	}
	return flag
}

// installExperimentFlags wires the registry's lookup into l (main.go calls it once
// the registry exists).
func installExperimentFlags(l *experimentFlagLookup, reg *experiment.Registry) {
	l.Set(reg.ExperimentFlag)
}
