package calibration

import "github.com/joustmania/agent/gamejournal"

// DefaultManualN is the window of a manual run (A1) while
// calibration.every_n_games is off or unreadable: the flag's default variant.
const DefaultManualN = 100

// Watermark is the journal position a completed calibration ends at (BR-007):
// the game id of the newest journal record when it completed. Only records
// journaled after it count. The zero value counts every record.
type Watermark struct {
	GameID string
}

// Selection is the result of Select.
type Selection struct {
	// Games are the most recent n eligible games after the watermark, in
	// journal append order (oldest first).
	Games []gamejournal.Record
	// Eligible is the number of eligible games after the watermark (step 2
	// counts it against N), at least len(Games).
	Eligible int
	// Excluded counts the ineligible games after the watermark per reason.
	Excluded map[Reason]int
}

// Select applies the watermark and BR-002 to the journal's records (oldest
// first, as gamejournal.Journal.Records returns them) and keeps the most recent
// n eligible games; n <= 0 keeps all of them. The order is the journal's append
// order, never the records' end time: the Pi has no real-time clock.
//
// A watermark that is no longer in the journal (evicted, BR-009) counts every
// record, as does the zero watermark.
func Select(recs []gamejournal.Record, after Watermark, n int) Selection {
	start := 0
	if after.GameID != "" {
		for i := len(recs) - 1; i >= 0; i-- {
			if recs[i].GameID == after.GameID {
				start = i + 1
				break
			}
		}
	}
	sel := Selection{Excluded: map[Reason]int{}}
	var eligible []gamejournal.Record
	for _, rec := range recs[start:] {
		if reason, ok := Eligible(rec); !ok {
			sel.Excluded[reason]++
			continue
		}
		eligible = append(eligible, rec)
	}
	sel.Eligible = len(eligible)
	if n > 0 && len(eligible) > n {
		eligible = eligible[len(eligible)-n:]
	}
	sel.Games = eligible
	return sel
}
