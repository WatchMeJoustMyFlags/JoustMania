package promote

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// git.go is the REAL local-target git client (TargetLocal): write files into a
// working tree and `git commit` them. It NEVER pushes — local target by definition
// does not reach a remote (a `git push` is structurally absent from this file, the
// safety rail for the local path). It is built ONLY by NewGitClientFromEnv behind
// the env gate, mirroring the GitHub client. Tests exercise it against a throwaway
// `git init` temp repo (real git, NO network).

// execGitClient runs git in a fixed working-tree directory.
type execGitClient struct {
	dir string // the working-tree root; files are written relative to it
	log *slog.Logger
}

// NewGitClientFromEnv returns the REAL local git client IFF the env gate is
// satisfied for a local-target apply, else nil (the caller substitutes the no-op).
// This is the ONLY constructor for a real local-git client.
//
// Gate conditions:
//   - target == TargetLocal
//   - AGENT_CODE_IMPROVEMENT_ENABLED == "true"   (same explicit opt-in as github)
//   - AGENT_LOCAL_REPO_DIR points at a directory  (where to commit)
//
// Returns (nil, nil) when not gated (the fail-closed default). Returns (nil, err)
// only when gated-on but the configured directory is missing/invalid.
func NewGitClientFromEnv(target Target, log *slog.Logger) (*execGitClient, error) {
	if log == nil {
		log = slog.Default()
	}
	if target != TargetLocal {
		return nil, nil
	}
	if !strings.EqualFold(strings.TrimSpace(os.Getenv("AGENT_CODE_IMPROVEMENT_ENABLED")), "true") {
		log.Info("code_improvement local git real client NOT built: AGENT_CODE_IMPROVEMENT_ENABLED != true")
		return nil, nil
	}
	dir := strings.TrimSpace(os.Getenv("AGENT_LOCAL_REPO_DIR"))
	if dir == "" {
		log.Info("code_improvement local git real client NOT built: AGENT_LOCAL_REPO_DIR unset (fail-closed to no-op)")
		return nil, nil
	}
	info, err := os.Stat(dir)
	if err != nil || !info.IsDir() {
		return nil, fmt.Errorf("AGENT_LOCAL_REPO_DIR=%q is not a directory: %v", dir, err)
	}
	log.Warn("code_improvement local git real client ENABLED (#936): local-target promotions WILL commit (no push)", "dir", dir)
	return NewGitClient(dir, log), nil
}

// NewGitClient builds a local git client over a working-tree directory. Exported
// so TESTS can point it at a t.TempDir() `git init` repo (real git, no network) —
// the strategy doc's "local-git tests may use a throwaway git init temp repo".
// This is a LOCAL-ONLY commit client; it does not and cannot push.
func NewGitClient(dir string, log *slog.Logger) *execGitClient {
	if log == nil {
		log = slog.Default()
	}
	return &execGitClient{dir: dir, log: log}
}

// Commit writes req.Files (paths relative to the working tree), stages them, and
// commits with req.Message. Returns the new commit SHA. NO push.
func (c *execGitClient) Commit(ctx context.Context, req CommitReq) (string, error) {
	for rel, content := range req.Files {
		abs := filepath.Join(c.dir, filepath.Clean("/"+rel)) // confine to the tree
		if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
			return "", fmt.Errorf("mkdir for %q: %w", rel, err)
		}
		if err := os.WriteFile(abs, []byte(content), 0o644); err != nil {
			return "", fmt.Errorf("write %q: %w", rel, err)
		}
		if _, err := c.run(ctx, "add", "--", rel); err != nil {
			return "", err
		}
	}
	if _, err := c.run(ctx, "commit", "-m", req.Message); err != nil {
		return "", err
	}
	sha, err := c.run(ctx, "rev-parse", "HEAD")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(sha), nil
}

// run executes `git <args>` in the working-tree dir and returns stdout.
func (c *execGitClient) run(ctx context.Context, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "git", args...)
	cmd.Dir = c.dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("git %s: %w: %s", strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	}
	return string(out), nil
}
