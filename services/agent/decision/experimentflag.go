package decision

import (
	"sync"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// The experiment-flag skip (UC-040 A40, BR-028; UC-043 BR-005; decide-contract §4.5
// step 2a; #1975). In a game that belongs to an experiment on flag X, in either arm,
// the agent does not decide a lever that changes the game value X sets: in the
// control arm the lever would confound the comparison, and in the experimental arm
// the coordinator blocks it experiment_override anyway, so the budget would go to
// nothing.

// experimentLevers maps an intervention to the experiment-catalog flag whose game
// value its coordinator handler replaces (flags.md §13.3). A factor on top of a value
// (player sensitivity, handicap, global difficulty) does not replace it and is not
// listed.
var experimentLevers = map[string]string{
	// handle_pacing_profile swaps the game's music windows.
	InterventionSetPacingProfile: "windows",
	// handle_global_sensitivity_override replaces the game's sensitivity level.
	InterventionAdjustGlobalSensitivity: "sensitivity",
}

// ExperimentLevers returns a copy of the lever -> experiment flag map (BR-028).
func ExperimentLevers() map[string]string {
	out := make(map[string]string, len(experimentLevers))
	for k, v := range experimentLevers {
		out[k] = v
	}
	return out
}

// movesExperimentFlag reports whether intervention is a lever of flagKey. An empty
// flagKey (no experiment, or one the registry does not know) matches nothing.
func movesExperimentFlag(intervention, flagKey string) bool {
	return flagKey != "" && experimentLevers[intervention] == flagKey
}

// SetExperimentFlagSource installs the lookup of the flag under test in a game (""
// when the game belongs to no experiment the agent knows). main.go wires it to the
// experiment registry. nil, the default, skips nothing. Set during construction.
func (l *Loop) SetExperimentFlagSource(src func(c gamecontext.GameContext) string) {
	l.experimentFlag = src
}

// experimentFlagOf returns the flag under test in c's experiment, "" when the loop
// has no source or the game belongs to no experiment the agent knows.
func (l *Loop) experimentFlagOf(c gamecontext.GameContext) string {
	if l.experimentFlag == nil {
		return ""
	}
	return l.experimentFlag(c)
}

// experimentFlagBlocks returns the flag under test when d is a lever of it in c's
// experiment (A40 step 1), "" when d is not blocked.
func (l *Loop) experimentFlagBlocks(c gamecontext.GameContext, d Decision) string {
	if _, lever := experimentLevers[d.Intervention]; !lever {
		return ""
	}
	flag := l.experimentFlagOf(c)
	if !movesExperimentFlag(d.Intervention, flag) {
		return ""
	}
	return flag
}

// offeredInterventions is the allow-list a remote decide request offers in c's
// game (A40 step 5, decide-contract §3.1): allowed without the levers of the flag
// under test. Outside an experiment it returns allowed unchanged. The call gate
// (§7.1) uses the same list, so a request left with only noop is never made.
//
// The group lever is offered only while the group gate is open for c (#2467,
// UC-040 A47, group.go): snapshot carries the live policy.
func (l *Loop) offeredInterventions(c gamecontext.GameContext, snapshot flags.Snapshot) []string {
	allowed := l.withoutClosedGroupLever(snapshot.Policy, c, snapshot.InterventionsAllowed)
	flag := l.experimentFlagOf(c)
	if flag == "" {
		return allowed
	}
	out := make([]string, 0, len(allowed))
	for _, a := range allowed {
		if !movesExperimentFlag(a, flag) {
			out = append(out, a)
		}
	}
	return out
}

// experimentFlagRefusals remembers the experiment_flag blocks this game's loop has
// already emitted, keyed by lever and flag (A40 step 6, as A21): a repeat only
// counts. At most one entry per lever and flag, so it stays bounded.
type experimentFlagRefusals struct {
	mu   sync.Mutex
	seen map[string]bool
}

// first reports whether this is the first experiment_flag block of intervention on
// flag, and records it.
func (r *experimentFlagRefusals) first(intervention, flag string) bool {
	key := intervention + "|" + flag
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.seen[key] {
		return false
	}
	if r.seen == nil {
		r.seen = map[string]bool{}
	}
	r.seen[key] = true
	return true
}
