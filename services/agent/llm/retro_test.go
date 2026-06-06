package llm

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// retroSnapshot is the shared flag snapshot for the retro golden scenarios. Only
// the capability model is read by BuildRetro (no variant), so the rest is
// representative but unused.
func retroSnapshot() flags.Snapshot {
	return flags.Snapshot{
		Mode: "llm",
		Objectives: map[string]float64{
			"balanced":  0.7,
			"chaos":     0.1,
			"endurance": 0.1,
		},
		Capability: flags.Capability{Model: "phi4-mini"},
	}
}

// retroThreePlayers: two eliminations and one survivor (the winner). Mixes real
// and never-observed signals like the in-game three-player context.
func retroThreePlayers() gamecontext.GameContext {
	return gamecontext.GameContext{
		SessionID: "session-7",
		Session: gamecontext.SessionSignals{
			DurationSeconds:     fptr(132.0),
			ActivePlayerCount:   iptr(1),
			GameActive:          bptr(false),
			GameMode:            sptr("ffa"),
			EliminationSequence: []string{"BB:22", "CC:33"},
		},
		Players: map[string]*gamecontext.PlayerSignals{
			"AA:11": {
				Serial:            "AA:11",
				MovementIntensity: fptr(1.25),
				MovementVariance:  fptr(0.40),
				BatteryPct:        fptr(72),
				SkillLevel:        fptr(0.8),
				Active:            bptr(true),
			},
			"BB:22": {
				Serial:            "BB:22",
				MovementIntensity: fptr(0.10),
				MovementVariance:  fptr(0.05),
				BatteryPct:        fptr(40),
				SkillLevel:        fptr(0.3),
				Active:            bptr(false),
			},
			// Eliminated player with all signals never observed.
			"CC:33": {Serial: "CC:33"},
		},
	}
}

type retroCase struct {
	name     string
	snapshot flags.Snapshot
	context  gamecontext.GameContext
}

func retroCases() []retroCase {
	return []retroCase{
		{
			name:     "retro_3players",
			snapshot: retroSnapshot(),
			context:  retroThreePlayers(),
		},
		{
			// All players eliminated -> no survivor -> winner unknown.
			name:     "retro_all_eliminated",
			snapshot: retroSnapshot(),
			context: gamecontext.GameContext{
				SessionID: "session-9",
				Session: gamecontext.SessionSignals{
					DurationSeconds:     fptr(60.0),
					ActivePlayerCount:   iptr(0),
					GameActive:          bptr(false),
					GameMode:            sptr("joust"),
					EliminationSequence: []string{"AA:11", "BB:22"},
				},
				Players: map[string]*gamecontext.PlayerSignals{
					"AA:11": {Serial: "AA:11", BatteryPct: fptr(30), SkillLevel: fptr(0.5)},
					"BB:22": {Serial: "BB:22", BatteryPct: fptr(25), SkillLevel: fptr(0.4)},
				},
			},
		},
		{
			// No players, nil session signals: every field renders "unknown"/[].
			name:     "retro_empty",
			snapshot: retroSnapshot(),
			context: gamecontext.GameContext{
				SessionID: "session-1",
				Players:   map[string]*gamecontext.PlayerSignals{},
			},
		},
		{
			// One survivor (winner) but every player signal never observed.
			name:     "retro_unknown_signals",
			snapshot: retroSnapshot(),
			context: gamecontext.GameContext{
				SessionID: "session-0",
				Session: gamecontext.SessionSignals{
					EliminationSequence: []string{"ZZ:99"},
				},
				Players: map[string]*gamecontext.PlayerSignals{
					"ZZ:99": {Serial: "ZZ:99"},
					"YY:88": {Serial: "YY:88"},
				},
			},
		},
	}
}

// renderRetroGolden serializes a RetroPrompt into the golden-file layout.
func renderRetroGolden(p RetroPrompt) string {
	var b strings.Builder
	b.WriteString("=== MODEL ===\n")
	b.WriteString(p.Model)
	b.WriteString("\n=== SYSTEM ===\n")
	b.WriteString(p.System)
	b.WriteString("\n=== USER ===\n")
	b.WriteString(p.User)
	b.WriteString("\n")
	return b.String()
}

