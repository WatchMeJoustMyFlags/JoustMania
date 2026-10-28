package experiment

import (
	"context"
	"strconv"
	"strings"
	"testing"
	"unicode/utf8"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/patterngame"
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
		decision.AttrExperimentThesis:    in.Hypothesis,
		AttrExperimentValue:              "0.5",
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

// The concluded and promoting transitions each emit a snapshot, and the
// game_concluded snapshot already carries the interim verdict of that game.
// UC: UC-035 4
func TestProgress_ConcludedAndPromotingSnapshots(t *testing.T) {
	spawner := &fakeSpawner{}
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: fakeVerdict{outcome: CohortOutcomePromote, concludeAtN: 1},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2,
	})
	startedExperiment(t, r)
	r.AllocateAndSpawn(context.Background())
	if _, err := r.ConcludeGame(context.Background(), spawner.calls[0].gameID, 0.9, 60); err != nil {
		t.Fatal(err)
	}
	want := []string{
		ProgressTriggerStarted,
		ProgressTriggerGameAssigned, ProgressTriggerGameAssigned,
		ProgressTriggerGameConcluded, ProgressTriggerConcluded, ProgressTriggerPromoting,
		ProgressTriggerTerminal,
	}
	if got := progressTriggers(sr); !equalTriggers(got, want) {
		t.Fatalf("UC-035/4 triggers = %v, want %v", got, want)
	}
	spans := progressSpans(sr)
	for i, wantStatus := range map[int]Status{4: StatusConcluded, 5: StatusPromoting} {
		if v, _ := rootSpanAttr(spans[i], decision.AttrExperimentStatus); v != string(wantStatus) {
			t.Errorf("%s snapshot experiment.status = %q, want %q", progressTriggersOf(spans[i]), v, wantStatus)
		}
	}
	if v, _ := rootSpanAttr(spans[3], decision.AttrVerdictOutcome); v != CohortOutcomePromote {
		t.Errorf("game_concluded verdict.outcome = %q, want %q (the verdict of this game)", v, CohortOutcomePromote)
	}
}

// verdict.* on a game_concluded snapshot is the interim verdict computed from that
// game, never the previous game's.
// UC: UC-035 4
func TestProgress_GameConcludedVerdictDoesNotLag(t *testing.T) {
	spawner := &fakeSpawner{}
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: fakeVerdict{concludeAtN: 1000, alwaysReport: true},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2,
	})
	startedExperiment(t, r)
	r.AllocateAndSpawn(context.Background())
	if _, err := r.ConcludeGame(context.Background(), spawner.calls[0].gameID, 0.4, 60); err != nil {
		t.Fatal(err)
	}
	spans := progressSpans(sr)
	last := spans[len(spans)-1]
	if progressTriggersOf(last) != ProgressTriggerGameConcluded {
		t.Fatalf("last trigger = %q, want game_concluded", progressTriggersOf(last))
	}
	if v, ok := rootSpanAttr(last, decision.AttrVerdictOutcome); !ok || v != OutcomeInconclusive {
		t.Fatalf("first game_concluded verdict.outcome = %q (set %v), want %q", v, ok, OutcomeInconclusive)
	}
}

// abortingVerdict tears every experiment down from inside Evaluate, the window
// between the fold and the game_concluded snapshot that a concurrent AbortAll
// (kill switch, experiments_enabled off) can hit.
type abortingVerdict struct{ r **Registry }

func (v abortingVerdict) Evaluate(journal.Summary) (journal.Verdict, bool) {
	(*v.r).AbortAll(context.Background(), "kill switch")
	return journal.Verdict{Outcome: OutcomeInconclusive}, true
}

// An experiment torn down between the fold and the game_concluded snapshot has
// already emitted its terminal snapshot: no game_concluded follows it.
// UC: UC-035 4
func TestProgress_TornDownDuringConcludeEmitsNoGameConcluded(t *testing.T) {
	spawner := &fakeSpawner{}
	var reg *Registry
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: abortingVerdict{r: &reg},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2,
	})
	reg = r
	startedExperiment(t, r)
	r.AllocateAndSpawn(context.Background())
	if _, err := r.ConcludeGame(context.Background(), spawner.calls[0].gameID, 0.9, 60); err != nil {
		t.Fatal(err)
	}
	want := []string{
		ProgressTriggerStarted,
		ProgressTriggerGameAssigned, ProgressTriggerGameAssigned,
		ProgressTriggerTerminal,
	}
	if got := progressTriggers(sr); !equalTriggers(got, want) {
		t.Fatalf("UC-035/4 triggers = %v, want %v (no game_concluded after terminal)", got, want)
	}
}

