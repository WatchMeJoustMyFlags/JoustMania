package jev

import "github.com/joustmania/agent/decide"

// staleReadingsPhrase describes a player marked readings_stale (UC-040 A50,
// BR-039, #2514): the state only, in both prompt profiles.
const staleReadingsPhrase = " Its movement readings are stale, so intensity, variance and skill are not known."

// stalePhrase is p's stale-readings sentence, empty when its readings are fresh.
func stalePhrase(p decide.PlayerState) string {
	if p.ReadingsStale {
		return staleReadingsPhrase
	}
	return ""
}

// freshOptions are the serials of options whose player in st is not marked
// readings_stale, in option order: the target candidates of a lever that needs
// fresh readings (decide.NeedsFreshReadings, UC-040 BR-039).
func freshOptions(options []string, st decide.State) []string {
	stale := map[string]bool{}
	for _, p := range st.Players {
		if p.ReadingsStale {
			stale[p.Serial] = true
		}
	}
	var out []string
	for _, o := range options {
		if !stale[o] {
			out = append(out, o)
		}
	}
	return out
}

// withoutUntargetable is ffa.intervention q without the levers that need fresh
// readings while no alive player of st has fresh readings (the first second of a
// game, an agent restart, a stalled export; UC-040 A50, BR-039, #2514): such a
// lever has no target candidate, so offering it would only waste the round. q is
// returned as is when an alive player is fresh or none is alive, when it offers none of those
// levers, or when nothing but noop would be left (the details stage's
// no-candidate path covers that).
func withoutUntargetable(q decide.Question, st decide.State) decide.Question {
	alive := 0
	for _, p := range st.Players {
		if !p.Alive {
			continue
		}
		if !p.ReadingsStale {
			return q
		}
		alive++
	}
	if alive == 0 {
		return q
	}
	out := decide.Question{ID: q.ID, Kind: q.Kind, Text: q.Text, Range: q.Range}
	acting := false
	for _, o := range q.Options {
		if decide.NeedsFreshReadings(o) {
			continue
		}
		out.Options = append(out.Options, o)
		if o != decide.OptionNoop {
			acting = true
		}
	}
	if !acting || len(out.Options) == len(q.Options) {
		return q
	}
	return out
}
