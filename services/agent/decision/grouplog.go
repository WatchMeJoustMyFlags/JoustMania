package decision

import (
	"strings"
	"sync"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// maxGroupRationaleRunes bounds the reason text kept per group decision.
const maxGroupRationaleRunes = 256

// GroupRecord is one group-shield decision as the game summary counts it (UC-040
// A47, BR-033, #2529). Outcome is GroupOutcomeApplied, GroupOutcomeBlocked or
// another outcome (failed, gated_off), which the summary counts as neither. Rescue
// is true when at least one target was near_threshold in the decide state at the
// decision. Rationale is the backend's reason text only: no arm, no backend.
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
}

// NewGroupLog builds an empty log.
func NewGroupLog() *GroupLog { return &GroupLog{byGame: make(map[string][]GroupRecord)} }

func (g *GroupLog) note(gameID string, r GroupRecord) {
	if g == nil || gameID == "" {
		return
	}
	g.mu.Lock()
	defer g.mu.Unlock()
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
	return out
}

// SetGroupLog wires the shared log. Set at construction, before the loop serves.
func (l *Loop) SetGroupLog(g *GroupLog) { l.groupLog = g }

// noteGroupDecision records a group-shield decision for the game summary.
func (l *Loop) noteGroupDecision(c gamecontext.GameContext, d Decision, outcome string) {
	if l.groupLog == nil {
		return
	}
	rationale := strings.TrimSpace(d.Reason)
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
