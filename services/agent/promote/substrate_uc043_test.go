package promote

// substrate_uc043_test.go: the shadow substrate validity bar on autonomous
// promotion (UC-043 A23, FR-152 g, FR-203, #2541). An autonomous real-default
// write is made only for an objective the operator attested for the running
// release; every other objective is a recorded no-op. Issue and PR modes, the
// reverts and the kill switch are never gated.

import (
	"context"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/joustmania/agent/experiment"
)

func substrateEvidence(objective string) Evidence {
	ev := testEvidence()
	ev.Objective = objective
	return ev
}

// barPromoter builds a Promoter with a watcher and a real-default writer, and the
// given bar (nil: SetSubstrateBar is never called).
func barPromoter(t *testing.T, bar map[string]bool, w Watcher) (*Promoter, *recordingRealDefault, *recordingReverter) {
	t.Helper()
	realDef, rev := &recordingRealDefault{}, &recordingReverter{}
	if w == nil {
		w = fakeWatcher{}
	}
	p := NewPromoter(&recordingGitHub{}, &recordingGit{}, realDef, w, rev, testRepo(), nil, discardLogger())
	if bar != nil {
		p.SetSubstrateBar(bar)
	}
	return p, realDef, rev
}

func wantSubstrateRefusal(t *testing.T, outcome Outcome, reason, objective string) {
	t.Helper()
	if outcome != OutcomeDiscarded {
		t.Fatalf("outcome = %q (%s), want discarded", outcome, reason)
	}
	if want := ReasonSubstrateNotValidated + " objective=" + objective; !strings.HasPrefix(reason, want) {
		t.Fatalf("reason = %q, want prefix %q", reason, want)
	}
}

// UC: UC-043 A23
func TestUC043_A23_SubstrateBar(t *testing.T) {
	t.Run("UC-043/A23 T1 no bar set refuses the autonomous write (fail closed)", func(t *testing.T) {
		p, realDef, _ := barPromoter(t, nil, nil)
		res := p.Promote(context.Background(), promoteProposal, promoteVerdict, autoOn, substrateEvidence("balanced"))
		wantSubstrateRefusal(t, res.Outcome, res.Reason, "balanced")
		if len(realDef.calls) != 0 {
			t.Fatalf("SetRealDefault calls = %d, want 0", len(realDef.calls))
		}
	})
	t.Run("UC-043/A23 T2 only an attested objective is written", func(t *testing.T) {
		p, realDef, _ := barPromoter(t, map[string]bool{"balanced": true}, nil)
		res := p.Promote(context.Background(), promoteProposal, promoteVerdict, autoOn, substrateEvidence("endurance"))
		wantSubstrateRefusal(t, res.Outcome, res.Reason, "endurance")
		if len(realDef.calls) != 0 {
			t.Fatalf("SetRealDefault calls = %d for endurance, want 0", len(realDef.calls))
		}
		res = p.Promote(context.Background(), promoteProposal, promoteVerdict, autoOn, substrateEvidence("balanced"))
		if res.Outcome != OutcomeApplied || len(realDef.calls) != 1 {
			t.Fatalf("balanced: outcome = %q (%s) calls %d, want applied once", res.Outcome, res.Reason, len(realDef.calls))
		}
	})
	t.Run("UC-043/A23 an empty objective is never validated", func(t *testing.T) {
		p, realDef, _ := barPromoter(t, map[string]bool{"balanced": true, "": true}, nil)
		res := p.Promote(context.Background(), promoteProposal, promoteVerdict, autoOn, substrateEvidence(""))
		wantSubstrateRefusal(t, res.Outcome, res.Reason, "")
		if len(realDef.calls) != 0 {
			t.Fatal("an empty objective wrote the real default")
		}
	})
	t.Run("UC-043/A23 T3 a below-bar hold key is discarded, never queued", func(t *testing.T) {
		rig := newDeferredRig(t, nil) // the rig's bar holds balanced only
		ev := holdEvidence()
		ev.Objective = "endurance"
		res := rig.p.Promote(context.Background(), holdProposal("exp-t3"), promoteVerdict, autoOn, ev)
		wantSubstrateRefusal(t, res.Outcome, res.Reason, "endurance")
		if got := rig.p.PendingFlags(); len(got) != 0 {
			t.Fatalf("pending = %v, want none", got)
		}
	})
	t.Run("UC-043/A23 T4 writeTracked refuses a queued write below the bar (backstop)", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		ev := holdEvidence()
		ev.Objective = "endurance"
		rig.p.mu.Lock()
		rig.p.pending = map[string]*deferredWrite{holdKey: {proposal: holdProposal("exp-t4"), evidence: ev, queuedAt: rig.p.clock()}}
		rig.p.mu.Unlock()
		rig.probe.set(false)
		rig.p.DrainDeferred(context.Background(), fixed(autoOn))
		if len(rig.realDef.calls) != 0 {
			t.Fatalf("SetRealDefault calls = %d, want 0", len(rig.realDef.calls))
		}
		got := rig.journal.all()
		if len(got) != 1 || !strings.HasPrefix(got[0], "exp-t4|discarded|"+ReasonSubstrateNotValidated+" objective=endurance") {
			t.Fatalf("journal = %v, want one discarded substrate_not_validated", got)
		}
	})
	t.Run("UC-043/A23 the bar is set once", func(t *testing.T) {
		p, _, _ := barPromoter(t, map[string]bool{"balanced": true}, nil)
		p.SetSubstrateBar(map[string]bool{"endurance": true})
		if !p.SubstrateValidated("balanced") || p.SubstrateValidated("endurance") {
			t.Fatal("a second SetSubstrateBar changed the bar")
		}
	})
}

