package llm

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamesummary"
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
		// #1158: the retro System prompt now renders the REAL intervention allow-list
		// as the recommendation surface (no phantom calibration flags), so the golden
		// snapshot carries a representative "standard" variant allow-list.
		InterventionsAllowed: []string{
			"play_audio_cue", "send_controller_effect", "adjust_volume",
			"adjust_music_tempo", "set_pacing_profile", "adjust_player_sensitivity",
			"grant_shield",
		},
		Capability: flags.Capability{Model: "phi4-mini"},
	}
}

// retroFullSnapshot is retroSnapshot with the production `full` intervention
// allow-list (#1160 review fix): it includes the REAL-TIME REACTIVE actions
// eliminate_player / revive_player / end_game / send_controller_effect, so the golden
// shows the reframed recommendation surface ("which intervention TYPES to enable /
// emphasize for the next session") reading coherently with reactive actions present —
// not the old incoherent "open the next game by eliminating a player" framing.
func retroFullSnapshot() flags.Snapshot {
	s := retroSnapshot()
	s.InterventionsAllowed = []string{
		"play_audio_cue", "send_controller_effect", "adjust_volume",
		"adjust_music_tempo", "set_pacing_profile", "adjust_player_sensitivity",
		"grant_shield", "adjust_global_sensitivity", "adjust_global_difficulty",
		"eliminate_player", "revive_player", "end_game",
	}
	return s
}

