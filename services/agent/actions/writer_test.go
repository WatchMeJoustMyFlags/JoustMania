package actions

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strconv"
	"sync"
	"testing"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flagservice"
)

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// call is one request the fake flag service received.
type call struct {
	method string // PUT | POST
	set    string
	flag   string
	write  flagservice.Write
	event  flagservice.Event
}

// fakeService records writes and answers err (nil = accepted).
type fakeService struct {
	mu    sync.Mutex
	calls []call
	err   error
}

func (f *fakeService) Put(_ context.Context, set, flag string, w flagservice.Write) (flagservice.Result, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, call{method: "PUT", set: set, flag: flag, write: w})
	return flagservice.Result{FlagSet: set, Flag: flag}, f.err
}

func (f *fakeService) Post(_ context.Context, set, flag string, ev flagservice.Event) (flagservice.Result, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, call{method: "POST", set: set, flag: flag, event: ev})
	return flagservice.Result{FlagSet: set, Flag: flag, Seq: int64(len(f.calls))}, f.err
}

func (f *fakeService) all() []call {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]call(nil), f.calls...)
}

func (f *fakeService) last(t *testing.T) call {
	t.Helper()
	all := f.all()
	if len(all) == 0 {
		t.Fatal("no write was sent")
	}
	return all[len(all)-1]
}

func newTestWriter(t *testing.T) (*Writer, *fakeService) {
	t.Helper()
	svc := &fakeService{}
	return NewWriter(svc, discardLogger()), svc
}