// UC: UC-043 A23
func TestUC043_A23_SeamForwardsTheObjective(t *testing.T) {
	for _, tc := range []struct {
		objective string
		applied   bool
	}{
		{"endurance", true},
		{"balanced", false},
		{"", false},
	} {
		t.Run("UC-043/A23 T5 intent objective "+tc.objective+" through ExperimentPromoter", func(t *testing.T) {
			p, realDef, _ := barPromoter(t, map[string]bool{"endurance": true}, nil)
			a := NewExperimentPromoter(p, fixedConfig(autoOn))
			view := promoteView()
			view.Intent.Objective = tc.objective
			res := a.Promote(context.Background(), view)
			if tc.applied {
				if res.Outcome != string(OutcomeApplied) || len(realDef.calls) != 1 {
					t.Fatalf("outcome = %q (%s) calls %d, want applied once", res.Outcome, res.Reason, len(realDef.calls))
				}
				return
			}
			wantSubstrateRefusal(t, Outcome(res.Outcome), res.Reason, tc.objective)
			if len(realDef.calls) != 0 {
				t.Fatalf("SetRealDefault calls = %d, want 0", len(realDef.calls))
			}
		})
	}
}

// UC: UC-043 A23
// UC: UC-043 BR-003
func TestUC043_A23_IssueAndPRModesUnaffected(t *testing.T) {
	t.Run("UC-043/A23 G1 issue mode opens the issue under a closed bar", func(t *testing.T) {
		gh := &recordingGitHub{issueURL: "https://example.invalid/issues/1"}
		p := NewPromoter(gh, &recordingGit{}, &recordingRealDefault{}, fakeWatcher{}, nil, testRepo(), nil, discardLogger())
		res := p.Promote(context.Background(), promoteProposal, promoteVerdict, Config{Mode: ModeIssue, Target: TargetLocal, Enabled: true}, substrateEvidence("endurance"))
		if res.Outcome != OutcomeApplied || len(gh.issues) != 1 {
			t.Fatalf("outcome = %q (%s) issues %d, want applied once", res.Outcome, res.Reason, len(gh.issues))
		}
	})
	t.Run("UC-043/A23 G1 pr mode opens the PR under a closed bar", func(t *testing.T) {
		gh := &recordingGitHub{prURL: "https://example.invalid/pull/1"}
		p := NewPromoter(gh, &recordingGit{}, &recordingRealDefault{}, fakeWatcher{}, nil, testRepo(), nil, discardLogger())
		res := p.Promote(context.Background(), promoteProposal, promoteVerdict, Config{Mode: ModePR, Target: TargetGitHub, Enabled: true}, substrateEvidence("endurance"))
		if res.Outcome != OutcomeApplied || len(gh.prs) != 1 {
			t.Fatalf("outcome = %q (%s) prs %d, want applied once", res.Outcome, res.Reason, len(gh.prs))
		}
	})
}

// UC: UC-043 A23
// UC: UC-043 A1
func TestUC043_A23_RevertsNeverGated(t *testing.T) {
	t.Run("UC-043/A23 G2 a watch revert runs under a closed bar", func(t *testing.T) {
		p, _, rev := barPromoter(t, nil, nil)
		entry := &appliedPromotion{proposal: promoteProposal}
		p.mu.Lock()
		p.applied[promoteProposal.FlagKey] = entry
		p.mu.Unlock()
		res := Result{Outcome: OutcomeApplied}
		p.revertReal(context.Background(), promoteProposal, entry, &res, nil, false)
		if res.Outcome != OutcomeReverted || rev.calls != 1 {
			t.Fatalf("outcome = %q (%s) reverts %d, want reverted once", res.Outcome, res.Reason, rev.calls)
		}
	})
	t.Run("UC-043/A23 G2 the kill switch reverts under a closed bar", func(t *testing.T) {
		rev := &listingReverter{flags: []string{promoteProposal.FlagKey}}
		p := NewPromoter(nil, nil, &recordingRealDefault{}, fakeWatcher{}, rev, testRepo(), nil, discardLogger())
		got := p.RevertOnKillSwitch(context.Background())
		if len(got) != 1 || got[0].Outcome != OutcomeReverted || rev.calls != 1 {
			t.Fatalf("kill switch = %+v reverts %d, want one reverted", got, rev.calls)
		}
	})
	t.Run("UC-043/A23 G2 a deferred revert runs under a closed bar", func(t *testing.T) {
		probe := &fakeProbe{live: false}
		journal := &journalRecorder{}
		p, _, rev := barPromoter(t, nil, nil)
		p.SetRealGameProbe(probe.probe)
		p.SetRewatchJournal(journal.sink)
		p.RestoreDeferredReverts(context.Background(), []experiment.RestoredRevert{{ExperimentID: "exp-g2", FlagKey: holdKey, Value: 20.0}})
		p.DrainDeferred(context.Background(), fixed(autoOn))
		if rev.calls != 1 {
			t.Fatalf("revert calls = %d, want 1 (journal %v)", rev.calls, journal.all())
		}
	})
}

