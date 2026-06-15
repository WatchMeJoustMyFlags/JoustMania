package promote

import (
	"context"
	"fmt"
	"time"

	"github.com/joustmania/agent/experiment"
)

// actions.go holds the per-mode promotion logic. Each helper mutates the passed
// *Result (the span attributes are stamped by record()). Every consequential
// effect routes through an injected seam, so these methods are exercised in unit
// tests with recording fakes — ZERO network, ZERO real git, ZERO real-default
// mutation (the SAFETY RAIL, see promote.go).

// promoteIssue opens a GitHub issue describing the hypothesis + implicated flag.
// NO code/flag is applied (#936: "issue — open an issue describing the hypothesis
// and the implicated file; no code applied"). The issue is ALWAYS opened through
// the GitHubClient seam: with the real client disabled (the default), that seam is
// the inert recording no-op, so this degrades to a RECORDED no-op (empty URL +
// reason), never a silent or accidental real action.
func (pr *Promoter) promoteIssue(ctx context.Context, p experiment.Proposal, ev Evidence, res *Result) {
	req := IssueReq{
		Title:  fmt.Sprintf("Flag experiment: %s", p.FlagKey),
		Body:   renderIssueBody(p, ev),
		Labels: []string{"agent", "code-improvement"},
	}
	url, err := pr.github.OpenIssue(ctx, req)
	if err != nil {
		// Fail-safe: a failed/declined open (incl. the no-op fake's recorded decline)
		// is a recorded no-op, never a crash and never a different action.
		res.Outcome = OutcomeDiscarded
		res.Reason = fmt.Sprintf("open issue: %v", err)
		return
	}
	res.Outcome = OutcomeApplied
	res.URL = url
}

// promotePR opens a PR with the STRUCTURED evidence body (#936 AC). Nothing is
// merged. Routing depends on target:
//   - github: open the PR via the GitHubClient (env-gated real client, else no-op).
//   - local:  commit the change into the local working tree via the GitClient
//     (a throwaway git repo in tests; no push). A local PR has no URL — the commit
//     SHA is recorded as the artifact.
func (pr *Promoter) promotePR(ctx context.Context, p experiment.Proposal, ev Evidence, target Target, res *Result) {
	body := renderPRBody(p, ev)

	switch target {
	case TargetGitHub:
		req := PRReq{
			Title: fmt.Sprintf("Flag experiment: %s", p.FlagKey),
			Body:  body,
			Head:  branchName(p),
			Base:  pr.repo.Base,
		}
		url, err := pr.github.OpenPR(ctx, req)
		if err != nil {
			res.Outcome = OutcomeDiscarded
			res.Reason = fmt.Sprintf("open pr: %v", err)
			return
		}
		res.Outcome = OutcomeApplied
		res.URL = url
	default: // TargetLocal
		req := CommitReq{
			Message: fmt.Sprintf("flag experiment: %s\n\n%s", p.FlagKey, body),
			Files:   map[string]string{evidenceFilePath(p): body},
		}
		sha, err := pr.git.Commit(ctx, req)
		if err != nil {
			res.Outcome = OutcomeDiscarded
			res.Reason = fmt.Sprintf("local commit: %v", err)
			return
		}
		res.Outcome = OutcomeApplied
		res.URL = "local:" + sha // not a network URL; the recorded local artifact
	}
}

