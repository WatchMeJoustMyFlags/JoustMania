package patterngame

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func params(overrides map[string]any) map[string]any {
	p := map[string]any{
		"players": 4.0, "stream_rate_hz": 30.0, "base": 1.0, "drift": 0.01, "amp": 0.1,
		"period_s": 3.0, "phase": 0.0, "burst_h": 0.5, "burst_period_s": 7.0,
		"d_base": 0.0, "d_drift": 0.01, "d_amp": 0.0, "d_phase": 0.25,
	}
	for k, v := range overrides {
		if v == nil {
			delete(p, k)
			continue
		}
		p[k] = v
	}
	return p
}

// fakeReader is a FlagReader over an in-memory variant table.
type fakeReader struct {
	selection string
	variants  map[string]any
	cap       int
	calls     []string
}

func (f *fakeReader) MockArchetypes(context.Context) string { return f.selection }
func (f *fakeReader) MockArchetype(_ context.Context, name string) (any, error) {
	f.calls = append(f.calls, name)
	v, ok := f.variants[name]
	if !ok {
		return map[string]any{}, errors.New("error processing the flag for evaluation")
	}
	return v, nil
}
func (f *fakeReader) MaxGameSeconds(context.Context) int { return f.cap }

func fiveArchetypes() map[string]any {
	v := map[string]any{"off": map[string]any{}}
	for i, n := range []string{"a1", "a2", "a3", "a4", "a5"} {
		v[n] = params(map[string]any{"base": 1.0 + float64(i)*0.01})
	}
	return v
}

// UC: UC-046 main A1 A2 A4 A5 A13 BR-003 BR-006 BR-013
func TestResolve(t *testing.T) {
	ctx := context.Background()
	t.Run("UC-046/main pins archetypes in selection order with digests and the cap", func(t *testing.T) {
		r := &fakeReader{selection: " a3, a1,a2 ,a4,a5", variants: fiveArchetypes(), cap: 600}
		pin := Resolve(ctx, r, SupportedMode, 5, "")
		if !pin.Active() {
			t.Fatalf("pin inactive: %s %v", pin.DiscardReason, pin.Errors)
		}
		if got := strings.Join(pin.Names(), ","); got != "a3,a1,a2,a4,a5" {
			t.Errorf("names = %s", got)
		}
		if pin.MaxGameSeconds != 600 {
			t.Errorf("cap = %d, want 600", pin.MaxGameSeconds)
		}
		for _, d := range pin.Digests() {
			if len(d) != 16 {
				t.Errorf("digest %q is not 16 hex digits", d)
			}
		}
	})
	t.Run("UC-046/A1 none or empty selection means noise", func(t *testing.T) {
		for _, sel := range []string{"", "none", "NONE", " , "} {
			pin := Resolve(ctx, &fakeReader{selection: sel}, SupportedMode, 5, "")
			if pin.Active() || pin.DiscardReason != "" {
				t.Errorf("selection %q: active=%v discard=%q", sel, pin.Active(), pin.DiscardReason)
			}
		}
	})
	t.Run("UC-046/A2 unknown or invalid archetype discards the experiment", func(t *testing.T) {
		v := fiveArchetypes()
		v["a5"] = params(map[string]any{"period_s": 0.0})
		pin := Resolve(ctx, &fakeReader{selection: "a1,a2,a3,a4,a5", variants: v, cap: 300}, SupportedMode, 5, "")
		if pin.DiscardReason != DiscardArchetypeInvalid || pin.Active() {
			t.Fatalf("discard = %q active=%v", pin.DiscardReason, pin.Active())
		}
		pin = Resolve(ctx, &fakeReader{selection: "a1,a2,a3,a4,ghost", variants: fiveArchetypes(), cap: 300}, SupportedMode, 5, "")
		if pin.DiscardReason != DiscardArchetypeInvalid || !strings.Contains(strings.Join(pin.Errors, ";"), "ghost") {
			t.Fatalf("unknown archetype: %q %v", pin.DiscardReason, pin.Errors)
		}
		pin = Resolve(ctx, &fakeReader{selection: "off,a1,a2,a3,a4", variants: fiveArchetypes(), cap: 300}, SupportedMode, 5, "")
		if pin.DiscardReason != DiscardArchetypeInvalid {
			t.Fatalf("the off variant must not be runnable: %q", pin.DiscardReason)
		}
	})
	t.Run("UC-046/A4 too few or duplicate archetypes", func(t *testing.T) {
		pin := Resolve(ctx, &fakeReader{selection: "a1,a2", variants: fiveArchetypes(), cap: 300}, SupportedMode, 5, "")
		if pin.DiscardReason != DiscardSelectionSmall {
			t.Fatalf("too small: %q", pin.DiscardReason)
		}
		pin = Resolve(ctx, &fakeReader{selection: "a1,a2,a3,a4,a1", variants: fiveArchetypes(), cap: 300}, SupportedMode, 5, "")
		if pin.DiscardReason != DiscardSelectionSmall {
			t.Fatalf("duplicate: %q", pin.DiscardReason)
		}
	})
	t.Run("UC-046/A13 a mode other than JoustFFA is unsupported", func(t *testing.T) {
		pin := Resolve(ctx, &fakeReader{selection: "a1,a2,a3,a4,a5", variants: fiveArchetypes(), cap: 300}, "JoustTeams", 5, "")
		if pin.DiscardReason != DiscardModeUnsupported {
			t.Fatalf("discard = %q", pin.DiscardReason)
		}
	})
	t.Run("UC-046/A5 override replaces the flag and a pin is a value copy", func(t *testing.T) {
		r := &fakeReader{selection: "a1,a2,a3,a4,a5", variants: fiveArchetypes(), cap: 300}
		pin := Resolve(ctx, r, SupportedMode, 0, "a2")
		if got := strings.Join(pin.Names(), ","); got != "a2" {
			t.Fatalf("override names = %s", got)
		}
		r.variants["a2"] = params(map[string]any{"base": 2.0})
		if pin.Archetypes[0].Params["base"] != 1.01 {
			t.Errorf("a later flag change altered the pinned parameters")
		}
	})
	t.Run("UC-046/BR-013 cap outside 30..3600 falls back to 300", func(t *testing.T) {
		for in, want := range map[int]int{0: 300, 29: 300, 30: 30, 3600: 3600, 3601: 300, 600: 600} {
			if got := ClampCap(in); got != want {
				t.Errorf("ClampCap(%d) = %d, want %d", in, got, want)
			}
		}
	})
}

