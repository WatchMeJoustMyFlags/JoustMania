package actions

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"os"
)

// neutralInterventions is the canonical, fully-neutral interventions document
// (every flag at its neutral defaultVariant, empty targeting, no agent-driven
// "active" variants). It is a byte-for-byte copy of services/flagd/
// interventions.json and is the self-heal target when the on-disk file is found
// unparseable. Embedding it means the agent can always restore a VALID, complete
// schema with no game effect even when the on-disk file has been poisoned and no
// last-known-good is in memory (e.g. corrupt before the first write).
//
//go:embed neutral_interventions.json
var neutralInterventions []byte

// NeutralDocument returns a copy of the embedded neutral interventions document.
// Exposed for tests asserting the self-heal target is valid.
func NeutralDocument() []byte {
	out := make([]byte, len(neutralInterventions))
	copy(out, neutralInterventions)
	return out
}

// HealIfCorrupt validates the interventions file at startup and self-heals it to
// the neutral document if it is missing or unparseable (#924). A poisoned file
// makes flagd reject the whole flag set, silently falling EVERY intervention back
// to its safe default — so the agent proactively repairs it before its first
// write rather than waiting for an operator.
//
// It returns:
//   - healed=false, err=nil   when the file already parses (no action taken);
//   - healed=true,  err=nil   when a corrupt/missing file was replaced with the
//     neutral document and agent_intervention_file_corruption_total{phase=
//     "startup"} was incremented;
//   - healed=false, err!=nil  when the file is corrupt AND the neutral write
//     itself failed (the agent should still start — log and continue).
//
// Safe for concurrent use; it takes the same mutex the write path uses.
func (w *Writer) HealIfCorrupt(ctx context.Context) (healed bool, err error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	raw, readErr := os.ReadFile(w.path)
	if readErr == nil {
		_, readErr = parseDoc(raw)
		if readErr == nil {
			return false, nil // already valid; leave any operator/agent state intact
		}
	} else if !errors.Is(readErr, os.ErrNotExist) {
		// A read error other than "missing" (e.g. permission) is not something we
		// can heal by writing; surface it so startup can log and continue.
		return false, fmt.Errorf("read interventions file %q for startup validation: %w", w.path, readErr)
	}

	w.recordCorruption(ctx, phaseStartup)
	w.log.Error("agent.intervention_file_corrupt",
		"phase", phaseStartup,
		"path", w.path,
		"error", readErr,
		"action", "writing neutral document")

	if werr := w.writeInPlace(NeutralDocument()); werr != nil {
		return false, fmt.Errorf("self-heal interventions file %q: %w", w.path, werr)
	}
	return true, nil
}
