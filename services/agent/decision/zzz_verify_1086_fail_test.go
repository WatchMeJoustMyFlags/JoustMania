package decision

import "testing"

// TestDeliberate1086GatingFailure is a THROWAWAY deliberate failure to verify the
// #1086 CI-gating fix (PR #1146) live: a genuine test failure must make the
// `CI Complete` required status post FAILURE and block the merge. This PR is
// created only to observe that, and will be CLOSED — never merged.
func TestDeliberate1086GatingFailure(t *testing.T) {
	t.Fatal("deliberate failure verifying #1086: CI Complete must post FAILURE and block merge")
}
