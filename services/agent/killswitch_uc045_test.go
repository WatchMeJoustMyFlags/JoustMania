package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"
	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/infracontext"
	"github.com/joustmania/agent/patterngame"
)

// killswitch_uc045_test.go covers the UC-045 (kill switch) flows the per-feature
// suites leave open (#1653): the experiment loop aborting a RUNNING experiment when
// `enabled` flips off (main step 5), the same abort when flagd becomes unreachable
// and both gates fall back to off (A3), and the infrastructure loop that `enabled`
// does not govern (A4 step 1, Post-S-3).

// switchableEvaluator is a flagd stand-in for the real flags client: it serves the
// configured booleans while reachable, and fails every read once unreachable, as an
// OpenFeature provider does when flagd is down. Missing keys fall back to the
// caller's default (an undefined flag).
type switchableEvaluator struct {
	mu          sync.Mutex
	booleans    map[string]bool
	objects     map[string]any
	unreachable bool
}

var errFlagdUnreachable = errors.New("flagd unreachable")

func (s *switchableEvaluator) setUnreachable() {
	s.mu.Lock()
	s.unreachable = true
	s.mu.Unlock()
}

func (s *switchableEvaluator) down() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.unreachable
}

func (s *switchableEvaluator) BooleanValue(_ context.Context, k string, d bool, _ openfeature.EvaluationContext, _ ...openfeature.Option) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.unreachable {
		return d, errFlagdUnreachable
	}
	if v, ok := s.booleans[k]; ok {
		return v, nil
	}
	return d, nil
}

func (s *switchableEvaluator) StringValue(_ context.Context, _ string, d string, _ openfeature.EvaluationContext, _ ...openfeature.Option) (string, error) {
	if s.down() {
		return d, errFlagdUnreachable
	}
	return d, nil
}

