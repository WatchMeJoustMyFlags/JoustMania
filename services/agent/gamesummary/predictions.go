package gamesummary

import (
	"sort"
	"time"
)

// predictions.go carries the scored predictions of a game into its Summary
// (UC-042 A12, #2479). The Book (package prediction) scores them; the Summary
// only copies the finalized results and tallies them. BuildSummary stays pure:
// the results arrive through BuildOptions.Predictions.

// Prediction outcomes as the Book records them (prediction.OutcomeHit and so on);
// repeated here because gamesummary must not import the prediction package.
const (
	PredictionHit        = "hit"
	PredictionMiss       = "miss"
	PredictionUnscorable = "unscorable"
)

// PredictionSourceControl is the source of a control prediction, registered only
// to measure a template's base rate (UC-040 A44 step 4).
const PredictionSourceControl = "control"

// PredictionOutcome is one scored prediction of the game. It carries the target
// serial of a target-scope prediction (like PlayerArc does), so the cross-game
// window never copies this type (UC-042 BR-004, A12 step 3).
type PredictionOutcome struct {
	ID string `json:"id"`
	// OffsetSeconds is when it was registered, relative to the game's first
	// observed timeline event (the same anchor as the player arcs).
	OffsetSeconds    float64 `json:"offset_seconds"`
	Backend          string  `json:"backend"`
	Source           string  `json:"source"`
	Template         string  `json:"template"`
	Characteristic   string  `json:"characteristic,omitempty"`
	Scope            string  `json:"scope,omitempty"`
	Target           string  `json:"target,omitempty"`
	HorizonS         int     `json:"horizon_s"`
	P                float64 `json:"p"`
	Rule             string  `json:"rule,omitempty"`
	Acted            bool    `json:"acted"`
	Outcome          string  `json:"outcome"`
	UnscorableReason string  `json:"unscorable_reason,omitempty"`
	DecidedEarly     bool    `json:"decided_early,omitempty"`

	// RegisteredAt is the registration time; BuildSummary turns it into
	// OffsetSeconds. Not serialized.
	RegisteredAt time.Time `json:"-"`
}

// PredictionTally is the count of one (backend, source, template) group, acted
// and counterfactual kept apart (UC-040 BR-034). It holds no serial, so the
// cross-game window may keep it.
type PredictionTally struct {
	Backend    string  `json:"backend"`
	Source     string  `json:"source"`
	Template   string  `json:"template"`
	Acted      bool    `json:"acted"`
	Hits       int     `json:"hits"`
	Misses     int     `json:"misses"`
	Unscorable int     `json:"unscorable"`
	BrierSum   float64 `json:"brier_sum"`
}

// Scored is the number of hits and misses; unscorable is neither.
func (t PredictionTally) Scored() int { return t.Hits + t.Misses }

// predictionOutcomes copies the results, sets the offsets from the anchor and
// orders them by registration time then id.
func predictionOutcomes(in []PredictionOutcome, anchor time.Time) []PredictionOutcome {
	if len(in) == 0 {
		return nil
	}
	out := make([]PredictionOutcome, len(in))
	copy(out, in)
	for i := range out {
		if !anchor.IsZero() && !out[i].RegisteredAt.IsZero() {
			out[i].OffsetSeconds = out[i].RegisteredAt.Sub(anchor).Seconds()
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		if !out[i].RegisteredAt.Equal(out[j].RegisteredAt) {
			return out[i].RegisteredAt.Before(out[j].RegisteredAt)
		}
		return out[i].ID < out[j].ID
	})
	return out
}

// TallyPredictions groups outcomes by backend, source, template and acted, in
// that order. Unscorable counts apart and adds nothing to the Brier sum.
func TallyPredictions(outcomes []PredictionOutcome) []PredictionTally {
	if len(outcomes) == 0 {
		return nil
	}
	type key struct {
		backend, source, template string
		acted                     bool
	}
	idx := map[key]*PredictionTally{}
	for _, o := range outcomes {
		k := key{o.Backend, o.Source, o.Template, o.Acted}
		t, ok := idx[k]
		if !ok {
			t = &PredictionTally{Backend: o.Backend, Source: o.Source, Template: o.Template, Acted: o.Acted}
			idx[k] = t
		}
		switch o.Outcome {
		case PredictionHit:
			t.Hits++
			t.BrierSum += (o.P - 1) * (o.P - 1)
		case PredictionMiss:
			t.Misses++
			t.BrierSum += o.P * o.P
		default:
			t.Unscorable++
		}
	}
	out := make([]PredictionTally, 0, len(idx))
	for _, t := range idx {
		out = append(out, *t)
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.Backend != b.Backend {
			return a.Backend < b.Backend
		}
		if a.Source != b.Source {
			return a.Source < b.Source
		}
		if a.Template != b.Template {
			return a.Template < b.Template
		}
		return a.Acted && !b.Acted
	})
	return out
}

// PredictionCounts are the totals the summary span carries.
type PredictionCounts struct {
	Scored, Hit, Miss, Unscorable int
	// Counterfactual and Controls count the predictions left out of the four above.
	Counterfactual, Controls int
}

// CountPredictions totals the ACTED decision predictions (summary.predictions.*):
// scored = hit + miss. Counterfactuals (acted=false) and controls are counted apart,
// never mixed into the acted calibration (UC-040 BR-034).
func CountPredictions(outcomes []PredictionOutcome) PredictionCounts {
	var c PredictionCounts
	for _, o := range outcomes {
		if o.Source == PredictionSourceControl {
			c.Controls++
			continue
		}
		if !o.Acted {
			c.Counterfactual++
			continue
		}
		switch o.Outcome {
		case PredictionHit:
			c.Hit++
			c.Scored++
		case PredictionMiss:
			c.Miss++
			c.Scored++
		default:
			c.Unscorable++
		}
	}
	return c
}
