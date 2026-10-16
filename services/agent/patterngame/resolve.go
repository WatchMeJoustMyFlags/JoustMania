package patterngame

import (
	"context"
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
	// DiscardReason is set when the experiment must be discarded (A2, A4, A13).
	DiscardReason string
	// Errors are the failed rules behind DiscardReason.
	Errors []string
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
		return pin // A1: noise
	}
	pin.MaxGameSeconds = ClampCap(r.MaxGameSeconds(ctx))
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
