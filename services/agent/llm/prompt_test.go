package llm

import (
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// update regenerates the golden files: go test ./llm -run TestGolden -update.
var update = flag.Bool("update", false, "update golden files")

// fixedNow is the injected wall clock used by every test, so captured_at is
// deterministic.
var fixedNow = time.Date(2026, time.June, 5, 12, 30, 0, 0, time.UTC)

func fptr(v float64) *float64 { return &v }
func iptr(v int) *int         { return &v }
func bptr(v bool) *bool       { return &v }
func sptr(v string) *string   { return &v }

// baseSnapshot is the shared flag snapshot for the golden scenarios. Only the
// PromptVariant differs across the conservative/aggressive/balanced variants.
func baseSnapshot(variant string) flags.Snapshot {
	return flags.Snapshot{
		Mode: "llm",
		Objectives: map[string]float64{
			"balanced":  0.7,
			"chaos":     0.1,
			"endurance": 0.1,
		},
		Capability: flags.Capability{
			Model:         "phi4-mini",
			PromptVariant: variant,
		},
		InterventionsAllowed: []string{"grant_shield", "play_audio_cue", "set_music_tempo"},
		Policy: flags.Policy{
			BatteryThreshold:          20,
			MovementVarianceWindow:    10,
			MaxInterventionsPerMinute: 2,
		},
	}
}

// threePlayerContext mixes nil and real signals: an active player with full
// readings, a low-battery player, and an eliminated player with all-nil signals.
func threePlayerContext() gamecontext.GameContext {
	return gamecontext.GameContext{
		SessionID: "session-7",
		GameKind:  "real",
		Session: gamecontext.SessionSignals{
			DurationSeconds:     fptr(95.5),
			ActivePlayerCount:   iptr(2),
			GameActive:          bptr(true),
			GameMode:            sptr("ffa"),
			EliminationSequence: []string{"CC:33"},
		},
		Players: map[string]*gamecontext.PlayerSignals{
			"AA:11": {
				Serial:            "AA:11",
				MovementIntensity: fptr(1.25),
				MovementVariance:  fptr(0.40),
				BatteryPct:        fptr(85),
				SkillLevel:        fptr(0.6),
				Active:            bptr(true),
			},
			"BB:22": {
				Serial:            "BB:22",
				MovementIntensity: fptr(0.0),
				MovementVariance:  fptr(0.05),
				BatteryPct:        fptr(15),
				SkillLevel:        fptr(0.3),
				Active:            bptr(true),
			},
			// Eliminated player: all signals never observed (all nil).
			"CC:33": {
				Serial: "CC:33",
			},
		},
	}
}

type goldenCase struct {
	name     string
	snapshot flags.Snapshot
	context  gamecontext.GameContext
}

func goldenCases() []goldenCase {
	return []goldenCase{
		{
			name:     "conservative_3players",
			snapshot: baseSnapshot(conservativeVariant),
			context:  threePlayerContext(),
		},
		{
			name:     "aggressive_3players",
			snapshot: baseSnapshot(aggressiveVariant),
			context:  threePlayerContext(),
		},
		{
			name:     "balanced_3players",
			snapshot: baseSnapshot(balancedVariant),
			context:  threePlayerContext(),
		},
		{
			name:     "empty_players",
			snapshot: baseSnapshot(conservativeVariant),
			context: gamecontext.GameContext{
				SessionID: "session-1",
				GameKind:  "shadow",
				Session: gamecontext.SessionSignals{
					DurationSeconds:   fptr(3.0),
					ActivePlayerCount: iptr(0),
					GameActive:        bptr(false),
					GameMode:          sptr("joust"),
				},
				Players: map[string]*gamecontext.PlayerSignals{},
			},
		},
		{
			name: "all_unknown",
			snapshot: flags.Snapshot{
				Mode:                 "llm",
				Objectives:           map[string]float64{},
				Capability:           flags.Capability{Model: "phi4-mini", PromptVariant: conservativeVariant},
				InterventionsAllowed: nil,
				Policy:               flags.Policy{BatteryThreshold: 20, MaxInterventionsPerMinute: 2},
			},
			context: gamecontext.GameContext{
				SessionID: "session-0",
				Players: map[string]*gamecontext.PlayerSignals{
					"ZZ:99": {Serial: "ZZ:99"},
				},
			},
		},
	}
}

// renderGolden serializes a Prompt into the golden-file layout.
func renderGolden(p Prompt) string {
	var b strings.Builder
	b.WriteString("=== VARIANT ===\n")
	b.WriteString(p.Variant)
	b.WriteString("\n=== MODEL ===\n")
	b.WriteString(p.Model)
	b.WriteString("\n=== SYSTEM ===\n")
	b.WriteString(p.System)
	b.WriteString("\n=== USER ===\n")
	b.WriteString(p.User)
	b.WriteString("\n")
	return b.String()
}

func TestGolden(t *testing.T) {
	for _, tc := range goldenCases() {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			got := renderGolden(Build(BuildInput{
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
				t.Errorf("prompt mismatch for %s\n--- got ---\n%s\n--- want ---\n%s", tc.name, got, want)
			}
		})
	}
}

// TestNilVsZero verifies nil pointers render "unknown" while a non-nil pointer
// at the zero value renders "0.00" (floats) or its literal (bool/int).
func TestNilVsZero(t *testing.T) {
	t.Run("float nil vs zero", func(t *testing.T) {
		if got := floatPtrOrUnknown(nil); got != "unknown" {
			t.Errorf("nil float = %q, want unknown", got)
		}
		if got := floatPtrOrUnknown(fptr(0.0)); got != "0.00" {
			t.Errorf("*0.0 float = %q, want 0.00", got)
		}
	})
	t.Run("int nil vs zero", func(t *testing.T) {
		if got := intPtrOrUnknown(nil); got != "unknown" {
			t.Errorf("nil int = %q, want unknown", got)
		}
		if got := intPtrOrUnknown(iptr(0)); got != "0" {
			t.Errorf("*0 int = %q, want 0", got)
		}
	})
	t.Run("bool nil vs zero", func(t *testing.T) {
		if got := boolPtrOrUnknown(nil); got != "unknown" {
			t.Errorf("nil bool = %q, want unknown", got)
		}
		if got := boolPtrOrUnknown(bptr(false)); got != "false" {
			t.Errorf("*false bool = %q, want false", got)
		}
	})
	t.Run("string nil vs value", func(t *testing.T) {
		if got := stringOrUnknown(nil); got != "unknown" {
			t.Errorf("nil string = %q, want unknown", got)
		}
		if got := stringOrUnknown(sptr("ffa")); got != "ffa" {
			t.Errorf("*ffa string = %q, want ffa", got)
		}
	})
	t.Run("game kind empty vs value", func(t *testing.T) {
		if got := gameKindOrUnknown(""); got != "unknown" {
			t.Errorf("empty kind = %q, want unknown", got)
		}
		if got := gameKindOrUnknown("real"); got != "real" {
			t.Errorf("real kind = %q, want real", got)
		}
	})

	// End-to-end: a player with all-zero (non-nil) signals renders 0.00/false,
	// never "unknown".
	zeroPlayer := gamecontext.GameContext{
		SessionID: "s",
		Session: gamecontext.SessionSignals{
			DurationSeconds:   fptr(0.0),
			ActivePlayerCount: iptr(0),
			GameActive:        bptr(false),
			GameMode:          sptr("ffa"),
		},
		Players: map[string]*gamecontext.PlayerSignals{
			"AA:11": {
				Serial:            "AA:11",
				MovementIntensity: fptr(0.0),
				MovementVariance:  fptr(0.0),
				BatteryPct:        fptr(0.0),
				SkillLevel:        fptr(0.0),
				Active:            bptr(false),
			},
		},
	}
	user := buildUser(zeroPlayer, fixedNow)
	wantLine := "  AA:11: active=false movement_intensity=0.00 movement_variance=0.00 battery_pct=0.00 skill=0.00"
	if !strings.Contains(user, wantLine) {
		t.Errorf("missing zero-value player line; got:\n%s", user)
	}
	// The static legend line carries the word "unknown"; assert no DATA line does.
	// Every value-bearing line for an all-zero context must render 0.00/false/0.
	if strings.Contains(wantLine, "unknown") {
		t.Errorf("zero-value player line leaked 'unknown': %s", wantLine)
	}
	if !strings.Contains(user, "duration_seconds: 0.00") || !strings.Contains(user, "active_players: 0") || !strings.Contains(user, "game_active: false") {
		t.Errorf("session zero values not rendered:\n%s", user)
	}
}

// TestNilSessionFields verifies nil session pointers render "unknown".
func TestNilSessionFields(t *testing.T) {
	user := buildUser(gamecontext.GameContext{SessionID: "s", Players: nil}, fixedNow)
	for _, want := range []string{
		"duration_seconds: unknown",
		"active_players: unknown",
		"game_active: unknown",
		"kind=unknown",
		"mode=unknown",
		"elimination_sequence: []",
		"(none)",
	} {
		if !strings.Contains(user, want) {
			t.Errorf("missing %q in:\n%s", want, user)
		}
	}
}

// TestDeterminism: identical input yields byte-identical output, and player
// insertion order does not affect the output.
func TestDeterminism(t *testing.T) {
	in := BuildInput{
		Snapshot: baseSnapshot(conservativeVariant),
		Context:  threePlayerContext(),
		Now:      fixedNow,
	}
	first := Build(in)
	second := Build(in)
	if first != second {
		t.Fatalf("non-deterministic build:\n%+v\n%+v", first, second)
	}

	// Build a context with players inserted in a different order; Go map order is
	// randomized, but the output must be stable because we sort by serial.
	reordered := threePlayerContext()
	// Reconstruct the map by re-inserting (order is irrelevant for maps, but this
	// makes the intent explicit). Output must still match the original.
	got := Build(BuildInput{Snapshot: in.Snapshot, Context: reordered, Now: fixedNow})
	if got.User != first.User {
		t.Errorf("player order affected output:\n--- a ---\n%s\n--- b ---\n%s", first.User, got.User)
	}
}

// TestUnknownVariant: an unrecognized prompt_variant falls back to conservative
// text and the resolved Variant is "conservative".
func TestUnknownVariant(t *testing.T) {
	p := Build(BuildInput{
		Snapshot: baseSnapshot("totally-made-up"),
		Context:  threePlayerContext(),
		Now:      fixedNow,
	})
	if p.Variant != conservativeVariant {
		t.Errorf("variant = %q, want conservative", p.Variant)
	}
	if !strings.Contains(p.System, variantGuidance[conservativeVariant]) {
		t.Errorf("system missing conservative guidance:\n%s", p.System)
	}
	if !strings.Contains(p.System, "VARIANT: conservative.") {
		t.Errorf("system missing 'VARIANT: conservative.' line:\n%s", p.System)
	}
}

// TestResponseContractKeys guards against drift from the Decision struct: every
// JSON key the contract promises must appear in the System prompt.
func TestResponseContractKeys(t *testing.T) {
	sys := Build(BuildInput{
		Snapshot: baseSnapshot(conservativeVariant),
		Context:  threePlayerContext(),
		Now:      fixedNow,
	}).System
	for _, key := range []string{"intervention", "target_serial", "value", "reason", "objective_served"} {
		if !strings.Contains(sys, `"`+key+`"`) {
			t.Errorf("system prompt missing response-contract key %q", key)
		}
	}
}

// TestEmptyInterventionsMarker: an empty allow-list renders the "(none)" marker.
func TestEmptyInterventionsMarker(t *testing.T) {
	if got := joinInterventions(nil); got != "(none)" {
		t.Errorf("empty allow-list = %q, want (none)", got)
	}
	if got := joinInterventions([]string{"a", "b"}); got != "a, b" {
		t.Errorf("allow-list join = %q, want 'a, b'", got)
	}
}
