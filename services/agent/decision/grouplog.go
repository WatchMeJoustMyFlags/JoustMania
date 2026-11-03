package decision

import (
	"strings"
	"sync"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// maxGroupRationaleRunes bounds the reason text kept per group decision.
const maxGroupRationaleRunes = 256

// maxGroupLogGames bounds how many games the log keeps records for, and
// maxGroupLogTaken how many taken game ids it remembers, so a record noted after
// its game's summary took the log (or for a game that never gets a game end) cannot
// grow the log without limit.
const (
	maxGroupLogGames = 8
	maxGroupLogTaken = 64
)

// GroupRecord is one group-shield decision as the game summary counts it (UC-040
// A47, BR-033, #2529). Outcome is GroupOutcomeApplied, GroupOutcomeBlocked or
// another outcome (failed, gated_off), which the summary counts as neither. Rescue
// is true when at least one target was near_threshold in the context the decision
// was recorded with (for a remote answer, the context at landing time). Rationale is the backend's reason text only: no arm, no backend.
type GroupRecord struct {
	Outcome   string
	Rescue    bool
	Rationale string
}

// GroupLog keeps the group-shield decisions of the live games until the game
// summary takes them. One log is shared by every per-game Loop; it is safe for
// concurrent use. Observation only: nothing reads it to decide.
type GroupLog struct {
	mu     sync.Mutex
	byGame map[string][]GroupRecord
	order  []string        // game ids of byGame, oldest first
	taken  map[string]bool // game ids Take already returned
	takenQ []string        // taken ids, oldest first
}

// NewGroupLog builds an empty log.
func NewGroupLog() *GroupLog {
	return &GroupLog{byGame: make(map[string][]GroupRecord), taken: make(map[string]bool)}
}

func (g *GroupLog) note(gameID string, r GroupRecord) {
	if g == nil || gameID == "" {
		return
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.taken[gameID] {
		return // the summary already took this game: nobody would read the record
	}
	if _, ok := g.byGame[gameID]; !ok {
		g.order = append(g.order, gameID)
		if len(g.order) > maxGroupLogGames {
			delete(g.byGame, g.order[0])
			g.order = g.order[1:]
		}
	}
	g.byGame[gameID] = append(g.byGame[gameID], r)
}

// Take returns the game's records in decision order and forgets them.
func (g *GroupLog) Take(gameID string) []GroupRecord {
	if g == nil {
		return nil
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	out := g.byGame[gameID]
	delete(g.byGame, gameID)
	for i, id := range g.order {
		if id == gameID {
			g.order = append(g.order[:i], g.order[i+1:]...)
			break
		}
	}
	if !g.taken[gameID] {
		g.taken[gameID] = true
		g.takenQ = append(g.takenQ, gameID)
		if len(g.takenQ) > maxGroupLogTaken {
			delete(g.taken, g.takenQ[0])
			g.takenQ = g.takenQ[1:]
		}
	}
	return out
}

// SetGroupLog wires the shared log. Set at construction, before the loop serves.
func (l *Loop) SetGroupLog(g *GroupLog) { l.groupLog = g }

// noteGroupDecision records a group-shield decision for the game summary.
func (l *Loop) noteGroupDecision(c gamecontext.GameContext, d Decision, outcome string) {
	if l.groupLog == nil {
		return
	}
	rationale := ""
	if !d.ReasonSynthesized { // a synthesized "decided by <backend>" would unblind the export
		rationale = strings.TrimSpace(d.Reason)
	}
	if r := []rune(rationale); len(r) > maxGroupRationaleRunes {
		rationale = string(r[:maxGroupRationaleRunes])
	}
	l.groupLog.note(c.SessionID, GroupRecord{Outcome: outcome, Rescue: groupCoversRescue(c, d.TargetSerials), Rationale: rationale})
}

// groupCoversRescue reports whether a target was near_threshold at the decision.
func groupCoversRescue(c gamecontext.GameContext, serials []string) bool {
	targets := make(map[string]bool, len(serials))
	for _, s := range serials {
		targets[s] = true
	}
	for _, p := range decide.BuildState(c).Players {
		if targets[p.Serial] && p.Danger == decide.DangerNearThreshold {
			return true
		}
	}
	return false
}
