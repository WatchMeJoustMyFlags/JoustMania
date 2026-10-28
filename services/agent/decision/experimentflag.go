package decision

import "github.com/joustmania/agent/gamecontext"

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

// experimentFlagBlocks reports whether d is a lever of the flag under test in c's
// experiment (A40 step 1).
func (l *Loop) experimentFlagBlocks(c gamecontext.GameContext, d Decision) bool {
	if l.experimentFlag == nil {
		return false
	}
	if _, lever := experimentLevers[d.Intervention]; !lever {
		return false
	}
	return movesExperimentFlag(d.Intervention, l.experimentFlag(c))
}
