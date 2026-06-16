package decision

import (
	"context"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamesummary"
	"github.com/joustmania/agent/gamewindow"
	"github.com/joustmania/agent/llm"
)

// context_window_test.go drives the M7-2 rolling-context-window injection (#929)
// end-to-end through the llm decision path: a wired gamewindow.Store + the #739
// inferBackend fake (records lastPromptSystem) proves the rendered cross-game block
// reaches the prompt, that N is read LIVE from the flag, and that the inference span
// carries agent.llm.context_games = the injected count.

// recordDistinctGames records summaries whose RENDERED header line is unique (the
// player count varies), so a test can assert WHICH games landed in the prompt — the
// rendered block uses an ordinal, not the GameID, so we distinguish on a visible
// field. Recorded oldest-first; the last recorded is the newest.
func recordDistinctGames(store *gamewindow.Store, playerCounts ...int) {
	for _, pc := range playerCounts {
		store.Record(gamesummary.Summary{
			GameKind:         "real",
			GameMode:         "joust",
			PlayerCount:      pc,
			EliminationCount: 3,
		})
	}
}

// playersLine is the rendered substring identifying a game with the given player
// count in the cross-game block (each game renders "... players=<n> ...").
func playersLine(pc int) string {
	return "players=" + strconv.Itoa(pc) + " "
}

func TestContextWindow_BlockReachesPrompt(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	snap := llmDecideSnapshot()
	snap.ContextGames = 2

	l, sr, _ := llmDecideLoop(t, snap, resolverWith(be), []Decision{{Intervention: "play_audio_cue", Reason: "rules"}})

	store := gamewindow.NewStore()
	recordDistinctGames(store, 2, 4, 6) // oldest players=2, newest players=6; N=2 -> 4 & 6
	l.SetContextWindow(store)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	if be.calls != 1 {
		t.Fatalf("backend Infer calls = %d, want 1", be.calls)
	}
	// The prompt must contain the EXACT rendered block for the last 2 summaries.
	want := gamewindow.Render(store.Recent(2))
	if !strings.Contains(be.lastPromptSystem, want) {
		t.Errorf("prompt System missing the rendered cross-game block:\n--- want block ---\n%s\n--- got system ---\n%s", want, be.lastPromptSystem)
	}
	// It must reflect N=2: the 2 newest (players=4, players=6) appear, the oldest
	// (players=2) does NOT.
	if !strings.Contains(be.lastPromptSystem, playersLine(4)) || !strings.Contains(be.lastPromptSystem, playersLine(6)) {
		t.Errorf("prompt missing the 2 newest games:\n%s", be.lastPromptSystem)
	}
	if strings.Contains(be.lastPromptSystem, playersLine(2)) {
		t.Errorf("prompt includes the oldest game (players=2), but N=2 should exclude it:\n%s", be.lastPromptSystem)
	}

	// The inference span carries context_games = 2 (the injected COUNT).
	if c := injectedCount(t, sr); c != 2 {
		t.Errorf("%s = %d, want 2", AttrLLMContextGames, c)
	}
}

func TestContextWindow_FlagIsLive(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	fl := &settableFlags{snap: llmDecideSnapshot()}
	fl.snap.ContextGames = 1

	l, sr, _ := llmDecideLoop(t, fl.snap, resolverWith(be), []Decision{{Intervention: "play_audio_cue", Reason: "rules"}})
	l.Flags = fl // swap in the mutable source so the flag can be flipped mid-test

	store := gamewindow.NewStore()
	recordDistinctGames(store, 2, 4, 6) // oldest players=2, newest players=6
	l.SetContextWindow(store)

	// First call: N=1 -> only the newest (players=6) injected, count 1.
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())
	if c := lastInjectedCount(t, sr); c != 1 {
		t.Fatalf("first call injected count = %d, want 1", c)
	}
	if !strings.Contains(be.lastPromptSystem, playersLine(6)) {
		t.Errorf("N=1 should inject the newest game (players=6):\n%s", be.lastPromptSystem)
	}
	if strings.Contains(be.lastPromptSystem, playersLine(4)) {
		t.Errorf("N=1 should inject ONLY the newest, but players=4 appeared:\n%s", be.lastPromptSystem)
	}

	// Flip the flag LIVE to N=3 — read on the very next call, no restart.
	fl.snap.ContextGames = 3
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())
	if c := lastInjectedCount(t, sr); c != 3 {
		t.Fatalf("second call injected count = %d, want 3 (live flag)", c)
	}
	// All three games now appear, including the oldest (players=2).
	for _, pc := range []int{2, 4, 6} {
		if !strings.Contains(be.lastPromptSystem, playersLine(pc)) {
			t.Errorf("N=3 should inject all three games, but players=%d missing:\n%s", pc, be.lastPromptSystem)
		}
	}
}

