package gamerunner

import "testing"

// The start config carries the spec's sensitivity, so an experiment arm's own
// resolved value reaches the coordinator (UC-046 BR-019, #2392).
//
// UC: UC-046 BR-019
func TestUC046_BR019_StartConfigCarriesSpecSensitivity_2392(t *testing.T) {
	for _, level := range []int{0, 2, 4} {
		cfg := buildStartConfig(Spec{GameName: "JoustFFA", Sensitivity: level, ExperimentID: "exp-1", Arm: "experimental"}, []string{"a", "b"})
		if got := cfg.GetSensitivity(); got != int32(level) {
			t.Errorf("StartGameConfig.sensitivity = %d, want %d", got, level)
		}
	}
}