// retroThreePlayers: two eliminations and one survivor (the winner). Mixes real
// and never-observed signals like the in-game three-player context.
func retroThreePlayers() gamecontext.GameContext {
	return gamecontext.GameContext{
		SessionID: "session-7",
		GameKind:  "real",
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

// retroTimelineContext is retroThreePlayers plus a full-session #916 narrative
// ending in the "end" phase, as the OnGameEnd snapshot would carry.
func retroTimelineContext() gamecontext.GameContext {
	ctx := retroThreePlayers()
	at := func(secsBefore int) time.Time {
		return fixedNow.Add(time.Duration(-secsBefore) * time.Second)
	}
	ctx.Timeline = []gamecontext.TimelineEvent{
		{At: at(150), Kind: gamecontext.EventPhase, Detail: "start"},
		{At: at(140), Kind: gamecontext.EventStateDelta, ActivePlayers: iptr(3), MeanIntensity: fptr(0.80)},
		{At: at(90), Kind: gamecontext.EventElimination, Serial: "BB:22", Order: 1},
		{At: at(40), Kind: gamecontext.EventStateDelta, ActivePlayers: iptr(2), MeanIntensity: fptr(0.45)},
		{At: at(0), Kind: gamecontext.EventPhase, Detail: "end"},
	}
	return ctx
}

// retroMovementContext is retroTimelineContext plus per-player movement series and
// a session game-speed/threshold track (#1082), so the retro golden exercises the
// sparkline + activity + proximity-to-death + tempo rendering at game end. AA:11 is
// the near-death survivor (winner); BB:22 faded to idle (eliminated #1); CC:33
// crossed the threshold and was eliminated #2.
func retroMovementContext() gamecontext.GameContext {
	ctx := retroTimelineContext()
	at := func(secsBefore int) time.Time {
		return fixedNow.Add(time.Duration(-secsBefore) * time.Second)
	}
	ctx.Session.MusicTempo = fptr(1.25)
	ctx.Session.DeathThreshold = fptr(1.75)
	ctx.Session.WarningThreshold = fptr(1.15)

	ms := func(secsBefore int, tempo, thr float64) gamecontext.SpeedSample {
		return gamecontext.SpeedSample{At: at(secsBefore), Tempo: fptr(tempo), DeathThreshold: fptr(thr)}
	}
	ctx.SpeedTrack = []gamecontext.SpeedSample{
		ms(140, 1.00, 1.50), ms(110, 1.08, 1.58), ms(80, 1.15, 1.65),
		ms(50, 1.20, 1.70), ms(20, 1.25, 1.75),
	}
	smp := func(secsBefore int, intensity float64) gamecontext.MovementSample {
		return gamecontext.MovementSample{At: at(secsBefore), Intensity: intensity}
	}
	ctx.Players["AA:11"].History = []gamecontext.MovementSample{
		smp(140, 0.50), smp(110, 0.90), smp(80, 1.30), smp(50, 1.60), smp(20, 1.68),
	}
	ctx.Players["BB:22"].History = []gamecontext.MovementSample{
		smp(140, 0.80), smp(110, 0.40), smp(80, 0.10), smp(50, 0.02),
	}
	ctx.Players["CC:33"].History = []gamecontext.MovementSample{
		smp(140, 1.10), smp(100, 1.50), smp(92, 1.80),
	}
	return ctx
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
				GameKind:  "shadow",
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
			// #916: the retro carries the same rolling-narrative section, ending in
			// the "end" phase transition (the OnGameEnd snapshot includes it).
			name:     "retro_timeline",
			snapshot: retroSnapshot(),
			context:  retroTimelineContext(),
		},
		{
			// #1082: per-player movement sparklines + activity + proximity-to-death +
			// the session game-speed track, at game end.
			name:     "retro_movement",
			snapshot: retroSnapshot(),
			context:  retroMovementContext(),
		},
		{
			// #1160 review fix: the production `full` allow-list, which includes the
			// reactive actions eliminate_player / revive_player / end_game. The golden
			// makes the reframed recommendation surface visible and coherent with those
			// real-time actions present (not "open the next game by eliminating a player").
			name:     "retro_full_allowlist",
			snapshot: retroFullSnapshot(),
			context:  retroMovementContext(),
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
				// #1158: build the per-game Summary the same way retro_capture.go does —
				// a pure fold over the finished-game snapshot with the injected Now — so
				// the golden exercises the per-player ARC stanzas (PlayerArc), not the old
				// flat terminal snapshot.
				Summary: gamesummary.BuildSummary(tc.context, gamesummary.BuildOptions{Now: fixedNow}),
				Now:     fixedNow,
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
	ctx := retroThreePlayers()
	in := RetroInput{
		Snapshot: retroSnapshot(),
		Context:  ctx,
		Summary:  gamesummary.BuildSummary(ctx, gamesummary.BuildOptions{Now: fixedNow}),
		Now:      fixedNow,
	}
	first := BuildRetro(in)
	second := BuildRetro(in)
	if first != second {
		t.Fatalf("non-deterministic BuildRetro:\n%+v\n%+v", first, second)
	}
}

// TestRetroContractKeys: the System prompt names every response-contract key, so
// it cannot drift from the documented surface (#1160: intervention_type/emphasis/
// session_focus — the reframed tuning surface, not the old lever/value setup framing).
func TestRetroContractKeys(t *testing.T) {
	sys := BuildRetro(RetroInput{Snapshot: retroSnapshot(), Context: retroThreePlayers(), Now: fixedNow}).System
	for _, key := range []string{"session_assessment", "suggestions", "intervention_type", "emphasis", "reason", "session_focus"} {
		if !strings.Contains(sys, `"`+key+`"`) {
			t.Errorf("system prompt missing response-contract key %q", key)
		}
	}
}

// TestRetroSystemReframedTuning asserts the #1160 review fix: the recommendation
// surface frames the allow-list as intervention TYPES to ENABLE/EMPHASIZE for the
// next session (tuning the agent's reactive vocabulary), NOT how the next game OPENS
// or is PACED. With the production `full` allow-list (eliminate_player / revive_player
// / end_game present) the old "open the next game by eliminating a player" framing was
// incoherent; the reframed prose must be present and the setup framing gone.
func TestRetroSystemReframedTuning(t *testing.T) {
	sys := BuildRetro(RetroInput{Snapshot: retroFullSnapshot(), Context: retroMovementContext(), Now: fixedNow}).System
	// The reframed tuning surface must be present.
	for _, want := range []string{
		"intervention TYPES",
		"ENABLE or EMPHASIZE",
		"REAL-TIME REACTIVE in-game",
	} {
		if !strings.Contains(sys, want) {
			t.Errorf("retro system prompt missing reframed tuning wording %q", want)
		}
	}
	// The old setup/opening framing must be gone.
	for _, gone := range []string{
		"how the next game should OPEN",
		"should OPEN and\nbe PACED",
		"next-game value/direction",
	} {
		if strings.Contains(sys, gone) {
			t.Errorf("retro system prompt still uses old setup framing %q", gone)
		}
	}
	// The full reactive allow-list must still be rendered verbatim (data unchanged).
	for _, lever := range []string{"eliminate_player", "revive_player", "end_game", "send_controller_effect"} {
		if !strings.Contains(sys, lever) {
			t.Errorf("retro system prompt dropped reactive intervention type %q from the full allow-list", lever)
		}
	}
}

// TestRetroSystemNoPhantomFlags asserts the four phantom calibration flags the
// pre-#1158 prompt invented are GONE from the System prompt — none are real
// agent-readable init flags (threshold_table / objective_variant exist nowhere;
// global_difficulty_factor / pacing_profile are game_coordinator intervention-domain
// flags the agent only writes INDIRECTLY via interventions, never reads at INIT).
// The retro must recommend REAL intervention levers, not phantom flags.
func TestRetroSystemNoPhantomFlags(t *testing.T) {
	sys := BuildRetro(RetroInput{Snapshot: retroSnapshot(), Context: retroThreePlayers(), Now: fixedNow}).System
	// threshold_table / objective_variant / global_difficulty_factor exist in no
	// agent-readable init surface and must be absent outright.
	for _, phantom := range []string{
		"global_difficulty_factor", "threshold_table", "objective_variant",
	} {
		if strings.Contains(sys, phantom) {
			t.Errorf("system prompt still cites phantom calibration flag %q", phantom)
		}
	}
	// The bare "pacing_profile" calibration flag is phantom; the legitimate
	// set_pacing_profile INTERVENTION (which contains it) is allowed. So scrub the
	// real lever name first, then assert the bare flag name is gone.
	scrubbed := strings.ReplaceAll(sys, "set_pacing_profile", "")
	if strings.Contains(scrubbed, "pacing_profile") {
		t.Errorf("system prompt still cites phantom calibration flag \"pacing_profile\" (not as set_pacing_profile)")
	}
	// The old calibration-surface framing ("read at game INIT") must be gone.
	if strings.Contains(sys, "read at\ngame INIT") || strings.Contains(sys, "CALIBRATION SURFACE") {
		t.Errorf("system prompt still frames an init-time calibration surface")
	}
	// The renamed advisory goal field must not collide with the in-game objective_served.
	if strings.Contains(sys, "objective_served") {
		t.Errorf("retro system prompt should use session_focus, not the in-game objective_served")
	}
}

// TestRetroSystemPhysicsGrounding asserts the System prompt carries the SAME
// physics + FFA-mode grounding the in-game prompt got (#1091), so the offline
// analyst reasons within the real mechanics (#1158 fix B).
func TestRetroSystemPhysicsGrounding(t *testing.T) {
	sys := BuildRetro(RetroInput{Snapshot: retroSnapshot(), Context: retroThreePlayers(), Now: fixedNow}).System
	for _, want := range []string{
		"HOW THE GAME PHYSICALLY WORKS:",
		"GAME MODE — FFA",
		// a real lever from the allow-list, proving the recommendation surface is real.
		"adjust_music_tempo",
	} {
		if !strings.Contains(sys, want) {
			t.Errorf("retro system prompt missing grounding %q", want)
		}
	}
}

// TestRetroUserPerPlayerArcs asserts the User prompt renders the rich per-player
// ARC fields from the Summary (#1158 fix A), not the old flat terminal snapshot.
func TestRetroUserPerPlayerArcs(t *testing.T) {
	ctx := retroMovementContext()
	user := BuildRetro(RetroInput{
		Snapshot: retroSnapshot(),
		Context:  ctx,
		Summary:  gamesummary.BuildSummary(ctx, gamesummary.BuildOptions{Now: fixedNow}),
		Now:      fixedNow,
	}).User
	for _, want := range []string{
		"Player arcs",
		"survival_seconds=",
		"elimination_offset_seconds=",
		"near_death_ratio=",
		"final_intensity=",
		"Engagement trend (room",
	} {
		if !strings.Contains(user, want) {
			t.Errorf("retro user prompt missing per-player arc field %q in:\n%s", want, user)
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