// promoteAutonomous applies the validated change to the REAL default, watches real
// games, and reverts on degradation (#936: "autonomous — apply, watch, revert per
// M7-7"). This is the consequential real-player-affecting path, so it is the most
// heavily guarded:
//
//   - KILL-SWITCH: a disabled agent (cfg.Enabled == false) NEVER mutates the real
//     default — degrades to a recorded no-op. autonomous is default-OFF AND behind
//     the kill-switch (the safety rail).
//   - SEPARATE WRITER: the real-default change goes through the injected
//     RealDefaultWriter — explicitly NOT the shadow experiment.Writer (which is
//     structurally shadow-only). A nil RealDefaultWriter (real path not wired) → a
//     recorded no-op; the shadow Writer is never reused to sneak a real change.
//   - WATCH + REVERT: after applying, the Watcher observes real-game fitness for a
//     window; on degradation vs the pre-promotion baseline the RealDefaultReverter
//     rolls the real default back (the OutcomeRevert path, reusing M7-7's
//     watch/revert seams).
//   - FAIL CLOSED WITHOUT A WATCH (#1016): a nil Watcher is a HARD STOP, NOT an
//     apply-without-watch. An unguarded autonomous promotion (apply to real players
//     with no auto-revert) is the unacceptable state, so we REFUSE to apply when no
//     Watcher is wired — checked BEFORE the real-default write, so nothing is ever
//     written without a connected safety net.
func (pr *Promoter) promoteAutonomous(ctx context.Context, p experiment.Proposal, cfg Config, ev Evidence, res *Result) {
	if !cfg.Enabled {
		res.Outcome = OutcomeDiscarded
		res.Reason = "autonomous requested but agent disabled (kill-switch); real default not changed"
		return
	}
	if pr.realDef == nil {
		res.Outcome = OutcomeDiscarded
		res.Reason = "autonomous requested but no real-default writer wired; real default not changed"
		return
	}
	// FAIL CLOSED (#1016): no safety net → refuse to apply. Checked BEFORE the write
	// so an autonomous promotion can NEVER touch the real default without a
	// watch→auto-revert loop connected behind it.
	if pr.watcher == nil {
		res.Outcome = OutcomeDiscarded
		res.Reason = "autonomous requested but no watch/auto-revert wired; refusing to apply to real default without a safety net (#1016)"
		return
	}

	// COOLDOWN (#1065 item 3): if this flag was reverted recently, refuse to re-apply
	// until the cooldown elapses. This is checked BEFORE the write so a thrash loop
	// (err→revert→re-promote→err→revert) cannot run hot — a reverted flag is parked
	// for the cooldown rather than immediately re-promoted into the same failure.
	if remaining, cooling := pr.cooldownRemaining(p.FlagKey); cooling {
		res.Outcome = OutcomeDiscarded
		res.Reason = fmt.Sprintf("autonomous re-promotion of %q suppressed: %s remaining in post-revert cooldown (#1065)", p.FlagKey, remaining.Round(time.Second))
		return
	}

	// The one sanctioned real-player-affecting write, through the gated seam.
	if err := pr.realDef.SetRealDefault(ctx, p.FlagKey, ev.RealDefaultValue); err != nil {
		res.Outcome = OutcomeDiscarded
		res.Reason = fmt.Sprintf("set real default: %v", err)
		return
	}
	res.Outcome = OutcomeApplied

	// Watch real-game fitness for the promoted flag's objective and act on the verdict.
	// Use the tri-state Observe when the watcher supports it so an INSUFFICIENT-games
	// result schedules a re-watch (#1065 item 4) instead of silently keeping an
	// unverified change; otherwise fall back to the boolean Degraded.
	verdict, err := pr.observe(ctx, p.FlagKey, ev.FitnessBefore)
	switch {
	case err != nil:
		// Cannot CONFIRM healthy — fail safe by reverting rather than leaving a
		// real-player change unverified.
		pr.revertReal(ctx, p, res, err)
	case verdict == VerdictDegraded:
		pr.revertReal(ctx, p, res, nil)
	case verdict == VerdictInsufficient:
		// Apply stands, but unverified. Schedule a background re-watch (if wired) that
		// reverts later if degradation is confirmed as more real games complete.
		pr.scheduleRewatch(ctx, p, ev)
	default: // VerdictHealthy — confirmed keep, nothing to do.
	}
}

// observe runs the watcher and returns a tri-state verdict. When the wired Watcher
// implements the richer Observer (the production fitnessWatcher does), its Observe is
// used so INSUFFICIENT evidence is distinguishable from a confirmed healthy keep.
// Otherwise it adapts the boolean Degraded (degraded→VerdictDegraded, else
// VerdictHealthy — a boolean-only watcher cannot signal insufficiency, so no re-watch
// is scheduled, matching the prior behavior).
func (pr *Promoter) observe(ctx context.Context, flagKey string, baseline float64) (WatchVerdict, error) {
	if obs, ok := pr.watcher.(Observer); ok {
		return obs.Observe(ctx, flagKey, baseline)
	}
	degraded, err := pr.watcher.Degraded(ctx, flagKey, baseline)
	if err != nil {
		return VerdictInsufficient, err
	}
	if degraded {
		return VerdictDegraded, nil
	}
	return VerdictHealthy, nil
}