func TestRetroGolden(t *testing.T) {
	for _, tc := range retroCases() {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			got := renderRetroGolden(BuildRetro(RetroInput{
				Snapshot: tc.snapshot,
				Context:  tc.context,
				Now:      fixedNow,
			}))
			path := filepath.Join("testdata", tc.name+".golden")
			if *update {
				if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
					t.Fatalf("write golden: %v", err)
				}
				return
			}
			want, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("read golden (run with -update to create): %v", err)
			}
			if got != string(want) {
				t.Errorf("retro prompt mismatch for %s\n--- got ---\n%s\n--- want ---\n%s", tc.name, got, want)
			}
		})
	}
}

// TestPlayerOutcome covers the elimination-position derivation: the first
// eliminated serial is "eliminated #1", an absent serial is a "survivor".
func TestPlayerOutcome(t *testing.T) {
	seq := []string{"BB:22", "CC:33"}
	cases := map[string]string{
		"BB:22": "eliminated #1",
		"CC:33": "eliminated #2",
		"AA:11": "survivor", // never eliminated
	}
	for serial, want := range cases {
		if got := playerOutcome(serial, seq); got != want {
			t.Errorf("playerOutcome(%q) = %q, want %q", serial, got, want)
		}
	}
	// Empty sequence: everyone is a survivor.
	if got := playerOutcome("AA:11", nil); got != "survivor" {
		t.Errorf("playerOutcome with no eliminations = %q, want survivor", got)
	}
}

// TestWinnerSerial: exactly one survivor wins; zero or multiple survivors -> unknown.
func TestWinnerSerial(t *testing.T) {
	t.Run("sole survivor wins", func(t *testing.T) {
		if got := winnerSerial([]string{"AA:11", "BB:22", "CC:33"}, []string{"BB:22", "CC:33"}); got != "AA:11" {
			t.Errorf("winner = %q, want AA:11", got)
		}
	})
	t.Run("all eliminated -> unknown", func(t *testing.T) {
		if got := winnerSerial([]string{"AA:11", "BB:22"}, []string{"AA:11", "BB:22"}); got != unknown {
			t.Errorf("winner = %q, want unknown", got)
		}
	})
	t.Run("multiple survivors -> unknown", func(t *testing.T) {
		if got := winnerSerial([]string{"AA:11", "BB:22", "CC:33"}, []string{"CC:33"}); got != unknown {
			t.Errorf("winner = %q, want unknown", got)
		}
	})
	t.Run("no players -> unknown", func(t *testing.T) {
		if got := winnerSerial(nil, nil); got != unknown {
			t.Errorf("winner = %q, want unknown", got)
		}
	})
}

// TestRetroDeterminism: identical input yields byte-identical output regardless
// of player map insertion order.
func TestRetroDeterminism(t *testing.T) {
	in := RetroInput{
		Snapshot: retroSnapshot(),
		Context:  retroThreePlayers(),
		Now:      fixedNow,
	}
	first := BuildRetro(in)
	second := BuildRetro(in)
	if first != second {
		t.Fatalf("non-deterministic BuildRetro:\n%+v\n%+v", first, second)
	}
}

// TestRetroContractKeys: the System prompt names every response-contract key and
// every calibration flag, so it cannot drift from the documented surface.
func TestRetroContractKeys(t *testing.T) {
	sys := BuildRetro(RetroInput{Snapshot: retroSnapshot(), Context: retroThreePlayers(), Now: fixedNow}).System
	for _, key := range []string{"session_assessment", "suggestions", "flag", "value", "reason"} {
		if !strings.Contains(sys, `"`+key+`"`) {
			t.Errorf("system prompt missing response-contract key %q", key)
		}
	}
	for _, flag := range []string{
		calibDifficultyFactor, calibPacingProfile, calibThresholdTable, calibObjectiveVariant,
	} {
		if !strings.Contains(sys, flag) {
			t.Errorf("system prompt missing calibration flag %q", flag)
		}
	}
}

// TestRetroEmptyPlayers: a session with no players renders the "(none)" marker
// and an unknown winner.
func TestRetroEmptyPlayers(t *testing.T) {
	user := BuildRetro(RetroInput{
		Snapshot: retroSnapshot(),
		Context:  gamecontext.GameContext{SessionID: "s"},
		Now:      fixedNow,
	}).User
	for _, want := range []string{
		"duration_seconds: unknown",
		"final_active_players: unknown",
		"elimination_order: []",
		"winner: unknown",
		"(none)",
	} {
		if !strings.Contains(user, want) {
			t.Errorf("missing %q in:\n%s", want, user)
		}
	}
}
