package decision

import "github.com/joustmania/agent/flags"

// rulesFor picks the engine for one decision cycle (#1810, C-035). It is the
// probe seam: the decision.probe_decisions flag, read from this cycle's snapshot,
// swaps in the probe engine (synthetic noop only), so a mid-game flip changes the
// NEXT cycle. Pattern games always keep the real engine (UC-046 exactness: a
// probe toggle inside one would void its recorded decision sequence). The real
// engine stays in l.Rules unwrapped, so type assertions on it keep working.
func (l *Loop) rulesFor(snapshot flags.Snapshot) RulesEngine {
	if snapshot.ProbeDecisions && l.ProbeRules != nil && !(l.pattern != nil && l.pattern.Active()) {
		return l.ProbeRules
	}
	return l.Rules
}
