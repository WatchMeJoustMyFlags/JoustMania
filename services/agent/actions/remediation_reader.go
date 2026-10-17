package actions

import (
	"context"
	"time"
)

// RemediationFlags is the flag read the remediation gate needs: the agent's
// OpenFeature wrapper (flags.Flags) satisfies it.
type RemediationFlags interface {
	RemediationAllowed(ctx context.Context) bool
}

// remediationReadTimeout bounds one flag read on the infra loop's path.
const remediationReadTimeout = 500 * time.Millisecond

// RemediationReader resolves the `remediation_allowed` gate (rollout set) for
// PR F's auto-rollback (R1, #1411): it evaluates the flag through flagd like
// every other flag, instead of reading rollout.json from disk. Any evaluation
// error resolves the default false, so auto-rollback stays recommend-only
// unless flagd says it is allowed (fail closed).
type RemediationReader struct {
	flags RemediationFlags
}

// NewRemediationReader builds the reader over the agent's flag client.
func NewRemediationReader(flags RemediationFlags) *RemediationReader {
	return &RemediationReader{flags: flags}
}

// RemediationAllowed evaluates remediation_allowed (default false).
func (r *RemediationReader) RemediationAllowed() bool {
	if r == nil || r.flags == nil {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), remediationReadTimeout)
	defer cancel()
	return r.flags.RemediationAllowed(ctx)
}
