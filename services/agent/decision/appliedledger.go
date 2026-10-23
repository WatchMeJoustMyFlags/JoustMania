package decision

import (
	"math"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// The agent's applied-value ledger (UC-040 A35, BR-023; decide-contract §4.5 step 3;
// #2046). The coordinator acts on a state or level flag only when its value changes,
// so writing the value the agent last wrote again changes nothing in the game; it
// only spends the per-game budget. The ledger remembers, per game (one Loop per game
// id), the last value the agent wrote for each intervention and target, and the
// permission chain blocks a decision that repeats it as already_applied, before the
// rate limit and before the shield ledger. The step is check-only: it reserves
// nothing, so its refusal cannot leak a reservation.
//
// The record is the agent's own, in memory: it does not see the flag service. A value
// the flag service lost (a restart, UC-053 A9; a player dropped on re-registration,
// UC-053 A13) is written again once the entry expires (appliedLedgerTTL). A failed
// write, or two writes to one key in flight at once (they may finish in either order),
// delete the entry, so unknown always means "write".

// appliedLedgerTTL bounds an entry: after it the same value is written again, which
// re-asserts a value the flag service lost. A constant, not a tunable (C-035).
const appliedLedgerTTL = 30 * time.Second

// idempotentInterventions are the interventions whose flag is of the registry kind
// state or level (flags.md §13.1, services/flag-service/registry.yaml): writing the
// same value again changes nothing at the coordinator. Grants and events are never
// in it. actions/idempotent_uc040_test.go pins it against the registry.
var idempotentInterventions = map[string]bool{
	InterventionAdjustMusicTempo:        true,
	InterventionAdjustVolume:            true,
	InterventionAdjustGlobalSensitivity: true,
	InterventionAdjustGlobalDifficulty:  true,
	InterventionSetPacingProfile:        true,
	InterventionRampTempo:               true,
	InterventionAutoRubberband:          true,
	InterventionAdjustPlayerSensitivity: true,
	InterventionSetPlayerHandicap:       true,
}

// IdempotentInterventions returns the interventions the already-applied check may
// suppress (state and level kinds), sorted.
func IdempotentInterventions() []string {
	out := make([]string, 0, len(idempotentInterventions))
	for i := range idempotentInterventions {
		out = append(out, i)
	}
	sort.Strings(out)
	return out
}

// normalisedValue is the comparison form of a decision value: a finite number in its
// shortest float form (so "0.8" and "0.80" match), else the trimmed string. An empty
// value stays empty: the writer then writes its constant default.
func normalisedValue(v string) string {
	t := strings.TrimSpace(v)
	if f, err := strconv.ParseFloat(t, 64); err == nil && !math.IsNaN(f) && !math.IsInf(f, 0) {
		return strconv.FormatFloat(f, 'g', -1, 64)
	}
	return t
}

func appliedKey(d Decision) string { return d.Intervention + "|" + d.TargetSerial }

// appliedEntry is the last value the agent wrote for one key in this game.
type appliedEntry struct {
	serial string
	value  string
	at     time.Time
	// reported: the first suppression against this entry was emitted in full; later
	// ones are quiet repeats (as A21).
	reported bool
}

// appliedLedger is the per-Loop applied-value record, under its own mutex.
type appliedLedger struct {
	mu       sync.Mutex
	entries  map[string]appliedEntry
	inflight map[string]int
	// overlapped marks a key that had two writes in flight at once since its in-flight
	// count was last 0: neither write may record its value.
	overlapped map[string]bool
}

// present reports whether serial is still in the game (a game-wide key always is).
func present(c gamecontext.GameContext, serial string) bool {
	return serial == "" || c.Players[serial] != nil
}

// check reports whether d repeats the value recorded for its key at now, and whether
// that is the first suppression against the entry. It drops every entry whose serial
// left the game and the key's entry when it has expired. It reserves nothing.
func (al *appliedLedger) check(c gamecontext.GameContext, d Decision, now time.Time) (match, first bool) {
	al.mu.Lock()
	defer al.mu.Unlock()
	for k, e := range al.entries {
		if !present(c, e.serial) {
			delete(al.entries, k)
		}
	}
	key := appliedKey(d)
	e, ok := al.entries[key]
	if !ok {
		return false, false
	}
	if !now.Before(e.at.Add(appliedLedgerTTL)) {
		delete(al.entries, key)
		return false, false
	}
	if e.value != normalisedValue(d.Value) {
		return false, false
	}
	first = !e.reported
	e.reported = true
	al.entries[key] = e
	return true, first
}

// begin marks a write of d in flight and returns its handle. A second write to the
// same key while one is in flight deletes the entry and taints the key until both end.
func (al *appliedLedger) begin(d Decision) *appliedFlight {
	key := appliedKey(d)
	al.mu.Lock()
	defer al.mu.Unlock()
	if al.inflight == nil {
		al.inflight = make(map[string]int)
		al.overlapped = make(map[string]bool)
	}
	al.inflight[key]++
	if al.inflight[key] > 1 {
		al.overlapped[key] = true
		delete(al.entries, key)
	}
	return &appliedFlight{ledger: al, key: key, serial: d.TargetSerial, value: normalisedValue(d.Value)}
}

// end closes one in-flight write: an applied write records its value (unless the key
// overlapped), a failed write deletes the entry, a gated write (nothing written,
// UC-040 A11) leaves it as it was.
func (al *appliedLedger) end(f *appliedFlight, wrote bool, err error, now time.Time) {
	al.mu.Lock()
	defer al.mu.Unlock()
	overlapped := al.overlapped[f.key]
	if al.inflight[f.key]--; al.inflight[f.key] <= 0 {
		delete(al.inflight, f.key)
		delete(al.overlapped, f.key)
	}
	switch {
	case err != nil || overlapped:
		delete(al.entries, f.key)
	case wrote:
		if al.entries == nil {
			al.entries = make(map[string]appliedEntry)
		}
		al.entries[f.key] = appliedEntry{serial: f.serial, value: f.value, at: now}
	}
}

// values returns the live (unexpired) entries at now as key -> normalised value, nil
// when there are none. It takes only the ledger's own mutex.
func (al *appliedLedger) values(now time.Time) map[string]string {
	al.mu.Lock()
	defer al.mu.Unlock()
	var out map[string]string
	for k, e := range al.entries {
		if !now.Before(e.at.Add(appliedLedgerTTL)) {
			continue
		}
		if out == nil {
			out = make(map[string]string, len(al.entries))
		}
		out[k] = e.value
	}
	return out
}

// appliedFlight is the handle of one tracked write. A nil handle (the write is not
// tracked: suppression off or not an idempotent intervention) does nothing.
type appliedFlight struct {
	ledger *appliedLedger
	key    string
	serial string
	value  string
}

func (f *appliedFlight) end(wrote bool, err error, now time.Time) {
	if f == nil {
		return
	}
	f.ledger.end(f, wrote, err, now)
}

// SetSuppressReapplied switches the already-applied check on (UC-040 A35, BR-023).
// Off (the construction default, the rules golden harness and every older test)
// keeps the loop writing every decision as before. Set during construction.
func (l *Loop) SetSuppressReapplied(on bool) { l.suppressReapplied = on }

// SetAlreadyAppliedRecorder installs the per-game timeline seam for the first
// suppression of a value (A35 step 3): main.go wires a closure over the Multiplexer
// and this loop's game id, so the narrative says "already in place", not "blocked".
// nil records nothing. Set during construction.
func (l *Loop) SetAlreadyAppliedRecorder(rec func(intervention, serial string)) {
	l.alreadyAppliedRecorder = rec
}

func (l *Loop) recordAlreadyApplied(intervention, serial string) {
	if l.alreadyAppliedRecorder != nil {
		l.alreadyAppliedRecorder(intervention, serial)
	}
}

// tracksApplied reports whether the already-applied ledger governs d.
func (l *Loop) tracksApplied(d Decision) bool {
	return l.suppressReapplied && idempotentInterventions[d.Intervention]
}

// alreadyApplied is the check-only step of the permission chain (decide-contract
// §4.5 step 3): whether d repeats the value this loop last wrote for its key, and
// whether this is the first suppression against that entry.
func (l *Loop) alreadyApplied(c gamecontext.GameContext, d Decision, now time.Time) (match, first bool) {
	if !l.tracksApplied(d) {
		return false, false
	}
	return l.applied.check(c, d, now)
}

// beginApplied marks d's write in flight when the ledger tracks it (nil otherwise).
func (l *Loop) beginApplied(d Decision) *appliedFlight {
	if !l.tracksApplied(d) {
		return nil
	}
	return l.applied.begin(d)
}

// AppliedValues is the rules engine's view of the ledger, taken once per ranking
// (A35 step 4): key (intervention|serial) -> normalised value of every live entry.
// nil while suppression is off.
func (l *Loop) AppliedValues() map[string]string {
	if !l.suppressReapplied {
		return nil
	}
	return l.applied.values(l.nowOrDefault()())
}