// UC: UC-043 A23
func TestUC043_A23_ParseValidatedObjectives(t *testing.T) {
	const release = "0.14.0"
	for _, tc := range []struct {
		name, env, version string
		want               []string
		warns              int
	}{
		{"unset", "", release, nil, 0},
		{"only separators", " , ,", release, nil, 0},
		{"real release tag", "balanced@0.14.0", release, []string{"balanced"}, 0},
		{"two entries", "balanced@0.14.0,endurance@0.14.0", release, []string{"balanced", "endurance"}, 0},
		{"trimmed and case-folded", " Balanced @ 0.14.0 ", release, []string{"balanced"}, 0},
		{"trailing comma adds nothing", "balanced@0.14.0,", release, []string{"balanced"}, 0},
		{"semicolon is not a separator", "balanced@0.14.0;endurance@0.14.0", release, nil, 1},
		{"v-prefixed entry", "balanced@v0.14.0", release, nil, 1},
		{"older release", "balanced@0.13.0", release, nil, 1},
		{"no release", "balanced", release, nil, 1},
		{"empty objective", "@0.14.0", release, nil, 1},
		{"chaos has no fitness", "chaos@0.14.0", release, nil, 1},
		{"typo", "endurence@0.14.0", release, nil, 1},
		{"latest even when it matches", "balanced@latest", "latest", nil, 1},
		{"main even when it matches", "balanced@main", "main", nil, 1},
		{"pr tag even when it matches", "balanced@pr-123", "pr-123", nil, 1},
		{"dev even when it matches", "balanced@0.0.0-dev", "0.0.0-dev", nil, 1},
		{"v-prefixed version", "balanced@v0.14.0", "v0.14.0", nil, 1},
		{"commit sha", "balanced@1c39ef1b6a1c39ef1b6a1c39ef1b6a1c39ef1b6a", "1c39ef1b6a1c39ef1b6a1c39ef1b6a1c39ef1b6a", nil, 1},
		{"empty version", "balanced@0.14.0", "", nil, 1},
	} {
		t.Run("UC-043/A23 T6 "+tc.name, func(t *testing.T) {
			got, warns := ParseValidatedObjectives(tc.env, tc.version)
			if list := ValidatedObjectiveList(got); !reflect.DeepEqual(list, append([]string{}, tc.want...)) {
				t.Errorf("validated = %v, want %v", list, tc.want)
			}
			if got[""] {
				t.Error("the empty objective was validated")
			}
			if len(warns) != tc.warns {
				t.Errorf("warnings = %q, want %d", warns, tc.warns)
			}
		})
	}
}

// TestUC043_A23_SingleRealDefaultWriteSite pins the claim the backstop rests on:
// every SetRealDefault call in the agent's production code is inside writeTracked,
// and writeTracked is called only by promoteAutonomous and drainWrites.
//
// UC: UC-043 A23
func TestUC043_A23_SingleRealDefaultWriteSite(t *testing.T) {
	calls := map[string][]string{} // selector -> enclosing "file:func"
	root := ".."                   // services/agent
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		f, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
		if err != nil {
			return err
		}
		for _, decl := range f.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				continue
			}
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				sel, ok := call.Fun.(*ast.SelectorExpr)
				if ok && (sel.Sel.Name == "SetRealDefault" || sel.Sel.Name == "writeTracked") {
					calls[sel.Sel.Name] = append(calls[sel.Sel.Name], filepath.Base(path)+":"+fn.Name.Name)
				}
				return true
			})
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Run("UC-043/A23 SetRealDefault is called only in writeTracked", func(t *testing.T) {
		if want := []string{"killswitch.go:writeTracked"}; !reflect.DeepEqual(calls["SetRealDefault"], want) {
			t.Fatalf("SetRealDefault call sites = %v, want %v", calls["SetRealDefault"], want)
		}
	})
	t.Run("UC-043/A23 writeTracked is called only by promoteAutonomous and drainWrites", func(t *testing.T) {
		got := append([]string(nil), calls["writeTracked"]...)
		want := []string{"actions.go:promoteAutonomous", "deferred.go:drainWrites"}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("writeTracked call sites = %v, want %v", got, want)
		}
	})
}
