package main

import (
	"context"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/experiment"
)

// run() arms a heartbeat ticker at experiment.ProgressHeartbeatInterval and every
// heartbeat tick emits one experiment.progress per live experiment (#2059).
// UC: UC-035 4
func TestExperimentLoop_ProgressHeartbeat(t *testing.T) {
	loop, _, _ := loopForConfigTest(t, 2) // gates closed: the refill ticks are inert
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	reg := experiment.NewRegistry(experiment.RegistryConfig{
		Root: t.TempDir(), MaxShadowGames: 2, Tracer: tp.Tracer("test"), Log: testLogger(),
	})
	loop.registry = reg

	refill := &fakeLoopTicker{c: make(chan time.Time)}
	armed := make(chan struct{})
	loop.newTicker = func(d time.Duration) loopTicker { close(armed); return refill }
	hb := &fakeLoopTicker{c: make(chan time.Time)}
	loop.newHeartbeat = func(d time.Duration) loopTicker { hb.Reset(d); return hb }

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		loop.run(ctx)
		close(done)
	}()
	<-armed // the inline first tick (which aborts everything, gates closed) is over

	id, err := reg.Declare(experiment.Intent{
		Hypothesis: "h", FlagKey: "death_grace_period_seconds", ExperimentalValue: 0.5,
		Objective: "balanced", TargetNPerArm: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := reg.Start(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	hb.c <- time.Now()
	cancel()
	<-done

	if got := hb.intervals(); len(got) != 1 || got[0] != experiment.ProgressHeartbeatInterval {
		t.Fatalf("heartbeat armed with %v, want [%v]", got, experiment.ProgressHeartbeatInterval)
	}
	beats := 0
	for _, s := range sr.Ended() {
		if s.Name() != experiment.SpanExperimentProgress {
			continue
		}
		for _, kv := range s.Attributes() {
			if string(kv.Key) == experiment.AttrExperimentProgressTrigger && kv.Value.AsString() == experiment.ProgressTriggerHeartbeat {
				beats++
			}
		}
	}
	if beats != 1 {
		t.Fatalf("heartbeat snapshots = %d, want 1", beats)
	}
}