// UC: UC-046 A2 BR-003 BR-009
func TestNewArchetype(t *testing.T) {
	t.Run("UC-046/BR-009 digest is stable and depends only on the parameters", func(t *testing.T) {
		a, errs := NewArchetype("x", params(nil))
		b, _ := NewArchetype("y", params(nil))
		c, _ := NewArchetype("x", params(map[string]any{"drift": 0.02}))
		if len(errs) > 0 {
			t.Fatal(errs)
		}
		if a.Digest != b.Digest || a.Digest == c.Digest {
			t.Errorf("digests a=%s b=%s c=%s", a.Digest, b.Digest, c.Digest)
		}
		var round map[string]float64
		if err := json.Unmarshal([]byte(a.ParamsJSON), &round); err != nil || len(round) != 13 {
			t.Errorf("ParamsJSON %q not a 13-field object: %v", a.ParamsJSON, err)
		}
		if a.Players != 4 || a.StreamRateHz != 30 {
			t.Errorf("players/rate = %d/%d", a.Players, a.StreamRateHz)
		}
	})
	cases := map[string]map[string]any{
		"players low":           {"players": 1.0},
		"players high":          {"players": 9.0},
		"players fractional":    {"players": 3.5},
		"rate":                  {"stream_rate_hz": 25.0},
		"period zero":           {"period_s": 0.0},
		"burst period zero":     {"burst_h": 0.5, "burst_period_s": 0.0},
		"phase one":             {"phase": 1.0},
		"drift high":            {"drift": 1.5},
		"slot base above 10 g":  {"base": 9.8, "d_base": 0.2},
		"missing field":         {"d_amp": nil},
		"unknown field":         {"extra": 1.0},
		"not a number":          {"amp": "0.1"},
		"boolean is not number": {"amp": true},
	}
	for name, overrides := range cases {
		t.Run("UC-046/A2 rejects "+name, func(t *testing.T) {
			if _, errs := NewArchetype("x", params(overrides)); len(errs) == 0 {
				t.Errorf("accepted %v", overrides)
			}
		})
	}
	t.Run("UC-046/A2 rejects the empty off variant and a bad name", func(t *testing.T) {
		if _, errs := NewArchetype("off", map[string]any{}); len(errs) == 0 {
			t.Error("accepted {}")
		}
		if _, errs := NewArchetype("Bad-Name", params(nil)); len(errs) == 0 {
			t.Error("accepted a bad name")
		}
	})
}