func TestContextWindow_NilWindowNoBlockZeroCount(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	snap := llmDecideSnapshot()
	snap.ContextGames = 3 // even with a flag set, no window means no block

	// No SetContextWindow call: contextWindow stays nil.
	l, sr, _ := llmDecideLoop(t, snap, resolverWith(be), nil)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	if strings.Contains(be.lastPromptSystem, "PRIOR GAMES") {
		t.Errorf("nil window must inject no cross-game block, but PRIOR GAMES appeared:\n%s", be.lastPromptSystem)
	}
	if c := lastInjectedCount(t, sr); c != 0 {
		t.Errorf("nil window injected count = %d, want 0", c)
	}
}

func TestContextWindow_ClampsToRetentionCap(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	snap := llmDecideSnapshot()
	snap.ContextGames = gamewindow.RetentionCap + 50 // absurd over-config

	l, sr, _ := llmDecideLoop(t, snap, resolverWith(be), nil)

	store := gamewindow.NewStore()
	// Record exactly 2 games; the injected count is bounded by what's held, not the flag.
	recordDistinctGames(store, 2, 4)
	l.SetContextWindow(store)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	if c := lastInjectedCount(t, sr); c != 2 {
		t.Errorf("injected count = %d, want 2 (clamped to window length, flag was %d)", c, snap.ContextGames)
	}
}

func TestContextWindow_EmptyWindowRendersMarker(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	snap := llmDecideSnapshot()
	snap.ContextGames = 3

	// Window wired but EMPTY (no games ended yet): the block is still present with the
	// explicit "(no prior games)" marker, and the count is 0.
	l, sr, _ := llmDecideLoop(t, snap, resolverWith(be), nil)
	l.SetContextWindow(gamewindow.NewStore())

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	if !strings.Contains(be.lastPromptSystem, "PRIOR GAMES") {
		t.Errorf("wired empty window should still render the PRIOR GAMES section:\n%s", be.lastPromptSystem)
	}
	if !strings.Contains(be.lastPromptSystem, "(no prior games)") {
		t.Errorf("empty window should render the (no prior games) marker:\n%s", be.lastPromptSystem)
	}
	if c := lastInjectedCount(t, sr); c != 0 {
		t.Errorf("empty window injected count = %d, want 0", c)
	}
}

// TestContextWindow_CaptureSpanAlsoCarriesCount asserts the capture path
// (agent.llm.prompt, emitted on the first throttled cycle) ALSO carries
// context_games and injects the same block — the cheap-on-capture half of #929.
func TestContextWindow_CaptureSpanAlsoCarriesCount(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	snap := llmDecideSnapshot()
	snap.ContextGames = 2

	l, sr, _ := llmDecideLoop(t, snap, resolverWith(be), nil)
	store := gamewindow.NewStore()
	recordDistinctGames(store, 2, 4, 6)
	l.SetContextWindow(store)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	caps := spansByName(sr.Ended(), SpanLLMPrompt)
	if len(caps) != 1 {
		t.Fatalf("agent.llm.prompt spans = %d, want 1", len(caps))
	}
	if v, ok := attrValue(caps[0], AttrLLMContextGames); !ok || v.AsInt64() != 2 {
		t.Errorf("capture span %s = %d (present=%v), want 2", AttrLLMContextGames, v.AsInt64(), ok)
	}
	// #1168: the System text no longer rides the span (only its fingerprint), so the
	// cross-game block's injection is asserted via the context_games attr above; the
	// block's TEXT reaching the System prompt is covered by the production-path test
	// (TestContextWindow_AsyncPathInjectsBlock's backend assertion). Here we just
	// confirm the span carries the System-prompt fingerprint instead of the full text.
	if v, ok := attrValue(caps[0], AttrLLMPromptSystemSHA); !ok || v.AsString() == "" {
		t.Errorf("capture span missing the System-prompt fingerprint (llm.prompt.system_sha256)")
	}
}

// recordingBlockingBackend is the async-path fake for #929: like blockingBackend
// (its Infer blocks on a release channel so the test can assert the loop returned
// while inference is still running) but it also CAPTURES the System prompt it
// received, so the test can prove the cross-game block reached the PRODUCTION
// (async, #917) prompt — runInfer assembles the prompt, not the sync llmDecide path.
type recordingBlockingBackend struct {
	name             string
	response         string
	release          chan struct{}
	started          chan struct{}
	mu               sync.Mutex
	lastPromptSystem string
	calls            int
}

func newRecordingBlockingBackend(name, response string) *recordingBlockingBackend {
	return &recordingBlockingBackend{
		name:     name,
		response: response,
		release:  make(chan struct{}),
		started:  make(chan struct{}),
	}
}

func (b *recordingBlockingBackend) Name() string                   { return b.name }
func (b *recordingBlockingBackend) Available(context.Context) bool { return true }

