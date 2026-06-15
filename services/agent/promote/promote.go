// Package promote implements M7-8 (#936): the PROMOTION flow for the agent's
// shadow-scoped flag experiments. It takes a VALIDATED (PROMOTE) experiment plus
// its fitness Evidence and ACTS according to two live flags:
//
//		code_improvement.mode   = issue | pr | autonomous   (the "gate")
//		code_improvement.target = local | github
//
//	  - issue       — open a GitHub issue describing the hypothesis + implicated
//	                  flag; NO code/flag applied.
//	  - pr          — open a PR with a STRUCTURED body (fitness before/after,
//	                  synthetic results, proposer reasoning, flag state); nothing
//	                  merged.
//	  - autonomous  — apply the validated change to the REAL default, watch real
//	                  games, and revert if fitness degrades (per M7-7's
//	                  Validator/Reverter seams).
//
// THE SAFETY RAIL (agreed, non-negotiable — see docs/research/m7-4-testing-strategy.md
// §3.3). Promotion is the one consequential, REAL-affecting piece of the track,
// so the design makes a real action impossible to reach by accident or from a
// test:
//
//   - The seams are interfaces (GitHubClient, GitClient, RealDefaultWriter). Unit
//     tests use RECORDING FAKES that assert "would open an issue/PR with body X" /
//     "would commit Y" / "would write real default Z" — ZERO network, ZERO real
//     git, ZERO real-default mutation.
//   - The REAL GitHub client + real git + real-default write are constructed ONLY
//     in main.go behind an EXPLICIT env gate (AGENT_CODE_IMPROVEMENT_ENABLED=true),
//     a token (GITHUB_TOKEN), and target=github — via NewGitHubClientFromEnv /
//     NewGitClientFromEnv / NewRealDefaultWriterFromEnv. There is NO test-reachable
//     constructor for any of them; a test that wants a real client cannot get one.
//   - The SAFE flag DEFAULTS are baked in: mode defaults to issue (NEVER
//     autonomous), target defaults to local (NEVER github). autonomous (apply-to-
//     real + watch) is additionally behind the agent `enabled` kill-switch — the
//     caller passes Config.Enabled, and a disabled agent degrades autonomous to a
//     recorded no-op.
//   - Real GitHub is default-off, fail-closed: with the env gate/token absent, the
//     real client is not built; main.go passes the inert recording no-op fakes, and
//     a github-target promotion degrades to a RECORDED no-op (logged + span reason),
//     never a silent or accidental real action.
//   - autonomous changing the real defaultVariant is a SEPARATE, higher-privilege
//     path than the shadow Writer (which is structurally shadow-only). It is the
//     injected RealDefaultWriter, mutated explicitly + gated — the shadow Writer is
//     NEVER reused to sneak a real-default change.
package promote

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/experiment"
)

// Mode is the resolved code_improvement.mode — the "gate" that selects promotion
// behaviour. The set is closed so dashboards can enumerate every gate.
type Mode string

const (
	// ModeIssue opens a GitHub issue describing the hypothesis + implicated flag.
	// NO code or flag change is applied. This is the SAFE default mode.
	ModeIssue Mode = "issue"
	// ModePR opens a PR with the structured fitness-evidence body. Nothing is merged
	// — a human reviews + merges. Lands per target (github → GitHub API, local →
	// local apply/commit).
	ModePR Mode = "pr"
	// ModeAutonomous applies the validated change to the REAL default, watches real
	// games, and reverts on degradation. The highest-privilege path; default-OFF and
	// additionally behind the agent kill-switch (Config.Enabled).
	ModeAutonomous Mode = "autonomous"
)

// Target is the resolved code_improvement.target — where a pr/commit lands.
type Target string

const (
	// TargetLocal applies/commits locally (a local git repo or in-process). The SAFE
	// default target — never reaches github.com.
	TargetLocal Target = "local"
	// TargetGitHub commits + opens a PR/issue via the GitHub API. Only reaches a real
	// client when the env gate + token are present (see NewGitHubClientFromEnv).
	TargetGitHub Target = "github"
)

