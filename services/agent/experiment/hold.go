package experiment

import (
	"context"
	"fmt"
	"strings"

	"github.com/joustmania/agent/patterngame"
)

// hold.go: experiments on the hold-mode shield flags (UC-043 A17, FR-162, #2096).
// The eight hold keys are in the experiment catalog only under conditions the
// dynamic declarer, the proposer vocabulary and the registry all apply: the pinned
// archetypes press the trigger, shield.activation is tried only toward hold, and a
// hold sub-parameter only while games resolve hold.

// Hold flag keys and values (flags.md §4, §13.3).
const (
	HoldActivationKey = "shield.activation"
	HoldValue         = "hold"
	// holdKeyPrefix covers the eight hold keys; the other shield keys (charge expiry,
	// the #2094 cue flags) are stack-only, so they never reach the catalog (C-034).
	holdKeyPrefix = "shield."
)

// Discard reasons of a hold-flag experiment (UC-043 A17 step 2).
const (
	// DiscardHoldNotRealDefault: a hold sub-parameter experiment while games do not
	// resolve hold for shield.activation (the sub-parameter would be inert in both arms).
	DiscardHoldNotRealDefault = "hold_not_real_default"
	// DiscardHoldAlreadyDefault: a shield.activation experiment while games already
	// resolve hold (both arms would play hold).
	DiscardHoldAlreadyDefault = "hold_already_default"
)

// IsHoldKey reports whether flagKey is a shield flag, which UC-043 A17 gates.
func IsHoldKey(flagKey string) bool { return strings.HasPrefix(flagKey, holdKeyPrefix) }

// HoldGate returns why an experiment on flagKey cannot run on the current flag
// defaults ("" = it can). An error means the defaults could not be read.
type HoldGate func(ctx context.Context, flagKey string) (reason string, err error)

// checkHoldValue refuses a shield.activation experiment toward anything but hold
// (UC-043 A17 step 1).
func checkHoldValue(flagKey string, value any) error {
	if flagKey != HoldActivationKey {
		return nil
	}
	if s, ok := value.(string); ok && s == HoldValue {
		return nil
	}
	return fmt.Errorf("%s is experimented on only toward %q, got %v (UC-043 A17)", HoldActivationKey, HoldValue, value)
}

// ArchetypesWithoutTriggerPolicy returns the names of the pinned archetypes that do
// not press the trigger (UC-046 BR-018). A hold-flag experiment needs none.
func ArchetypesWithoutTriggerPolicy(pin patterngame.Pin) []string {
	var missing []string
	for _, a := range pin.Archetypes {
		if !a.HasTriggerPolicy() {
			missing = append(missing, a.Name)
		}
	}
	return missing
}
