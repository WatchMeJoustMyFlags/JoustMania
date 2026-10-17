package experiment

import (
	"context"
	"sync"
)

// targeting_writer.go is the REAL TargetingWriter seam (#977) the registry (#978)
// injects — the production replacement for the recording fake used in
// registry_test.go. It adapts the Gate (Review, which validates the invariant AND
// writes on accept) for Apply, and the Writer (Strip, which un-nests one
// experiment's branch + variant) for teardown.
//
// SAFETY: Apply routes through Gate.Review, so an experiment's flag write is held
// to THE INVARIANT (real resolution unchanged, game set only) exactly as a
// proposer write is — the registry cannot bypass the gate. Strip goes straight to
// the Writer (no Gate): removing an experiment value can only ever restore the
// pre-experiment resolution, so it needs no invariant check (design §10).
type GateTargetingWriter struct {
	gate   *Gate
	writer *Writer

	mu sync.Mutex
	// epochs: the flag service's boot epoch when each experiment's arm value
	// was written. A different epoch means a restart lost the value.
	epochs map[string]string
}

// epochReader is the optional flag-service capability Reassert needs.
type epochReader interface {
	Epoch(ctx context.Context) (string, error)
}

// NewGateTargetingWriter builds the seam over a Gate (for gated Apply) and the
// Writer (for Strip); main.go constructs them as a pair.
func NewGateTargetingWriter(gate *Gate, writer *Writer) *GateTargetingWriter {
	return &GateTargetingWriter{gate: gate, writer: writer, epochs: map[string]string{}}
}

// Apply writes the experiment-scoped shadow targeting for p through the Gate,
// which validates the invariant and applies the write on accept (a rejected
// proposal returns the typed *RejectError and writes nothing). It satisfies
// experiment.TargetingWriter.
func (g *GateTargetingWriter) Apply(ctx context.Context, p Proposal) error {
	// Read the epoch BEFORE the write: a restart in between leaves the old epoch
	// recorded, so the next Reassert writes the value again (the safe direction).
	epoch := g.epoch(ctx)
	if err := g.gate.Review(ctx, p); err != nil {
		return err
	}
	g.mu.Lock()
	g.epochs[p.ExperimentID] = epoch
	g.mu.Unlock()
	return nil
}

// Reassert writes p's arm value again when the flag service restarted since it
// was written (a new boot epoch): the restart dropped it, and the experiment's
// next shadow games would otherwise resolve the baseline on both arms. The
// registry calls it before each pair spawn. An unreadable epoch is an error, so
// the pair is not spawned.
func (g *GateTargetingWriter) Reassert(ctx context.Context, p Proposal) error {
	er, ok := g.writer.Service().(epochReader)
	if !ok {
		return nil
	}
	cur, err := er.Epoch(ctx)
	if err != nil {
		return err
	}
	g.mu.Lock()
	written, known := g.epochs[p.ExperimentID]
	g.mu.Unlock()
	if known && written == cur {
		return nil
	}
	return g.Apply(ctx, p)
}

func (g *GateTargetingWriter) epoch(ctx context.Context) string {
	if er, ok := g.writer.Service().(epochReader); ok {
		if e, err := er.Epoch(ctx); err == nil {
			return e
		}
	}
	return ""
}

// Strip removes the experiment's arm value for (flagKey, experimentID). A
// missing value/flag is a no-op (idempotent teardown). It satisfies
// experiment.TargetingWriter.
func (g *GateTargetingWriter) Strip(ctx context.Context, flagKey, experimentID string) error {
	g.mu.Lock()
	delete(g.epochs, experimentID)
	g.mu.Unlock()
	return g.writer.Strip(ctx, flagKey, experimentID)
}