// Outcome is what the promotion ACHIEVED (#936 AC: code_improvement.outcome). The
// set is closed; distinct from experiment.Outcome (the M7-7 verdict).
type Outcome string

const (
	// OutcomeApplied — the requested action happened: an issue/PR was opened, or the
	// autonomous real-default change was applied.
	OutcomeApplied Outcome = "applied"
	// OutcomeDiscarded — the promotion did NOT act: the verdict was not PROMOTE, the
	// agent was disabled (autonomous), or a github action degraded to an inert
	// recorded no-op because the real path was not enabled. The Reason explains which.
	OutcomeDiscarded Outcome = "discarded"
	// OutcomeReverted — autonomous applied to the real default but a degradation was
	// observed, so the real-default change was rolled back (per M7-7's Reverter seam).
	OutcomeReverted Outcome = "reverted"
)

// Config is the live, per-promotion flag configuration the Promoter acts under
// (#936 flags), resolved fresh each promotion so flipping the mode flag takes
// effect on the NEXT promotion with no restart — the same never-cached contract
// as the #847/#935 gate flags. The caller resolves these via flags.Promotion(ctx)
// (see flags/flags.go) + the agent kill-switch and passes the value in, so this
// package stays free of an OpenFeature dependency (the Gate/Writer/Validator all
// follow this "package owns logic, caller owns flagd" split).
type Config struct {
	// Mode is code_improvement.mode (default issue). Selects issue/pr/autonomous.
	Mode Mode
	// Target is code_improvement.target (default local). Selects local/github.
	Target Target
	// Enabled is the agent `enabled` kill-switch (flags.Snapshot.Enabled). It gates
	// ONLY the autonomous mode: a real-default mutation must never happen while the
	// agent is disabled. issue/pr do not consume it (opening an issue/PR is not a
	// live real-player change). A disabled agent degrades autonomous to a recorded
	// no-op (OutcomeDiscarded + reason), never a silent real change.
	Enabled bool
}

// Evidence is the fitness + provenance the promotion records into an issue/PR
// body (#936 AC: "PR description is structured: fitness before/after, synthetic
// results, reasoning summary, flag state"). It is assembled by the caller from the
// M7-7 Decision + the live flag snapshot and threaded into the body renderer.
type Evidence struct {
	// FitnessBefore / FitnessAfter / FitnessDelta are the M7-7 baseline-vs-synthetic
	// fitness numbers (experiment.Decision). Recorded in the body AND on the span.
	FitnessBefore float64
	FitnessAfter  float64
	FitnessDelta  float64
	// SyntheticGames is how many synthetic/shadow games the experiment was validated
	// against (experiment.Decision.Games) — the sample size behind FitnessAfter.
	SyntheticGames int
	// Reasoning is the coding-agent/proposer reasoning summary — the agent's
	// hypothesis for why the change helps. Sourced from Proposal.Rationale (the
	// proposer side, #931, is a parallel PR; here we render whatever rationale the
	// validated proposal carried).
	Reasoning string
	// FlagState is a human-readable snapshot of the relevant flag state at improvement
	// time (e.g. "death_grace_period_ms: default=300ms, experimental=500ms;
	// objectives=balanced_focused"). The caller assembles it from the live flag
	// snapshot so a reviewer sees the control-plane context the experiment ran under.
	FlagState string
	// RealDefaultValue is the value the autonomous path would write as the REAL
	// defaultVariant for the flag (typically the experiment's experimental value once
	// validated). Used only by ModeAutonomous; ignored by issue/pr.
	RealDefaultValue any
}

// IssueReq is the GitHub issue a ModeIssue promotion opens.
type IssueReq struct {
	// Title is the issue title (e.g. "Flag experiment: death_grace_period_ms").
	Title string
	// Body is the structured hypothesis + implicated-flag description.
	Body string
	// Labels tag the issue for triage (e.g. agent, code-improvement).
	Labels []string
}

