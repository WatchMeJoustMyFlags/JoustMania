package experiment

import (
	"context"
	"strconv"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decision"
)

// progress_test.go covers experiment.progress (#2059, docs/specs/telemetry.md §4.1):
// a registry snapshot per state change and per heartbeat, its own root with a Link to
// agent.experiment, numbers equal to the journal summary.

func progressSpans(sr *tracetest.SpanRecorder) []sdktrace.ReadOnlySpan {
	var out []sdktrace.ReadOnlySpan
	for _, s := range sr.Ended() {
		if s.Name() == SpanExperimentProgress {
			out = append(out, s)
		}
	}
	return out
}

func progressTriggers(sr *tracetest.SpanRecorder) []string {
	var out []string
	for _, s := range progressSpans(sr) {
		v, _ := rootSpanAttr(s, AttrExperimentProgressTrigger)
		out = append(out, v)
	}
	return out
}

// armEvent is one experiment.arm event's numbers.
type armEvent struct {
	assigned, samples int64
	mean              float64
}

func armEvents(t *testing.T, s sdktrace.ReadOnlySpan) map[string]armEvent {
	t.Helper()
	out := map[string]armEvent{}
	for _, ev := range s.Events() {
		if ev.Name != EventExperimentArm {
			continue
		}
		var arm string
		var a armEvent
		for _, kv := range ev.Attributes {
			switch string(kv.Key) {
			case decision.AttrExperimentArm:
				arm = kv.Value.AsString()
			case AttrExperimentArmAssigned:
				a.assigned = kv.Value.AsInt64()
			case AttrExperimentArmSamples:
				a.samples = kv.Value.AsInt64()
			case AttrExperimentArmMeanFitness:
				a.mean = kv.Value.AsFloat64()
			}
		}
		out[arm] = a
	}
	return out
}

func equalTriggers(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}

// UC: UC-035 4
func TestProgress_EmittedOnEveryStateChange(t *testing.T) {
	spawner := &fakeSpawner{}
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)
	r.AllocateAndSpawn(context.Background()) // binds one game per arm
	if len(spawner.calls) != 2 {
		t.Fatalf("spawned = %d, want 2", len(spawner.calls))
	}
	if _, err := r.ConcludeGame(context.Background(), spawner.calls[0].gameID, 0.4, 60); err != nil {
		t.Fatal(err)
	}
	r.ReleaseGame(spawner.calls[1].gameID, "game_error")
	if err := r.Abort(context.Background(), id, "operator stop"); err != nil {
		t.Fatal(err)
	}

	want := []string{
		ProgressTriggerStarted,
		ProgressTriggerGameAssigned, ProgressTriggerGameAssigned,
		ProgressTriggerGameConcluded, ProgressTriggerGameReleased,
		ProgressTriggerTerminal,
	}
	if got := progressTriggers(sr); !equalTriggers(got, want) {
		t.Fatalf("UC-035/4 triggers = %v, want %v", got, want)
	}
	last := progressSpans(sr)[len(want)-1]
	if v, _ := rootSpanAttr(last, AttrExperimentEndReason); v != "operator stop" {
		t.Errorf("experiment.end_reason = %q, want %q", v, "operator stop")
	}
	if v, _ := rootSpanAttr(last, decision.AttrExperimentStatus); v != string(StatusAborted) {
		t.Errorf("experiment.status = %q, want %q", v, StatusAborted)
	}
	for _, s := range progressSpans(sr)[:len(want)-1] {
		if _, ok := rootSpanAttr(s, AttrExperimentEndReason); ok {
			t.Errorf("%s snapshot carries experiment.end_reason; terminal only", s.Name())
		}
	}
}

// UC: UC-035 4
func TestProgress_AttributeSet(t *testing.T) {
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)
	spans := progressSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("experiment.progress = %d, want 1", len(spans))
	}
	in := sampleIntent()
	want := map[string]string{
		decision.AttrExperimentID:        id,
		decision.AttrExperimentStatus:    string(StatusRunning),
		decision.AttrExperimentFlagKey:   in.FlagKey,
		decision.AttrExperimentObjective: in.Objective,
		decision.AttrExperimentTargetN:   strconv.Itoa(r.entries[id].journal.Intent().TargetNPerArm), // reconciled up to min-N
		decision.AttrExperimentArms:      ArmExperimental + "," + ArmControl,
		AttrExperimentProgressTrigger:    ProgressTriggerStarted,
	}
	got := map[string]string{}
	for _, kv := range spans[0].Attributes() {
		got[string(kv.Key)] = kv.Value.Emit()
	}
	if len(got) != len(want) {
		t.Errorf("attributes = %v, want exactly %v (no verdict yet)", got, want)
	}
	for k, w := range want {
		if got[k] != w {
			t.Errorf("%s = %q, want %q", k, got[k], w)
		}
	}
	arms := armEvents(t, spans[0])
	if len(arms) != 2 {
		t.Fatalf("experiment.arm events = %v, want one per arm", arms)
	}
}

