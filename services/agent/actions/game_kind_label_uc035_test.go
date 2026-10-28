package actions

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// counterByGameKind sums the named counter per game_kind label value.
func counterByGameKind(t *testing.T, reader *metric.ManualReader, name string) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	out := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != name {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("expected Sum[int64], got %T", m.Data)
			}
			for _, dp := range sum.DataPoints {
				kind, ok := dp.Attributes.Value("game_kind")
				if !ok {
					t.Fatalf("%s datapoint without game_kind: %v", name, dp.Attributes)
				}
				out[kind.AsString()] += dp.Value
			}
		}
	}
	return out
}

// UC: UC-035 A3
// The agent's two counters carry game_kind in the TELEMETRY vocabulary
// (primary / shadow), although the loop reasons in the flag vocabulary
// (real / shadow) (#1249, #2294).
func TestAgentCounters_GameKindLabel(t *testing.T) {
	cases := []struct {
		name     string
		gameKind string
		want     string
	}{
		{"UC-035/A3 real game is labelled primary", gamecontext.GameKindReal, "primary"},
		{"UC-035/A3 shadow game is labelled shadow", gamecontext.GameKindShadow, "shadow"},
		{"UC-035/A3 unknown kind never counts as primary", "", "shadow"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			snap := flags.Snapshot{
				Enabled:              true,
				InterventionsAllowed: []string{decision.InterventionAdjustVolume},
				Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
			}
			l, _, reader, _ := meteredLoop(t, snap,
				oneDecision{Intervention: decision.InterventionAdjustVolume, Reason: "quieter"}, true)

			l.OnEvaluate(context.Background(),
				gamecontext.GameContext{SessionID: uc040GameID, GameKind: c.gameKind}, testTrigger())

			for _, name := range []string{"agent_decisions_total", "agent_intervention_writes_total"} {
				got := counterByGameKind(t, reader, name)
				if got[c.want] != 1 || len(got) != 1 {
					t.Errorf("%s by game_kind = %v, want {%s: 1}", name, got, c.want)
				}
			}
		})
	}
}
