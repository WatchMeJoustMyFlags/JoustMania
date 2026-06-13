package promote

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// git_test.go exercises the REAL local git client against a throwaway `git init`
// temp repo (real git, NO network — there is no push). This is the strategy doc's
// "local-git tests may use a throwaway git init temp repo".

// initTempRepo creates an isolated git repo in t.TempDir() with an initial commit,
// or skips the test if git is unavailable.
func initTempRepo(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not available")
	}
	dir := t.TempDir()
	run := func(args ...string) {
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		// Pin identity + no signing so the commit succeeds in any CI env.
		cmd.Env = append(os.Environ(),
			"GIT_AUTHOR_NAME=test", "GIT_AUTHOR_EMAIL=test@example.com",
			"GIT_COMMITTER_NAME=test", "GIT_COMMITTER_EMAIL=test@example.com",
			"GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_SYSTEM=/dev/null",
		)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %s: %v: %s", strings.Join(args, " "), err, out)
		}
	}
	run("init")
	run("commit", "--allow-empty", "-m", "init")
	return dir
}

func TestLocalGitClientCommits(t *testing.T) {
	dir := initTempRepo(t)
	// The exec client inherits the test's pinned git identity env via the process.
	t.Setenv("GIT_AUTHOR_NAME", "test")
	t.Setenv("GIT_AUTHOR_EMAIL", "test@example.com")
	t.Setenv("GIT_COMMITTER_NAME", "test")
	t.Setenv("GIT_COMMITTER_EMAIL", "test@example.com")

	c := NewGitClient(dir, discardLogger())
	sha, err := c.Commit(context.Background(), CommitReq{
		Message: "flag experiment: death_grace_period_ms",
		Files:   map[string]string{"agent-experiments/death-grace-period-ms.md": "# evidence\n"},
	})
	if err != nil {
		t.Fatalf("Commit: %v", err)
	}
	if len(strings.TrimSpace(sha)) < 7 {
		t.Errorf("commit SHA looks wrong: %q", sha)
	}
	// The file landed in the working tree.
	if _, err := os.Stat(filepath.Join(dir, "agent-experiments/death-grace-period-ms.md")); err != nil {
		t.Errorf("committed file not written: %v", err)
	}
	// And NO remote was added (local target never pushes).
	cmd := exec.Command("git", "remote")
	cmd.Dir = dir
	out, _ := cmd.CombinedOutput()
	if strings.TrimSpace(string(out)) != "" {
		t.Errorf("local git client added a remote: %q", out)
	}
}

// TestPromoterPRLocalUsesRealGit wires the Promoter over the REAL local git client
// (temp repo) end to end: mode=pr, target=local commits the evidence body.
func TestPromoterPRLocalUsesRealGit(t *testing.T) {
	dir := initTempRepo(t)
	t.Setenv("GIT_AUTHOR_NAME", "test")
	t.Setenv("GIT_AUTHOR_EMAIL", "test@example.com")
	t.Setenv("GIT_COMMITTER_NAME", "test")
	t.Setenv("GIT_COMMITTER_EMAIL", "test@example.com")

	p := NewPromoter(nil, NewGitClient(dir, discardLogger()), nil, nil, nil, testRepo(), nil, discardLogger())
	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModePR, Target: TargetLocal}, testEvidence())

	if res.Outcome != OutcomeApplied {
		t.Fatalf("outcome = %q, want applied", res.Outcome)
	}
	if !strings.HasPrefix(res.URL, "local:") {
		t.Errorf("local URL = %q, want local: prefix", res.URL)
	}
	// The evidence file exists with the structured body.
	data, err := os.ReadFile(filepath.Join(dir, "agent-experiments/death-grace-period-ms.md"))
	if err != nil {
		t.Fatalf("evidence file: %v", err)
	}
	if !strings.Contains(string(data), "Fitness before/after") {
		t.Errorf("committed evidence missing fitness section")
	}
}