// UC: UC-053 main A4 BR-003 BR-006
// UC: UC-040 main
// UC: UC-040 A47 BR-033
func TestWriter_EveryInterventionIsOneGameScopedWrite(t *testing.T) {
	cases := []struct {
		name      string
		d         decision.Decision
		method    string
		flag      string
		serial    string // expected scope serial (PUT) or payload serial (POST)
		value     any    // PUT value
		payload   map[string]any
		wantError bool
	}{
		{name: "UC-053/A4 eliminate is an event with the serial in the payload",
			d:      decision.Decision{Intervention: decision.InterventionEliminatePlayer, TargetSerial: "MOCK0001"},
			method: "POST", flag: flagEliminatePlayer, payload: map[string]any{"serial": "MOCK0001"}},
		{name: "UC-053/A4 revive is an event",
			d:      decision.Decision{Intervention: decision.InterventionRevivePlayer, TargetSerial: "MOCK0002"},
			method: "POST", flag: flagRevivePlayer, payload: map[string]any{"serial": "MOCK0002"}},
		{name: "UC-053/A4 audio cue carries the sound, default agent_cue",
			d:      decision.Decision{Intervention: decision.InterventionPlayAudioCue},
			method: "POST", flag: flagAudioCue, payload: map[string]any{"sound": "agent_cue"}},
		{name: "UC-053/A4 controller effect carries serial and effect; empty serial broadcasts",
			d:      decision.Decision{Intervention: decision.InterventionSendControllerEffect, Value: "pulse"},
			method: "POST", flag: flagControllerEffect, payload: map[string]any{"serial": "", "effect": "pulse"}},
		{name: "UC-053/A4 end_game: the game is the scope",
			d:      decision.Decision{Intervention: decision.InterventionEndGame},
			method: "POST", flag: flagEndGame, payload: map[string]any{}},
		{name: "UC-040/A47 group shield is one event with the serial set and the seconds",
			d: decision.Decision{Intervention: decision.InterventionGrantGroupShield,
				TargetSerials: []string{"MOCK0001", "MOCK0002", "MOCK0003"}, Value: "4.5"},
			method: "POST", flag: flagGroupShield,
			payload: map[string]any{"serials": "MOCK0001,MOCK0002,MOCK0003", "seconds": "4.5"}},
		{name: "UC-040/A47 group shield without a stamped length uses the fail-closed default",
			d: decision.Decision{Intervention: decision.InterventionGrantGroupShield,
				TargetSerials: []string{"MOCK0001", "MOCK0002"}},
			method: "POST", flag: flagGroupShield,
			payload: map[string]any{"serials": "MOCK0001,MOCK0002", "seconds": strconv.FormatFloat(defaultShieldSeconds, 'f', -1, 64)}},
		{name: "UC-040/A47 group shield of one player is refused without a write",
			d: decision.Decision{Intervention: decision.InterventionGrantGroupShield,
				TargetSerials: []string{"MOCK0001"}}, wantError: true},
		{name: "UC-053/main music tempo is game state",
			d:      decision.Decision{Intervention: decision.InterventionAdjustMusicTempo, Value: "1.2"},
			method: "PUT", flag: flagMusicTempoOverride, value: 1.2},
		{name: "UC-053/main global sensitivity is an integer",
			d:      decision.Decision{Intervention: decision.InterventionAdjustGlobalSensitivity, Value: "3"},
			method: "PUT", flag: flagGlobalSensitivityOverride, value: 3},
		{name: "UC-053/main pacing profile string",
			d:      decision.Decision{Intervention: decision.InterventionSetPacingProfile},
			method: "PUT", flag: flagPacingProfile, value: defaultPacingProfile},
		{name: "UC-053/main a malformed ramp degrades to the safe default",
			d:      decision.Decision{Intervention: decision.InterventionRampTempo, Value: "9:9:bounce"},
			method: "PUT", flag: flagTempoRamp, value: defaultTempoRamp},
		{name: "UC-053/main a ramp within the 15 s cap is sent as is",
			d:      decision.Decision{Intervention: decision.InterventionRampTempo, Value: "1.3:15:ease"},
			method: "PUT", flag: flagTempoRamp, value: "1.3:15:ease"},
		{name: "UC-053/main a ramp longer than the 15 s cap degrades to the safe default (#1840)",
			d:      decision.Decision{Intervention: decision.InterventionRampTempo, Value: "1.3:30:linear"},
			method: "PUT", flag: flagTempoRamp, value: defaultTempoRamp},
		{name: "UC-053/main a level is per player",
			d:      decision.Decision{Intervention: decision.InterventionAdjustPlayerSensitivity, TargetSerial: "MOCK0003", Value: "0.8"},
			method: "PUT", flag: flagPlayerSensitivityFactor, serial: "MOCK0003", value: 0.8},
		{name: "UC-053/main a shield grant is per player",
			d:      decision.Decision{Intervention: decision.InterventionGrantShield, TargetSerial: "MOCK0003"},
			method: "PUT", flag: flagShieldSeconds, serial: "MOCK0003", value: float64(defaultShieldSeconds)},
		{name: "UC-040/A16 a toughness boost grant writes toughness_boost_seconds (#1838)",
			d:      decision.Decision{Intervention: decision.InterventionToughnessBoost, TargetSerial: "MOCK0003"},
			method: "PUT", flag: "toughness_boost_seconds", serial: "MOCK0003", value: "5:2.0"},
		{name: "UC-040/A16 an out-of-bounds toughness boost factor degrades to the default (#1838)",
			d:      decision.Decision{Intervention: decision.InterventionToughnessBoost, TargetSerial: "MOCK0003", Value: "5:9.0"},
			method: "PUT", flag: "toughness_boost_seconds", serial: "MOCK0003", value: "5:2.0"},
		{name: "UC-053/main an escalating soft penalty value never escalates",
			d:      decision.Decision{Intervention: decision.InterventionSoftPenalty, TargetSerial: "MOCK0003", Value: "explode"},
			method: "PUT", flag: flagSoftPenaltyAction, serial: "MOCK0003", value: defaultSoftPenalty},
		{name: "UC-053/main a grant without a serial is refused before any write",
			d:         decision.Decision{Intervention: decision.InterventionGrantShield},
			wantError: true},
		{name: "UC-053/A4 an elimination without a serial is refused",
			d:         decision.Decision{Intervention: decision.InterventionEliminatePlayer},
			wantError: true},
		{name: "UC-053/main an unknown intervention is refused",
			d:         decision.Decision{Intervention: "teleport"},
			wantError: true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w, svc := newTestWriter(t)
			c.d.GameID = "game_0a1b2c3d4e5f"
			err := w.Apply(context.Background(), c.d)
			if c.wantError {
				if err == nil || len(svc.all()) != 0 {
					t.Fatalf("want a refusal without a write, got err=%v writes=%d", err, len(svc.all()))
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			got := svc.last(t)
			if got.method != c.method || got.set != FlagSetInterventions || got.flag != c.flag {
				t.Fatalf("got %s %s/%s, want %s interventions/%s", got.method, got.set, got.flag, c.method, c.flag)
			}
			if c.method == "POST" {
				if got.event.Scope.GameID != c.d.GameID || got.event.Scope.Serial != "" || got.event.Writer != "agent" {
					t.Fatalf("event scope %+v writer %q", got.event.Scope, got.event.Writer)
				}
				if len(got.event.Payload) != len(c.payload) {
					t.Fatalf("payload %v, want %v", got.event.Payload, c.payload)
				}
				for k, v := range c.payload {
					if got.event.Payload[k] != v {
						t.Fatalf("payload %v, want %v", got.event.Payload, c.payload)
					}
				}
				return
			}
			if got.write.Scope.GameID != c.d.GameID || got.write.Scope.Serial != c.serial {
				t.Fatalf("scope %+v, want game %s serial %q", got.write.Scope, c.d.GameID, c.serial)
			}
			if got.write.Value != c.value || got.write.Variant != "" || got.write.Persist {
				t.Fatalf("write %+v, want value %v (%T)", got.write, c.value, c.value)
			}
		})
	}
}

// UC: UC-053 BR-003
// UC: UC-040 A23 BR-002
func TestWriter_NoGameNoWrite(t *testing.T) {
	t.Run("UC-053/BR-003 a decision without its game id is never written (FR-136)", func(t *testing.T) {
		w, svc := newTestWriter(t)
		err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionAdjustVolume})
		if !errors.Is(err, errNoGame) || len(svc.all()) != 0 {
			t.Fatalf("err=%v writes=%d", err, len(svc.all()))
		}
	})
	t.Run("UC-040/A23 the probe noop writes nothing and succeeds", func(t *testing.T) {
		w, svc := newTestWriter(t)
		if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionNoop}); err != nil || len(svc.all()) != 0 {
			t.Fatalf("err=%v writes=%d", err, len(svc.all()))
		}
	})
	t.Run("UC-040/BR-002 a decision with its coordinator game id is written for that game", func(t *testing.T) {
		w, svc := newTestWriter(t)
		err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionAdjustVolume, GameID: "game_0a1b2c3d4e5f"})
		if err != nil || len(svc.all()) != 1 || svc.last(t).write.Scope.GameID != "game_0a1b2c3d4e5f" {
			t.Fatalf("err=%v writes=%d", err, len(svc.all()))
		}
	})
}

