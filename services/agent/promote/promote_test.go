package promote

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/experiment"
)

// ---- RECORDING FAKES for the three promotion seams (#936 safety rail). The
// Promoter owns only the dispatch logic; opening an issue/PR, committing, and the
// real-default write are faked here so every mode is exercised against EXACTLY what
// it "would" do — ZERO network, ZERO real git, ZERO real-default mutation. None of
// these fakes touch github.com or run git; they only record. ----

// recordingGitHub records the issue/PR it was asked to open and returns a canned
// URL (or a configured error). It is the unit-test stand-in for the GitHubClient
// wire boundary.
type recordingGitHub struct {
	issues   []IssueReq
	prs      []PRReq
	issueURL string
	prURL    string
	issueErr error
	prErr    error
}

func (g *recordingGitHub) OpenIssue(_ context.Context, req IssueReq) (string, error) {
	g.issues = append(g.issues, req)
	return g.issueURL, g.issueErr
}

func (g *recordingGitHub) OpenPR(_ context.Context, req PRReq) (string, error) {
	g.prs = append(g.prs, req)
	return g.prURL, g.prErr
}

// recordingGit records the commits it was asked to make and returns a canned SHA.
type recordingGit struct {
	commits []CommitReq
	sha     string
	err     error
}

func (g *recordingGit) Commit(_ context.Context, req CommitReq) (string, error) {
	g.commits = append(g.commits, req)
	return g.sha, g.err
}

// recordingRealDefault records the real-default writes it was asked to make. This
// is the highest-privilege seam; the fake NEVER writes a real file — it only
// records that a real-default change "would" have happened.
type recordingRealDefault struct {
	calls []struct {
		flag  string
		value any
	}
	err error
}

func (w *recordingRealDefault) SetRealDefault(_ context.Context, flag string, value any) error {
	w.calls = append(w.calls, struct {
		flag  string
		value any
	}{flag, value})
	return w.err
}

// fakeWatcher reports a configured degradation verdict.
type fakeWatcher struct {
	degraded bool
	err      error
}

func (w fakeWatcher) Degraded(context.Context, string) (bool, error) { return w.degraded, w.err }

// recordingReverter records real-default reverts.
type recordingReverter struct {
	calls int
	err   error
}

func (r *recordingReverter) RevertRealDefault(context.Context, string) error {
	r.calls++
	return r.err
}

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func testRepo() RepoRef {
	return RepoRef{Owner: "WatchMeJoustMyFlags", Name: "JoustMania", Base: "main"}
}

var promoteProposal = experiment.Proposal{
	FlagKey:           "death_grace_period_ms",
	ExperimentalValue: 500,
	Rationale:         "longer grace may extend sessions",
}

var promoteVerdict = experiment.Decision{
	Outcome:       experiment.OutcomePromote,
	FitnessBefore: 0.50,
	FitnessAfter:  0.72,
	Games:         5,
}

func testEvidence() Evidence {
	return Evidence{
		FitnessBefore:    0.50,
		FitnessAfter:     0.72,
		FitnessDelta:     0.22,
		SyntheticGames:   5,
		Reasoning:        "longer grace reduces early eliminations",
		FlagState:        "death_grace_period_ms: default=300, experimental=500",
		RealDefaultValue: 500,
	}
}

// promoterWithRecorder wires a Promoter over the given seams with an in-memory span
// recorder, mirroring the validate_test.go recorder pattern.
func promoterWithRecorder(t *testing.T, gh GitHubClient, git GitClient, realDef RealDefaultWriter, w Watcher, rev RealDefaultReverter) (*Promoter, *tracetest.SpanRecorder) {
	t.Helper()
	rec := tracetest.NewSpanRecorder()
	tp := trace.NewTracerProvider(trace.WithSpanProcessor(rec))
	p := NewPromoter(gh, git, realDef, w, rev, testRepo(), tp.Tracer("test"), discardLogger())
	return p, rec
}

func spanByName(t *testing.T, rec *tracetest.SpanRecorder, name string) trace.ReadOnlySpan {
	t.Helper()
	for _, s := range rec.Ended() {
		if s.Name() == name {
			return s
		}
	}
	t.Fatalf("span %q not found", name)
	return nil
}

func stringAttr(s trace.ReadOnlySpan, key string) string {
	for _, a := range s.Attributes() {
		if string(a.Key) == key {
			return a.Value.AsString()
		}
	}
	return ""
}