// UC: UC-046 main BR-003
func TestShippedArchetypeVariants(t *testing.T) {
	t.Run("UC-046/main every shipped variant validates in Go and both flag files agree", func(t *testing.T) {
		var flagsets [2]map[string]any
		for i, p := range []string{"../../flagd/agent.json", "../../flagd/ci/agent.json"} {
			data, err := os.ReadFile(filepath.Clean(p))
			if err != nil {
				t.Fatal(err)
			}
			var doc struct {
				Flags map[string]map[string]any `json:"flags"`
			}
			if err := json.Unmarshal(data, &doc); err != nil {
				t.Fatal(err)
			}
			flagsets[i] = doc.Flags[ArchetypeFlagKey]["variants"].(map[string]any)
		}
		if !reflect.DeepEqual(flagsets[0], flagsets[1]) {
			t.Fatal("agent.json and ci/agent.json archetypes differ")
		}
		n := 0
		for name, v := range flagsets[0] {
			if name == "off" {
				continue
			}
			n++
			if _, errs := NewArchetype(name, v); len(errs) > 0 {
				t.Errorf("%s: %v", name, errs)
			}
		}
		if n < 5 {
			t.Errorf("only %d archetypes; verdict_min_pairs needs 5", n)
		}
	})
}

// UC: UC-046 BR-006 BR-007 BR-011
func TestSeedsAndDigest(t *testing.T) {
	t.Run("UC-046/BR-007 chaos seed is a pure function of rng_seed", func(t *testing.T) {
		if ChaosSeed(7) != ChaosSeed(7) || ChaosSeed(7) == ChaosSeed(8) {
			t.Error("chaos seed not deterministic per rng_seed")
		}
	})
	t.Run("UC-046/BR-006 one-shot default seed is FNV of the archetype name", func(t *testing.T) {
		if ArchetypeSeed("slow_cascade") == 0 || ArchetypeSeed("a") == ArchetypeSeed("b") {
			t.Error("bad archetype seed")
		}
	})
	t.Run("UC-046/BR-011 flag digest covers both files in order", func(t *testing.T) {
		dir := t.TempDir()
		a, g := filepath.Join(dir, "agent.json"), filepath.Join(dir, "game.json")
		_ = os.WriteFile(a, []byte("A"), 0o600)
		_ = os.WriteFile(g, []byte("G"), 0o600)
		d1 := FlagDigest(a, g)
		if len(d1) != 64 || d1 == FlagDigest(g, a) {
			t.Fatalf("digest %q not order-sensitive sha256", d1)
		}
		_ = os.WriteFile(g, []byte("G2"), 0o600)
		if FlagDigest(a, g) == d1 {
			t.Error("digest unchanged after a flag edit")
		}
		if FlagDigest(a, filepath.Join(dir, "missing")) == FlagDigest(a) {
			t.Error("a missing file must change the digest")
		}
	})
}

