package actions

import (
	"bytes"
	"context"
	"os"
	"testing"

	"github.com/joustmania/agent/decision"
)

// UC: UC-040 A18 BR-011
func TestEndGameAddressedToDecidingGame(t *testing.T) {
	t.Run("UC-040/A18 end_game payload is the deciding game's id", func(t *testing.T) {
		w, path := newTestWriter(t)
		if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionEndGame, GameID: "game_shadow00"}); err != nil {
			t.Fatalf("apply: %v", err)
		}
		nonce, game := splitNonce(activeValueOf(t, path, flagEndGame))
		if nonce == "" || game != "game_shadow00" {
			t.Fatalf("end_game value = %q:%q, want <nonce>:game_shadow00", nonce, game)
		}
	})

	t.Run("UC-040/A18 end_game without a game id is refused and writes nothing", func(t *testing.T) {
		w, path := newTestWriter(t)
		before, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionEndGame}); err == nil {
			t.Fatal("un-addressed end_game was written; it would end the real game whichever loop decided it")
		}
		after, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(before, after) {
			t.Fatal("refused end_game still modified the interventions file")
		}
	})
}

// UC: UC-040 A19 BR-012
func TestGrantVariantPerDecision(t *testing.T) {
	grants := []struct {
		name  string
		iv    string
		flag  string
		value string
	}{
		{"shield", decision.InterventionGrantShield, flagShieldSeconds, "5"},
		{"partial_shield", decision.InterventionPartialShield, flagPartialShieldSeconds, "5:2.0"},
		{"soft_penalty", decision.InterventionSoftPenalty, flagSoftPenaltyAction, "warn"},
	}
	for _, g := range grants {
		t.Run("UC-040/A19 "+g.name+" same-value re-grant is a fresh variant replacing the previous one", func(t *testing.T) {
			w, path := newTestWriter(t)
			apply := func(serial string) {
				t.Helper()
				if err := w.Apply(context.Background(), decision.Decision{Intervention: g.iv, TargetSerial: serial, Value: g.value}); err != nil {
					t.Fatalf("apply %s: %v", serial, err)
				}
			}
			apply("AA")
			first := grantName(t, path, g.flag, "AA")
			apply("AA")
			second := grantName(t, path, g.flag, "AA") // exactly one variant for AA
			if first == second {
				t.Fatalf("re-grant reused variant %q; the coordinator would not see a new decision", first)
			}
			assertTargetingResolves(t, path, g.flag, "AA", second)
			assertStructurallyValid(t, path)
		})

		t.Run("UC-040/A19 "+g.name+" granting BB leaves AA's grant unchanged", func(t *testing.T) {
			w, path := newTestWriter(t)
			if err := w.Apply(context.Background(), decision.Decision{Intervention: g.iv, TargetSerial: "AA", Value: g.value}); err != nil {
				t.Fatal(err)
			}
			aa := grantName(t, path, g.flag, "AA")
			if err := w.Apply(context.Background(), decision.Decision{Intervention: g.iv, TargetSerial: "BB", Value: g.value}); err != nil {
				t.Fatal(err)
			}
			if got := grantName(t, path, g.flag, "AA"); got != aa {
				t.Fatalf("AA's grant variant changed %q -> %q when BB was granted", aa, got)
			}
			assertTargetingResolves(t, path, g.flag, "AA", aa)
			assertTargetingResolves(t, path, g.flag, "BB", grantName(t, path, g.flag, "BB"))
			assertTargetingResolves(t, path, g.flag, "ZZ", neutralNone)
		})

		t.Run("UC-040/A19 "+g.name+" RevertTargeted removes the player's grant", func(t *testing.T) {
			w, path := newTestWriter(t)
			for _, s := range []string{"AA", "BB"} {
				if err := w.Apply(context.Background(), decision.Decision{Intervention: g.iv, TargetSerial: s, Value: g.value}); err != nil {
					t.Fatal(err)
				}
			}
			if err := w.RevertTargeted(g.flag, "AA"); err != nil {
				t.Fatalf("revert: %v", err)
			}
			assertTargetingResolves(t, path, g.flag, "AA", neutralNone)
			assertTargetingResolves(t, path, g.flag, "BB", grantName(t, path, g.flag, "BB"))
			if err := w.RevertTargeted(g.flag, "BB"); err != nil {
				t.Fatal(err)
			}
			if _, ok := readFlag(t, path, g.flag)["targeting"]; ok {
				t.Fatal("targeting should be removed once no players remain")
			}
			assertStructurallyValid(t, path)
		})
	}
}
