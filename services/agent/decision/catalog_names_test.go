package decision

import (
	"reflect"
	"sort"
	"testing"

	"github.com/joustmania/agent/decide"
)

// TestInterventionConstantsMatchCatalog: the Intervention* constants (string
// literals, the C-034 guard parses them) spell exactly the names in
// decide.Catalog, so every table derived from the catalog covers them all.
func TestInterventionConstantsMatchCatalog(t *testing.T) {
	want := []string{
		InterventionPlayAudioCue, InterventionSendControllerEffect, InterventionAdjustVolume,
		InterventionAdjustMusicTempo, InterventionAdjustPlayerSensitivity, InterventionSetPlayerHandicap,
		InterventionGrantShield, InterventionToughnessBoost, InterventionSoftPenalty,
		InterventionAdjustGlobalSensitivity, InterventionAdjustGlobalDifficulty, InterventionSetPacingProfile,
		InterventionRampTempo, InterventionAutoRubberband, InterventionEliminatePlayer,
		InterventionRevivePlayer, InterventionEndGame,
	}
	sort.Strings(want)
	if got := decide.Names(nil); !reflect.DeepEqual(got, want) {
		t.Errorf("decide.Catalog names = %v, decision constants = %v", got, want)
	}
	if InterventionNoop != decide.IntvNoop {
		t.Errorf("InterventionNoop = %q, decide.IntvNoop = %q", InterventionNoop, decide.IntvNoop)
	}
}
