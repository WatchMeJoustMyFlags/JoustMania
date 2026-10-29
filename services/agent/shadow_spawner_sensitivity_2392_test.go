package main

import (
	"context"
	"errors"
	"log/slog"
	"testing"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/gamerunner"
	"github.com/joustmania/agent/patterngame"
)

// armSensitivity stands in for flags.Flags.ShadowGameInt over a flag the flag
// service rendered for an experiment on sensitivity: the experimental arm of exp-1
// resolves 4, every other context the baseline 2.
func armSensitivity(_ context.Context, key, mode, experimentID, arm string) (int64, error) {
	if key != "sensitivity" || mode != "JoustFFA" {
		return 0, errors.New("unexpected read " + key + " in " + mode)
	}
	if experimentID == "exp-1" && arm == experiment.ArmExperimental {
		return 4, nil
	}
	return 2, nil
}

func patternSpawnCtx() context.Context {
	return experiment.WithPatternSpawn(context.Background(), experiment.PatternSpawn{
		Archetype: patterngame.Archetype{Name: "steady"}, MaxGameSeconds: 120,
	})
}

// An experiment on sensitivity starts its two arms with different sensitivities:
// each arm's start config carries the arm's own resolved value, not the fixed
// SHADOW_GAME_SENSITIVITY (#2392).
//
// UC: UC-046 BR-019
func TestUC046_BR019_ArmsStartWithTheirOwnSensitivity_2392(t *testing.T) {
	t.Setenv("SHADOW_GAME_SENSITIVITY", "1")
	s := newShadowSpawner(gamerunner.Config{}, slog.Default(), armSensitivity)
	ctx := patternSpawnCtx()

	t.Run("UC-046/BR-019 each arm resolves its own sensitivity", func(t *testing.T) {
		control, err := s.armSpec(ctx, "exp-1", experiment.ArmControl, 7)
		if err != nil {
			t.Fatal(err)
		}
		experimental, err := s.armSpec(ctx, "exp-1", experiment.ArmExperimental, 7)
		if err != nil {
			t.Fatal(err)
		}
		if control.Sensitivity != 2 || experimental.Sensitivity != 4 {
			t.Fatalf("control=%d experimental=%d, want 2 and 4", control.Sensitivity, experimental.Sensitivity)
		}
	})
	t.Run("UC-046/BR-019 an unreadable or out-of-range value refuses the spawn", func(t *testing.T) {
		for name, resolve := range map[string]armIntResolver{
			"unreadable":   func(context.Context, string, string, string, string) (int64, error) { return 0, errors.New("down") },
			"out of range": func(context.Context, string, string, string, string) (int64, error) { return 9, nil },
		} {
			s := newShadowSpawner(gamerunner.Config{}, slog.Default(), resolve)
			if _, err := s.armSpec(ctx, "exp-1", experiment.ArmControl, 7); err == nil {
				t.Errorf("%s: want the spawn refused", name)
			}
			if _, err := s.Spawn(ctx, "exp-1", experiment.ArmControl, 7); err == nil {
				t.Errorf("%s: Spawn started a game", name)
			}
		}
	})
	t.Run("UC-046/BR-019 a shadow game without an experiment keeps SHADOW_GAME_SENSITIVITY", func(t *testing.T) {
		spec, err := s.armSpec(ctx, "", "", 0)
		if err != nil {
			t.Fatal(err)
		}
		if spec.Sensitivity != 1 {
			t.Fatalf("sensitivity = %d, want SHADOW_GAME_SENSITIVITY 1", spec.Sensitivity)
		}
	})
}

// A spawn refused on the arm's sensitivity is counted by reason (#2392).
func TestUC046_BR019_RefusedSpawnIsCounted_2392(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	for reason, resolve := range map[string]armIntResolver{
		"sensitivity_unreadable": func(context.Context, string, string, string, string) (int64, error) {
			return 0, errors.New("not ready")
		},
		"sensitivity_out_of_range": func(context.Context, string, string, string, string) (int64, error) { return 9, nil },
	} {
		s := newShadowSpawner(gamerunner.Config{}, slog.Default(), resolve)
		s.SetSpawnFailureCounter(newSpawnFailureCounter(mp))
		ctx := patternSpawnCtx()
		if _, err := s.armSpec(ctx, "exp-1", "control", 1); err == nil {
			t.Fatalf("%s: want refusal", reason)
		}
		var rm metricdata.ResourceMetrics
		if err := reader.Collect(context.Background(), &rm); err != nil {
			t.Fatal(err)
		}
		found := false
		for _, sm := range rm.ScopeMetrics {
			for _, m := range sm.Metrics {
				if m.Name != metricSpawnFailures {
					continue
				}
				for _, dp := range m.Data.(metricdata.Sum[int64]).DataPoints {
					if v, _ := dp.Attributes.Value("reason"); v.AsString() == reason && dp.Value == 1 {
						found = true
					}
				}
			}
		}
		if !found {
			t.Errorf("no %s{reason=%s}=1", metricSpawnFailures, reason)
		}
	}
}