func (s *switchableEvaluator) IntValue(_ context.Context, _ string, d int64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (int64, error) {
	if s.down() {
		return d, errFlagdUnreachable
	}
	return d, nil
}

func (s *switchableEvaluator) FloatValue(_ context.Context, _ string, d float64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (float64, error) {
	if s.down() {
		return d, errFlagdUnreachable
	}
	return d, nil
}

func (s *switchableEvaluator) ObjectValue(_ context.Context, k string, d any, _ openfeature.EvaluationContext, _ ...openfeature.Option) (any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.unreachable {
		return d, errFlagdUnreachable
	}
	if v, ok := s.objects[k]; ok {
		return v, nil
	}
	return d, nil
}

// TestKillSwitch_AbortsRunningExperiment: both gates on, the seed experiment runs
// with shadow games in flight; the operator turns `enabled` off and the next
// experiment tick aborts it and frees every shadow slot (experiments_enabled stays on).
// UC: UC-045 main
func TestKillSwitch_AbortsRunningExperiment(t *testing.T) {
	loop, spawner, lc := loopForConfigTest(t, 8)
	var mu sync.Mutex
	agentEnabled := true
	loop.enabledOverride = func(context.Context) bool {
		mu.Lock()
		defer mu.Unlock()
		return agentEnabled
	}
	lc.set(flags.ExperimentConfig{Enabled: true, ShadowEffectiveConcurrency: 4})
	ctx := context.Background()

	loop.allocateIfEnabled(ctx)
	if got := loop.registry.LiveCount(); got != 1 {
		t.Fatalf("precondition: LiveCount=%d, want 1 running experiment", got)
	}
	if loop.registry.TotalInFlight() == 0 || len(spawner.drain()) == 0 {
		t.Fatal("precondition: no shadow game in flight before the kill switch")
	}

	mu.Lock()
	agentEnabled = false
	mu.Unlock()
	loop.allocateIfEnabled(ctx)

	if got := loop.registry.LiveCount(); got != 0 {
		t.Fatalf("kill switch off did not abort the running experiment: LiveCount=%d, want 0", got)
	}
	if n := loop.registry.TotalInFlight(); n != 0 {
		t.Fatalf("kill switch off left %d shadow slots in flight, want 0", n)
	}
	if recs := spawner.drain(); len(recs) != 0 {
		t.Fatalf("kill switch off still spawned %d shadow games", len(recs))
	}
}

// TestKillSwitch_ForceEndsPatternGames: a pattern experiment runs with pattern
// games in flight. The operator turns `enabled` off; the next experiment tick
// force-ends every in-flight pattern game (UC-046 A12) and aborts the experiment.
// The promotion revert of the same step (#1704) is covered by
// promotion_killswitch_uc043_test.go.
// UC: UC-045 main
func TestKillSwitch_ForceEndsPatternGames(t *testing.T) {
	t.Run("UC-045/main step 5 in-flight pattern games are force-ended with kill_switch", func(t *testing.T) {
		ctx := context.Background()
		arch, errs := patterngame.NewArchetype("only", map[string]any{
			"players": 4.0, "stream_rate_hz": 30.0, "base": 1.0, "drift": 0.01, "amp": 0.1,
			"period_s": 3.0, "phase": 0.0, "burst_h": 0.0, "burst_period_s": 0.0,
			"d_base": 0.0, "d_drift": 0.01, "d_amp": 0.0, "d_phase": 0.25,
		})
		if len(errs) > 0 {
			t.Fatalf("archetype: %v", errs)
		}
		pinner := &countingPinner{pin: patterngame.Pin{Selection: "only", MaxGameSeconds: 300, Archetypes: []patterngame.Archetype{arch}}}
		spawner := &recordingSpawner{}
		tracker := patterngame.NewTracker()

		log := testLogger()
		svc := newGameFileForLoop(t)

		writer := experiment.NewWriter(svc, log)
		targeting := experiment.NewGateTargetingWriter(experiment.NewGate(writer, nil, log), writer)
		reg := experiment.NewRegistry(experiment.RegistryConfig{
			Root: t.TempDir(), MaxShadowGames: 4, EffectiveConcurrency: 2, Spawner: spawner,
			Verdict: experiment.NewVerdict(3, 0.5, nil), Targeting: targeting, Log: log,
			Patterns: pinner, PatternTracker: tracker,
		})
		lc := &liveConfig{}
		lc.set(flags.ExperimentConfig{Enabled: true, ShadowEffectiveConcurrency: 2})
		lc.setSeed(testSeed("nonstop.respawn_seconds", 6.0, 50))
		var mu sync.Mutex
		agentEnabled := true
		loop := &experimentLoop{
			registry:       reg,
			log:            log,
			expDefaults:    flags.ExperimentDefaults{Tick: time.Hour, DynamicMaxConcurrent: defaultDynamicMaxConcurrent},
			configOverride: lc.get,
			enabledOverride: func(context.Context) bool {
				mu.Lock()
				defer mu.Unlock()
				return agentEnabled
			},
		}

		loop.allocateIfEnabled(ctx)
		games := spawner.drain()
		if reg.LiveCount() != 1 || len(games) == 0 {
			t.Fatalf("precondition: LiveCount=%d spawned=%d, want a running pattern experiment", reg.LiveCount(), len(games))
		}
		var aborted []string
		for _, g := range games {
			gid := g.gameID
			tracker.Register(patterngame.Info{GameID: gid, Abort: func() {
				mu.Lock()
				aborted = append(aborted, gid)
				mu.Unlock()
			}})
		}

		mu.Lock()
		agentEnabled = false
		mu.Unlock()
		loop.allocateIfEnabled(ctx)

		mu.Lock()
		got := len(aborted)
		mu.Unlock()
		if got != len(games) {
			t.Fatalf("kill switch force-ended %d of %d in-flight pattern games", got, len(games))
		}
		for _, g := range games {
			rec, ok := tracker.Finish(g.gameID, patterngame.OutcomeAborted, patterngame.GameEvents{}, nil)
			if !ok || len(rec.NonExactReasons) != 1 || rec.NonExactReasons[0] != patterngame.ReasonKillSwitch {
				t.Errorf("game %s: non-exact reasons = %v, want [%s]", g.gameID, rec.NonExactReasons, patterngame.ReasonKillSwitch)
			}
		}
		if n := reg.LiveCount(); n != 0 {
			t.Fatalf("kill switch left %d experiments running", n)
		}
	})
}

// TestKillSwitch_FlagdUnreachableStopsExperimentLoop drives the REAL flags client
// (no enabledOverride / configOverride seam): with flagd reachable both gates are on
// and the seed experiment runs; once flagd is unreachable BOTH gates fail closed
// (`enabled` and, since #1907, experiments_enabled, with no env fallback even when
// the removed AGENT_EXPERIMENTS_ENABLED is set), so the next tick aborts the
// experiment on either gate.
// UC: UC-045 A3
func TestKillSwitch_FlagdUnreachableStopsExperimentLoop(t *testing.T) {
	t.Setenv("AGENT_EXPERIMENTS_ENABLED", "true") // ignored since #1907
	loop, spawner, _ := loopForConfigTest(t, 8)
	ev := &switchableEvaluator{
		booleans: map[string]bool{"enabled": true, "experiments_enabled": true},
		objects:  map[string]any{"experiment_seed": testSeed("nonstop.respawn_seconds", 6.0, 50)},
	}
	loop.flags = flags.New(ev, nil)
	loop.configOverride = nil
	loop.enabledOverride = nil
	loop.expDefaults.ShadowEffectiveConcurrency = 4
	ctx := context.Background()

	loop.allocateIfEnabled(ctx)
	if got := loop.registry.LiveCount(); got != 1 {
		t.Fatalf("precondition: LiveCount=%d, want 1 running experiment", got)
	}
	spawner.drain()

	ev.setUnreachable()
	if loop.config(ctx).Enabled {
		t.Fatal("experiments_enabled must fail closed (off) when flagd is unreachable")
	}
	if loop.flags.Evaluate(ctx).Enabled {
		t.Fatal("enabled must fail closed (off) when flagd is unreachable")
	}
	loop.allocateIfEnabled(ctx)

	if got := loop.registry.LiveCount(); got != 0 {
		t.Fatalf("flagd outage did not stop the experiment loop: LiveCount=%d, want 0", got)
	}
	if n := loop.registry.TotalInFlight(); n != 0 {
		t.Fatalf("flagd outage left %d shadow slots in flight, want 0", n)
	}
	if recs := spawner.drain(); len(recs) != 0 {
		t.Fatalf("flagd outage still spawned %d shadow games", len(recs))
	}
}

// recordingRollout is a rollout actuator that always writes (rollout_enabled on).
type recordingRollout struct {
	mu      sync.Mutex
	written []string
}

func (r *recordingRollout) SetControllerCount(variant string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.written = append(r.written, variant)
	return nil
}

func (r *recordingRollout) DryRun() bool { return false }

func (r *recordingRollout) NextStage(current int) (string, int, bool) {
	if current == 0 {
		return "one", 1, true
	}
	return "", 0, false
}

func (r *recordingRollout) StageVariantForValue(value int) (string, bool) {
	names := map[int]string{0: "none", 1: "one"}
	n, ok := names[value]
	return n, ok
}

func (r *recordingRollout) calls() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.written...)
}

