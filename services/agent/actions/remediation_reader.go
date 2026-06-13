package actions

import (
	"fmt"
	"log/slog"
	"os"
	"sync"

	"github.com/joustmania/agent/flagset"
)

// flagRemediationAllowed is the rollout-domain flag whose resolved value gates
// auto-rollback. It lives in the SAME file the agent already owns and writes
// (services/flagd/rollout.json), so the agent reads it directly from that
// document rather than standing up a second flagd RPC domain client.
const flagRemediationAllowed = "remediation_allowed"

// RemediationReader resolves the `remediation_allowed` gate from the rollout
// flag file. PR F's auto-rollback is allowed ONLY when this is true; on any
// error (file missing, unparseable, flag absent) it returns false so the loop
// falls back to recommend-only — the fail-closed safe default.
//
// remediation_allowed lives in the `rollout` flagd domain (rollout.json,
// flagSetId "rollout"), NOT the agent domain (agent.json). The agent already
// owns rollout.json's path (it is the sole writer via RolloutWriter), so the
// simplest authoritative source is to resolve the flag's defaultVariant from
// that document directly — reusing the same order-preserving parser as the
// writer — rather than registering a second OpenFeature named provider. It
// reads the file fresh on each call so flipping the flag takes effect live.
type RemediationReader struct {
	path string
	log  *slog.Logger

	mu sync.Mutex
}

// NewRemediationReader builds a reader over the rollout flag file at path.
// path "" uses DefaultRolloutPath. log nil uses slog.Default().
func NewRemediationReader(path string, log *slog.Logger) *RemediationReader {
	if path == "" {
		path = DefaultRolloutPath
	}
	if log == nil {
		log = slog.Default()
	}
	return &RemediationReader{path: path, log: log}
}

// NewRemediationReaderFromEnv builds a reader using ROLLOUT_FLAG_PATH (default
// DefaultRolloutPath), mirroring NewRolloutWriterFromEnv — both target the same
// file, so the reader and writer always agree on the path.
func NewRemediationReaderFromEnv(log *slog.Logger) *RemediationReader {
	return NewRemediationReader(flagset.ResolvePath("ROLLOUT_FLAG_PATH", "rollout"), log)
}

// RemediationAllowed resolves the `remediation_allowed` flag's currently-served
// boolean value from the rollout file: it reads defaultVariant, looks that
// variant up in the variants map, and returns its bool. ANY error path (read,
// parse, missing flag/variant, non-bool value) returns false — auto-rollback
// stays gated off unless the document unambiguously says it is allowed.
func (r *RemediationReader) RemediationAllowed() bool {
	allowed, err := r.resolve()
	if err != nil {
		r.log.Debug("agent.remediation_allowed fell back to false", "path", r.path, "error", err)
		return false
	}
	return allowed
}

// resolve performs the file read + defaultVariant resolution. Separated from
// RemediationAllowed so tests can assert on the error path explicitly.
func (r *RemediationReader) resolve() (bool, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	raw, err := os.ReadFile(r.path)
	if err != nil {
		return false, fmt.Errorf("read rollout file %q: %w", r.path, err)
	}
	doc, err := parseDoc(raw)
	if err != nil {
		return false, err
	}
	f, err := doc.flag(flagRemediationAllowed)
	if err != nil {
		return false, err
	}

	var defaultVariant string
	if ok, err := f.get("defaultVariant", &defaultVariant); err != nil {
		return false, err
	} else if !ok {
		return false, fmt.Errorf("flag %q has no defaultVariant", flagRemediationAllowed)
	}

	variants := map[string]any{}
	if ok, err := f.get("variants", &variants); err != nil {
		return false, err
	} else if !ok {
		return false, fmt.Errorf("flag %q has no variants object", flagRemediationAllowed)
	}

	val, ok := variants[defaultVariant]
	if !ok {
		return false, fmt.Errorf("flag %q defaultVariant %q not in variants", flagRemediationAllowed, defaultVariant)
	}
	b, ok := val.(bool)
	if !ok {
		return false, fmt.Errorf("flag %q variant %q is not a boolean", flagRemediationAllowed, defaultVariant)
	}
	return b, nil
}
