package promote

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

// github.go is the REAL GitHub API client — the consequential, network-touching
// path of the safety rail. It is built ONLY by NewGitHubClientFromEnv, which
// returns a real client ONLY when ALL of the gate conditions hold:
//
//	AGENT_CODE_IMPROVEMENT_ENABLED=true   (explicit opt-in beyond the kill-switch)
//	GITHUB_TOKEN present                  (the credential)
//	target == github                      (the runtime flag, checked by the caller)
//
// Absent any one, NewGitHubClientFromEnv returns nil and main.go falls back to the
// inert recording no-op (NewNoopGitHubClient). There is NO other constructor for a
// real client — a test cannot instantiate one, so no unit/integration test can
// reach github.com (the safety rail's structural guarantee). Stdlib net/http only:
// two simple endpoints (issues, pulls) do not justify a new dependency (the M7-4
// testing-strategy doc §4 "skip Microcks / go-github").

// httpGitHubClient posts to the GitHub REST API. Constructed only via
// NewGitHubClientFromEnv behind the env gate + token.
type httpGitHubClient struct {
	httpClient *http.Client
	baseURL    string // https://api.github.com (overridable for a sandbox/e2e host)
	token      string
	owner      string
	repo       string
	log        *slog.Logger
}

// NewGitHubClientFromEnv returns the REAL GitHub client IFF the env gate is fully
// satisfied, else nil (the caller substitutes the no-op). This is the ONLY place a
// real GitHub client is ever constructed. It deliberately takes the resolved
// target so the gate includes the runtime flag: a real client is never built for a
// local-target promotion even with the env gate + token set.
//
// Gate conditions (ALL required):
//   - target == TargetGitHub                         (runtime flag)
//   - AGENT_CODE_IMPROVEMENT_ENABLED == "true"       (explicit env opt-in)
//   - GITHUB_TOKEN non-empty                          (credential)
//   - GITHUB_REPOSITORY == "owner/repo"               (where to act)
//
// Returns (nil, nil) when the gate is not satisfied — that is the EXPECTED,
// fail-closed default, not an error. Returns (nil, err) only when the gate IS
// satisfied but a required value is malformed (e.g. a bad GITHUB_REPOSITORY), so a
// misconfigured opt-in fails loudly rather than silently dialing nothing.
func NewGitHubClientFromEnv(target Target, log *slog.Logger) (*httpGitHubClient, error) {
	if log == nil {
		log = slog.Default()
	}
	if target != TargetGitHub {
		return nil, nil // not a github target — no real client (use the no-op)
	}
	if !strings.EqualFold(strings.TrimSpace(os.Getenv("AGENT_CODE_IMPROVEMENT_ENABLED")), "true") {
		log.Info("code_improvement github real client NOT built: AGENT_CODE_IMPROVEMENT_ENABLED != true")
		return nil, nil
	}
	token := strings.TrimSpace(os.Getenv("GITHUB_TOKEN"))
	if token == "" {
		log.Warn("code_improvement github real client NOT built: GITHUB_TOKEN absent (fail-closed to no-op)")
		return nil, nil
	}
	owner, repo, ok := splitRepo(os.Getenv("GITHUB_REPOSITORY"))
	if !ok {
		return nil, fmt.Errorf("AGENT_CODE_IMPROVEMENT_ENABLED is set but GITHUB_REPOSITORY=%q is not owner/repo", os.Getenv("GITHUB_REPOSITORY"))
	}
	baseURL := strings.TrimRight(getenvDefault("GITHUB_API_URL", "https://api.github.com"), "/")
	log.Warn("code_improvement GitHub real client ENABLED (#936): promotions with target=github WILL open real issues/PRs",
		"repo", owner+"/"+repo, "api", baseURL)
	return &httpGitHubClient{
		httpClient: &http.Client{Timeout: 15 * time.Second},
		baseURL:    baseURL,
		token:      token,
		owner:      owner,
		repo:       repo,
		log:        log,
	}, nil
}

// OpenIssue POSTs to /repos/{owner}/{repo}/issues and returns the html_url.
func (c *httpGitHubClient) OpenIssue(ctx context.Context, req IssueReq) (string, error) {
	payload := map[string]any{"title": req.Title, "body": req.Body}
	if len(req.Labels) > 0 {
		payload["labels"] = req.Labels
	}
	return c.post(ctx, fmt.Sprintf("/repos/%s/%s/issues", c.owner, c.repo), payload)
}

// OpenPR POSTs to /repos/{owner}/{repo}/pulls and returns the html_url.
func (c *httpGitHubClient) OpenPR(ctx context.Context, req PRReq) (string, error) {
	payload := map[string]any{
		"title": req.Title,
		"body":  req.Body,
		"head":  req.Head,
		"base":  req.Base,
	}
	return c.post(ctx, fmt.Sprintf("/repos/%s/%s/pulls", c.owner, c.repo), payload)
}

// post sends a JSON POST to the GitHub API and returns the response's html_url.
func (c *httpGitHubClient) post(ctx context.Context, path string, payload any) (string, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return "", fmt.Errorf("marshal payload: %w", err)
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return "", fmt.Errorf("build request: %w", err)
	}
	httpReq.Header.Set("Authorization", "Bearer "+c.token)
	httpReq.Header.Set("Accept", "application/vnd.github+json")
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("X-GitHub-Api-Version", "2022-11-28")

	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return "", fmt.Errorf("github POST %s: %w", path, err)
	}
	defer func() { _ = resp.Body.Close() }()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("github POST %s: status %d: %s", path, resp.StatusCode, string(respBody))
	}
	var parsed struct {
		HTMLURL string `json:"html_url"`
	}
	if err := json.Unmarshal(respBody, &parsed); err != nil {
		return "", fmt.Errorf("parse github response: %w", err)
	}
	return parsed.HTMLURL, nil
}

// splitRepo parses "owner/repo" into its parts.
func splitRepo(s string) (owner, repo string, ok bool) {
	parts := strings.SplitN(strings.TrimSpace(s), "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", false
	}
	return parts[0], parts[1], true
}

// getenvDefault returns the env var or def if empty.
func getenvDefault(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}
