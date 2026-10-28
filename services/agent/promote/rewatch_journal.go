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

// journalRewatch journals the one outcome of a re-watch revert the re-watch
// owned (UC-043 step 7 and A9, #1883): `reverted` with the watch reason, or
// `applied` with the failure reason when the revert failed or no reverter is
// wired. The re-watch tries the revert once; a retry after a failure, and its
// `reverted` event, belong to the kill switch (A1, #1753). Without a sink or an
// experiment id nothing is journaled.
func (pr *Promoter) journalRewatch(experimentID string, res Result) {
	pr.mu.Lock()
	sink := pr.rewatchJournal
	pr.mu.Unlock()
	if sink == nil || experimentID == "" {
		return
	}
	sink(experimentID, experiment.PromotionResult{
		Outcome:       string(res.Outcome),
		Reason:        RewatchReasonPrefix + res.Reason,
		RevertPending: res.RevertPending,
	})
}

// RewatchReasonPrefix marks a promotion reason journaled by the re-watch.
const RewatchReasonPrefix = "re-watch: "
