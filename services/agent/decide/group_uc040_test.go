package decide

import (
	"slices"
	"strings"
	"testing"
)

// group_uc040_test.go covers the group options of ffa.target (#2467 PR 4; UC-040
// A47, BR-033; decide-contract §2): their encoding, their resolution, and that a
// request without a group lever is unchanged.

// UC: UC-040 A47 BR-033
func TestGroupTargetOptions(t *testing.T) {
	for _, tc := range []struct {
		name  string
		alive []string
		want  []string
	}{
		{"UC-040/A47 one alive player: none", []string{"A"}, nil},
		{"UC-040/A47 two alive players: all only", []string{"B", "A"}, []string{"group:all"}},
		{"UC-040/A47 three alive players: all plus one all_except each, sorted", []string{"C", "A", "B"},
			[]string{"group:all", "group:all_except:A", "group:all_except:B", "group:all_except:C"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := GroupTargetOptions(tc.alive); !slices.Equal(got, tc.want) {
				t.Fatalf("options = %v, want %v", got, tc.want)
			}
		})
	}
	t.Run("UC-040/A47 resolution", func(t *testing.T) {
		alive := []string{"C", "A", "B"}
		if got, ok := ResolveGroupTarget("group:all", alive); !ok || !slices.Equal(got, []string{"A", "B", "C"}) {
			t.Fatalf("group:all = %v %v", got, ok)
		}
		if got, ok := ResolveGroupTarget("group:all_except:B", alive); !ok || !slices.Equal(got, []string{"A", "C"}) {
			t.Fatalf("all_except:B = %v %v", got, ok)
		}
		for _, bad := range []string{"group:all_except:Z", "group:some", "A", "none", "group:all_except:"} {
			if _, ok := ResolveGroupTarget(bad, alive); ok {
				t.Fatalf("%q must not resolve", bad)
			}
		}
		if _, ok := ResolveGroupTarget("group:all_except:A", []string{"A", "B"}); ok {
			t.Fatal("all_except with two alive would name one player: not an option")
		}
	})
}

// UC: UC-040 A47 BR-033
func TestFFAQuestions_GroupOptions(t *testing.T) {
	st := State{Players: []PlayerState{{Serial: "A", Alive: true}, {Serial: "B", Alive: true}, {Serial: "C", Alive: true}, {Serial: "D"}}}
	target := func(qs []Question) Question { return qs[1] }
	t.Run("UC-040/A47 without the group lever the target question is unchanged", func(t *testing.T) {
		got := target(FFAQuestions(st, []string{IntvGrantShield}))
		if !slices.Equal(got.Options, []string{"none", "A", "B", "C"}) || strings.Contains(got.Text, "group") {
			t.Fatalf("target = %+v, want the v1 question", got)
		}
	})
	t.Run("UC-040/A47 with the group lever the group options follow the serials", func(t *testing.T) {
		got := target(FFAQuestions(st, []string{IntvGrantShield, IntvGrantGroupShield}))
		want := []string{"none", "A", "B", "C", "group:all", "group:all_except:A", "group:all_except:B", "group:all_except:C"}
		if !slices.Equal(got.Options, want) {
			t.Fatalf("options = %v, want %v", got.Options, want)
		}
		if !strings.HasSuffix(got.Text, groupTargetText) {
			t.Fatalf("text = %q, want the group sentence", got.Text)
		}
	})
	t.Run("UC-040/A47 one alive player: no group option, text unchanged", func(t *testing.T) {
		one := State{Players: []PlayerState{{Serial: "A", Alive: true}}}
		got := target(FFAQuestions(one, []string{IntvGrantGroupShield}))
		if !slices.Equal(got.Options, []string{"none", "A"}) || strings.Contains(got.Text, "group") {
			t.Fatalf("target = %+v", got)
		}
	})
	t.Run("UC-040/A47 the lever is effect-only and never player-targeted", func(t *testing.T) {
		iv, ok := ByName(IntvGrantGroupShield)
		if !ok || !iv.GroupTargeted || iv.PlayerTargeted || iv.Weight != 1 || iv.Protective {
			t.Fatalf("catalog row = %+v", iv)
		}
		if _, bounded := MagnitudeBounds(IntvGrantGroupShield); bounded {
			t.Fatal("the group lever takes the BR-029 length, no magnitude")
		}
	})
	t.Run("UC-040/A45 a model prediction on a group target names no player", func(t *testing.T) {
		ask := PredictionQuestion(st)
		if _, reason := ValidatePrediction(*ask, Answer{Choice: "survives_15s", Confidence: 0.6}, IntvGrantGroupShield, TargetGroupAll); reason != PredictionDropNoTarget {
			t.Fatalf("reason = %q, want %q", reason, PredictionDropNoTarget)
		}
	})
}