// Observer is the tri-state watch interface (#1065 item 4). The production
// fitnessWatcher implements it; the Promoter type-asserts for it so a boolean-only
// Watcher still works (it just cannot trigger a re-watch).
type Observer interface {
	Observe(ctx context.Context, flagKey string, baseline float64) (WatchVerdict, error)
}

// scheduleRewatch hands an insufficient-games apply to the background re-watcher
// (#1065 item 4) so it is re-observed as more real games complete. nil rewatcher →
// the apply stands unverified (prior behavior). The revert closure performs a gated,
// recorded fail-safe revert with its own Result so the journal/span reflects the
// later auto-revert.
func (pr *Promoter) scheduleRewatch(ctx context.Context, p experiment.Proposal, ev Evidence) {
	if pr.rewatcher == nil {
		pr.log.Info("autonomous watch: insufficient real games and no re-watcher wired; apply stands unverified",
			"flag", p.FlagKey)
		return
	}
	pr.log.Info("autonomous watch: insufficient real games; scheduling background re-watch (#1065)",
		"flag", p.FlagKey)
	revert := func(rctx context.Context) {
		var rres Result
		pr.revertReal(rctx, p, &rres, nil)
		pr.log.Warn("autonomous re-watch confirmed degradation; reverted real default (#1065)",
			"flag", p.FlagKey, "outcome", string(rres.Outcome), "reason", rres.Reason)
	}
	pr.rewatcher.Schedule(ctx, p.FlagKey, ev.FitnessBefore, pr.watcher, revert)
}

// cooldownRemaining reports the time left in flagKey's post-revert cooldown, and
// whether it is still cooling. Outside the cooldown (or never reverted) it returns
// (0, false). Guarded by mu.
func (pr *Promoter) cooldownRemaining(flagKey string) (time.Duration, bool) {
	if pr.cooldown <= 0 {
		return 0, false
	}
	pr.mu.Lock()
	defer pr.mu.Unlock()
	last, ok := pr.lastRevert[flagKey]
	if !ok {
		return 0, false
	}
	elapsed := pr.clock().Sub(last)
	if elapsed >= pr.cooldown {
		return 0, false
	}
	return pr.cooldown - elapsed, true
}

// markReverted stamps flagKey's last-revert time so the cooldown gate can suppress an
// immediate re-promotion. Guarded by mu.
func (pr *Promoter) markReverted(flagKey string) {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	if pr.lastRevert == nil {
		pr.lastRevert = map[string]time.Time{}
	}
	pr.lastRevert[flagKey] = pr.clock()
}

// clock returns the (possibly faked) time source; defaults to time.Now if unset.
func (pr *Promoter) clock() time.Time {
	if pr.now != nil {
		return pr.now()
	}
	return time.Now()
}

// revertReal rolls the autonomous real-default change back on a degradation (or an
// unconfirmable watch). A nil RealDefaultReverter (no rollback wired) records the
// degradation but cannot undo it — logged + reasoned so it is visible, never
// silent. On a successful revert the outcome becomes OutcomeReverted.
func (pr *Promoter) revertReal(ctx context.Context, p experiment.Proposal, res *Result, watchErr error) {
	reason := "real-game fitness degraded after autonomous change"
	if watchErr != nil {
		reason = fmt.Sprintf("could not confirm real-game fitness healthy (%v); reverting fail-safe", watchErr)
	}
	if pr.reverter == nil {
		// Applied but cannot roll back — record the degradation so it is actionable.
		res.Reason = reason + "; no real-default reverter wired"
		return
	}
	if err := pr.reverter.RevertRealDefault(ctx, p.FlagKey); err != nil {
		res.Reason = fmt.Sprintf("%s; revert failed: %v", reason, err)
		return
	}
	// Start the post-revert cooldown so a re-promotion of this flag cannot immediately
	// re-trigger the same failure (#1065 item 3). Marked only on a SUCCESSFUL revert —
	// a failed revert (above) leaves no cooldown, surfacing the unrolled-back state.
	pr.markReverted(p.FlagKey)
	res.Outcome = OutcomeReverted
	res.Reason = reason
}