// PRReq is the GitHub pull request a ModePR / TargetGitHub promotion opens.
type PRReq struct {
	// Title is the PR title.
	Title string
	// Body is the STRUCTURED, machine-readable evidence body (see renderPRBody):
	// fitness before/after, synthetic results, reasoning, flag state.
	Body string
	// Head is the branch carrying the change; Base is the branch to merge into.
	Head string
	Base string
}

// CommitReq is a local-target commit (TargetLocal): the change written + committed
// into a working tree (no network). Used by the GitClient seam.
type CommitReq struct {
	// Message is the commit message.
	Message string
	// Files maps repo-relative path -> new file contents written before the commit.
	Files map[string]string
}

// Result is the recorded outcome of one Promote, returned to the caller for the
// span/log and any follow-up. Never nil-meaningful: even a no-op returns a Result
// with OutcomeDiscarded + a Reason, so a promotion is always attributable.
type Result struct {
	// Mode / Target are the resolved flags the promotion acted under (echoed for the
	// caller's log without re-reading flagd).
	Mode   Mode
	Target Target
	// Outcome is applied | discarded | reverted.
	Outcome Outcome
	// URL is the opened issue/PR URL (empty for local/no-op).
	URL string
	// Reason explains a no-op / degrade / revert (empty on a clean applied). Its
	// presence on a github-target Result is the SAFETY RAIL's visible proof that the
	// real path was unreachable and the promotion degraded to an inert recorded no-op.
	Reason string
}

// GitHubClient is the GitHub API wire boundary (the SAFETY RAIL's network seam).
// Unit tests inject a recording fake; the REAL client is built ONLY in main.go
// behind the env gate + token (NewGitHubClientFromEnv) and is never reachable from
// a test constructor.
type GitHubClient interface {
	// OpenIssue opens a GitHub issue and returns its URL.
	OpenIssue(ctx context.Context, req IssueReq) (string, error)
	// OpenPR opens a GitHub pull request and returns its URL.
	OpenPR(ctx context.Context, req PRReq) (string, error)
}

// GitClient is the local-target git seam (TargetLocal): write files + commit into
// a working tree. Unit tests use a throwaway `git init` temp repo (real git, NO
// network — there is no push); the real client is built only in main.go.
type GitClient interface {
	// Commit writes req.Files and commits them, returning the commit SHA. It MUST
	// NOT push (local target never reaches a remote).
	Commit(ctx context.Context, req CommitReq) (string, error)
}

// RealDefaultWriter performs the autonomous mode's REAL defaultVariant mutation —
// the SEPARATE, higher-privilege path the safety rail isolates from the shadow
// Writer. It is explicitly NOT experiment.Writer (which is structurally shadow-
// scoped and can never change a real resolution). A nil RealDefaultWriter means
// "no real-default mechanism wired", so autonomous degrades to a recorded no-op.
// The real implementation is built only in main.go behind the env gate.
type RealDefaultWriter interface {
	// SetRealDefault changes the REAL defaultVariant for flagKey to value. This is the
	// one sanctioned real-player-affecting write; it is gated by the env gate at
	// construction AND the kill-switch at call time.
	SetRealDefault(ctx context.Context, flagKey string, value any) error
}