// UC: UC-046 A7 A8 A10 A12 BR-008 BR-013 BR-014
func TestTracker(t *testing.T) {
	arch, _ := NewArchetype("x", params(nil))
	info := func(id string) Info {
		return Info{GameID: id, Archetype: arch, Seed: 9, Serials: []string{"S0", "S1", "S2", "S3"}, PairIndex: -1}
	}
	slots := func(frames ...uint64) []SlotRecord {
		out := make([]SlotRecord, len(frames))
		for i, f := range frames {
			out[i] = SlotRecord{Slot: i, FramesDelivered: f}
		}
		return out
	}
	t.Run("UC-046/A7 remote backend and decisions with target slots", func(t *testing.T) {
		tr := NewTracker()
		tr.Register(info("g"))
		obs := LoopObserver{Tracker: tr, GameID: "g"}
		if !obs.Active() {
			t.Fatal("not active")
		}
		if seed, ok := obs.SeedSource()(); !ok || seed != ChaosSeed(9) {
			t.Errorf("seed = %d,%v", seed, ok)
		}
		if slot, ok := obs.RecordDecision("send_controller_effect", "S2", "rules"); !ok || slot != 2 {
			t.Errorf("slot = %d,%v", slot, ok)
		}
		if slot, _ := obs.RecordDecision("adjust_music_tempo", "", "rules"); slot != -1 {
			t.Errorf("non-player target slot = %d", slot)
		}
		obs.MarkNonExact(ReasonRemoteBackend)
		rec, _ := tr.Finish("g", OutcomeWinner, slots(400, 300, 200, 100))
		if fmt.Sprint(rec.DecisionSequence()) != "[send_controller_effect:2 adjust_music_tempo:-1]" {
			t.Errorf("decisions = %v", rec.DecisionSequence())
		}
		if fmt.Sprint(rec.NonExactReasons) != "[remote_backend]" || rec.Exact() {
			t.Errorf("reasons = %v", rec.NonExactReasons)
		}
		if LoopObserver(obs).Active() {
			t.Error("finished game still active")
		}
	})
	t.Run("UC-046/A8 a second live pattern game or a live other game is concurrent", func(t *testing.T) {
		tr := NewTracker()
		tr.Register(info("g1"))
		tr.Register(info("g2"))
		r1, _ := tr.Finish("g1", OutcomeWinner, nil)
		if fmt.Sprint(r1.NonExactReasons) != "[concurrent_game]" {
			t.Errorf("g1 reasons = %v", r1.NonExactReasons)
		}
		tr2 := NewTracker()
		tr2.SetConcurrencyProbe(func(string) bool { return true })
		tr2.Register(info("g3"))
		r3, _ := tr2.Finish("g3", OutcomeWinner, nil)
		if fmt.Sprint(r3.NonExactReasons) != "[concurrent_game]" {
			t.Errorf("probe reasons = %v", r3.NonExactReasons)
		}
	})
	t.Run("UC-046/A10 an overrun makes the game non-exact", func(t *testing.T) {
		tr := NewTracker()
		tr.Register(info("g"))
		s := slots(400, 300, 200, 100)
		s[1].FrameOverruns = 1
		rec, _ := tr.Finish("g", OutcomeWinner, s)
		if fmt.Sprint(rec.NonExactReasons) != "[frame_overrun]" {
			t.Errorf("reasons = %v", rec.NonExactReasons)
		}
	})
	t.Run("UC-046/A12 abort calls the runner hook and marks kill_switch", func(t *testing.T) {
		tr := NewTracker()
		called := 0
		in := info("g")
		in.Abort = func() { called++ }
		tr.Register(in)
		tr.Abort("g", ReasonKillSwitch)
		tr.Abort("unknown", ReasonKillSwitch)
		rec, _ := tr.Finish("g", OutcomeAborted, nil)
		if called != 1 || fmt.Sprint(rec.NonExactReasons) != "[kill_switch]" {
			t.Errorf("called=%d reasons=%v", called, rec.NonExactReasons)
		}
	})
	t.Run("UC-046/BR-013 a timeout alone stays exact", func(t *testing.T) {
		tr := NewTracker()
		tr.Register(info("g"))
		rec, _ := tr.Finish("g", OutcomeTimeout, slots(9000, 120, 8995, 300))
		if !rec.Exact() {
			t.Errorf("timeout non-exact: %v", rec.NonExactReasons)
		}
		if fmt.Sprint(rec.EliminationOrder) != "[1 3]" || rec.WinnerSlot != -1 {
			t.Errorf("order/winner = %v/%d, want [1 3]/-1", rec.EliminationOrder, rec.WinnerSlot)
		}
	})
	t.Run("UC-046/BR-014 elimination order by delivered frames, ties by slot", func(t *testing.T) {
		order, winner := EliminationOrder(slots(500, 100, 100, 300), true, 30)
		if fmt.Sprint(order) != "[1 2 3]" || winner != 0 {
			t.Errorf("order/winner = %v/%d", order, winner)
		}
		order, winner = EliminationOrder(nil, true, 30)
		if order != nil || winner != -1 {
			t.Errorf("empty = %v/%d", order, winner)
		}
	})
	t.Run("UC-046/A1 selection parsing", func(t *testing.T) {
		if got := ParseSelection(" a, b,,c "); fmt.Sprint(got) != "[a b c]" {
			t.Errorf("parse = %v", got)
		}
		if HasDuplicate([]string{"a", "b", "a"}) != "a" || HasDuplicate([]string{"a"}) != "" {
			t.Error("HasDuplicate")
		}
	})
}
