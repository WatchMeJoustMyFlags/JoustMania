package promote

import (
	"context"
	"strings"
	"testing"

	"github.com/joustmania/agent/experiment"
)

// safety_test.go is the explicit proof of THE SAFETY RAIL (#936). It asserts that
// with NO real client wired — the DEFAULT a test or a non-gated main.go gets — a
// promotion can ONLY degrade to an inert RECORDED no-op, never a real GitHub call,
// real git, or real-default mutation. If any of these tests could reach
// github.com / a real git push, that is a blocker.

// TestSafetyDefaultPromoterNeverActsForReal: a Promoter built with NO explicit
// seams (the maximally-safe construction) falls back to the inert recording no-op
// clients, so a github-target PR degrades to a RECORDED no-op with a visible reason
// — never a network call.
func TestSafetyDefaultPromoterNeverActsForReal(t *testing.T) {
	// nil github + nil git => NewPromoter substitutes the inert recording no-ops.
	// nil realDef => autonomous cannot mutate a real default.
	p := NewPromoter(nil, nil, nil, nil, nil, testRepo(), nil, discardLogger())

	t.Run("github PR degrades to recorded no-op", func(t *testing.T) {
		res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
			Config{Mode: ModePR, Target: TargetGitHub}, testEvidence())
		if res.Outcome != OutcomeDiscarded {
			t.Fatalf("outcome = %q, want discarded (no real client)", res.Outcome)
		}
		if res.URL != "" {
			t.Errorf("a no-op produced a URL %q — did it reach github?", res.URL)
		}
		if !strings.Contains(res.Reason, "github target not enabled") {
			t.Errorf("reason = %q, want it to name the disabled github path", res.Reason)
		}
	})

	t.Run("autonomous with no real-default writer degrades to recorded no-op", func(t *testing.T) {
		res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
			Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}, testEvidence())
		if res.Outcome != OutcomeDiscarded {
			t.Fatalf("outcome = %q, want discarded (no real-default writer)", res.Outcome)
		}
		if !strings.Contains(res.Reason, "no real-default writer wired") {
			t.Errorf("reason = %q, want it to name the missing real-default writer", res.Reason)
		}
	})
}

// TestSafetyAutonomousBlockedByKillSwitch: autonomous (the real-player-affecting
// path) NEVER mutates the real default while the agent is disabled, even with a
// real-default writer wired. The kill-switch is a hard gate independent of the env
// gate.
func TestSafetyAutonomousBlockedByKillSwitch(t *testing.T) {
	realDef := &recordingRealDefault{}
	p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, fakeWatcher{}, &recordingReverter{})

	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: false}, testEvidence())

	if res.Outcome != OutcomeDiscarded {
		t.Fatalf("outcome = %q, want discarded (kill-switch)", res.Outcome)
	}
	if len(realDef.calls) != 0 {
		t.Fatalf("SAFETY: a disabled agent mutated the real default %d time(s)", len(realDef.calls))
	}
	if !strings.Contains(res.Reason, "kill-switch") {
		t.Errorf("reason = %q, want it to name the kill-switch", res.Reason)
	}
}

// TestSafetyEnvGatedConstructorsReturnNilWhenUngated: the ONLY constructors for the
// real clients return nil unless the explicit env gate is satisfied. This is the
// structural proof that, without the env gate, main.go gets a nil real client and
// substitutes the no-op — a test (which sets no env) can NEVER build a real client.
// We do NOT set the env here, so every gated constructor must return nil.
func TestSafetyEnvGatedConstructorsReturnNilWhenUngated(t *testing.T) {
	// Defensive: ensure the gate env is unset for this test (t.Setenv restores it).
	t.Setenv("AGENT_CODE_IMPROVEMENT_ENABLED", "")
	t.Setenv("GITHUB_TOKEN", "")
	t.Setenv("AGENT_AUTONOMOUS_ENABLED", "")

	if gh, err := NewGitHubClientFromEnv(TargetGitHub, discardLogger()); gh != nil || err != nil {
		t.Errorf("NewGitHubClientFromEnv(ungated) = (%v, %v), want (nil, nil)", gh, err)
	}
	// Even a github target with the gate off must not build a client.
	if gh, _ := NewGitHubClientFromEnv(TargetLocal, discardLogger()); gh != nil {
		t.Error("NewGitHubClientFromEnv(local) returned a real github client")
	}
	if git, err := NewGitClientFromEnv(TargetLocal, discardLogger()); git != nil || err != nil {
		t.Errorf("NewGitClientFromEnv(ungated) = (%v, %v), want (nil, nil)", git, err)
	}
	if w, err := NewRealDefaultWriterFromEnv(discardLogger()); w != nil || err != nil {
		t.Errorf("NewRealDefaultWriterFromEnv(ungated) = (%v, %v), want (nil, nil)", w, err)
	}
}

// TestSafetyEnvGateTokenStillRequired: with the explicit opt-in env set but the
// TOKEN absent, the real GitHub client is STILL not built (fail-closed) — the
// token is a required, separate condition.
func TestSafetyEnvGateTokenStillRequired(t *testing.T) {
	t.Setenv("AGENT_CODE_IMPROVEMENT_ENABLED", "true")
	t.Setenv("GITHUB_TOKEN", "") // token absent
	t.Setenv("GITHUB_REPOSITORY", "owner/repo")

	gh, err := NewGitHubClientFromEnv(TargetGitHub, discardLogger())
	if gh != nil || err != nil {
		t.Errorf("NewGitHubClientFromEnv(enabled, no token) = (%v, %v), want (nil, nil)", gh, err)
	}
}

// TestSafetyNoopClientsRecordButDoNotAct: the no-op clients record what they WOULD
// do (so a disabled main.go is still observable) but always decline — proving the
// recorded-no-op contract end to end.
func TestSafetyNoopClientsRecordButDoNotAct(t *testing.T) {
	gh := NewNoopGitHubClient(discardLogger())
	if _, err := gh.OpenIssue(context.Background(), IssueReq{Title: "x"}); err == nil {
		t.Error("no-op OpenIssue should decline")
	}
	if len(gh.Issues) != 1 {
		t.Errorf("no-op did not RECORD the issue it would open: %d", len(gh.Issues))
	}
	git := NewNoopGitClient(discardLogger())
	if _, err := git.Commit(context.Background(), CommitReq{Message: "x"}); err == nil {
		t.Error("no-op Commit should decline")
	}
	if len(git.Commits) != 1 {
		t.Errorf("no-op did not RECORD the commit it would make: %d", len(git.Commits))
	}
}

// sanity: keep experiment import used even if a future edit drops the only use.
var _ = experiment.OutcomePromote
