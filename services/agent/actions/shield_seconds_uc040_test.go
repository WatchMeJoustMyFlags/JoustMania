package actions

import (
	"testing"

	"github.com/joustmania/agent/decision"
)

// The writer's default shield length and the shield ledger's (UC-040 BR-022) must be
// the same value: the ledger takes the shield's end, and so the cooldown, from it.
//
// UC: UC-040 A33 BR-022
func TestDefaultShieldSecondsMatchesTheLedger(t *testing.T) {
	t.Run("UC-040/A33 the writer and the shield ledger default to the same length", func(t *testing.T) {
		if defaultShieldSeconds != decision.DefaultShieldSeconds {
			t.Fatalf("writer default %v != decision.DefaultShieldSeconds %v", defaultShieldSeconds, decision.DefaultShieldSeconds)
		}
	})
}
