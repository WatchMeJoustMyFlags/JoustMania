package actions

import (
	"os"
	"path/filepath"
	"testing"
)

// writeRolloutFile writes content to a temp rollout file and returns its path.
func writeRolloutFile(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "rollout.json")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write temp rollout file: %v", err)
	}
	return path
}

const rolloutDocAllowed = `{
  "metadata": { "flagSetId": "rollout" },
  "flags": {
    "remediation_allowed": {
      "state": "ENABLED",
      "variants": { "on": true, "off": false },
      "defaultVariant": "on"
    }
  }
}`

const rolloutDocBlocked = `{
  "metadata": { "flagSetId": "rollout" },
  "flags": {
    "remediation_allowed": {
      "state": "ENABLED",
      "variants": { "on": true, "off": false },
      "defaultVariant": "off"
    }
  }
}`

func TestRemediationReader_AllowedTrue(t *testing.T) {
	path := writeRolloutFile(t, rolloutDocAllowed)
	r := NewRemediationReader(path, nil)
	if !r.RemediationAllowed() {
		t.Fatalf("RemediationAllowed() = false, want true (defaultVariant on)")
	}
}

func TestRemediationReader_BlockedFalse(t *testing.T) {
	path := writeRolloutFile(t, rolloutDocBlocked)
	r := NewRemediationReader(path, nil)
	if r.RemediationAllowed() {
		t.Fatalf("RemediationAllowed() = true, want false (defaultVariant off)")
	}
}

func TestRemediationReader_LiveFlip(t *testing.T) {
	// Reads the file fresh each call: rewriting the file flips the result with no
	// reader rebuild (mirrors flagd live evaluation).
	path := writeRolloutFile(t, rolloutDocBlocked)
	r := NewRemediationReader(path, nil)
	if r.RemediationAllowed() {
		t.Fatalf("initial RemediationAllowed() = true, want false")
	}
	if err := os.WriteFile(path, []byte(rolloutDocAllowed), 0o644); err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if !r.RemediationAllowed() {
		t.Fatalf("after flip RemediationAllowed() = false, want true")
	}
}

func TestRemediationReader_MissingFileFalse(t *testing.T) {
	r := NewRemediationReader(filepath.Join(t.TempDir(), "nope.json"), nil)
	if r.RemediationAllowed() {
		t.Fatalf("missing file → RemediationAllowed() = true, want false (fail-closed)")
	}
}

func TestRemediationReader_MalformedFalse(t *testing.T) {
	path := writeRolloutFile(t, `{ not json`)
	r := NewRemediationReader(path, nil)
	if r.RemediationAllowed() {
		t.Fatalf("malformed doc → RemediationAllowed() = true, want false (fail-closed)")
	}
}

func TestRemediationReader_FlagAbsentFalse(t *testing.T) {
	path := writeRolloutFile(t, `{
  "metadata": { "flagSetId": "rollout" },
  "flags": { "target_backend": { "variants": { "python": "python" }, "defaultVariant": "python" } }
}`)
	r := NewRemediationReader(path, nil)
	if r.RemediationAllowed() {
		t.Fatalf("flag absent → RemediationAllowed() = true, want false (fail-closed)")
	}
}

func TestRemediationReader_NonBoolVariantFalse(t *testing.T) {
	// defaultVariant points at a non-boolean variant value → fail-closed.
	path := writeRolloutFile(t, `{
  "flags": {
    "remediation_allowed": {
      "variants": { "on": "yes" },
      "defaultVariant": "on"
    }
  }
}`)
	r := NewRemediationReader(path, nil)
	if r.RemediationAllowed() {
		t.Fatalf("non-bool variant → RemediationAllowed() = true, want false (fail-closed)")
	}
}

func TestRemediationReader_DanglingDefaultVariantFalse(t *testing.T) {
	// defaultVariant names a variant that does not exist → fail-closed.
	path := writeRolloutFile(t, `{
  "flags": {
    "remediation_allowed": {
      "variants": { "on": true, "off": false },
      "defaultVariant": "maybe"
    }
  }
}`)
	r := NewRemediationReader(path, nil)
	if r.RemediationAllowed() {
		t.Fatalf("dangling defaultVariant → RemediationAllowed() = true, want false (fail-closed)")
	}
}

func TestNewRemediationReaderFromEnv_UsesRolloutPath(t *testing.T) {
	path := writeRolloutFile(t, rolloutDocAllowed)
	t.Setenv("ROLLOUT_FLAG_PATH", path)
	r := NewRemediationReaderFromEnv(nil)
	if !r.RemediationAllowed() {
		t.Fatalf("FromEnv reader did not resolve allowed from ROLLOUT_FLAG_PATH")
	}
}
