package actions

import (
	"context"
	"os"
	"path/filepath"
	"sort"
	"testing"

	"gopkg.in/yaml.v3"

	"github.com/joustmania/agent/decision"
)

// The already-applied check (UC-040 A35, BR-023) may suppress only the interventions
// whose flag is of the registry kind state or level: writing the same value again
// changes nothing at the coordinator. A grant or an event is a new write every time.
// This pins decision.IdempotentInterventions to services/flag-service/registry.yaml
// through the flag each intervention's write goes to.
//
// UC: UC-040 A35 BR-023
func TestIdempotentInterventionsMatchRegistryKinds(t *testing.T) {
	t.Run("UC-040/BR-023 the suppressible set is exactly the state and level interventions", func(t *testing.T) {
		raw, err := os.ReadFile(filepath.Join("..", "..", "flag-service", "registry.yaml"))
		if err != nil {
			t.Fatalf("read registry: %v", err)
		}
		var doc struct {
			Flags []struct {
				Set  string `yaml:"set"`
				Key  string `yaml:"key"`
				Kind string `yaml:"kind"`
			} `yaml:"flags"`
		}
		if err := yaml.Unmarshal(raw, &doc); err != nil {
			t.Fatalf("parse registry: %v", err)
		}
		kinds := map[string]string{}
		for _, f := range doc.Flags {
			if f.Set == FlagSetInterventions {
				kinds[f.Key] = f.Kind
			}
		}

		all := []string{
			decision.InterventionPlayAudioCue, decision.InterventionSendControllerEffect,
			decision.InterventionAdjustVolume, decision.InterventionAdjustMusicTempo,
			decision.InterventionAdjustPlayerSensitivity, decision.InterventionSetPlayerHandicap,
			decision.InterventionGrantShield, decision.InterventionToughnessBoost,
			decision.InterventionSoftPenalty, decision.InterventionAdjustGlobalSensitivity,
			decision.InterventionAdjustGlobalDifficulty, decision.InterventionSetPacingProfile,
			decision.InterventionRampTempo, decision.InterventionAutoRubberband,
			decision.InterventionEliminatePlayer, decision.InterventionRevivePlayer,
			decision.InterventionEndGame,
		}
		var want []string
		covered := map[string]bool{}
		for _, intervention := range all {
			w, svc := newTestWriter(t)
			d := decision.Decision{Intervention: intervention, GameID: "game_1", TargetSerial: "MOCK0001"}
			if err := w.Apply(context.Background(), d); err != nil {
				t.Fatalf("%s: write failed: %v", intervention, err)
			}
			flag := svc.last(t).flag
			kind, ok := kinds[flag]
			if !ok {
				t.Fatalf("%s writes %q, which has no interventions row in registry.yaml", intervention, flag)
			}
			covered[flag] = true
			if kind == "state" || kind == "level" {
				want = append(want, intervention)
			}
		}
		if len(covered) != len(kinds) {
			t.Fatalf("the interventions reach %d of the %d registry interventions flags: list the new intervention here", len(covered), len(kinds))
		}
		sort.Strings(want)
		got := decision.IdempotentInterventions()
		if len(got) != len(want) {
			t.Fatalf("IdempotentInterventions = %v, want %v", got, want)
		}
		for i := range got {
			if got[i] != want[i] {
				t.Fatalf("IdempotentInterventions = %v, want %v", got, want)
			}
		}
	})
}
