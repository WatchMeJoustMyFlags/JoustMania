package flags

import (
	"context"
	"errors"
	"testing"
	"time"
)

// game_hold_uc040_test.go: the `hold` variant of shield.activation (#2096) follows the
// coordinator's per-game latch in the agent's charge ledger (UC-040 A31 step 1, BR-019,
// BR-027): a flip to `hold` never changes the mode of a running game.

var errUnreadable = errors.New("PROVIDER_NOT_READY")

// UC: UC-040 A31 BR-019 BR-027
func TestShieldCharge_HoldReadKeepsTheGamesFirstMode(t *testing.T) {
	for _, tc := range []struct {
		name  string
		reads []string // shield.activation per read, same game
		want  []bool   // trigger per read
	}{
		{"UC-040/A31 a hold read in a game first read as trigger stays trigger", []string{"trigger", "hold", "hold"}, []bool{true, true, true}},
		{"UC-040/A31 a hold read in a game first read as instant is not trigger", []string{"instant", "hold"}, []bool{false, false}},
		{"UC-040/A31 a game first read as hold is a hold game: the ledger never applies", []string{"hold", "trigger", "hold"}, []bool{false, false, false}},
		{"UC-040/A31 instant and trigger stay per read in a non-hold game", []string{"trigger", "instant", "trigger", "hold"}, []bool{true, false, true, true}},
		{"UC-040/A31 instant then trigger then hold follows the first read", []string{"instant", "trigger", "hold"}, []bool{false, true, false}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			stub := stubEvaluator{strings: map[string]string{}, floats: map[string]float64{}}
			f := gameFlags(stub, nil)
			for i, mode := range tc.reads {
				stub.strings["shield.activation"] = mode
				trigger, expiry := f.ShieldCharge(context.Background(), "g-hold")
				if trigger != tc.want[i] {
					t.Fatalf("read %d (%q): trigger = %v, want %v", i, mode, trigger, tc.want[i])
				}
				if expiry != DefaultShieldChargeExpiry {
					t.Fatalf("read %d (%q): expiry = %s, want the default", i, mode, expiry)
				}
			}
		})
	}
}

// UC: UC-040 A31 BR-027
func TestShieldCharge_HoldLatchIsPerGameAndBounded(t *testing.T) {
	stub := stubEvaluator{strings: map[string]string{"shield.activation": "trigger"}, floats: map[string]float64{}}
	f := gameFlags(stub, nil)
	now := time.Unix(1_800_000_000, 0)
	f.now = func() time.Time { return now }

	if trigger, _ := f.ShieldCharge(context.Background(), "g-a"); !trigger {
		t.Fatal("UC-040/A31 g-a first read trigger: want trigger")
	}
	stub.strings["shield.activation"] = "hold"
	if trigger, _ := f.ShieldCharge(context.Background(), "g-b"); trigger {
		t.Fatal("UC-040/A31 g-b first read hold: another game's latch must not leak")
	}
	if trigger, _ := f.ShieldCharge(context.Background(), "g-a"); !trigger {
		t.Fatal("UC-040/A31 g-a hold read: want trigger (its first read)")
	}
	// After the 1 h bound the game's first read is forgotten: a hold read is a hold game.
	now = now.Add(gameModeLatchTTL + time.Second)
	if trigger, _ := f.ShieldCharge(context.Background(), "g-c"); trigger {
		t.Fatal("UC-040/A31 g-c first read hold: want no trigger")
	}
	if trigger, _ := f.ShieldCharge(context.Background(), "g-a"); trigger {
		t.Fatal("UC-040/A31 g-a after the TTL: the latch is dropped, a hold read is not trigger")
	}
	if n := f.gameModes.size(); n > 2 {
		t.Fatalf("UC-040/A31 the per-game latch keeps %d games after the TTL, want at most 2", n)
	}
}

// UC: UC-040 A31
func TestShieldCharge_UnreadableModeDoesNotLatch(t *testing.T) {
	stub := stubEvaluator{strings: map[string]string{"shield.activation": "hold"}, floats: map[string]float64{}, errs: map[string]error{"shield.activation": errUnreadable}}
	f := gameFlags(stub, nil)
	if trigger, _ := f.ShieldCharge(context.Background(), "g-u"); trigger {
		t.Fatal("UC-040/A31 unreadable: fail open")
	}
	delete(stub.errs, "shield.activation")
	stub.strings["shield.activation"] = "trigger"
	if trigger, _ := f.ShieldCharge(context.Background(), "g-u"); !trigger {
		t.Fatal("UC-040/A31 an unreadable read must not latch the game: the first real read is trigger")
	}
	stub.strings["shield.activation"] = "hold"
	if trigger, _ := f.ShieldCharge(context.Background(), "g-u"); !trigger {
		t.Fatal("UC-040/A31 hold after a first trigger read: want trigger")
	}
}

// UC: UC-040 A31 BR-027
func TestLatchShieldMode_TheFirstSignalFixesTheGamesMode(t *testing.T) {
	stub := stubEvaluator{strings: map[string]string{"shield.activation": "trigger"}, floats: map[string]float64{}, errs: map[string]error{}}
	f := gameFlags(stub, nil)
	f.LatchShieldMode(context.Background(), "g-early") // the game's first signal: trigger
	stub.strings["shield.activation"] = "hold"         // flipped before the first grant decision
	if trigger, _ := f.ShieldCharge(context.Background(), "g-early"); !trigger {
		t.Fatal("UC-040/A31 the first read at the game's first signal was trigger: a hold read must stay trigger")
	}

	stub.errs["shield.activation"] = errUnreadable
	f.LatchShieldMode(context.Background(), "g-unread") // unreadable: latches nothing
	delete(stub.errs, "shield.activation")
	stub.strings["shield.activation"] = "trigger"
	if trigger, _ := f.ShieldCharge(context.Background(), "g-unread"); !trigger {
		t.Fatal("UC-040/A31 an unreadable first signal must not latch the game")
	}
	var nilFlags *Flags
	nilFlags.LatchShieldMode(context.Background(), "g-nil") // no panic
}