// Watcher observes real games after an autonomous real-default change and reports
// whether fitness DEGRADED vs the pre-promotion baseline (so the change should be
// reverted). It reuses M7-7's fitness measurement seam where wired.
//
// FAIL-CLOSED CONTRACT (#1016): a nil Watcher is NOT "apply without a watch" — it
// is a HARD STOP. An autonomous real-default change with no safety net wired is the
// single most dangerous state in the loop (apply to real players with no
// auto-revert), so promoteAutonomous REFUSES to apply when the Watcher is nil
// rather than applying unguarded. A safety net that is not connected blocks the
// action; it is never silently skipped.
type Watcher interface {
	// Degraded reports whether real-game fitness degraded vs baseline after the
	// change (true → the Promoter reverts via RealDefaultReverter). baseline is the
	// pre-promotion real-game fitness (Evidence.FitnessBefore) the recent window is
	// compared against. An error is treated as "cannot confirm healthy" → fail-safe
	// revert.
	Degraded(ctx context.Context, flagKey string, baseline float64) (bool, error)
}

// RealDefaultReverter rolls back an autonomous real-default change on degradation.
// nil means no revert mechanism wired (a degradation is then recorded but not
// rolled back — logged so it is visible). Distinct from experiment.Reverter (the
// shadow rollback): this reverts the REAL default.
type RealDefaultReverter interface {
	// RevertRealDefault restores the prior real defaultVariant for flagKey.
	RevertRealDefault(ctx context.Context, flagKey string) error
}

// Promoter orchestrates a promotion per the live mode/target. It owns ONLY the
// dispatch + outcome logic; every consequential effect (open issue/PR, commit,
// write real default, watch, revert) is an injected seam, so the whole flow is
// unit-testable with recording fakes and the real path is built only in main.go.
type Promoter struct {
	github   GitHubClient        // wire boundary; recording fake in tests, env-gated real client in main.go
	git      GitClient           // local-target commit; temp git repo in tests
	realDef  RealDefaultWriter   // autonomous real-default mutation (nil = not wired)
	watcher  Watcher             // autonomous live watch (nil = HARD STOP: autonomous refuses to apply, #1016)
	reverter RealDefaultReverter // autonomous degradation rollback (nil = not wired)
	repo     RepoRef             // owner/name/base used to build issue/PR/commit requests
	tracer   trace.Tracer
	log      *slog.Logger

	// --- autonomous re-promotion COOLDOWN (#1065 item 3) ---
	// A revert (especially the fail-safe revert on a telemetry error) must not be able
	// to thrash: err→revert→re-promote→err→revert with no pause. After a revert the
	// flag enters a cooldown; an autonomous re-promotion of the SAME flag inside the
	// cooldown is refused (recorded no-op), so the loop cannot run hot. Defaults to
	// DefaultRevertCooldown; SetCooldown overrides (env-driven from main.go).
	cooldown   time.Duration
	now        func() time.Time // injectable clock (tests); defaults to time.Now
	mu         sync.Mutex       // guards lastRevert
	lastRevert map[string]time.Time

	// --- re-watch scheduling (#1065 item 4) ---
	// When a watch sees INSUFFICIENT real games, the apply stands but is unverified. If
	// a rewatcher is wired, the Promoter schedules a background re-watch that polls as
	// more real games complete and reverts if a degradation is later CONFIRMED. nil →
	// no re-watch (the apply stands unverified, exactly today's behavior). Wired in
	// main.go alongside the live watcher.
	rewatcher Rewatcher
}

// DefaultRevertCooldown is the default post-revert cooldown before the same flag may
// be autonomously re-promoted (#1065 item 3). Conservative: long enough that a
// telemetry-error revert loop cannot spin, short enough that a genuinely better arm
// can be retried after the transient clears.
const DefaultRevertCooldown = 10 * time.Minute

// Rewatcher schedules a background re-watch of an autonomously-applied flag whose
// initial watch had INSUFFICIENT real games to judge (#1065 item 4). The Promoter
// hands it everything needed to re-observe and act: the flag, the pre-promotion
// baseline, the live Watcher, and a revert closure that performs (and records) a
// fail-safe revert. The implementation polls as more real games complete and invokes
// revert on a CONFIRMED degradation. It runs in the background (a promotion call must
// not block on real games), so a nil Rewatcher means "no re-watch" (apply stands).
type Rewatcher interface {
	// Schedule starts a background re-watch. It MUST NOT block the caller. watch is the
	// live Watcher (reuse the same degradation policy); revert performs the gated
	// fail-safe revert + journalling when a later cycle confirms degradation.
	Schedule(ctx context.Context, flagKey string, baseline float64, watch Watcher, revert func(ctx context.Context))
}