// A pattern experiment whose pin cannot be restored is discarded on rehydrate: it
// emits only its terminal snapshot, never a rehydrate snapshot with status running.
// UC: UC-046 BR-015
func TestProgress_LostPinRehydrateEmitsOnlyTerminal(t *testing.T) {
	root := t.TempDir()
	r1 := newTestRegistry(t, RegistryConfig{Root: root, MaxShadowGames: 2, Spawner: &fakeSpawner{}})
	id := startedExperiment(t, r1)
	r1.AllocateAndSpawn(context.Background()) // a game assigned with no pin

	r2, sr2 := rootSpanRegistry(t, RegistryConfig{Root: root, MaxShadowGames: 2, Spawner: &patternSpawner{},
		Patterns: &queuePinner{pins: []patterngame.Pin{testPin(t, "a")}}})
	if err := r2.Rehydrate([]string{id}); err != nil {
		t.Fatal(err)
	}
	if got := progressTriggers(sr2); !equalTriggers(got, []string{ProgressTriggerTerminal}) {
		t.Fatalf("UC-046/BR-015 triggers after a lost-pin rehydrate = %v, want [terminal]", got)
	}
}

// hookSpawner runs onSpawn (once) inside its first Spawn call: after the pair's
// slots are reserved, before any game is bound.
type hookSpawner struct {
	fakeSpawner
	onSpawn func()
}

func (h *hookSpawner) Spawn(ctx context.Context, experimentID, arm string, seed uint64) (string, error) {
	if f := h.onSpawn; f != nil {
		h.onSpawn = nil
		f()
	}
	return h.fakeSpawner.Spawn(ctx, experimentID, arm, seed)
}

// experiment.arm_stats.assigned counts bound in-flight games only: a pending spawn
// reservation is not a game yet.
// UC: UC-035 4
func TestProgress_AssignedExcludesSpawnReservations(t *testing.T) {
	spawner := &hookSpawner{}
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)
	reserved := 0
	spawner.onSpawn = func() {
		r.mu.Lock()
		for gid := range r.entries[id].games {
			if isReservation(gid) {
				reserved++
			}
		}
		r.mu.Unlock()
		r.ProgressHeartbeat()
	}
	r.AllocateAndSpawn(context.Background())
	if reserved == 0 {
		t.Fatal("no spawn reservation pending inside Spawn; the test does not cover the exclusion")
	}
	var hb []sdktrace.ReadOnlySpan
	for _, s := range progressSpans(sr) {
		if progressTriggersOf(s) == ProgressTriggerHeartbeat {
			hb = append(hb, s)
		}
	}
	if len(hb) != 1 {
		t.Fatalf("heartbeat snapshots = %d, want 1", len(hb))
	}
	for arm, a := range armEvents(t, hb[0]) {
		if a.assigned != 0 {
			t.Errorf("arm %s assigned = %d with only reservations pending, want 0", arm, a.assigned)
		}
	}
}

// The concluded snapshot carries the FINAL verdict (#2042), not the last interim
// one: a give-up conclusion with no interim verdict still reports verdict.outcome.
// UC: UC-035 4
func TestProgress_ConcludedSnapshotCarriesFinalVerdict(t *testing.T) {
	spawner := &fakeSpawner{}
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: fakeVerdict{concludeAtN: 1000},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2, MaxGamesPerExperiment: 1,
	})
	startedExperiment(t, r)
	r.AllocateAndSpawn(context.Background())
	if _, err := r.ConcludeGame(context.Background(), spawner.calls[0].gameID, 0.4, 60); err != nil {
		t.Fatal(err)
	}
	var gameConcluded, concluded int = -1, -1
	spans := progressSpans(sr)
	for i, s := range spans {
		switch progressTriggersOf(s) {
		case ProgressTriggerGameConcluded:
			gameConcluded = i
		case ProgressTriggerConcluded:
			concluded = i
		}
	}
	if gameConcluded < 0 || concluded < 0 {
		t.Fatalf("triggers = %v, want game_concluded and concluded", progressTriggers(sr))
	}
	if _, ok := rootSpanAttr(spans[gameConcluded], decision.AttrVerdictOutcome); ok {
		t.Fatalf("game_concluded carries a verdict, want none (no interim verdict)")
	}
	if v, ok := rootSpanAttr(spans[concluded], decision.AttrVerdictOutcome); !ok || v != OutcomeInconclusive {
		t.Fatalf("concluded verdict.outcome = %q (set %v), want final %q", v, ok, OutcomeInconclusive)
	}
}

