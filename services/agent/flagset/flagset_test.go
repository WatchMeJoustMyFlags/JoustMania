package flagset

import (
	"os"
	"path/filepath"
	"testing"
)

func writeFile(t *testing.T, dir, name string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(`{"flags":{}}`), 0o644); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
}

func TestFlagDir(t *testing.T) {
	t.Setenv(FlagDirEnv, "")
	if got := FlagDir(); got != DefaultFlagDir {
		t.Errorf("FlagDir() default = %q, want %q", got, DefaultFlagDir)
	}
	t.Setenv(FlagDirEnv, "/custom/dir")
	if got := FlagDir(); got != "/custom/dir" {
		t.Errorf("FlagDir() = %q, want /custom/dir", got)
	}
}

func TestActiveFlagFileIsPlainJoin(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(FlagDirEnv, dir)
	for _, domain := range []string{"game", "interventions", "rollout"} {
		if got := ActiveFlagFile(domain); got != filepath.Join(dir, domain+".json") {
			t.Errorf("ActiveFlagFile(%q) = %q, want %q", domain, got, filepath.Join(dir, domain+".json"))
		}
	}
}

func TestResolvePathHonoursOverrideFirst(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, dir, "interventions.json")
	t.Setenv(FlagDirEnv, dir)

	// Override env wins outright.
	t.Setenv("INTERVENTIONS_FLAG_PATH", "/explicit/interventions.json")
	if got := ResolvePath("INTERVENTIONS_FLAG_PATH", "interventions"); got != "/explicit/interventions.json" {
		t.Errorf("override path = %q", got)
	}

	// Unset override falls back to the centralised resolution.
	t.Setenv("INTERVENTIONS_FLAG_PATH", "")
	if got := ResolvePath("INTERVENTIONS_FLAG_PATH", "interventions"); got != filepath.Join(dir, "interventions.json") {
		t.Errorf("fallback path = %q", got)
	}
}

func TestResolvePathProductionDefaultsUnchanged(t *testing.T) {
	// No env set: production in-container defaults must match the old hardcoded
	// /etc/flagd/<domain>.json paths.
	t.Setenv(FlagDirEnv, "")
	// Lock the production write target for EVERY domain the agent writes — these are
	// the single most dangerous regression vector (a silent move would break the
	// agent's flag writes). The expected value must equal the dir component of each
	// writer's Default*Path const; if FlagDir/the const ever drift, this fails.
	for _, c := range []struct{ env, domain, want string }{
		{"GAME_FLAG_PATH", "game", "/etc/flagd/game.json"},
		{"INTERVENTIONS_FLAG_PATH", "interventions", "/etc/flagd/interventions.json"},
		{"ROLLOUT_FLAG_PATH", "rollout", "/etc/flagd/rollout.json"},
	} {
		t.Setenv(c.env, "")
		if got := ResolvePath(c.env, c.domain); got != c.want {
			t.Errorf("production %s default = %q, want %q", c.domain, got, c.want)
		}
	}
}
