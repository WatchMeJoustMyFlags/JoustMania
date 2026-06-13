// Package flagset is the Go single source of truth for the active flagd
// flag-file path (issue #959), mirroring lib/flagd_paths.py.
//
// The agent writes flag mutations to host-bind-mounted flagd files
// (interventions/rollout/game). Which file is active for a domain used to be
// resolved per-writer via a domain-specific env (INTERVENTIONS_FLAG_PATH,
// ROLLOUT_FLAG_PATH, GAME_FLAG_PATH) with a hardcoded /etc/flagd/<domain>.json
// default. This package centralises that resolution behind one knob, the flag
// directory FLAGD_FLAG_DIR, so the experiment writer and the action/rollout
// writers all agree and the Python harness (lib/flagd_paths.py) uses the
// identical convention: the active flag file is a plain join
// $FLAGD_FLAG_DIR/<domain>.json. No .ci.json naming, no overlay mapping.
//
// Knob:
//
//	FLAGD_FLAG_DIR  directory holding the flag files (default /etc/flagd).
//
// A domain-specific env override (e.g. INTERVENTIONS_FLAG_PATH), when set, still
// wins — it is the explicit per-writer escape hatch and keeps backward compat.
package flagset

import (
	"os"
	"path/filepath"
)

const (
	// FlagDirEnv overrides the directory holding the flag files.
	FlagDirEnv = "FLAGD_FLAG_DIR"

	// DefaultFlagDir is the in-container flagd flag directory.
	DefaultFlagDir = "/etc/flagd"
)

// FlagDir returns the directory holding the flag files (FLAGD_FLAG_DIR, default
// DefaultFlagDir).
func FlagDir() string {
	if dir := os.Getenv(FlagDirEnv); dir != "" {
		return dir
	}
	return DefaultFlagDir
}

// ActiveFlagFile returns the active host flag-file path for domain: a plain join
// FlagDir()/<domain>.json. This is the single source of truth; callers must not
// hardcode /etc/flagd/<domain>.json.
func ActiveFlagFile(domain string) string {
	return filepath.Join(FlagDir(), domain+".json")
}

// ResolvePath picks the active flag-file path for domain, honouring an explicit
// per-writer env override first (the legacy escape hatch), then falling back to
// the centralised flag-dir resolution. envOverride is the domain-specific env
// var name (e.g. "GAME_FLAG_PATH"); pass "" to skip the override.
func ResolvePath(envOverride, domain string) string {
	if envOverride != "" {
		if p := os.Getenv(envOverride); p != "" {
			return p
		}
	}
	return ActiveFlagFile(domain)
}