// --- mode=issue: opens an issue with hypothesis + flag, applies NOTHING else. ---
func TestPromoteIssueMode(t *testing.T) {
	gh := &recordingGitHub{issueURL: "https://github.com/WatchMeJoustMyFlags/JoustMania/issues/42"}
	git := &recordingGit{}
	realDef := &recordingRealDefault{}
	p, rec := promoterWithRecorder(t, gh, git, realDef, nil, nil)

	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModeIssue, Target: TargetLocal}, testEvidence())

	if res.Outcome != OutcomeApplied {
		t.Fatalf("outcome = %q, want applied", res.Outcome)
	}
	if len(gh.issues) != 1 {
		t.Fatalf("OpenIssue calls = %d, want 1", len(gh.issues))
	}
	// The issue body must name the hypothesis and the implicated flag.
	body := gh.issues[0].Body
	if !strings.Contains(body, promoteProposal.FlagKey) {
		t.Errorf("issue body missing flag key:\n%s", body)
	}
	if !strings.Contains(body, "longer grace reduces early eliminations") {
		t.Errorf("issue body missing reasoning:\n%s", body)
	}
	// Nothing else applied: no PR, no commit, no real-default change.
	if len(gh.prs) != 0 || len(git.commits) != 0 || len(realDef.calls) != 0 {
		t.Errorf("issue mode applied more than an issue: prs=%d commits=%d realDef=%d",
			len(gh.prs), len(git.commits), len(realDef.calls))
	}
	span := spanByName(t, rec, SpanPromote)
	if got := stringAttr(span, AttrGate); got != "issue" {
		t.Errorf("gate attr = %q, want issue", got)
	}
	if got := stringAttr(span, AttrOutcome); got != "applied" {
		t.Errorf("outcome attr = %q, want applied", got)
	}
	if got := stringAttr(span, AttrURL); got != gh.issueURL {
		t.Errorf("url attr = %q, want %q", got, gh.issueURL)
	}
}

// --- mode=pr, target=github: opens a PR with a STRUCTURED body, merges nothing. ---
func TestPromotePRModeGitHub(t *testing.T) {
	gh := &recordingGitHub{prURL: "https://github.com/WatchMeJoustMyFlags/JoustMania/pull/99"}
	git := &recordingGit{}
	realDef := &recordingRealDefault{}
	p, rec := promoterWithRecorder(t, gh, git, realDef, nil, nil)

	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModePR, Target: TargetGitHub}, testEvidence())

	if res.Outcome != OutcomeApplied {
		t.Fatalf("outcome = %q, want applied", res.Outcome)
	}
	if len(gh.prs) != 1 {
		t.Fatalf("OpenPR calls = %d, want 1", len(gh.prs))
	}
	body := gh.prs[0].Body
	// The #936 AC: structured body with fitness before/after, synthetic results,
	// reasoning, flag state.
	for _, want := range []string{
		"Fitness before/after", "0.5000", "0.7200", // before/after
		"Synthetic game results", "Synthetic games run:** 5",
		"Reasoning summary", "longer grace reduces early eliminations",
		"Flag state at improvement time", "death_grace_period_ms: default=300, experimental=500",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("PR body missing %q:\n%s", want, body)
		}
	}
	// No local commit, no real-default change, and the PR is opened — not merged
	// (the fake only records OpenPR; there is no merge call in the interface).
	if len(git.commits) != 0 || len(realDef.calls) != 0 {
		t.Errorf("pr/github mode applied locally: commits=%d realDef=%d", len(git.commits), len(realDef.calls))
	}
	span := spanByName(t, rec, SpanPromote)
	if got := stringAttr(span, AttrTarget); got != "github" {
		t.Errorf("target attr = %q, want github", got)
	}
}

// --- mode=pr, target=local: commits to the local git seam, opens no PR. ---
func TestPromotePRModeLocal(t *testing.T) {
	gh := &recordingGitHub{}
	git := &recordingGit{sha: "abc123"}
	p, _ := promoterWithRecorder(t, gh, git, nil, nil, nil)

	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModePR, Target: TargetLocal}, testEvidence())

	if res.Outcome != OutcomeApplied {
		t.Fatalf("outcome = %q, want applied", res.Outcome)
	}
	if len(git.commits) != 1 {
		t.Fatalf("Commit calls = %d, want 1", len(git.commits))
	}
	if len(gh.prs) != 0 || len(gh.issues) != 0 {
		t.Errorf("local target reached github: prs=%d issues=%d", len(gh.prs), len(gh.issues))
	}
	if !strings.HasPrefix(res.URL, "local:") {
		t.Errorf("local commit URL = %q, want local: prefix", res.URL)
	}
	// The committed file carries the structured body.
	if got := git.commits[0].Files; len(got) != 1 {
		t.Errorf("commit files = %d, want 1", len(got))
	}
}

// --- mode=autonomous: applies to the REAL default (separate writer), outcome
// applied; a degradation reverts. ---
func TestPromoteAutonomousApplied(t *testing.T) {
	realDef := &recordingRealDefault{}
	p, rec := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, nil, nil)

	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}, testEvidence())

	if res.Outcome != OutcomeApplied {
		t.Fatalf("outcome = %q, want applied", res.Outcome)
	}
	if len(realDef.calls) != 1 {
		t.Fatalf("SetRealDefault calls = %d, want 1", len(realDef.calls))
	}
	if realDef.calls[0].flag != promoteProposal.FlagKey || realDef.calls[0].value != 500 {
		t.Errorf("real-default call = %+v, want flag=%s value=500", realDef.calls[0], promoteProposal.FlagKey)
	}
	span := spanByName(t, rec, SpanPromote)
	if got := stringAttr(span, AttrGate); got != "autonomous" {
		t.Errorf("gate attr = %q, want autonomous", got)
	}
}