func TestCapSpanText(t *testing.T) {
	if got := capSpanText("short"); got != "short" {
		t.Fatalf("short text changed: %q", got)
	}
	long := strings.Repeat("a", 600)
	got := capSpanText(long)
	if len(got) != maxSpanTextLen || !strings.HasSuffix(got, "...") {
		t.Fatalf("ascii cap: len=%d", len(got))
	}
	// multi-byte runes straddling the cut stay valid UTF-8
	multi := capSpanText(strings.Repeat("\u00e4", 400))
	if len(multi) > maxSpanTextLen || !utf8.ValidString(multi) || !strings.HasSuffix(multi, "...") {
		t.Fatalf("utf8 cap: len=%d valid=%v", len(multi), utf8.ValidString(multi))
	}
}

func TestProgress_ThesisAndValueCapped(t *testing.T) {
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	in := sampleIntent()
	in.Hypothesis = strings.Repeat("h", 2000)
	in.ExperimentalValue = strings.Repeat("v", 2000)
	id, err := r.Declare(in)
	if err != nil {
		t.Fatalf("declare: %v", err)
	}
	if err := r.Start(context.Background(), id); err != nil {
		t.Fatalf("start: %v", err)
	}
	spans := progressSpans(sr)
	if len(spans) == 0 {
		t.Fatal("no experiment.progress span")
	}
	seen := 0
	for _, kv := range spans[0].Attributes() {
		k := string(kv.Key)
		if k == decision.AttrExperimentThesis || k == AttrExperimentValue {
			seen++
			if v := kv.Value.AsString(); len(v) != maxSpanTextLen || !strings.HasSuffix(v, "...") {
				t.Errorf("%s: len=%d, want %d ending in ...", k, len(v), maxSpanTextLen)
			}
		}
	}
	if seen != 2 {
		t.Fatalf("saw %d capped attrs, want 2", seen)
	}
}

// A promotion refused because another writer owns the real overlay (UC-043 A19,
// #2276) puts the winning value on the experiment's snapshots as a recommendation.
// UC: UC-043 A19
func TestProgress_RefusedPromotionCarriesRecommendation(t *testing.T) {
	spawner := &fakeSpawner{}
	promoter := &fakePromoter{result: PromotionResult{
		Outcome: PromotionDiscarded, Reason: "real_overlay_owned_by_another_writer owner=operator", ForeignOwner: "operator",
	}}
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: fakeVerdict{outcome: CohortOutcomePromote, concludeAtN: 1},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2, Promoter: promoter,
	})
	startedExperiment(t, r)
	r.AllocateAndSpawn(context.Background())
	if _, err := r.ConcludeGame(context.Background(), spawner.calls[0].gameID, 0.9, 60); err != nil {
		t.Fatal(err)
	}
	spans := progressSpans(sr)
	last := spans[len(spans)-1]
	for key, want := range map[string]string{
		AttrExperimentRecommendationFlag:  "death_grace_period_seconds",
		AttrExperimentRecommendationValue: "0.5",
		AttrExperimentRecommendationOwner: "operator",
	} {
		if got, _ := rootSpanAttr(last, key); got != want {
			t.Errorf("terminal snapshot %s = %q, want %q", key, got, want)
		}
	}
	if _, ok := rootSpanAttr(spans[0], AttrExperimentRecommendationOwner); ok {
		t.Error("started snapshot carries a recommendation")
	}
}

