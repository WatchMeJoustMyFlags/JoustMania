package decision

import (
	"reflect"
	"sort"
	"testing"

	"github.com/joustmania/agent/decide"
)

// TestPlayerTargetedInSync: decide.PlayerTargeted (used by backends that need
// to know whether an intervention takes a player target, e.g. jev's two-stage
// round, #1262) matches the set mapAnswers enforces.
func TestPlayerTargetedInSync(t *testing.T) {
	var here []string
	for k := range playerTargetedInterventions {
		here = append(here, k)
	}
	sort.Strings(here)
	if got := decide.PlayerTargetedInterventions(); !reflect.DeepEqual(got, here) {
		t.Errorf("decide.PlayerTargetedInterventions() = %v, decision has %v", got, here)
	}
}
