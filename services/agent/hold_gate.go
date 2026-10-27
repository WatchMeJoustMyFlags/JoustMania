package main

import (
	"context"
	"encoding/json"
	"sync"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/gamecontext"
)

// gameAttrsFrom looks up a game's kind and experiment arm in the Multiplexer for
// the agent's shield reads (flags.SetGameAttrs, #2096); an unknown game is real.
func gameAttrsFrom(mux *gamecontext.Multiplexer) func(gameID string) flags.GameAttrs {
	return func(gameID string) flags.GameAttrs {
		snap, ok := mux.Snapshot(gameID)
		if !ok {
			return flags.GameAttrs{}
		}
		return flags.GameAttrs{GameKind: snap.GameKind, ExperimentID: snap.ExperimentID, Arm: snap.Arm}
	}
}

// hold_gate.go: the experiment catalog's conditions for the hold-mode shield flags
// (UC-043 A17 step 1, FR-162, #2096), shared by the dynamic declarer, the proposer
// vocabulary and the registry's pin and per-tick checks.

// resolvedActivation returns the value real games and shadow games outside an arm
// resolve for shield.activation: its effective default. The flag service renders
// a stage, admin or real overlay as the defaultVariant every reader gets
// (flag-service state/render.go effectiveDefault), and the experiment rung only
// matches game_kind != real with an experiment_id and arm. The hold keys have no
// baseline targeting (tests/contract/test_flagd_resolution.py
// test_hold_keys_have_no_baseline_targeting), so the shadow-resolved and the
// real-resolved value are both this one. ok=false when it cannot be read.
func resolvedActivation(catalog map[string]flagservice.FlagView) (string, bool) {
	view, ok := catalog[experiment.HoldActivationKey]
	if !ok {
		return "", false
	}
	raw, ok := view.VariantValues[view.EffectiveDefault]
	if !ok {
		return "", false
	}
	var mode string
	if err := json.Unmarshal(raw, &mode); err != nil {
		return "", false
	}
	return mode, true
}

// holdValueReason returns why an experiment on flagKey cannot run on the
// catalog's current defaults ("" = it can; any non-hold key can). An unreadable
// shield.activation fails closed.
func holdValueReason(catalog map[string]flagservice.FlagView, flagKey string) string {
	if !experiment.IsHoldKey(flagKey) {
		return ""
	}
	mode, ok := resolvedActivation(catalog)
	if flagKey == experiment.HoldActivationKey {
		switch {
		case !ok:
			return experiment.DiscardHoldNotRealDefault
		case mode == experiment.HoldValue:
			return experiment.DiscardHoldAlreadyDefault
		}
		return ""
	}
	if !ok || mode != experiment.HoldValue {
		return experiment.DiscardHoldNotRealDefault
	}
	return ""
}

// holdGate decides whether the declarer and the proposer may offer a hold key.
// policy reports whether the archetypes experiment.mock_archetypes resolves to
// all press the trigger in a Free-for-All shadow mode (UC-046 BR-018); it is set
// once the experiment loop has built its pinner. Until then, and on a nil gate,
// no hold key is offered (fail closed).
type holdGate struct {
	mu     sync.Mutex
	policy func(ctx context.Context) bool
}

func (g *holdGate) setPolicy(policy func(ctx context.Context) bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.policy = policy
}

func (g *holdGate) policyOK(ctx context.Context) bool {
	if g == nil {
		return false
	}
	g.mu.Lock()
	policy := g.policy
	g.mu.Unlock()
	return policy != nil && policy(ctx)
}

// filter returns the predicate that keeps a catalog key offerable: any non-hold
// key, or a hold key while the archetypes press the trigger and the defaults fit
// (UC-043 A17 step 1). The policy is read at most once per filter.
func (g *holdGate) filter(ctx context.Context, catalog map[string]flagservice.FlagView) func(key string) bool {
	var once sync.Once
	var policy bool
	return func(key string) bool {
		if !experiment.IsHoldKey(key) {
			return true
		}
		once.Do(func() { policy = g.policyOK(ctx) })
		return policy && holdValueReason(catalog, key) == ""
	}
}

// registryHoldGate is the registry's experiment.HoldGate: the defaults part of
// the gate, read from the flag service (the archetype part is checked on the
// experiment's own pin).
func registryHoldGate(r gameFlagReader) experiment.HoldGate {
	return func(ctx context.Context, flagKey string) (string, error) {
		catalog, err := readGameCatalogCtx(ctx, r)
		if err != nil {
			return "", err
		}
		return holdValueReason(catalog, flagKey), nil
	}
}