func TestPromoteAutonomousRevertsOnDegradation(t *testing.T) {
	realDef := &recordingRealDefault{}
	rev := &recordingReverter{}
	p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, fakeWatcher{degraded: true}, rev)

	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}, testEvidence())

	if res.Outcome != OutcomeReverted {
		t.Fatalf("outcome = %q, want reverted", res.Outcome)
	}
	if len(realDef.calls) != 1 {
		t.Errorf("SetRealDefault calls = %d, want 1 (applied then reverted)", len(realDef.calls))
	}
	if rev.calls != 1 {
		t.Errorf("RevertRealDefault calls = %d, want 1", rev.calls)
	}
}

func TestPromoteAutonomousRevertsOnUnconfirmableWatch(t *testing.T) {
	realDef := &recordingRealDefault{}
	rev := &recordingReverter{}
	// Watch errors → cannot confirm healthy → fail-safe revert.
	p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, fakeWatcher{err: errors.New("telemetry unreachable")}, rev)

	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}, testEvidence())

	if res.Outcome != OutcomeReverted {
		t.Fatalf("outcome = %q, want reverted (fail-safe)", res.Outcome)
	}
	if rev.calls != 1 {
		t.Errorf("RevertRealDefault calls = %d, want 1", rev.calls)
	}
}

// --- only a PROMOTE verdict is ever acted on. ---
func TestPromoteNonPromoteVerdictIsNoop(t *testing.T) {
	gh := &recordingGitHub{}
	realDef := &recordingRealDefault{}
	p, _ := promoterWithRecorder(t, gh, &recordingGit{}, realDef, nil, nil)

	for _, verdict := range []experiment.Outcome{experiment.OutcomeDiscard, experiment.OutcomeRevert} {
		res := p.Promote(context.Background(), promoteProposal,
			experiment.Decision{Outcome: verdict},
			Config{Mode: ModeAutonomous, Target: TargetGitHub, Enabled: true}, testEvidence())
		if res.Outcome != OutcomeDiscarded {
			t.Errorf("verdict %q: outcome = %q, want discarded", verdict, res.Outcome)
		}
	}
	if len(gh.issues)+len(gh.prs)+len(realDef.calls) != 0 {
		t.Errorf("a non-promote verdict acted: issues=%d prs=%d realDef=%d",
			len(gh.issues), len(gh.prs), len(realDef.calls))
	}
}

// --- a client error degrades to a recorded no-op, never a different action. ---
func TestPromoteClientErrorIsRecordedNoop(t *testing.T) {
	gh := &recordingGitHub{issueErr: errors.New("rate limited")}
	p, _ := promoterWithRecorder(t, gh, &recordingGit{}, nil, nil, nil)
	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModeIssue, Target: TargetGitHub}, testEvidence())
	if res.Outcome != OutcomeDiscarded {
		t.Fatalf("outcome = %q, want discarded on client error", res.Outcome)
	}
	if !strings.Contains(res.Reason, "rate limited") {
		t.Errorf("reason = %q, want it to carry the client error", res.Reason)
	}
	if len(gh.issues) != 1 {
		t.Errorf("the attempt should still be recorded: %d", len(gh.issues))
	}
}

// --- target selects github vs local fake (#936 AC). ---
func TestTargetSelectsClient(t *testing.T) {
	t.Run("github target uses GitHubClient", func(t *testing.T) {
		gh := &recordingGitHub{prURL: "u"}
		git := &recordingGit{}
		p, _ := promoterWithRecorder(t, gh, git, nil, nil, nil)
		p.Promote(context.Background(), promoteProposal, promoteVerdict,
			Config{Mode: ModePR, Target: TargetGitHub}, testEvidence())
		if len(gh.prs) != 1 || len(git.commits) != 0 {
			t.Errorf("github target: prs=%d commits=%d, want 1,0", len(gh.prs), len(git.commits))
		}
	})
	t.Run("local target uses GitClient", func(t *testing.T) {
		gh := &recordingGitHub{}
		git := &recordingGit{sha: "s"}
		p, _ := promoterWithRecorder(t, gh, git, nil, nil, nil)
		p.Promote(context.Background(), promoteProposal, promoteVerdict,
			Config{Mode: ModePR, Target: TargetLocal}, testEvidence())
		if len(git.commits) != 1 || len(gh.prs) != 0 {
			t.Errorf("local target: commits=%d prs=%d, want 1,0", len(git.commits), len(gh.prs))
		}
	})
}