// TestWriter_RefusesNonGameID: the partition for unlabeled signals has a
// `session-<n>` id, not a coordinator game id; a decision taken there is
// refused like an unscoped one, before any flag-service call. The counter and
// span side is TestDecideLoop_RefusesNonGameID.
// UC: UC-040 A18 BR-002
// UC: UC-041 A5
func TestWriter_RefusesNonGameID(t *testing.T) {
	for _, id := range []string{"session-1", "s-1", "g", "Game_1", "shadow_1"} {
		t.Run("UC-040/A18 refuses "+id, func(t *testing.T) {
			w, svc := newTestWriter(t)
			err := w.Apply(context.Background(), decision.Decision{
				Intervention: decision.InterventionEndGame, GameID: id,
			})
			if !errors.Is(err, errNoGame) {
				t.Fatalf("err=%v, want errNoGame", err)
			}
			if n := len(svc.all()); n != 0 {
				t.Fatalf("flag service got %d writes for game id %q, want 0", n, id)
			}
		})
	}
}

// UC: UC-053 A7 A8
func TestWriter_FailsClosed(t *testing.T) {
	t.Run("UC-053/A7 a rejected write is an error (fail closed)", func(t *testing.T) {
		w, svc := newTestWriter(t)
		svc.err = &flagservice.Error{Status: 503, Reason: flagservice.ReasonNoSyncStream}
		err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionAdjustVolume, GameID: "game_g"})
		if !flagservice.IsReason(err, flagservice.ReasonNoSyncStream) {
			t.Fatalf("err %v", err)
		}
	})
	t.Run("UC-053/A8 an unreachable flag service fails the decision once, with no second write", func(t *testing.T) {
		w, svc := newTestWriter(t)
		svc.err = errors.New("dial tcp: connection refused")
		err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionAdjustVolume, GameID: "game_g"})
		if err == nil {
			t.Fatal("an unreachable flag service must fail the decision (fail closed)")
		}
		if n := len(svc.all()); n != 1 {
			t.Fatalf("writer sent %d writes for one decision, want 1 (no retry)", n)
		}
	})
}

