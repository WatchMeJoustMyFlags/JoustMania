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