// SetCooldown overrides the post-revert re-promotion cooldown (#1065 item 3). A
// non-positive duration leaves the default in place. Called from main.go to make the
// cooldown env-tunable without widening NewPromoter's heavily-used signature.
func (pr *Promoter) SetCooldown(d time.Duration) {
	if d > 0 {
		pr.cooldown = d
	}
}

// SetClock injects the time source the cooldown uses (tests drive a fake clock). A
// nil clock is ignored. Production uses the default time.Now.
func (pr *Promoter) SetClock(now func() time.Time) {
	if now != nil {
		pr.now = now
	}
}

// SetRewatcher wires the background re-watch scheduler (#1065 item 4). nil leaves
// re-watch disabled (an insufficient-games apply stands unverified, as today).
func (pr *Promoter) SetRewatcher(r Rewatcher) {
	pr.rewatcher = r
}

// SeedLastRevert pre-populates the post-revert cooldown map from durably-persisted
// revert timestamps (#1076 item 3), so post-revert thrash protection survives an
// agent restart. Called once from main.go after construction with the reverter's
// PersistedRevertTimes. A nil/empty map is a no-op. Existing in-memory entries are
// only overwritten by a LATER persisted stamp (a fresh in-process revert always
// wins), so seeding can never roll a cooldown backward.
func (pr *Promoter) SeedLastRevert(times map[string]time.Time) {
	if len(times) == 0 {
		return
	}
	pr.mu.Lock()
	defer pr.mu.Unlock()
	if pr.lastRevert == nil {
		pr.lastRevert = map[string]time.Time{}
	}
	for k, t := range times {
		if existing, ok := pr.lastRevert[k]; !ok || t.After(existing) {
			pr.lastRevert[k] = t
		}
	}
}

// RepoRef identifies the GitHub repo + default base branch a promotion targets.
// Populated from env in main.go (GITHUB_REPOSITORY / GITHUB_BASE_BRANCH); the
// fakes in tests use a fixed value.
type RepoRef struct {
	Owner string
	Name  string
	Base  string
}

// NewPromoter builds a Promoter over the injected seams. github/git default to the
// inert recording no-ops when nil (so a Promoter is ALWAYS safe to construct in a
// test or when the real path is disabled — it can only no-op). realDef/watcher/
// reverter may be nil (autonomous degrades accordingly). tracer nil uses the global
// tracer; log nil uses slog.Default().
func NewPromoter(github GitHubClient, git GitClient, realDef RealDefaultWriter, watcher Watcher, reverter RealDefaultReverter, repo RepoRef, tracer trace.Tracer, log *slog.Logger) *Promoter {
	if github == nil {
		// Default to the inert recording no-op: a Promoter built without an explicit
		// client can NEVER reach github.com. The real client is opt-in (main.go only).
		github = NewNoopGitHubClient(log)
	}
	if git == nil {
		git = NewNoopGitClient(log)
	}
	if tracer == nil {
		tracer = otel.Tracer(instrumentationName)
	}
	if log == nil {
		log = slog.Default()
	}
	return &Promoter{
		github:     github,
		git:        git,
		realDef:    realDef,
		watcher:    watcher,
		reverter:   reverter,
		repo:       repo,
		tracer:     tracer,
		log:        log,
		cooldown:   DefaultRevertCooldown,
		now:        time.Now,
		lastRevert: map[string]time.Time{},
	}
}

