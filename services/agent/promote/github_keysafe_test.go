package promote

import (
	"bytes"
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func setGitHubEnv(t *testing.T, apiURL string) {
	t.Helper()
	t.Setenv("AGENT_CODE_IMPROVEMENT_ENABLED", "true")
	t.Setenv("GITHUB_TOKEN", "ghp_secret_token_value")
	t.Setenv("GITHUB_REPOSITORY", "owner/repo")
	t.Setenv("GITHUB_API_URL", apiURL)
}

// TestNewGitHubClientFromEnv_UnsafeAPIURLDisablesClient: a GITHUB_API_URL that is
// not key-safe (plain http to a remote host, credentials, query, not http(s))
// builds no client, so GITHUB_TOKEN is sent nowhere; the log names the reason and
// never the URL (#1886).
// UC: UC-043 A14
// UC: C-016
func TestNewGitHubClientFromEnv_UnsafeAPIURLDisablesClient(t *testing.T) {
	for _, raw := range []string{
		"http://github.example.com",
		"http://192.168.1.5/api",
		"https://user:pw@api.github.com",
		"https://api.github.com?x=1",
		"ftp://api.github.com",
	} {
		t.Run("UC-043/A14 rejects "+raw, func(t *testing.T) {
			setGitHubEnv(t, raw)
			var buf bytes.Buffer
			log := slog.New(slog.NewTextHandler(&buf, nil))
			gh, err := NewGitHubClientFromEnv(TargetGitHub, log)
			if gh != nil || err != nil {
				t.Fatalf("NewGitHubClientFromEnv(%q) = (%v, %v), want (nil, nil)", raw, gh, err)
			}
			out := buf.String()
			if !strings.Contains(out, "GITHUB_API_URL") || strings.Contains(out, "ENABLED") {
				t.Errorf("log should reject GITHUB_API_URL and not say ENABLED: %s", out)
			}
			for _, secret := range []string{raw, "pw@", "ghp_secret_token_value"} {
				if strings.Contains(out, secret) {
					t.Errorf("log leaks %q: %s", secret, out)
				}
			}
		})
	}
}

// TestNewGitHubClientFromEnv_SafeAPIURLBuildsClient: https and loopback http are
// accepted, and the ENABLED log does not carry the URL.
// UC: UC-043 A14
// UC: C-016
func TestNewGitHubClientFromEnv_SafeAPIURLBuildsClient(t *testing.T) {
	for _, raw := range []string{"https://api.github.com", "http://127.0.0.1:9999", "http://localhost:8080/"} {
		t.Run("UC-043/A14 accepts "+raw, func(t *testing.T) {
			setGitHubEnv(t, raw)
			var buf bytes.Buffer
			gh, err := NewGitHubClientFromEnv(TargetGitHub, slog.New(slog.NewTextHandler(&buf, nil)))
			if gh == nil || err != nil {
				t.Fatalf("NewGitHubClientFromEnv(%q) = (%v, %v), want a client", raw, gh, err)
			}
			if !strings.Contains(buf.String(), "ENABLED") || strings.Contains(buf.String(), "api=") {
				t.Errorf("ENABLED log missing, or it carries the api URL: %s", buf.String())
			}
		})
	}
}

// TestGitHubClient_FollowsNoRedirect: a redirect from the API host is returned
// as the response, so the token never reaches the redirect target (#1886).
// UC: UC-043 A14
// UC: C-016
func TestGitHubClient_FollowsNoRedirect(t *testing.T) {
	var targetHits atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		targetHits.Add(1)
		_, _ = w.Write([]byte(`{"html_url":"https://x/1"}`))
	}))
	defer target.Close()
	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+r.URL.Path, http.StatusTemporaryRedirect)
	}))
	defer redirector.Close()

	setGitHubEnv(t, redirector.URL)
	gh, err := NewGitHubClientFromEnv(TargetGitHub, discardLogger())
	if gh == nil || err != nil {
		t.Fatalf("client = (%v, %v)", gh, err)
	}
	if _, err := gh.OpenIssue(context.Background(), IssueReq{Title: "t"}); err == nil {
		t.Error("OpenIssue followed the redirect and succeeded, want a status error")
	}
	if n := targetHits.Load(); n != 0 {
		t.Errorf("redirect target received %d requests, want 0", n)
	}
}