// rolloutHealthSpan appends a controller.bluetooth_health span for an ACTIVE
// rollout (target backend set, stage 0) with healthy Bluetooth signals.
func rolloutHealthSpan(td ptrace.Traces, serial string) {
	span := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	span.SetName(infracontext.SpanBluetoothHealth)
	span.Attributes().PutDouble(infracontext.AttrEventGapMs, 20)
	span.Attributes().PutDouble(infracontext.AttrDroppedEventsPct, 0.01)
	span.Attributes().PutDouble(infracontext.AttrMovementUpdateHz, 60)
	span.Attributes().PutInt(infracontext.AttrActiveControllers, 1)
	span.Attributes().PutStr(infracontext.AttrTargetBackend, "rust")
	span.Attributes().PutInt(infracontext.AttrRolloutCount, 0)
	ev := span.Events().AppendEmpty()
	ev.SetName(infracontext.EventControllerSample)
	ev.Attributes().PutStr(infracontext.AttrControllerSerial, serial)
	ev.Attributes().PutDouble(infracontext.AttrMovementUpdateHz, 60)
	ev.Attributes().PutStr(infracontext.AttrControllerAdapter, "python")
}

// TestKillSwitch_InfraLoopKeepsEvaluatingWhileAgentOff: with `enabled` off (the
// fail-closed baseline the kill switch restores) a Bluetooth-health batch through
// the receiver still reaches the real infrastructure loop, which evaluates fitness
// and records an agent.infrastructure.decision span: `enabled` does not govern the
// rollout, only rollout_enabled / remediation_allowed do.
// UC: UC-045 A4 Post-S-3
func TestKillSwitch_InfraLoopKeepsEvaluatingWhileAgentOff(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	agentFlags := flags.New(&switchableEvaluator{booleans: map[string]bool{"enabled": false}}, nil)
	if agentFlags.Evaluate(context.Background()).Enabled {
		t.Fatal("precondition: the agent must be off")
	}

	// The infra loop takes its tracer from the global provider at construction.
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	prev := otel.GetTracerProvider()
	otel.SetTracerProvider(tp)
	t.Cleanup(func() {
		otel.SetTracerProvider(prev)
		_ = tp.Shutdown(context.Background())
	})
	rollout := &recordingRollout{}
	infraLoop := decision.NewInfraLoop(logger, decision.DefaultRolloutDwell, decision.NewLiveBluetoothFitness(), rollout)
	infraLoop.SetGate(func(infracontext.InfraContext, time.Time) bool { return true })

	mux := gamecontext.NewMultiplexer(func(string) *gamecontext.Store {
		return gamecontext.NewStore(time.Hour, time.Hour, fixedClock())
	})
	loops := decision.NewLoopSet(func(string) *decision.Loop { return decision.NewLoop(agentFlags, logger) })
	infraStore := infracontext.NewStore(time.Hour, fixedClock())
	r := &traceReceiver{pipe: newPipeline(mux, loops, time.Hour).withInfra(infraStore, infraLoop)}

	td := ptrace.NewTraces()
	rolloutHealthSpan(td, "A")
	if _, err := r.Export(context.Background(), ptraceotlp.NewExportRequestFromTraces(td)); err != nil {
		t.Fatalf("Export error: %v", err)
	}

	var decisions int
	for _, s := range sr.Ended() {
		if s.Name() == decision.SpanInfraDecision {
			decisions++
		}
	}
	if decisions == 0 {
		t.Fatal("agent off stopped the infrastructure loop: no agent.infrastructure.decision span")
	}
	// Healthy fitness at stage 0 with rollout_enabled on: the loop expands as usual.
	if got := rollout.calls(); len(got) != 1 || got[0] != "one" {
		t.Fatalf("agent off changed the rollout: writes = %v, want [one]", got)
	}
}
