package promote

import (
	"context"
	"errors"
	"log/slog"
)

// noop.go holds the INERT recording no-op clients that are the DEFAULT GitHubClient
// / GitClient (see NewPromoter). They are the safety rail's fail-closed floor: a
// Promoter built without an explicit real client — i.e. every test, and main.go
// whenever the env gate/token/target are not all present — can only RECORD that it
// "would have" acted, never touch github.com or a real git remote.
//
// errNoopGitHub / errNoopGit are returned so the per-mode logic degrades to a
// RECORDED no-op (OutcomeDiscarded + reason on the span), making an unreachable
// real path VISIBLE in the trace rather than silent (#936 safety rail).

var (
	// errNoopGitHub is returned by the no-op GitHub client. Its presence in a Result
	// reason proves the github target degraded to an inert recorded no-op because the
	// real client was not enabled.
	errNoopGitHub = errors.New("github target not enabled (no real client wired; set AGENT_CODE_IMPROVEMENT_ENABLED=true + GITHUB_TOKEN + target=github)")
	// errNoopGit is returned by the no-op local git client.
	errNoopGit = errors.New("local git target not enabled (no real git client wired)")
)

// noopGitHubClient records the requests it WOULD have sent and declines them. It
// has ZERO network code by construction — there is no http client, no URL, no
// token anywhere in this type, so no bug or test can make it dial github.com.
type noopGitHubClient struct {
	log *slog.Logger
	// Issues / PRs record what would have been opened, for tests that assert "the
	// agent would open an issue/PR with body X" against the DEFAULT (disabled) path.
	Issues []IssueReq
	PRs    []PRReq
}

// NewNoopGitHubClient builds the inert recording GitHub client. This is the default
// the Promoter falls back to; it is also exported so tests can assert against the
// recorded requests. log nil uses slog.Default().
func NewNoopGitHubClient(log *slog.Logger) *noopGitHubClient {
	if log == nil {
		log = slog.Default()
	}
	return &noopGitHubClient{log: log}
}

// OpenIssue records the request and declines (no network).
func (c *noopGitHubClient) OpenIssue(_ context.Context, req IssueReq) (string, error) {
	c.Issues = append(c.Issues, req)
	c.log.Info("code_improvement github DISABLED: would open issue (recorded no-op)", "title", req.Title)
	return "", errNoopGitHub
}

// OpenPR records the request and declines (no network).
func (c *noopGitHubClient) OpenPR(_ context.Context, req PRReq) (string, error) {
	c.PRs = append(c.PRs, req)
	c.log.Info("code_improvement github DISABLED: would open PR (recorded no-op)", "title", req.Title)
	return "", errNoopGitHub
}

// noopGitClient records the commits it WOULD have made and declines them. ZERO git
// shell-out by construction.
type noopGitClient struct {
	log *slog.Logger
	// Commits records what would have been committed.
	Commits []CommitReq
}

// NewNoopGitClient builds the inert recording local-git client (the default).
func NewNoopGitClient(log *slog.Logger) *noopGitClient {
	if log == nil {
		log = slog.Default()
	}
	return &noopGitClient{log: log}
}

// Commit records the request and declines (no git).
func (c *noopGitClient) Commit(_ context.Context, req CommitReq) (string, error) {
	c.Commits = append(c.Commits, req)
	c.log.Info("code_improvement local git DISABLED: would commit (recorded no-op)", "files", len(req.Files))
	return "", errNoopGit
}