// A deferred write refused after the experiment ended (RecordPromotion) emits a
// fresh snapshot with the recommendation; a plain later outcome emits none.
// UC: UC-043 A19
func TestProgress_DeferredRefusalEmitsRecommendationSnapshot(t *testing.T) {
	spawner := &fakeSpawner{}
	promoter := &fakePromoter{result: PromotionResult{Outcome: PromotionDiscarded, Reason: "deferred"}}
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: fakeVerdict{outcome: CohortOutcomePromote, concludeAtN: 1},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2, Promoter: promoter,
	})
	id := startedExperiment(t, r)
	r.AllocateAndSpawn(context.Background())
	if _, err := r.ConcludeGame(context.Background(), spawner.calls[0].gameID, 0.9, 60); err != nil {
		t.Fatal(err)
	}
	before := len(progressSpans(sr))
	r.RecordPromotion(id, PromotionResult{Outcome: PromotionDiscarded, Reason: "other"})
	if n := len(progressSpans(sr)); n != before {
		t.Fatalf("a plain later outcome emitted %d snapshots", n-before)
	}
	r.RecordPromotion(id, PromotionResult{Outcome: PromotionDiscarded, Reason: "real_overlay_owned_by_another_writer owner=script", ForeignOwner: "script"})
	spans := progressSpans(sr)
	if len(spans) != before+1 {
		t.Fatalf("snapshots = %d, want %d", len(spans), before+1)
	}
	last := spans[len(spans)-1]
	if got, _ := rootSpanAttr(last, AttrExperimentProgressTrigger); got != ProgressTriggerRecommendation {
		t.Errorf("trigger = %q", got)
	}
	if got, _ := rootSpanAttr(last, AttrExperimentRecommendationOwner); got != "script" {
		t.Errorf("owner = %q, want script", got)
	}
	// The dashboard reason line must survive the snapshot becoming the newest.
	want, _ := rootSpanAttr(spans[before-1], AttrExperimentEndReason)
	if got, ok := rootSpanAttr(last, AttrExperimentEndReason); !ok || got != want || got == "" {
		t.Errorf("recommendation end_reason = %q (set %v), want %q", got, ok, want)
	}
	if got, _ := rootSpanAttr(last, decision.AttrExperimentStatus); got != string(StatusDiscarded) {
		t.Errorf("status = %q, want discarded", got)
	}
}

// After a restart a done experiment is not in memory: its deferred refusal still
// emits a recommendation snapshot, built from the journal, with the end reason.
// UC: UC-043 A19
func TestProgress_DeferredRefusalAfterRestartEmitsSnapshot(t *testing.T) {
	root := t.TempDir()
	spawner := &fakeSpawner{}
	r1, _ := rootSpanRegistry(t, RegistryConfig{
		Root: root, Spawner: spawner, Verdict: fakeVerdict{outcome: CohortOutcomePromote, concludeAtN: 1},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2,
		Promoter: &fakePromoter{result: PromotionResult{Outcome: PromotionDiscarded, Reason: "deferred"}},
	})
	id := startedExperiment(t, r1)
	r1.AllocateAndSpawn(context.Background())
	if _, err := r1.ConcludeGame(context.Background(), spawner.calls[0].gameID, 0.9, 60); err != nil {
		t.Fatal(err)
	}

	r2, sr2 := rootSpanRegistry(t, RegistryConfig{Root: root, Spawner: &fakeSpawner{}, MaxShadowGames: 2})
	if err := r2.Rehydrate([]string{id}); err != nil {
		t.Fatal(err)
	}
	r2.RecordPromotion(id, PromotionResult{Outcome: PromotionDiscarded, Reason: "real_overlay_owned_by_another_writer owner=script", ForeignOwner: "script"})
	spans := progressSpans(sr2)
	if len(spans) != 1 {
		t.Fatalf("snapshots after restart = %d, want 1", len(spans))
	}
	if got, _ := rootSpanAttr(spans[0], AttrExperimentProgressTrigger); got != ProgressTriggerRecommendation {
		t.Errorf("trigger = %q", got)
	}
	if got, _ := rootSpanAttr(spans[0], AttrExperimentRecommendationOwner); got != "script" {
		t.Errorf("owner = %q, want script", got)
	}
	if got, ok := rootSpanAttr(spans[0], AttrExperimentEndReason); !ok || got == "" {
		t.Errorf("end_reason = %q (set %v), want the teardown reason", got, ok)
	}
	if got, _ := rootSpanAttr(spans[0], decision.AttrExperimentStatus); got != string(StatusDiscarded) {
		t.Errorf("status = %q, want discarded", got)
	}
}

// UC: UC-043 A19
func TestLastForeignOwner(t *testing.T) {
	ev := []journal.Event{
		{Promotion: &PromotionResult{ForeignOwner: "a"}},
		{Promotion: &PromotionResult{}},
	}
	if got := lastForeignOwner(ev); got != "a" {
		t.Errorf("lastForeignOwner = %q, want a", got)
	}
	if got := lastForeignOwner(nil); got != "" {
		t.Errorf("lastForeignOwner(nil) = %q", got)
	}
}
