package experiment

import (
	"context"
	"fmt"
	"sync"
)

// ProposalSink receives a decoded Proposer proposal. The production sink is a
// RegistrySink: the proposal becomes a declared experiment that the registry
// runs in real shadow games (experiment id + arms, FR-138), so its value is one
// a game actually reads. A bare Gate is never the Proposer's sink in production:
// an experiment-arm value with an id no registered game carries is unreachable
// (PR #1417 review B2).
type ProposalSink interface {
	Review(ctx context.Context, p Proposal) error
}

// RegistrySink declares a proposal as an experiment and starts it (Start writes
// the arm value through the Gate, so the invariant checks still apply).
type RegistrySink struct {
	Registry *Registry
	// Objective is the fitness objective the declared experiment optimises.
	Objective string
	// Budget returns the live-experiment budget; a proposal is refused once the
	// registry holds that many live experiments. nil or <= 0 = the registry's capacity.
	Budget func(ctx context.Context) int
	// Lock, when set, serialises the check-and-declare with the other declarers
	// (the dynamic declarer's mutex) so they cannot over-declare together.
	Lock sync.Locker
}

// Review declares and starts p. A refusal is a *RejectError; a failed Start
// aborts the declared experiment so nothing is left behind.
func (s *RegistrySink) Review(ctx context.Context, p Proposal) error {
	if s == nil || s.Registry == nil {
		return &RejectError{Reason: ReasonNoExperimentLoop}
	}
	if s.Lock != nil {
		s.Lock.Lock()
		defer s.Lock.Unlock()
	}
	if _, busy := s.Registry.ActiveFlags()[p.FlagKey]; busy {
		return &RejectError{Reason: ReasonFlagUnderTest, Detail: p.FlagKey}
	}
	budget := s.Registry.Capacity()
	if s.Budget != nil {
		if b := s.Budget(ctx); b > 0 && (budget <= 0 || b < budget) {
			budget = b
		}
	}
	if budget > 0 && s.Registry.LiveCount() >= budget {
		return &RejectError{Reason: ReasonNoCapacity, Detail: fmt.Sprintf("%d live experiments", s.Registry.LiveCount())}
	}
	id, err := s.Registry.Declare(Intent{
		Hypothesis:        p.Rationale,
		FlagKey:           p.FlagKey,
		ExperimentalValue: p.ExperimentalValue,
		Objective:         s.Objective,
	})
	if err != nil {
		return err
	}
	if err := s.Registry.Start(ctx, id); err != nil {
		_ = s.Registry.Abort(ctx, id, AbortReasonStartFailed)
		return err
	}
	return nil
}

// LateSink forwards to a sink set after construction (main.go builds the
// Proposer before the experiment loop). Until it is set, every proposal is
// refused with ReasonNoExperimentLoop: no write happens.
type LateSink struct {
	mu   sync.RWMutex
	sink ProposalSink
}

// Set installs the sink.
func (l *LateSink) Set(s ProposalSink) {
	l.mu.Lock()
	l.sink = s
	l.mu.Unlock()
}

// Review forwards p, or refuses it when no sink is set.
func (l *LateSink) Review(ctx context.Context, p Proposal) error {
	l.mu.RLock()
	s := l.sink
	l.mu.RUnlock()
	if s == nil {
		return &RejectError{Reason: ReasonNoExperimentLoop}
	}
	return s.Review(ctx, p)
}
