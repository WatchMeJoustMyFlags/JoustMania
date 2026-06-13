package experiment

import "context"

// targeting_writer.go is the REAL TargetingWriter seam (#977) the registry (#978)
// injects — the production replacement for the recording fake used in
// registry_test.go. It adapts the Gate (Review, which validates the invariant AND
// writes on accept) for Apply, and the Writer (Strip, which un-nests one
// experiment's branch + variant) for teardown.
//
// SAFETY: Apply routes through Gate.Review, so an experiment's flag write is held
// to THE INVARIANT (real resolution unchanged, game.json-only) exactly as a
// proposer write is — the registry cannot bypass the gate. Strip goes straight to
// the Writer (no Gate): removing a shadow branch can only ever restore a
// strictly-more-real-protective rule, so it needs no invariant check (design §10).
type GateTargetingWriter struct {
	gate   *Gate
	writer *Writer
}

// NewGateTargetingWriter builds the seam over a Gate (for gated Apply) and the
// Writer (for Strip). The Writer MUST be the SAME one the Gate wraps so Apply and
// Strip operate on one file under one mutex; main.go constructs them as a pair.
func NewGateTargetingWriter(gate *Gate, writer *Writer) *GateTargetingWriter {
	return &GateTargetingWriter{gate: gate, writer: writer}
}

// Apply writes the experiment-scoped shadow targeting for p through the Gate,
// which validates the invariant and applies the write on accept (a rejected
// proposal returns the typed *RejectError and writes nothing). It satisfies
// experiment.TargetingWriter.
func (g *GateTargetingWriter) Apply(ctx context.Context, p Proposal) error {
	return g.gate.Review(ctx, p)
}

// Strip removes the experiment's targeting branch + reserved variant for
// (flagKey, experimentID). A missing branch/flag is a no-op (idempotent
// teardown). It satisfies experiment.TargetingWriter.
func (g *GateTargetingWriter) Strip(_ context.Context, flagKey, experimentID string) error {
	return g.writer.Strip(flagKey, experimentID)
}
