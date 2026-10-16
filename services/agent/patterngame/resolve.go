package patterngame

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
)

// FlagReader is what pinning needs from the agent flags (flags.md §3.1).
type FlagReader interface {
	// MockArchetypes is the raw experiment.mock_archetypes value ("" when unreadable).
	MockArchetypes(ctx context.Context) string
	// MockArchetype evaluates experiment.mock_archetype with archetype=<name>; an
	// unknown name is an evaluation error.
	MockArchetype(ctx context.Context, name string) (any, error)
	// MaxGameSeconds is experiment.max_game_seconds (DefaultMaxGameSeconds when unreadable).
	MaxGameSeconds(ctx context.Context) int
}

// Pin is what an experiment pins at its first spawn (UC-046 step 2).
type Pin struct {
	// Selection is the selection flag value as read.
	Selection string
	// Archetypes in pair order; empty = no pattern games (A1, or discarded).
	Archetypes []Archetype
	// MaxGameSeconds is the pinned safety cap (BR-013).
	MaxGameSeconds int
	// CapNote is set when the flag value fell back to the default (BR-013).
	CapNote string
	// DiscardReason is set when the experiment must be discarded (A2, A4, A13).
	DiscardReason string
	// Errors are the failed rules behind DiscardReason.
	Errors []string
}

// ParamsJSON returns the pinned canonical parameter objects in pair order.
func (p Pin) ParamsJSON() []string {
	out := make([]string, len(p.Archetypes))
	for i, a := range p.Archetypes {
		out[i] = a.ParamsJSON
	}
	return out
}

// Active reports whether the experiment runs pattern games.
func (p Pin) Active() bool { return len(p.Archetypes) > 0 && p.DiscardReason == "" }

// Names returns the pinned archetype names in pair order.
func (p Pin) Names() []string {
	out := make([]string, len(p.Archetypes))
	for i, a := range p.Archetypes {
		out[i] = a.Name
	}
	return out
}

// Digests returns the pinned parameter digests in pair order.
func (p Pin) Digests() []string {
	out := make([]string, len(p.Archetypes))
	for i, a := range p.Archetypes {
		out[i] = a.Digest
	}
	return out
}

// Resolve reads the selection, resolves and validates every archetype and the
// cap (UC-046 step 2). minPairs is verdict_min_pairs (0 = no size check, the
// one-shot runner); mode is the shadow game mode. override, when non-empty,
// replaces the selection flag (SHADOW_GAME_ARCHETYPE for the one-shot runner).
func Resolve(ctx context.Context, r FlagReader, mode string, minPairs int, override string) Pin {
	pin := Pin{Selection: strings.TrimSpace(override)}
	if pin.Selection == "" {
		pin.Selection = r.MockArchetypes(ctx)
	}
	names := ParseSelection(pin.Selection)
	if len(names) == 0 {
		// UC-046 A1: experiments are archetype-only; nothing selected, nothing runs.
		pin.DiscardReason = DiscardSelectionEmpty
		pin.Errors = []string{fmt.Sprintf("%s selects no archetype", SelectionFlagKey)}
		return pin
	}
	raw := r.MaxGameSeconds(ctx)
	pin.MaxGameSeconds = ClampCap(raw)
	if pin.MaxGameSeconds != raw {
		pin.CapNote = fmt.Sprintf("%s=%d outside %d..%d, using %d", CapFlagKey, raw, MinMaxGameSeconds, MaxMaxGameSeconds, pin.MaxGameSeconds)
	}
	if mode != SupportedMode {
		pin.DiscardReason = DiscardModeUnsupported
		pin.Errors = []string{fmt.Sprintf("mode %s (pattern games are %s only)", mode, SupportedMode)}
		return pin
	}
	if dup := HasDuplicate(names); dup != "" {
		pin.DiscardReason = DiscardSelectionSmall
		pin.Errors = []string{fmt.Sprintf("archetype %q listed twice", dup)}
		return pin
	}
	if minPairs > 0 && len(names) < minPairs {
		pin.DiscardReason = DiscardSelectionSmall
		pin.Errors = []string{fmt.Sprintf("%d distinct archetypes < verdict_min_pairs %d", len(names), minPairs)}
		return pin
	}
	for _, name := range names {
		raw, err := r.MockArchetype(ctx, name)
		if err != nil {
			pin.Errors = append(pin.Errors, fmt.Sprintf("%s: unknown archetype (%v)", name, err))
			continue
		}
		a, errs := NewArchetype(name, raw)
		if len(errs) > 0 {
			for _, e := range errs {
				pin.Errors = append(pin.Errors, name+": "+e)
			}
			continue
		}
		pin.Archetypes = append(pin.Archetypes, a)
	}
	if len(pin.Errors) > 0 {
		pin.DiscardReason = DiscardArchetypeInvalid
		pin.Archetypes = nil
	}
	return pin
}

// RestorePin rebuilds a pin from its journaled form (UC-046 BR-015): the names,
// canonical parameter objects and digests of the pattern_pinned event. Every
// archetype is re-validated and its digest must match; any mismatch is an error
// (the caller discards the experiment with DiscardPinLost).
func RestorePin(selection string, names, paramsJSON, digests []string, maxGameSeconds int) (Pin, error) {
	if len(names) == 0 || len(names) != len(paramsJSON) || len(names) != len(digests) {
		return Pin{}, fmt.Errorf("pinned archetypes incomplete: %d names, %d parameter objects, %d digests",
			len(names), len(paramsJSON), len(digests))
	}
	pin := Pin{Selection: selection, MaxGameSeconds: ClampCap(maxGameSeconds)}
	for i, name := range names {
		var raw map[string]any
		if err := json.Unmarshal([]byte(paramsJSON[i]), &raw); err != nil {
			return Pin{}, fmt.Errorf("%s: pinned parameters unreadable: %w", name, err)
		}
		a, errs := NewArchetype(name, raw)
		if len(errs) > 0 {
			return Pin{}, fmt.Errorf("%s: pinned parameters invalid: %s", name, strings.Join(errs, "; "))
		}
		if a.Digest != digests[i] {
			return Pin{}, fmt.Errorf("%s: digest %s != pinned %s", name, a.Digest, digests[i])
		}
		pin.Archetypes = append(pin.Archetypes, a)
	}
	return pin, nil
}
