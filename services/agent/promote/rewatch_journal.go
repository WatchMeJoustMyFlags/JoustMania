package promote

import "github.com/joustmania/agent/experiment"

// RewatchJournal journals a later outcome of an experiment's promotion. The
// experiment loop wires Registry.RecordPromotion behind it, so a re-watch revert
// appends a `promotion` event to the experiment's journal (UC-043 step 7, #1883).
type RewatchJournal func(experimentID string, res experiment.PromotionResult)

// SetRewatchJournal wires where a re-watch revert's outcome is journaled. nil
// leaves the outcome logged only.
func (pr *Promoter) SetRewatchJournal(j RewatchJournal) {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	pr.rewatchJournal = j
}

// journalRewatch journals the outcome of a re-watch revert the re-watch owned
// (UC-043 step 7, #1883): `reverted` with the watch reason, or `applied` with the
// failure reason when the revert failed or no reverter is wired. A run of failed
// re-watch reverts of one promotion journals one failure event; the revert that
// succeeds after it journals `reverted`. The failure marker is the re-watch's own
// (rewatchFailReported): the kill switch keeps its own failure report (#1753).
// An empty experiment id has nothing to journal on.
func (pr *Promoter) journalRewatch(experimentID string, entry *appliedPromotion, res Result) {
	pr.mu.Lock()
	sink := pr.rewatchJournal
	if res.Outcome == OutcomeApplied && entry != nil {
		already := entry.rewatchFailReported
		entry.rewatchFailReported = true
		if already {
			pr.mu.Unlock()
			return
		}
	}
	pr.mu.Unlock()
	if sink == nil || experimentID == "" {
		return
	}
	sink(experimentID, experiment.PromotionResult{
		Outcome: string(res.Outcome),
		Reason:  RewatchReasonPrefix + res.Reason,
	})
}

// RewatchReasonPrefix marks a promotion reason journaled by the re-watch.
const RewatchReasonPrefix = "re-watch: "
