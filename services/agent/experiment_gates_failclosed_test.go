package main

import (
	"context"
	"testing"

	"github.com/open-feature/go-sdk/openfeature"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
)

// keyErrEvaluator answers the booleans it holds and fails every other boolean read,
// as a provider does for a flag it cannot resolve. Non-boolean reads fall back to the
// caller's default with an error.
type keyErrEvaluator struct {
	switchableEvaluator
}

func (k *keyErrEvaluator) BooleanValue(_ context.Context, key string, d bool, _ openfeature.EvaluationContext, _ ...openfeature.Option) (bool, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if v, ok := k.booleans[key]; ok {
		return v, nil
	}
	return d, errFlagdUnreachable
}

// TestExperimentGates_FailClosedIgnoringEnv (#1907): the experiment gates
// (experiments_enabled, experiment_dynamic_enabled) and the proposer gate close on
// their own when flagd cannot answer, whatever the removed AGENT_EXPERIMENTS_ENABLED /
// AGENT_EXPERIMENT_DYNAMIC_ENABLED / AGENT_VERDICT_MIN_N env vars say. Before #1907
// those env vars were the fallback, so an outage with them set left the gates open.
// UC: UC-045 A3
func TestExperimentGates_FailClosedIgnoringEnv(t *testing.T) {
	t.Setenv("AGENT_EXPERIMENTS_ENABLED", "true")
	t.Setenv("AGENT_EXPERIMENT_DYNAMIC_ENABLED", "true")
	t.Setenv("AGENT_VERDICT_MIN_N", "1")
	ctx := context.Background()

	t.Run("UC-045/A3 flagd unreachable: every gate closed", func(t *testing.T) {
		ev := &switchableEvaluator{booleans: map[string]bool{"enabled": true, "experiments_enabled": true}}
		ev.setUnreachable()
		f := flags.New(ev, nil)

		cfg := f.Experiment(ctx, experimentDefaults())
		if cfg.Enabled {
			t.Error("experiments_enabled resolved on with flagd unreachable; want closed (env ignored)")
		}
		if cfg.DynamicEnabled {
			t.Error("experiment_dynamic_enabled resolved on with flagd unreachable; want closed (env ignored)")
		}
		if cfg.VerdictMinN != experiment.DefaultMinNPerArm {
			t.Errorf("verdict_min_n = %d with flagd unreachable, want the compiled default %d (env ignored)",
				cfg.VerdictMinN, experiment.DefaultMinNPerArm)
		}
		if ok, reason := proposerPermit(f, true)(ctx); ok || reason != experiment.SkipReasonAgentDisabled {
			t.Errorf("proposerPermit = (%v, %q), want (false, %q)", ok, reason, experiment.SkipReasonAgentDisabled)
		}
	})

	t.Run("UC-045/A3 enabled on, experiments_enabled unresolved: closed", func(t *testing.T) {
		ev := &keyErrEvaluator{switchableEvaluator{booleans: map[string]bool{"enabled": true}}}
		f := flags.New(ev, nil)

		if f.Experiment(ctx, experimentDefaults()).Enabled {
			t.Error("experiments_enabled resolved on while unresolvable; want closed (env ignored)")
		}
		if ok, reason := proposerPermit(f, true)(ctx); ok || reason != experiment.SkipReasonExperimentsDisabled {
			t.Errorf("proposerPermit = (%v, %q), want (false, %q)", ok, reason, experiment.SkipReasonExperimentsDisabled)
		}
	})
}
