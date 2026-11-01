package goal

import (
	"reflect"
	"testing"

	"github.com/joustmania/agent/decide"
)

// UC: UC-042 A12 BR-009
// TestOffBand: which goal terms are outside their band now and can already be
// judged (#2498): jev's gate names only these, so a running game is not pushed
// toward acting for a duration minimum it cannot have reached yet.
func TestOffBand(t *testing.T) {
	long := FromObjectives(map[string]float64{"endurance": 1}, 180, 60)    // duration>=180, activity 0.30..0.60, pace<=1
	quick := FromObjectives(map[string]float64{"accelerate": 1}, 180, 600) // duration<=600, pace>=2
	values := func(m map[string]float64) func(char, phase string) (float64, bool) {
		return func(char, phase string) (float64, bool) {
			v, ok := m[Label(char, phase)]
			return v, ok
		}
	}
	for _, tc := range []struct {
		name    string
		profile Profile
		now     map[string]float64
		want    []string
	}{
		{"UC-042/A12 early game below its duration minimum: nothing to judge yet", long,
			map[string]float64{decide.CharDuration: 95, decide.CharActivity: 0.42, decide.CharEliminationPace: 0.6}, nil},
		{"UC-042/A12 activity above its band is off", long,
			map[string]float64{decide.CharDuration: 95, decide.CharActivity: 0.85, decide.CharEliminationPace: 0.6},
			[]string{decide.CharActivity}},
		{"UC-042/A12 activity below its band and pace above its maximum are off, in term order", long,
			map[string]float64{decide.CharDuration: 95, decide.CharActivity: 0.1, decide.CharEliminationPace: 2},
			[]string{decide.CharActivity, decide.CharEliminationPace}},
		{"UC-042/A12 an unmeasured term is not judged", long, map[string]float64{}, nil},
		{"UC-042/A12 a duration above its maximum is off", FromObjectives(map[string]float64{"accelerate": 1}, 180, 60),
			map[string]float64{decide.CharDuration: 75, decide.CharEliminationPace: 3}, []string{decide.CharDuration}},
		{"UC-042/A12 a pace minimum at 5 s with no elimination is not judged yet", quick,
			map[string]float64{decide.CharDuration: 5, decide.CharEliminationPace: 0}, nil},
		{"UC-042/A12 a pace minimum at 90 s with no elimination is off", quick,
			map[string]float64{decide.CharDuration: 90, decide.CharEliminationPace: 0}, []string{decide.CharEliminationPace}},
		{"UC-042/A12 a pace minimum after the first elimination is judged early", quick,
			map[string]float64{decide.CharDuration: 40, decide.CharEliminationPace: 1.5}, []string{decide.CharEliminationPace}},
		{"UC-042/A12 a pace maximum exceeded early is off", long,
			map[string]float64{decide.CharDuration: 10, decide.CharActivity: 0.42, decide.CharEliminationPace: 6},
			[]string{decide.CharEliminationPace}},
		{"UC-042/A12 an endgame term before the endgame (unmeasured) is not judged",
			Profile{Name: "x", Terms: []Term{{Characteristic: decide.CharActivity, Min: ptr(0.6), Phase: PhaseEndgame, Weight: 1}}},
			map[string]float64{decide.CharActivity: 0.1}, nil},
		{"UC-042/A12 an endgame term in the endgame is judged",
			Profile{Name: "x", Terms: []Term{{Characteristic: decide.CharActivity, Min: ptr(0.6), Phase: PhaseEndgame, Weight: 1}}},
			map[string]float64{"activity@endgame": 0.1}, []string{"activity@endgame"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := OffBand(tc.profile, values(tc.now)); !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("OffBand = %v, want %v", got, tc.want)
			}
		})
	}
}