// UC: UC-035 4
func TestProgress_OwnRootLinkedToTheExperimentSpan(t *testing.T) {
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)
	expCtx, ok := r.ExperimentSpanContext(id)
	if !ok {
		t.Fatal("no agent.experiment span context")
	}
	r.ProgressHeartbeat()
	_ = r.Abort(context.Background(), id, "done") // terminal: the span ends first

	spans := progressSpans(sr)
	if len(spans) != 3 {
		t.Fatalf("experiment.progress = %d, want 3 (started, heartbeat, terminal)", len(spans))
	}
	for _, s := range spans {
		if s.Parent().IsValid() {
			t.Errorf("%v: parent %s, want its own root", progressTriggersOf(s), s.Parent().SpanID())
		}
		if s.SpanContext().TraceID() == expCtx.TraceID() {
			t.Errorf("%v: in the agent.experiment trace, want its own trace", progressTriggersOf(s))
		}
		links := s.Links()
		if len(links) != 1 || links[0].SpanContext.SpanID() != expCtx.SpanID() || links[0].SpanContext.TraceID() != expCtx.TraceID() {
			t.Fatalf("%v: links = %v, want one Link to agent.experiment", progressTriggersOf(s), links)
		}
		if len(links[0].Attributes) != 1 || string(links[0].Attributes[0].Key) != "link.kind" || links[0].Attributes[0].Value.AsString() != "experiment" {
			t.Errorf("link attributes = %v, want link.kind=experiment", links[0].Attributes)
		}
	}
}

func progressTriggersOf(s sdktrace.ReadOnlySpan) string {
	v, _ := rootSpanAttr(s, AttrExperimentProgressTrigger)
	return v
}

// The snapshot's per-arm numbers equal the journal summary after every fold, and a
// game counts in samples only once the journal folded it.
// UC: UC-035 4
func TestProgress_NumbersEqualTheJournalSummary(t *testing.T) {
	spawner := &fakeSpawner{}
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)
	fitness := []float64{0.2, 0.6, 0.5, 0.9}
	for i, f := range fitness {
		r.AllocateAndSpawn(context.Background())
		g := spawner.calls[len(spawner.calls)-1]
		if _, err := r.ConcludeGame(context.Background(), g.gameID, f, 60); err != nil {
			t.Fatal(err)
		}
		spans := progressSpans(sr)
		last := spans[len(spans)-1]
		if trig := progressTriggersOf(last); trig != ProgressTriggerGameConcluded {
			t.Fatalf("round %d: last trigger = %q, want game_concluded", i, trig)
		}
		summary := r.entries[id].journal.Summary()
		got := armEvents(t, last)
		for arm, st := range summary.Arms {
			a := got[arm]
			if a.samples != int64(st.Count) || a.mean != st.Mean {
				t.Fatalf("round %d arm %s: span samples=%d mean=%v, journal count=%d mean=%v",
					i, arm, a.samples, a.mean, st.Count, st.Mean)
			}
		}
		inFlight := map[string]int64{}
		for gid, arm := range r.entries[id].games {
			if !isReservation(gid) {
				inFlight[arm]++
			}
		}
		for arm, a := range got {
			if a.assigned != inFlight[arm] {
				t.Fatalf("round %d arm %s: assigned=%d, in flight=%d", i, arm, a.assigned, inFlight[arm])
			}
		}
	}
}

// A game the registry does not know (never bound, so never folded) leaves no snapshot.
// UC: UC-035 4
func TestProgress_UnfoldedGameEmitsNothing(t *testing.T) {
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	startedExperiment(t, r)
	before := len(progressSpans(sr))
	if _, err := r.ConcludeGame(context.Background(), "not-ours", 0.9, 60); err != nil {
		t.Fatal(err)
	}
	if n := len(progressSpans(sr)); n != before {
		t.Fatalf("experiment.progress after an unknown game = %d, want %d", n, before)
	}
}

// UC: UC-035 4
func TestProgress_RehydrateEmitsExactlyOnce(t *testing.T) {
	root := t.TempDir()
	r1, _ := rootSpanRegistry(t, RegistryConfig{
		Root: root, Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2,
	})
	id := startedExperiment(t, r1)

	r2, sr2 := rootSpanRegistry(t, RegistryConfig{
		Root: root, Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2,
	})
	if err := r2.Rehydrate([]string{id}); err != nil {
		t.Fatal(err)
	}
	if err := r2.Rehydrate([]string{id}); err != nil { // a known id is left alone
		t.Fatal(err)
	}
	if got := progressTriggers(sr2); !equalTriggers(got, []string{ProgressTriggerRehydrate}) {
		t.Fatalf("triggers after rehydrate = %v, want [rehydrate]", got)
	}
	expCtx, _ := r2.ExperimentSpanContext(id)
	if l := progressSpans(sr2)[0].Links(); len(l) != 1 || l[0].SpanContext.SpanID() != expCtx.SpanID() {
		t.Errorf("rehydrate snapshot links = %v, want the fresh agent.experiment span", l)
	}
}

// UC: UC-035 4
func TestProgress_HeartbeatCoversLiveExperimentsOnly(t *testing.T) {
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 4,
	})
	live := startedExperiment(t, r)
	in := sampleIntent()
	in.FlagKey = "other_flag"
	done, err := r.Declare(in)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Start(context.Background(), done); err != nil {
		t.Fatal(err)
	}
	_ = r.Abort(context.Background(), done, "stop")
	before := len(progressSpans(sr))

	r.ProgressHeartbeat()
	spans := progressSpans(sr)[before:]
	if len(spans) != 1 {
		t.Fatalf("heartbeat snapshots = %d, want 1 (the live experiment)", len(spans))
	}
	if v, _ := rootSpanAttr(spans[0], decision.AttrExperimentID); v != live {
		t.Errorf("heartbeat experiment.id = %q, want %q", v, live)
	}
	if progressTriggersOf(spans[0]) != ProgressTriggerHeartbeat {
		t.Errorf("trigger = %q, want heartbeat", progressTriggersOf(spans[0]))
	}
	var nilRegistry *Registry
	nilRegistry.ProgressHeartbeat() // nil-safe
}
