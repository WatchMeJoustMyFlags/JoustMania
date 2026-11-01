package decision

// UC: UC-040 A51

import (
	"strconv"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

// TestEnduranceTempoSpeedsUp pins R1's direction (#2487, UC-040 A51, BR-040): the
// coordinator's death threshold slides from SLOW_MAX to FAST_MAX with the tempo, so
// R1, which wants the session to last, writes the fast end of adjust_music_tempo's
// bounds (a higher threshold, more movement tolerated), never a slow or default value.
func TestEnduranceTempoSpeedsUp(t *testing.T) {
	players := map[string]testPlayer{"A": {active: true}, "B": {active: true}}
	bounds, ok := decide.MagnitudeBounds(InterventionAdjustMusicTempo)
	if !ok {
		t.Fatal("adjust_music_tempo has no magnitude bounds")
	}
	cands := enduranceCandidates(gameCtx(30, []string{"C"}, players), defaultFit())
	if len(cands) != 2 {
		t.Fatalf("candidates = %v, want R1 tempo + R2 cue", interventionsOf(cands))
	}

	t.Run("UC-040/A51 R1 writes the fast end of the tempo bounds", func(t *testing.T) {
		r1 := cands[0]
		if r1.rule != "R1" || r1.decision.Intervention != InterventionAdjustMusicTempo {
			t.Fatalf("first candidate = %s %s, want R1 adjust_music_tempo", r1.rule, r1.decision.Intervention)
		}
		v, err := strconv.ParseFloat(r1.decision.Value, 64)
		if err != nil {
			t.Fatalf("R1 Value = %q, want a number (an empty Value writes the 1.15 default)", r1.decision.Value)
		}
		if v != bounds.Max {
			t.Errorf("R1 tempo = %v, want the fast end %v (above the default %v)", v, bounds.Max, bounds.Default)
		}
		// The literal too: the `fast` variant of music_tempo_override and the
		// coordinator clamp FAST_MUSIC_SPEED, so a wider bounds table cannot drag R1
		// past what the game applies.
		if v != 1.3 {
			t.Errorf("R1 tempo = %v, want 1.3 (the fast variant, FAST_MUSIC_SPEED)", v)
		}
		if !strings.Contains(r1.decision.Reason, "speed up") || strings.Contains(r1.decision.Reason, "slow") {
			t.Errorf("R1 reason = %q, want it to say speed up", r1.decision.Reason)
		}
		if !r1.difficulty {
			t.Error("R1 must stay a difficulty candidate (variance cooldown, battery policy)")
		}
	})

	t.Run("UC-040/A51 R2 is unchanged", func(t *testing.T) {
		r2 := cands[1]
		if r2.rule != "R2" || r2.decision.Intervention != InterventionPlayAudioCue || r2.decision.Value != "" {
			t.Errorf("second candidate = %s %s %q, want R2 play_audio_cue with the default cue", r2.rule, r2.decision.Intervention, r2.decision.Value)
		}
	})
}
