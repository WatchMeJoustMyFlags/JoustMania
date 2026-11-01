package flags

import (
	"context"
	"errors"
	"testing"
)

// UC: UC-040 A47 A48 BR-033
// The agent reads the group-shield gate like the coordinator's _group_grant_policy
// (#2467 PR 4): fail closed, max_per_game clamped to 0..3, max_targets to 2..6 with
// 0 or less meaning off.
func TestEvaluate_GroupGrantPolicy(t *testing.T) {
	for _, tc := range []struct {
		name                string
		bools               map[string]bool
		ints                map[string]int64
		errs                map[string]error
		wantOn, wantReal    bool
		wantPerGame, wantMx int
	}{
		{"UC-040/BR-033 missing flags read closed", nil, nil, nil, false, false, 0, 0},
		{"UC-040/BR-033 the flag values", map[string]bool{keyGroupGrantEnabled: true, keyGroupGrantRealGames: true},
			map[string]int64{keyGroupGrantMaxPerGame: 2, keyGroupGrantMaxTargets: 4}, nil, true, true, 2, 4},
		{"UC-040/BR-033 clamped to the registry ceilings", nil,
			map[string]int64{keyGroupGrantMaxPerGame: 9, keyGroupGrantMaxTargets: 9}, nil, false, false, 3, 6},
		{"UC-040/BR-033 max_targets 1 reads 2, negative values read off", nil,
			map[string]int64{keyGroupGrantMaxPerGame: -1, keyGroupGrantMaxTargets: 1}, nil, false, false, 0, 2},
		{"UC-040/BR-033 read errors read closed", nil, nil, map[string]error{
			keyGroupGrantEnabled: errors.New("boom"), keyGroupGrantMaxPerGame: errors.New("boom"), keyGroupGrantMaxTargets: errors.New("boom"),
		}, false, false, 0, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := New(stubEvaluator{booleans: tc.bools, ints: tc.ints, errs: tc.errs}, nil).Evaluate(context.Background()).Policy
			if p.GroupGrantEnabled != tc.wantOn || p.GroupGrantRealGames != tc.wantReal ||
				p.GroupGrantMaxPerGame != tc.wantPerGame || p.GroupGrantMaxTargets != tc.wantMx {
				t.Fatalf("policy = %v/%v/%d/%d, want %v/%v/%d/%d", p.GroupGrantEnabled, p.GroupGrantRealGames,
					p.GroupGrantMaxPerGame, p.GroupGrantMaxTargets, tc.wantOn, tc.wantReal, tc.wantPerGame, tc.wantMx)
			}
		})
	}
}