// Promote acts on a validated experiment per the live cfg. p is the PROMOTE'd
// Proposal, dec is the M7-7 verdict, ev is the assembled fitness Evidence. It
// emits the code_improvement.promote span carrying gate (mode) + target + outcome
// (+ the fitness evidence + any degrade reason), and returns the recorded Result.
//
// FAIL-SAFE / RECORDED: a promotion NEVER silently acts or silently no-ops. If the
// verdict was not PROMOTE, or a github target is unreachable (real client absent),
// or autonomous is requested while disabled, the promotion degrades to
// OutcomeDiscarded with a Reason on the span — visible, never silent. The real,
// consequential paths run only through the env-gated injected seams.
func (pr *Promoter) Promote(ctx context.Context, p experiment.Proposal, dec experiment.Decision, cfg Config, ev Evidence) Result {
	mode := normalizeMode(cfg.Mode)
	target := normalizeTarget(cfg.Target)

	ctx, span := pr.tracer.Start(ctx, SpanPromote, trace.WithAttributes(
		attribute.String(AttrGate, string(mode)),
		attribute.String(AttrTarget, string(target)),
		attribute.String(AttrFlagKey, p.FlagKey),
		attribute.Float64(AttrFitnessBefore, ev.FitnessBefore),
		attribute.Float64(AttrFitnessAfter, ev.FitnessAfter),
		attribute.Float64(AttrFitnessDelta, ev.FitnessDelta),
	))
	defer span.End()

	res := Result{Mode: mode, Target: target}

	// GUARD 1: only a validated PROMOTE verdict is ever promoted (#936 depends on
	// M7-7: "only validated changes are promoted"). Anything else is a recorded
	// no-op — the agent does not act on a discard/revert verdict.
	if dec.Outcome != experiment.OutcomePromote {
		res.Outcome = OutcomeDiscarded
		res.Reason = fmt.Sprintf("verdict %q is not promote; nothing promoted", dec.Outcome)
		return pr.record(span, res)
	}

	switch mode {
	case ModeIssue:
		pr.promoteIssue(ctx, p, ev, &res)
	case ModePR:
		pr.promotePR(ctx, p, ev, target, &res)
	case ModeAutonomous:
		pr.promoteAutonomous(ctx, p, cfg, ev, &res)
	default:
		// Unreachable after normalizeMode, but fail safe: an unknown mode acts as the
		// safest mode (no real change), recorded.
		res.Outcome = OutcomeDiscarded
		res.Reason = fmt.Sprintf("unknown mode %q; treated as no-op", mode)
	}

	return pr.record(span, res)
}

// record stamps the outcome/url/reason on the span and logs the promotion, then
// returns the Result. A non-applied github-target outcome marks the span with an
// error status so a degraded/unreachable promotion is visible in Jaeger (it is a
// recorded no-op, not a failure of the agent, so the message is the reason).
func (pr *Promoter) record(span trace.Span, res Result) Result {
	span.SetAttributes(
		attribute.String(AttrOutcome, string(res.Outcome)),
		attribute.String(AttrURL, res.URL),
		attribute.String(AttrReason, res.Reason),
	)
	if res.Reason != "" {
		span.SetStatus(codes.Error, res.Reason)
	}
	pr.log.Info("code_improvement.promoted",
		"gate", string(res.Mode),
		"target", string(res.Target),
		"outcome", string(res.Outcome),
		"url", res.URL,
		"reason", res.Reason,
	)
	return res
}

// normalizeMode coerces an unknown/empty mode to the SAFE default (issue) — the
// safety rail's "default mode is never autonomous". A garbled flag value can never
// silently become a real-default mutation.
func normalizeMode(m Mode) Mode {
	switch m {
	case ModeIssue, ModePR, ModeAutonomous:
		return m
	default:
		return ModeIssue
	}
}

// normalizeTarget coerces an unknown/empty target to the SAFE default (local) —
// the safety rail's "default target is never github". A garbled flag value can
// never silently route to github.com.
func normalizeTarget(t Target) Target {
	switch t {
	case TargetLocal, TargetGitHub:
		return t
	default:
		return TargetLocal
	}
}