func (b *recordingBlockingBackend) Infer(ctx context.Context, p llm.Prompt) (string, error) {
	b.mu.Lock()
	b.calls++
	first := b.calls == 1
	b.lastPromptSystem = p.System
	b.mu.Unlock()
	if first {
		close(b.started)
	}
	select {
	case <-b.release:
		return b.response, nil
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

func (b *recordingBlockingBackend) systemPrompt() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.lastPromptSystem
}

// TestContextWindow_AsyncPathInjectsBlock is the #929 re-integration coverage onto
// the #917 ASYNC inference path — the path PRODUCTION takes. A loop wired WITH a
// context provider (async enabled) + a populated context window must, when it fires
// an async Infer, carry the rendered "PRIOR GAMES" block in the System prompt the
// backend receives AND stamp agent.llm.context_games = the injected count on the
// agent.llm.infer span (emitted at apply time). Without runInfer's renderContextBlock
// call this whole feature would be bypassed in production — this test proves it isn't.
func TestContextWindow_AsyncPathInjectsBlock(t *testing.T) {
	be := newRecordingBlockingBackend("phi4-mini", validShieldResponse)
	snap := asyncSnapshot()
	snap.ContextGames = 2

	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, _ := asyncLoop(t, snap, resolverWith(be), provider, "g1", nil)

	store := gamewindow.NewStore()
	recordDistinctGames(store, 2, 4, 6) // oldest players=2, newest players=6; N=2 -> 4 & 6
	l.SetContextWindow(store)

	// Fire the async inference (the loop returns promptly; Infer is blocked).
	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	select {
	case <-be.started:
	case <-time.After(2 * time.Second):
		t.Fatal("async Infer never started — the loop did not fire on the async path")
	}
	close(be.release) // let the call complete
	l.AwaitInflight() // join the apply goroutine so the infer span is recorded

	if be.calls != 1 {
		t.Fatalf("backend Infer calls = %d, want 1", be.calls)
	}

	// 1) The PRODUCTION prompt carries the rendered cross-game block for the last 2.
	got := be.systemPrompt()
	want := gamewindow.Render(store.Recent(2))
	if !strings.Contains(got, want) {
		t.Errorf("async prompt System missing the cross-game block:\n--- want block ---\n%s\n--- got system ---\n%s", want, got)
	}
	// N=2: the 2 newest (players=4,6) appear; the oldest (players=2) does NOT.
	if !strings.Contains(got, playersLine(4)) || !strings.Contains(got, playersLine(6)) {
		t.Errorf("async prompt missing the 2 newest games:\n%s", got)
	}
	if strings.Contains(got, playersLine(2)) {
		t.Errorf("async prompt includes the oldest game (players=2), but N=2 excludes it:\n%s", got)
	}

	// 2) The agent.llm.infer span (emitted at APPLY time) carries the injected count.
	if c := lastInjectedCount(t, sr); c != 2 {
		t.Errorf("async infer span %s = %d, want 2", AttrLLMContextGames, c)
	}
}

// TestContextWindow_AsyncNilWindowNoBlock confirms an async loop WITHOUT a context
// window injects no cross-game block and reports context_games = 0 — the async path
// is purely additive, exactly like the sync path's nil-window case.
func TestContextWindow_AsyncNilWindowNoBlock(t *testing.T) {
	be := newRecordingBlockingBackend("phi4-mini", validShieldResponse)
	snap := asyncSnapshot()
	snap.ContextGames = 3 // flag set, but no window wired

	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, _ := asyncLoop(t, snap, resolverWith(be), provider, "g1", nil)
	// NOTE: no l.SetContextWindow — window is nil.

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	select {
	case <-be.started:
	case <-time.After(2 * time.Second):
		t.Fatal("async Infer never started")
	}
	close(be.release)
	l.AwaitInflight()

	if strings.Contains(be.systemPrompt(), "PRIOR GAMES") {
		t.Errorf("nil window must inject no cross-game block on the async path:\n%s", be.systemPrompt())
	}
	if c := lastInjectedCount(t, sr); c != 0 {
		t.Errorf("async nil-window injected count = %d, want 0", c)
	}
}

// injectedCount reads the FIRST inference span's context_games attribute.
func injectedCount(t *testing.T, sr *tracetest.SpanRecorder) int64 {
	t.Helper()
	infs := spansByName(sr.Ended(), SpanLLMInfer)
	if len(infs) == 0 {
		t.Fatalf("no agent.llm.infer span recorded")
	}
	v, ok := attrValue(infs[0], AttrLLMContextGames)
	if !ok {
		t.Fatalf("first agent.llm.infer span missing %s", AttrLLMContextGames)
	}
	return v.AsInt64()
}

// lastInjectedCount reads the MOST RECENT inference span's context_games attribute,
// for tests that call OnEvaluate more than once (the live-flag test).
func lastInjectedCount(t *testing.T, sr *tracetest.SpanRecorder) int64 {
	t.Helper()
	infs := spansByName(sr.Ended(), SpanLLMInfer)
	if len(infs) == 0 {
		t.Fatalf("no agent.llm.infer span recorded")
	}
	v, ok := attrValue(infs[len(infs)-1], AttrLLMContextGames)
	if !ok {
		t.Fatalf("last agent.llm.infer span missing %s", AttrLLMContextGames)
	}
	return v.AsInt64()
}