// UC: UC-053 A1 A8
// UC: UC-050 main
func TestRolloutWriter(t *testing.T) {
	t.Run("UC-053/A1 a stage move is a stack overlay on rollout/current_controller_count", func(t *testing.T) {
		svc := &fakeService{}
		w := NewRolloutWriter(svc, discardLogger())
		if err := w.SetControllerCount(StageThreeVariant); err != nil {
			t.Fatal(err)
		}
		got := svc.last(t)
		if got.method != "PUT" || got.set != FlagSetRollout || got.flag != flagCurrentControllerCount ||
			got.write.Variant != StageThreeVariant || got.write.Scope != (flagservice.Scope{}) {
			t.Fatalf("got %+v", got)
		}
	})
	t.Run("UC-053/A5 an unknown stage is refused before any write", func(t *testing.T) {
		svc := &fakeService{}
		if err := NewRolloutWriter(svc, discardLogger()).SetControllerCount("seven"); err == nil || len(svc.all()) != 0 {
			t.Fatalf("err=%v writes=%d", err, len(svc.all()))
		}
	})
	t.Run("UC-053/A8 a failed write is returned", func(t *testing.T) {
		svc := &fakeService{err: errors.New("unreachable")}
		if err := NewRolloutWriter(svc, discardLogger()).SetControllerCount(StageOneVariant); err == nil {
			t.Fatal("want an error")
		}
	})
	t.Run("UC-050/main the ladder walks none, one, three, six, all and stops", func(t *testing.T) {
		if v, val, ok := NextStage(StageThreeValue); !ok || v != StageSixVariant || val != StageSixValue {
			t.Fatalf("NextStage(3)=%s,%d,%v", v, val, ok)
		}
		if _, _, ok := NextStage(StageAllValue); ok {
			t.Fatal("no stage after all")
		}
		if _, ok := StageVariantForValue(4); ok {
			t.Fatal("4 is not a stage")
		}
	})
}

type staticRemediation bool

func (s staticRemediation) RemediationAllowed(context.Context) bool { return bool(s) }

// UC: UC-051 A1 BR-002
func TestRemediationReader(t *testing.T) {
	t.Run("UC-051/BR-002 remediation_allowed on allows", func(t *testing.T) {
		if !NewRemediationReader(staticRemediation(true)).RemediationAllowed() {
			t.Fatal("true flag must allow")
		}
	})
	t.Run("UC-051/A1 remediation_allowed off blocks", func(t *testing.T) {
		if NewRemediationReader(staticRemediation(false)).RemediationAllowed() {
			t.Fatal("false flag must not allow")
		}
	})
	t.Run("UC-051/BR-002 no flag source fails closed", func(t *testing.T) {
		if NewRemediationReader(nil).RemediationAllowed() {
			t.Fatal("no flag source must fail closed")
		}
	})
}

// newTestRolloutWriter returns a RolloutWriter over a recording fake service.
func newTestRolloutWriter(t *testing.T) (*RolloutWriter, *fakeService) {
	t.Helper()
	svc := &fakeService{}
	return NewRolloutWriter(svc, discardLogger()), svc
}

// defaultVariantOf is the variant flagd would serve for flag: the last
// stack-wide variant written to the fake service, else the baseline "none".
func defaultVariantOf(t *testing.T, svc *fakeService, flag string) string {
	t.Helper()
	variant := StageNoneVariant
	for _, c := range svc.all() {
		if c.method == "PUT" && c.flag == flag && c.write.Scope == (flagservice.Scope{}) {
			variant = c.write.Variant
		}
	}
	return variant
}
