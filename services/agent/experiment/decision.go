package experiment

// Outcome is the verdict on an experiment that the promoter (promote.Promoter)
// acts on. The set is closed so the audit trail can enumerate every result.
type Outcome string

const (
	// OutcomePromote — the experiment earned promotion. The real-default change is
	// the promoter's gated job (#936).
	OutcomePromote Outcome = "promote"
	// OutcomeDiscard — the experiment did not earn promotion; nothing is promoted.
	OutcomeDiscard Outcome = "discard"
	// OutcomeRevert — the experiment degraded fitness and was rolled back. The
	// promoter treats it like a discard (only promote acts).
	OutcomeRevert Outcome = "revert"
)

// Decision is the verdict plus the fitness numbers it rested on, handed to
// promote.Promoter.Promote (built by promote's ExperimentPromoter from a
// concluded cohort verdict).
type Decision struct {
	// Outcome is the verdict (promote/discard/revert).
	Outcome Outcome
	// FitnessBefore is the baseline (control-arm) fitness.
	FitnessBefore float64
	// FitnessAfter is the fitness with the experiment active.
	FitnessAfter float64
	// Games is the number of games the verdict rests on.
	Games int
}
