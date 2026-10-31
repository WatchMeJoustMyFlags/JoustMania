package state

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/flag-service/fserr"
)

// UC: UC-053 main BR-003 BR-007 Post-S-1
func TestGameScopedWrite_ResolvesInItsGameOnly(t *testing.T) {
	t.Run("UC-053/main a game value resolves for that game and no other", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a", "coord/1", "MOCK0001", "MOCK0002")
		f.register("game_b", "coord/1", "MOCK0003")
		res := f.put(WriteRequest{Set: "interventions", Flag: "music_tempo_override", Value: raw("1.15"),
			Scope: Scope{GameID: "game_a"}, Writer: "agent"})
		if !strings.HasPrefix(res.Variant, "re1_") {
			t.Fatalf("variant %q is not an opaque epoch-carrying name", res.Variant)
		}
		if v, val := f.resolve("music_tempo_override", map[string]any{"gameId": "game_a"}); v != res.Variant || val != 1.15 {
			t.Fatalf("game_a resolved %s=%v, want %s=1.15", v, val, res.Variant)
		}
		for _, ctx := range []map[string]any{{"gameId": "game_b"}, {}} {
			if v, _ := f.resolve("music_tempo_override", ctx); v != "none" {
				t.Fatalf("context %v resolved %s, want the baseline none", ctx, v)
			}
		}
	})
	t.Run("UC-053/main a per-player value resolves for that game and serial only", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a", "coord/1", "MOCK0001", "MOCK0002")
		f.register("game_b", "coord/1", "MOCK0001")
		res := f.put(WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"),
			Scope: Scope{GameID: "game_a", Serial: "MOCK0001"}})
		cases := []struct {
			ctx  map[string]any
			want string
		}{
			{map[string]any{"gameId": "game_a", "targetingKey": "MOCK0001"}, res.Variant},
			{map[string]any{"gameId": "game_a", "targetingKey": "MOCK0002"}, "none"},
			{map[string]any{"gameId": "game_b", "targetingKey": "MOCK0001"}, "none"},
		}
		for _, c := range cases {
			if v, _ := f.resolve("shield_seconds", c.ctx); v != c.want {
				t.Errorf("context %v resolved %s, want %s", c.ctx, v, c.want)
			}
		}
	})
	t.Run("UC-053/main every grant gets a fresh variant, even for the same value", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a", "coord/1", "MOCK0001")
		first := f.put(WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"), Scope: Scope{GameID: "game_a", Serial: "MOCK0001"}})
		second := f.put(WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"), Scope: Scope{GameID: "game_a", Serial: "MOCK0001"}})
		if first.Variant == second.Variant {
			t.Fatalf("a re-grant reused variant %s", first.Variant)
		}
	})
	t.Run("UC-053/main an accepted write bumps the snapshot version and wakes the stream", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		<-f.sub.C // the initial wake-up
		f.register("game_a", "coord/1", "MOCK0001")
		before := f.state.Version()
		res := f.put(WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "game_a"}})
		if res.SnapshotVersion <= before {
			t.Fatalf("snapshot version %d not above %d", res.SnapshotVersion, before)
		}
		select {
		case <-f.sub.C:
		default:
			t.Fatal("the sync stream was not woken")
		}
	})
}

// UC: UC-053 main BR-007
func TestRender_EveryFlagCarriesItsFlagSetId(t *testing.T) {
	t.Run("UC-053/main the merged payload tags every flag with metadata.flagSetId", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		payload, _, err := f.state.Snapshot("")
		if err != nil {
			t.Fatal(err)
		}
		var cfg struct {
			Flags map[string]struct {
				Metadata map[string]any `json:"metadata"`
			} `json:"flags"`
		}
		if err := json.Unmarshal(payload, &cfg); err != nil {
			t.Fatal(err)
		}
		total := 0
		for _, set := range f.state.base.Order {
			for key := range f.state.base.Sets[set].Flags {
				total++
				got := cfg.Flags[key].Metadata["flagSetId"]
				if got != set {
					t.Errorf("%s: metadata.flagSetId=%v, want %s", key, got, set)
				}
			}
		}
		if len(cfg.Flags) != total {
			t.Fatalf("payload holds %d flags, want %d", len(cfg.Flags), total)
		}
	})
	t.Run("UC-053/main a selector narrows the payload to one set", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{served: []string{"system", "observability"}})
		payload, _, err := f.state.Snapshot("observability")
		if err != nil {
			t.Fatal(err)
		}
		var cfg struct {
			Flags    map[string]any `json:"flags"`
			Metadata map[string]any `json:"metadata"`
		}
		_ = json.Unmarshal(payload, &cfg)
		if len(cfg.Flags) != len(f.state.base.Sets["observability"].Flags) || cfg.Metadata["flagSetId"] != "observability" {
			t.Fatalf("selector payload: %d flags, metadata %v", len(cfg.Flags), cfg.Metadata)
		}
		if _, _, err := f.state.Snapshot("agent"); err == nil {
			t.Fatal("an unserved set must not be served")
		}
		all, _, _ := f.state.Snapshot("")
		if containsStr(all, `"decision.backend"`) {
			t.Fatal("an unserved set leaked into the full payload")
		}
	})
}

// UC: UC-053 A1 BR-001
func TestStackOverlay_SubstitutesDefaultVariant(t *testing.T) {
	t.Run("UC-053/A1 a stack overlay points defaultVariant at the declared variant", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		res := f.put(WriteRequest{Set: "agent", Flag: "decision.backend", Variant: "jev", Writer: "stage"})
		if res.Variant != "jev" {
			t.Fatalf("variant %q, want the declared jev", res.Variant)
		}
		if got := f.snapshotFlag("decision.backend")["defaultVariant"]; got != "jev" {
			t.Fatalf("defaultVariant %v, want jev", got)
		}
	})
	t.Run("UC-053/A1 baseline targeting still wins over an overlay (Werewolf sensitivity)", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.put(WriteRequest{Set: "game", Flag: "sensitivity", Variant: "slow"})
		if v, _ := f.resolve("sensitivity", map[string]any{"game_mode": "Werewolf"}); v != "fast" {
			t.Fatalf("Werewolf resolved %s, want the baseline rule's fast", v)
		}
		if v, _ := f.resolve("sensitivity", map[string]any{"game_mode": "JoustFFA"}); v != "slow" {
			t.Fatalf("JoustFFA resolved %s, want the overlay slow", v)
		}
	})
	t.Run("UC-053/A1 a free value gets an opaque o-variant; a TTL expires it", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		res := f.put(WriteRequest{Set: "agent", Flag: "decision.openai.model", Value: raw(`"vendor-model-1"`), TTLSeconds: ttl(60)})
		if !strings.HasPrefix(res.Variant, "oe1_") {
			t.Fatalf("variant %q, want an o<epoch>_<n> name", res.Variant)
		}
		if _, val := f.resolve("decision.openai.model", nil); val != "vendor-model-1" {
			t.Fatalf("resolved %v", val)
		}
		f.clock.Advance(61 * time.Second)
		if n := f.state.ExpireOverlays(); n != 1 {
			t.Fatalf("expired %d overlays, want 1", n)
		}
		// The flag that was written, not a neighbour: after the TTL it falls back
		// to the baseline "unset" variant (empty model), not the o-variant.
		if v, val := f.resolve("decision.openai.model", nil); v != "unset" || val != "" {
			t.Fatalf("after the TTL decision.openai.model resolved %s=%v, want the baseline unset=\"\"", v, val)
		}
	})
	t.Run("UC-053/A1 DELETE ?writer=stage clears that writer's overlays only", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.put(WriteRequest{Set: "agent", Flag: "decision.backend", Variant: "jev", Writer: "stage"})
		f.put(WriteRequest{Set: "agent", Flag: "enabled", Variant: "on", Writer: "stage"})
		f.put(WriteRequest{Set: "agent", Flag: "rollout_enabled", Variant: "on", Writer: "operator"})
		n, err := f.state.ClearWriter("agent", "stage")
		if err != nil || n != 2 {
			t.Fatalf("cleared %d (%v), want 2", n, err)
		}
		if f.snapshotFlag("rollout_enabled")["defaultVariant"] != "on" {
			t.Fatal("another writer's overlay was cleared")
		}
	})
	t.Run("UC-053/A1 a value equal to a declared variant selects that variant", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		res := f.put(WriteRequest{Set: "agent", Flag: "policy.max_interventions_per_minute", Value: raw("4")})
		if res.Variant != "aggressive" {
			t.Fatalf("variant %q, want the declared aggressive (= 4)", res.Variant)
		}
	})
}

// UC: UC-053 A5 BR-001 BR-002
func TestPut_Rejections(t *testing.T) {
	f := newFixture(t, fixtureOpts{served: []string{"system", "observability", "interventions", "agent", "game", "user", "controller"}})
	f.register("game_a", "coord/1", "MOCK0001")
	cases := []struct {
		name   string
		req    WriteRequest
		status int
		reason string
	}{
		{"UC-053/A5 unknown flag set", WriteRequest{Set: "nope", Flag: "x", Variant: "a"}, http.StatusNotFound, fserr.UnknownFlagSet},
		{"UC-053/A5 unknown flag", WriteRequest{Set: "agent", Flag: "nope", Variant: "a"}, http.StatusNotFound, fserr.UnknownFlag},
		{"UC-053/A5 flag of another set", WriteRequest{Set: "agent", Flag: "sensitivity", Variant: "slow"}, http.StatusNotFound, fserr.UnknownFlag},
		{"UC-053/A5 BR-001 read-only flag", WriteRequest{Set: "system", Flag: "idle.enabled", Variant: "off"}, http.StatusUnprocessableEntity, fserr.FlagReadOnly},
		{"UC-053/A5 both variant and value", WriteRequest{Set: "agent", Flag: "enabled", Variant: "on", Value: raw("true")}, http.StatusBadRequest, fserr.Malformed},
		{"UC-053/A5 neither variant nor value", WriteRequest{Set: "agent", Flag: "enabled"}, http.StatusBadRequest, fserr.Malformed},
		{"UC-053/A5 stack flag with a game scope", WriteRequest{Set: "agent", Flag: "enabled", Variant: "on", Scope: Scope{GameID: "game_a"}}, http.StatusBadRequest, fserr.Malformed},
		{"UC-053/A5 game flag without a game", WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5")}, http.StatusBadRequest, fserr.Malformed},
		{"UC-053/A5 grant without a serial", WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"), Scope: Scope{GameID: "game_a"}}, http.StatusBadRequest, fserr.Malformed},
		{"UC-053/A5 arm without an experiment id", WriteRequest{Set: "agent", Flag: "enabled", Variant: "on", Scope: Scope{Arm: "experimental"}}, http.StatusBadRequest, fserr.Malformed},
		{"UC-053/A5 event flag written with PUT", WriteRequest{Set: "interventions", Flag: "end_game", Variant: "none", Scope: Scope{GameID: "game_a"}}, http.StatusBadRequest, fserr.Malformed},
		{"UC-053/A5 value above the range", WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("1.5"), Scope: Scope{GameID: "game_a"}}, http.StatusUnprocessableEntity, fserr.OutOfBounds},
		{"UC-053/A5 non-integer for an int range", WriteRequest{Set: "interventions", Flag: "global_sensitivity_override", Value: raw("2.5"), Scope: Scope{GameID: "game_a"}}, http.StatusUnprocessableEntity, fserr.OutOfBounds},
		{"UC-053/A5 undeclared variant", WriteRequest{Set: "agent", Flag: "decision.backend", Variant: "gpt"}, http.StatusUnprocessableEntity, fserr.OutOfBounds},
		{"UC-053/A5 declared variant outside the allowed list", WriteRequest{Set: "interventions", Flag: "pacing_profile", Variant: "none", Scope: Scope{GameID: "game_a"}}, http.StatusUnprocessableEntity, fserr.OutOfBounds},
		{"UC-053/A5 malformed composite value", WriteRequest{Set: "interventions", Flag: "tempo_ramp", Value: raw(`"1.3:8:bounce"`), Scope: Scope{GameID: "game_a"}}, http.StatusUnprocessableEntity, fserr.OutOfBounds},
		{"UC-053/A5 a free string that is not a string", WriteRequest{Set: "agent", Flag: "decision.openai.base_url", Value: raw("42")}, http.StatusUnprocessableEntity, fserr.OutOfBounds},
		{"UC-053/A5 persist on a non-persistable flag", WriteRequest{Set: "agent", Flag: "enabled", Variant: "on", Persist: true}, http.StatusBadRequest, fserr.Malformed},
		{"UC-053/A5 persist with a TTL", WriteRequest{Set: "game", Flag: "sensitivity", Variant: "slow", Persist: true, TTLSeconds: ttl(10)}, http.StatusBadRequest, fserr.Malformed},
		{"UC-053/A5 TTL on a game value", WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "game_a"}, TTLSeconds: ttl(10)}, http.StatusBadRequest, fserr.Malformed},
		{"UC-053/A5 fraction outside the bounds", WriteRequest{Set: "controller", Flag: "bluetooth_backend", Variant: "rust", Scope: Scope{FractionPercent: intp(0)}}, http.StatusUnprocessableEntity, fserr.OutOfBounds},
		{"UC-053/A5 serial not in the game", WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"), Scope: Scope{GameID: "game_a", Serial: "MOCK0099"}}, http.StatusUnprocessableEntity, fserr.SerialNotInGame},
		{"UC-053/A6 game not registered", WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "game_zz"}}, http.StatusConflict, fserr.GameNotLive},
		{"UC-053/A5 set still on its file source", WriteRequest{Set: "rollout", Flag: "current_controller_count", Variant: "one"}, http.StatusConflict, fserr.SetNotServed},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			before := f.state.Version()
			_, err := f.state.Put(c.req)
			wantReason(t, err, c.status, c.reason)
			if f.state.Version() != before {
				t.Fatal("a rejected write changed the configuration")
			}
		})
	}
}

// UC: UC-053 A7
func TestPut_NoSyncStream(t *testing.T) {
	t.Run("UC-053/A7 a write is rejected while flagd has no open stream", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{noStream: true})
		_, err := f.state.Put(WriteRequest{Set: "agent", Flag: "enabled", Variant: "on"})
		wantReason(t, err, http.StatusServiceUnavailable, fserr.NoSyncStream)
	})
	t.Run("UC-053/A7 the game lifecycle is exempt", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{noStream: true})
		if _, err := f.state.RegisterGame(Game{ID: "game_a", CoordinatorInstance: "c/1", Serials: []string{"S1"}}); err != nil {
			t.Fatalf("register without a stream: %v", err)
		}
		if _, err := f.state.Reconcile("c/1", nil); err != nil {
			t.Fatalf("reconcile without a stream: %v", err)
		}
	})
	t.Run("UC-053/A7 a stream for another set does not count", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{noStream: true})
		sub, _ := f.state.Subscribe("system")
		defer sub.Close()
		_, err := f.state.Put(WriteRequest{Set: "agent", Flag: "enabled", Variant: "on"})
		wantReason(t, err, http.StatusServiceUnavailable, fserr.NoSyncStream)
	})
}

// UC: UC-053 A4 BR-006
func TestAppend_EventList(t *testing.T) {
	t.Run("UC-053/A4 events append with increasing seq, per game, carrying the epoch", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{k: 3})
		f.register("game_a", "c/1", "MOCK0001", "MOCK0002")
		f.register("game_b", "c/1", "MOCK0003")
		for i, serial := range []string{"MOCK0001", "MOCK0002"} {
			res, err := f.state.Append(AppendRequest{Set: "interventions", Flag: "eliminate_player", Scope: Scope{GameID: "game_a"}, Payload: map[string]any{"serial": serial}})
			if err != nil || res.Seq != int64(i+1) {
				t.Fatalf("append %d: seq %d err %v", i, res.Seq, err)
			}
		}
		_, val := f.resolve("eliminate_player", map[string]any{"gameId": "game_a"})
		list := val.(map[string]any)
		events := list["events"].([]any)
		if list["epoch"] != "e1" || list["seq"] != float64(2) || len(events) != 2 {
			t.Fatalf("event list %v", list)
		}
		if p := events[1].(map[string]any)["payload"].(map[string]any); p["serial"] != "MOCK0002" {
			t.Fatalf("second event payload %v: a burst for different players must not collapse", p)
		}
		if v, _ := f.resolve("eliminate_player", map[string]any{"gameId": "game_b"}); v != "none" {
			t.Fatalf("game_b resolved %s, want the baseline", v)
		}
	})
	t.Run("UC-053/A4 the list keeps the last K events", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{k: 3})
		f.register("game_a", "c/1")
		for i := 0; i < 5; i++ {
			if _, err := f.state.Append(AppendRequest{Set: "interventions", Flag: "audio_cue", Scope: Scope{GameID: "game_a"}, Payload: map[string]any{"sound": "agent_cue"}}); err != nil {
				t.Fatal(err)
			}
		}
		_, val := f.resolve("audio_cue", map[string]any{"gameId": "game_a"})
		events := val.(map[string]any)["events"].([]any)
		if len(events) != 3 || events[0].(map[string]any)["seq"] != float64(3) {
			t.Fatalf("events %v, want seq 3..5", events)
		}
	})
	t.Run("UC-053/A4 a cleared list keeps its seq, so the next event is not dropped", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{k: 3})
		f.register("game_a", "c/1", "MOCK0001")
		for i := 0; i < 2; i++ {
			if _, err := f.state.Append(AppendRequest{Set: "interventions", Flag: "audio_cue", Scope: Scope{GameID: "game_a"}, Payload: map[string]any{"sound": "agent_cue"}}); err != nil {
				t.Fatal(err)
			}
		}
		res, err := f.state.Clear(ClearRequest{Set: "interventions", Flag: "audio_cue", Scope: Scope{GameID: "game_a"}})
		if err != nil || res.Cleared != 1 {
			t.Fatalf("clear: %+v %v", res, err)
		}
		_, val := f.resolve("audio_cue", map[string]any{"gameId": "game_a"})
		if l := val.(map[string]any); len(l["events"].([]any)) != 0 || l["seq"] != float64(2) {
			t.Fatalf("after clear %v, want no events and seq 2", l)
		}
		next, err := f.state.Append(AppendRequest{Set: "interventions", Flag: "audio_cue", Scope: Scope{GameID: "game_a"}, Payload: map[string]any{"sound": "agent_cue"}})
		if err != nil || next.Seq != 3 {
			t.Fatalf("append after clear: seq %d err %v, want 3 (a restart at 1 is dropped by the coordinator)", next.Seq, err)
		}
	})
	t.Run("UC-053/A7 an append without a sync stream is refused", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{noStream: true})
		if _, err := f.state.RegisterGame(Game{ID: "game_a", CoordinatorInstance: "c/1"}); err != nil {
			t.Fatal(err)
		}
		_, err := f.state.Append(AppendRequest{Set: "interventions", Flag: "audio_cue", Scope: Scope{GameID: "game_a"}, Payload: map[string]any{"sound": "agent_cue"}})
		wantReason(t, err, http.StatusServiceUnavailable, fserr.NoSyncStream)
	})
	t.Run("UC-053/A4 payload rules", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a", "c/1", "MOCK0001")
		bad := []struct {
			flag    string
			payload map[string]any
			status  int
			reason  string
		}{
			{"eliminate_player", map[string]any{"serial": "MOCK0009"}, http.StatusUnprocessableEntity, fserr.SerialNotInGame},
			{"eliminate_player", map[string]any{}, http.StatusBadRequest, fserr.Malformed},
			{"controller_effect", map[string]any{"serial": "", "effect": "explode"}, http.StatusUnprocessableEntity, fserr.OutOfBounds},
			{"audio_cue", map[string]any{"sound": "../../etc/passwd"}, http.StatusUnprocessableEntity, fserr.OutOfBounds},
			{"end_game", map[string]any{"serial": "MOCK0001"}, http.StatusUnprocessableEntity, fserr.OutOfBounds},
		}
		for _, b := range bad {
			_, err := f.state.Append(AppendRequest{Set: "interventions", Flag: b.flag, Scope: Scope{GameID: "game_a"}, Payload: b.payload})
			wantReason(t, err, b.status, b.reason)
		}
		if _, err := f.state.Append(AppendRequest{Set: "interventions", Flag: "controller_effect", Scope: Scope{GameID: "game_a"}, Payload: map[string]any{"serial": "", "effect": "rumble"}}); err != nil {
			t.Fatalf("a broadcast effect: %v", err)
		}
		if _, err := f.state.Append(AppendRequest{Set: "interventions", Flag: "end_game", Scope: Scope{GameID: "game_a"}}); err != nil {
			t.Fatalf("end_game with an empty payload: %v", err)
		}
		_, err := f.state.Append(AppendRequest{Set: "interventions", Flag: "end_game", Scope: Scope{GameID: "game_zz"}})
		wantReason(t, err, http.StatusConflict, fserr.GameNotLive)
		_, err = f.state.Append(AppendRequest{Set: "interventions", Flag: "volume_override", Scope: Scope{GameID: "game_a"}})
		wantReason(t, err, http.StatusBadRequest, fserr.Malformed)
	})
}

// UC: UC-053 main BR-003 Post-S-3
// UC: UC-040 A25 BR-016
func TestEndGame_DropsScopeAndValues(t *testing.T) {
	t.Run("UC-053/main after the game ends nothing of it is left", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a", "c/1", "MOCK0001")
		f.put(WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "game_a"}})
		f.put(WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("3"), Scope: Scope{GameID: "game_a", Serial: "MOCK0001"}})
		if _, err := f.state.Append(AppendRequest{Set: "interventions", Flag: "end_game", Scope: Scope{GameID: "game_a"}}); err != nil {
			t.Fatal(err)
		}
		end, found := f.state.EndGame("game_a", EndReasonEnded)
		if !found || end.DroppedValues != 3 {
			t.Fatalf("end %+v found %v, want 3 dropped values", end, found)
		}
		payload, _, _ := f.state.Snapshot("")
		if containsStr(payload, "game_a") {
			t.Fatal("the configuration still mentions game_a")
		}
		_, err := f.state.Put(WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "game_a"}})
		wantReason(t, err, http.StatusConflict, fserr.GameNotLive)
		if f.state.Stats().ScopeValues != 0 {
			t.Fatal("scope values remain")
		}
	})
	t.Run("UC-053/main a repeat registration is already_registered and keeps the values", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a", "c/1", "MOCK0001")
		f.put(WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "game_a"}})
		already, err := f.state.RegisterGame(Game{ID: "game_a", CoordinatorInstance: "c/1", Serials: []string{"MOCK0001", "MOCK0002"}})
		if err != nil || !already {
			t.Fatalf("already %v err %v", already, err)
		}
		if f.state.Stats().ScopeValues != 1 {
			t.Fatal("re-registration dropped values")
		}
		if !f.state.Game("game_a").HasSerial("MOCK0002") {
			t.Fatal("re-registration did not update the serials")
		}
	})
}

// UC: UC-053 A13
func TestReregistration_DropsLeftPlayersValues(t *testing.T) {
	t.Run("UC-053/A13 a registration without a serial drops that player's values, keeps the rest", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a", "c/1", "MOCK0001", "MOCK0002")
		f.put(WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"), Scope: Scope{GameID: "game_a", Serial: "MOCK0001"}})
		kept := f.put(WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("7"), Scope: Scope{GameID: "game_a", Serial: "MOCK0002"}})
		f.put(WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "game_a"}})
		registeredAt := f.state.Game("game_a").RegisteredAt
		f.clock.Advance(10 * time.Second)

		already, err := f.state.RegisterGame(Game{ID: "game_a", CoordinatorInstance: "c/1", Serials: []string{"MOCK0002"}})
		if err != nil || !already {
			t.Fatalf("already %v err %v", already, err)
		}
		if got := f.state.Stats().ScopeValues; got != 2 {
			t.Fatalf("%d scope values, want 2 (MOCK0002's and the game-wide one)", got)
		}
		ctx := func(serial string) map[string]any {
			return map[string]any{"gameId": "game_a", "targetingKey": serial}
		}
		if v, _ := f.resolve("shield_seconds", ctx("MOCK0001")); v != "none" {
			t.Fatalf("MOCK0001 still resolves its dropped value (variant %s)", v)
		}
		if v, _ := f.resolve("shield_seconds", ctx("MOCK0002")); v != kept.Variant {
			t.Fatalf("MOCK0002 resolves %s, want its kept %s", v, kept.Variant)
		}
		if !f.state.Game("game_a").RegisteredAt.Equal(registeredAt) {
			t.Fatal("re-registration restarted the game's age")
		}
	})
}

// UC: UC-053 A10
func TestReconcile_OnlyThatInstance(t *testing.T) {
	t.Run("UC-053/A10 a reconcile from instance A leaves instance B's games intact", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a1", "coordA/1", "S1")
		f.register("game_a2", "coordA/1", "S2")
		f.register("game_b1", "coordB/1", "S3")
		f.put(WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "game_a2"}})
		ends, err := f.state.Reconcile("coordA/1", []Game{{ID: "game_a1", Kind: "real", Serials: []string{"S1"}}})
		if err != nil {
			t.Fatal(err)
		}
		if len(ends) != 1 || ends[0].GameID != "game_a2" || ends[0].Reason != EndReasonReconciled || ends[0].DroppedValues != 1 {
			t.Fatalf("ends %+v, want game_a2 reconciled with 1 value", ends)
		}
		if f.state.Game("game_b1") == nil || f.state.Game("game_a1") == nil {
			t.Fatal("a kept or foreign game was dropped")
		}
	})
	t.Run("UC-053/A10 a restarted coordinator's empty reconcile drops all its games", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a1", "coordA/1", "S1")
		f.register("game_b1", "coordB/1", "S3")
		ends, _ := f.state.Reconcile("coordA/1", nil)
		if len(ends) != 1 || f.state.Game("game_b1") == nil {
			t.Fatalf("ends %+v", ends)
		}
	})
	t.Run("UC-053/A10 reconcile registers the listed games", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		if _, err := f.state.Reconcile("coordA/2", []Game{{ID: "game_new", Serials: []string{"S1"}}}); err != nil {
			t.Fatal(err)
		}
		if g := f.state.Game("game_new"); g == nil || g.CoordinatorInstance != "coordA/2" {
			t.Fatalf("game %+v", g)
		}
		if _, err := f.state.Reconcile("coordA/2", []Game{{ID: "x", CoordinatorInstance: "other/1"}}); err == nil {
			t.Fatal("a game of another instance must be rejected")
		}
	})
}

// UC: UC-053 A11
// UC: UC-040 BR-016
func TestExpireGames(t *testing.T) {
	t.Run("UC-053/A11 a scope older than the maximum game age is dropped", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_old", "c/1", "S1")
		f.clock.Advance(30 * time.Minute)
		f.register("game_new", "c/1", "S2")
		f.clock.Advance(31 * time.Minute)
		ends := f.state.ExpireGames()
		if len(ends) != 1 || ends[0].GameID != "game_old" || ends[0].Reason != EndReasonExpired {
			t.Fatalf("ends %+v", ends)
		}
		if f.state.Game("game_new") == nil {
			t.Fatal("a young game expired")
		}
	})
}

// UC: UC-053 A11
func TestExpireGames_LastSeen(t *testing.T) {
	t.Run("UC-053/A11 a game reconciled every few minutes never expires", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_long", "c/1", "S1")
		registeredAt := f.state.Game("game_long").RegisteredAt
		for i := 0; i < 30; i++ { // 2.5 h in 5 min steps
			f.clock.Advance(5 * time.Minute)
			if _, err := f.state.Reconcile("c/1", []Game{{ID: "game_long", Serials: []string{"S1"}}}); err != nil {
				t.Fatal(err)
			}
			if ends := f.state.ExpireGames(); len(ends) != 0 {
				t.Fatalf("step %d: a reconciled game expired: %+v", i, ends)
			}
		}
		if !f.state.Game("game_long").RegisteredAt.Equal(registeredAt) {
			t.Fatal("registered_at changed")
		}
	})

	t.Run("UC-053/A11 a re-registration refreshes last-seen", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a", "c/1", "S1")
		f.clock.Advance(50 * time.Minute)
		f.register("game_a", "c/1", "S1")
		f.clock.Advance(50 * time.Minute)
		if ends := f.state.ExpireGames(); len(ends) != 0 {
			t.Fatalf("expired %+v", ends)
		}
	})

	t.Run("UC-053/A11 a game not seen for the maximum age expires", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a", "c/1", "S1")
		f.clock.Advance(30 * time.Minute)
		f.register("game_a", "c/1", "S1")
		f.clock.Advance(61 * time.Minute)
		ends := f.state.ExpireGames()
		if len(ends) != 1 || ends[0].Reason != EndReasonExpired {
			t.Fatalf("ends %+v", ends)
		}
	})
}

// UC: UC-053 A3
func TestExperimentArm_NeverResolvesForReal(t *testing.T) {
	t.Run("UC-053/A3 an arm value resolves only for non-real games of that experiment and arm", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		// The registry declares no experiment+arm row before #1415; give one flag the scope for the test.
		f.state.flags["shadow_policy"].policy.Scopes = append(f.state.flags["shadow_policy"].policy.Scopes, "experiment+arm")
		res := f.put(WriteRequest{Set: "game", Flag: "shadow_policy", Variant: "block", Scope: Scope{ExperimentID: "exp_42", Arm: "experimental"}})
		cases := []struct {
			ctx  map[string]any
			want string
		}{
			{map[string]any{"game_kind": "shadow", "experiment_id": "exp_42", "arm": "experimental"}, res.Variant},
			{map[string]any{"game_kind": "real", "experiment_id": "exp_42", "arm": "experimental"}, "allow"},
			{map[string]any{"game_kind": "shadow", "experiment_id": "exp_42", "arm": "control"}, "allow"},
			{map[string]any{"game_kind": "shadow", "experiment_id": "exp_7", "arm": "experimental"}, "allow"},
		}
		for _, c := range cases {
			if v, _ := f.resolve("shadow_policy", c.ctx); v != c.want {
				t.Errorf("context %v resolved %s, want %s", c.ctx, v, c.want)
			}
		}
		if _, err := f.state.Clear(ClearRequest{Set: "game", Flag: "shadow_policy", Scope: Scope{ExperimentID: "exp_42", Arm: "experimental"}}); err != nil {
			t.Fatal(err)
		}
		if v, _ := f.resolve("shadow_policy", cases[0].ctx); v != "allow" {
			t.Fatal("the cleared arm value still resolves")
		}
	})
}

// UC: UC-053 A9
func TestRestart_NewEpochNeverReusesVariants(t *testing.T) {
	t.Run("UC-053/A9 a restart starts from the baseline with a new epoch", func(t *testing.T) {
		before := newFixture(t, fixtureOpts{epoch: "aaaa"})
		before.register("game_a", "c/1", "S1")
		r1 := before.put(WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"), Scope: Scope{GameID: "game_a", Serial: "S1"}})
		after := newFixture(t, fixtureOpts{epoch: "bbbb"})
		after.register("game_a", "c/1", "S1")
		r2 := after.put(WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"), Scope: Scope{GameID: "game_a", Serial: "S1"}})
		if r1.Variant == r2.Variant || !strings.Contains(r2.Variant, "bbbb") {
			t.Fatalf("variants %s and %s", r1.Variant, r2.Variant)
		}
		if after.state.Stats().LiveGames != 1 || after.state.Stats().ScopeValues != 1 {
			t.Fatal("the restarted state carried values over")
		}
	})
}

// UC: UC-053 A2 A12 BR-004
func TestAdminPersistence(t *testing.T) {
	newPersisting := func(t *testing.T, path, epoch string) *fixture {
		return newFixture(t, fixtureOpts{stateFile: path, epoch: epoch})
	}
	t.Run("UC-053/A2 a persisted admin value survives a restart, nothing else does", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		f := newPersisting(t, path, "e1")
		res := f.put(WriteRequest{Set: "game", Flag: "sensitivity", Variant: "slow", Persist: true, Writer: "menu"})
		if !res.Persisted {
			t.Fatal("the result does not say persisted")
		}
		f.put(WriteRequest{Set: "agent", Flag: "decision.backend", Variant: "jev", Writer: "stage"})
		f.put(WriteRequest{Set: "agent", Flag: "interventions_allowed", Variant: "full", Writer: "test"}) // persistable flag, no persist asked
		g := newPersisting(t, path, "e2")
		if v := g.snapshotFlag("sensitivity")["defaultVariant"]; v != "slow" {
			t.Fatalf("sensitivity after restart %v, want slow", v)
		}
		if v := g.snapshotFlag("decision.backend")["defaultVariant"]; v != "rules" {
			t.Fatalf("decision.backend after restart %v, want the baseline", v)
		}
		if v := g.snapshotFlag("interventions_allowed")["defaultVariant"]; v != "ambient" {
			t.Fatalf("interventions_allowed after restart %v: a write without persist must not persist", v)
		}
	})
	t.Run("UC-053/A2 an in-memory overlay sits on top of the admin value and clearing it shows the admin value", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		f := newPersisting(t, path, "e1")
		f.put(WriteRequest{Set: "agent", Flag: "interventions_allowed", Variant: "standard", Persist: true, Writer: "menu"})
		f.put(WriteRequest{Set: "agent", Flag: "interventions_allowed", Variant: "full", Writer: "stage"})
		if v := f.snapshotFlag("interventions_allowed")["defaultVariant"]; v != "full" {
			t.Fatalf("got %v, want the stage overlay", v)
		}
		if _, err := f.state.ClearWriter("agent", "stage"); err != nil {
			t.Fatal(err)
		}
		if v := f.snapshotFlag("interventions_allowed")["defaultVariant"]; v != "standard" {
			t.Fatalf("got %v, want the admin value back", v)
		}
		if _, err := f.state.Clear(ClearRequest{Set: "agent", Flag: "interventions_allowed", Persist: true}); err != nil {
			t.Fatal(err)
		}
		g := newPersisting(t, path, "e2")
		if v := g.snapshotFlag("interventions_allowed")["defaultVariant"]; v != "ambient" {
			t.Fatalf("after DELETE ?persist=true and a restart: %v, want the baseline", v)
		}
	})
	t.Run("UC-053/A2 the write is atomic: no temp file is left", func(t *testing.T) {
		dir := t.TempDir()
		f := newPersisting(t, filepath.Join(dir, "admin.json"), "e1")
		f.put(WriteRequest{Set: "user", Flag: "current_game", Variant: "Werewolf", Persist: true})
		entries, _ := os.ReadDir(dir)
		if len(entries) != 1 || entries[0].Name() != "admin.json" {
			t.Fatalf("directory holds %v", entries)
		}
	})
	t.Run("UC-053/A12 a corrupt state file is moved aside and the service starts from the baseline", func(t *testing.T) {
		dir := t.TempDir()
		path := filepath.Join(dir, "admin.json")
		if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
			t.Fatal(err)
		}
		base := newFixture(t, fixtureOpts{}).state.base
		_, report, err := New(base, newFixture(t, fixtureOpts{}).state.reg, Options{Epoch: "e9", StateFile: path, Now: func() time.Time { return time.Unix(1700000000, 0) }})
		if err != nil {
			t.Fatalf("a corrupt state file refused the start: %v", err)
		}
		if !report.Corrupt || report.CorruptPath != path+".corrupt-1700000000" {
			t.Fatalf("report %+v", report)
		}
		if _, err := os.Stat(report.CorruptPath); err != nil {
			t.Fatalf("the corrupt file was not moved aside: %v", err)
		}
	})
	t.Run("UC-053/A12 a state file with a version below the supported one is moved aside as corrupt", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		doc := `{"version":0,"values":[{"flag_set":"game","flag":"sensitivity","variant":"slow","written_at":"2026-10-16T12:00:00Z"}]}`
		if err := os.WriteFile(path, []byte(doc), 0o600); err != nil {
			t.Fatal(err)
		}
		f := newFixture(t, fixtureOpts{})
		g, report, err := New(f.state.base, f.state.reg, Options{Epoch: "e9", Served: f.state.base.Order, StateFile: path,
			Now: func() time.Time { return time.Unix(1700000000, 0) }, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
		if err != nil {
			t.Fatalf("an unsupported version refused the start: %v", err)
		}
		if !report.Corrupt || report.CorruptPath != path+".corrupt-1700000000" || report.AdminValues != 0 {
			t.Fatalf("report %+v, want corrupt, moved aside, no admin values", report)
		}
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatalf("the file was not moved aside: %v", err)
		}
		if _, err := os.Stat(report.CorruptPath); err != nil {
			t.Fatalf("no corrupt copy: %v", err)
		}
		if v := (&fixture{t: t, state: g}).snapshotFlag("sensitivity")["defaultVariant"]; v == "slow" {
			t.Fatal("a value from an unsupported-version file was applied")
		}
	})
	t.Run("UC-053/A12 a corrupt file whose rename fails stays in place and the error is logged", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
			t.Fatal(err)
		}
		orig := moveAside
		moveAside = func(string, string) error { return errors.New("rename refused") }
		t.Cleanup(func() { moveAside = orig })
		var logs bytes.Buffer
		f := newFixture(t, fixtureOpts{})
		_, report, err := New(f.state.base, f.state.reg, Options{Epoch: "e9", Served: f.state.base.Order, StateFile: path,
			Logger: slog.New(slog.NewTextHandler(&logs, nil))})
		if err != nil {
			t.Fatalf("a failed rename refused the start: %v", err)
		}
		if !report.Corrupt || report.CorruptPath != "" {
			t.Fatalf("report %+v, want corrupt with no corrupt path", report)
		}
		if raw, err := os.ReadFile(path); err != nil || string(raw) != "{not json" {
			t.Fatalf("the corrupt file did not stay in place: %q %v", raw, err)
		}
		if m, _ := filepath.Glob(path + ".corrupt-*"); len(m) != 0 {
			t.Fatalf("corrupt copies %v after a failed rename", m)
		}
		if out := logs.String(); !strings.Contains(out, "level=ERROR") || !strings.Contains(out, "could not be moved aside") || !strings.Contains(out, "rename refused") {
			t.Fatalf("the failed move was not logged as an error:\n%s", out)
		}
	})
	t.Run("UC-053/A2 stale entries are dropped, the rest load", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		doc := `{"version":1,"values":[
			{"flag_set":"game","flag":"sensitivity","variant":"fast","written_at":"2026-10-16T12:00:00Z"},
			{"flag_set":"game","flag":"num_teams","variant":"eleven","written_at":"2026-10-16T12:00:00Z"},
			{"flag_set":"agent","flag":"decision.backend","variant":"jev","written_at":"2026-10-16T12:00:00Z"},
			{"flag_set":"game","flag":"gone_flag","variant":"x","written_at":"2026-10-16T12:00:00Z"}]}`
		if err := os.WriteFile(path, []byte(doc), 0o600); err != nil {
			t.Fatal(err)
		}
		fx := newFixture(t, fixtureOpts{})
		_, report, err := New(fx.state.base, fx.state.reg, Options{Epoch: "e3", StateFile: path})
		if err != nil {
			t.Fatal(err)
		}
		// decision.backend is persistable since #1682 (UC-044 Post-S-2): it loads with sensitivity.
		if report.AdminValues != 2 || len(report.Dropped) != 2 {
			t.Fatalf("report %+v, want 2 restored and 2 dropped", report)
		}
	})
	t.Run("UC-053/A2 an unwritable state file refuses the persisted write and rolls it back (#2368)", func(t *testing.T) {
		dir := t.TempDir()
		blocker := filepath.Join(dir, "file")
		if err := os.WriteFile(blocker, nil, 0o600); err != nil {
			t.Fatal(err)
		}
		f := newPersisting(t, filepath.Join(blocker, "admin.json"), "e1") // parent is a file
		_, err := f.state.Put(WriteRequest{Set: "game", Flag: "num_teams", Variant: "four", Persist: true})
		if err == nil || err.Status != 500 || err.Reason != fserr.ReasonPersistFailed {
			t.Fatalf("err = %v, want 500 persist_failed", err)
		}
		if f.state.PersistErrors() != 1 {
			t.Fatalf("persist errors %d, want 1", f.state.PersistErrors())
		}
		if v := f.snapshotFlag("num_teams")["defaultVariant"]; v == "four" {
			t.Fatalf("the refused write still applies in memory: %v", v)
		}
	})
	t.Run("UC-053/A2 a persisted write that fails to save keeps the earlier admin value and overlay (#2368)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		f := newPersisting(t, path, "e1")
		f.put(WriteRequest{Set: "game", Flag: "num_teams", Variant: "two", Persist: true, Writer: "menu"})
		f.put(WriteRequest{Set: "game", Flag: "num_teams", Variant: "three", Writer: "stage"}) // stack overlay
		breakStateFile(t, path)
		_, err := f.state.Put(WriteRequest{Set: "game", Flag: "num_teams", Variant: "four", Persist: true, Writer: "menu"})
		if err == nil || err.Status != 500 {
			t.Fatalf("err = %v, want 500", err)
		}
		if v := f.snapshotFlag("num_teams")["defaultVariant"]; v != "three" {
			t.Fatalf("got %v, want the stage overlay back", v)
		}
		if _, cerr := f.state.ClearWriter("game", "stage"); cerr != nil {
			t.Fatal(cerr)
		}
		if v := f.snapshotFlag("num_teams")["defaultVariant"]; v != "two" {
			t.Fatalf("got %v, want the earlier admin value two", v)
		}
	})
	t.Run("UC-053/A2 a persisted clear that fails to save keeps the admin value and is not counted persisted (#2368)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		f := newPersisting(t, path, "e1")
		f.put(WriteRequest{Set: "game", Flag: "num_teams", Variant: "four", Persist: true, Writer: "menu"})
		breakStateFile(t, path)
		res, err := f.state.Clear(ClearRequest{Set: "game", Flag: "num_teams", Persist: true})
		if err == nil || err.Status != 500 || err.Reason != fserr.ReasonPersistFailed {
			t.Fatalf("err = %v, want 500 persist_failed", err)
		}
		if res.Persisted || res.Cleared != 0 {
			t.Fatalf("result %+v reports a persisted clear", res)
		}
		if f.state.PersistErrors() != 1 {
			t.Fatalf("persist errors %d, want 1", f.state.PersistErrors())
		}
		if v := f.snapshotFlag("num_teams")["defaultVariant"]; v != "four" {
			t.Fatalf("got %v: the memory must keep what the file still holds", v)
		}
	})
	t.Run("UC-053/A2 a kill switch's persisted write applies despite a failing save and reports persisted false (#2368)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		f := newPersisting(t, path, "e1")
		f.put(WriteRequest{Set: "agent", Flag: "interventions_allowed", Variant: "full", Persist: true, Writer: "menu"})
		breakStateFile(t, path)
		res, err := f.state.Put(WriteRequest{Set: "agent", Flag: "interventions_allowed", Variant: "none", Persist: true, Writer: "menu"})
		if err != nil {
			t.Fatalf("err = %v: a kill switch must not be refused on a failed save", err)
		}
		if res.Persisted {
			t.Fatal("reported persisted although the save failed")
		}
		if f.state.PersistErrors() != 1 {
			t.Fatalf("persist errors %d, want 1", f.state.PersistErrors())
		}
		if v := f.snapshotFlag("interventions_allowed")["defaultVariant"]; v != "none" {
			t.Fatalf("got %v, want the kill switch none applied in memory", v)
		}
		if st := f.state.Stats(); st.PersistedValues != 0 || st.OverlaysBySet["agent"] != 1 {
			t.Fatalf("persisted overlays %d, agent in-memory overlays %d: an in-memory kill switch must not count as persisted", st.PersistedValues, st.OverlaysBySet["agent"])
		}
	})
	t.Run("UC-053/A2 a kill switch's persisted clear applies despite a failing save and is flagged not persisted (#2368)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		f := newPersisting(t, path, "e1")
		f.put(WriteRequest{Set: "agent", Flag: "interventions_allowed", Variant: "full", Persist: true, Writer: "menu"})
		breakStateFile(t, path)
		res, err := f.state.Clear(ClearRequest{Set: "agent", Flag: "interventions_allowed", Persist: true})
		if err != nil {
			t.Fatalf("err = %v: a kill switch clear must not be refused on a failed save", err)
		}
		if res.Cleared != 1 || res.Persisted || !res.PersistFailed {
			t.Fatalf("result %+v, want cleared 1, persisted false, persist failed", res)
		}
		if f.state.PersistErrors() != 1 {
			t.Fatalf("persist errors %d, want 1", f.state.PersistErrors())
		}
		if v := f.snapshotFlag("interventions_allowed")["defaultVariant"]; v == "full" {
			t.Fatalf("got %v: the cleared kill switch value still applies", v)
		}
	})
	t.Run("UC-053/A2 a later successful save marks an in-memory kill switch persisted again (#2368)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		f := newPersisting(t, path, "e1")
		f.put(WriteRequest{Set: "agent", Flag: "interventions_allowed", Variant: "full", Persist: true, Writer: "menu"})
		breakStateFile(t, path)
		if res, err := f.state.Put(WriteRequest{Set: "agent", Flag: "interventions_allowed", Variant: "none", Persist: true, Writer: "menu"}); err != nil || res.Persisted {
			t.Fatalf("res %+v err %v, want the kill switch applied in memory, not persisted", res, err)
		}
		if st := f.state.Stats(); st.PersistedValues != 0 {
			t.Fatalf("persisted overlays %d before the repair, want 0", st.PersistedValues)
		}
		if err := os.RemoveAll(path); err != nil {
			t.Fatal(err)
		}
		if res := f.put(WriteRequest{Set: "game", Flag: "sensitivity", Variant: "slow", Persist: true, Writer: "menu"}); !res.Persisted {
			t.Fatal("the later write was not persisted")
		}
		if st := f.state.Stats(); st.PersistedValues != 2 {
			t.Fatalf("persisted overlays %d, want 2: the save wrote the kill switch too", st.PersistedValues)
		}
	})
	t.Run("UC-053/A2 with persistence disabled a persisted write is not an error and reports persisted false", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		res := f.put(WriteRequest{Set: "game", Flag: "num_teams", Variant: "four", Persist: true})
		if res.Persisted {
			t.Fatal("reported persisted without a state file")
		}
	})
}

// breakStateFile makes the next state-file save fail: the file path becomes a
// non-empty directory, so the atomic rename onto it fails.
func breakStateFile(t *testing.T, path string) {
	t.Helper()
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(path, "x"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(path, "x", "y"), []byte("y"), 0o600); err != nil {
		t.Fatal(err)
	}
}

// UC: UC-052 main
func TestFraction_RendersAFractionalElse(t *testing.T) {
	t.Run("UC-052/main a fraction overlay splits between its variant and the effective default", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.put(WriteRequest{Set: "controller", Flag: "bluetooth_backend", Variant: "python", Scope: Scope{FractionPercent: intp(10)}, Writer: "canary"})
		targeting, _ := json.Marshal(f.snapshotFlag("bluetooth_backend")["targeting"])
		want := `{"fractional":[["python",10],["rust",90]]}`
		if string(targeting) != want {
			t.Fatalf("targeting %s, want %s", targeting, want)
		}
	})
}

// fillGameToTheBound registers a game with the given serials and writes the
// widest realistic set of values and events into it; it returns the bytes the
// game adds to the snapshot.
func fillGameToTheBound(t *testing.T, f *fixture, gameID string, serials []string) int {
	t.Helper()
	f.register(gameID, "c/1", serials...)
	before, _, _ := f.state.Snapshot("")
	for _, s := range serials {
		for _, w := range []WriteRequest{
			{Flag: "player_sensitivity_factor", Value: raw("1.2")},
			{Flag: "player_handicap_factor", Value: raw("1.25")},
			{Flag: "shield_seconds", Value: raw("5")},
			{Flag: "toughness_boost_seconds", Value: raw(`"5:2.0"`)},
			{Flag: "soft_penalty_action", Value: raw(`"tighten:5:0.5"`)},
		} {
			w.Set, w.Scope = "interventions", Scope{GameID: gameID, Serial: s}
			f.put(w)
		}
	}
	for _, w := range []WriteRequest{
		{Flag: "music_tempo_override", Value: raw("1.15")}, {Flag: "global_sensitivity_override", Value: raw("3")},
		{Flag: "global_difficulty_factor", Value: raw("1.5")}, {Flag: "volume_override", Value: raw("0.5")},
		{Flag: "pacing_profile", Variant: "calm"}, {Flag: "tempo_ramp", Value: raw(`"1.3:8:linear"`)},
		{Flag: "auto_rubberband", Variant: "gentle"},
	} {
		w.Set, w.Scope = "interventions", Scope{GameID: gameID}
		f.put(w)
	}
	for i := 0; i < 20; i++ {
		for _, flag := range []string{"eliminate_player", "revive_player"} {
			if _, err := f.state.Append(AppendRequest{Set: "interventions", Flag: flag, Scope: Scope{GameID: gameID}, Payload: map[string]any{"serial": serials[i%len(serials)]}}); err != nil {
				t.Fatal(err)
			}
		}
		for _, a := range []AppendRequest{
			{Flag: "audio_cue", Payload: map[string]any{"sound": "agent_cue"}},
			{Flag: "controller_effect", Payload: map[string]any{"serial": serials[i%len(serials)], "effect": "rumble"}},
			{Flag: "end_game"},
		} {
			a.Set, a.Scope = "interventions", Scope{GameID: gameID}
			if _, err := f.state.Append(a); err != nil {
				t.Fatal(err)
			}
		}
	}
	after, _, _ := f.state.Snapshot("")
	return len(after) - len(before)
}

// UC: UC-053 main
// NFR: NFR-051
func TestNFR051_SnapshotBounded(t *testing.T) {
	t.Run("UC-053/main 8 games x 16 players stay within 64 KiB per game and 1 MiB per snapshot", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		empty, _, _ := f.state.Snapshot("")
		for g := 0; g < 8; g++ {
			gameID := fmt.Sprintf("game_%012d", g)
			serials := make([]string, 16)
			for p := range serials {
				serials[p] = fmt.Sprintf("MOCK%04d", g*16+p)
			}
			if perGame := fillGameToTheBound(t, f, gameID, serials); perGame > 64<<10 {
				t.Fatalf("game %d adds %d bytes, over 64 KiB", g, perGame)
			}
		}
		full, _, _ := f.state.Snapshot("")
		t.Logf("snapshot: %d bytes empty, %d bytes with 8 full games", len(empty), len(full))
		if len(full) > 1<<20 {
			t.Fatalf("snapshot %d bytes, over 1 MiB", len(full))
		}
	})
	t.Run("UC-053/main one game with the 64-serial cap stays within 64 KiB (NFR-051)", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		serials := make([]string, MaxSerialsPerGame)
		for p := range serials {
			serials[p] = fmt.Sprintf("MOCK%04d", p)
		}
		perGame := fillGameToTheBound(t, f, "game_000000000000", serials)
		t.Logf("64 players add %d bytes", perGame)
		if perGame > 64<<10 {
			t.Fatalf("a 64-player game adds %d bytes, over 64 KiB", perGame)
		}
	})
}

// UC: UC-053 main
func TestClear_RemovesTheValue(t *testing.T) {
	t.Run("UC-053/main DELETE of a game value and a serial value", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a", "c/1", "S1")
		f.put(WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "game_a"}})
		f.put(WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"), Scope: Scope{GameID: "game_a", Serial: "S1"}})
		for _, req := range []ClearRequest{
			{Set: "interventions", Flag: "volume_override", Scope: Scope{GameID: "game_a"}},
			{Set: "interventions", Flag: "shield_seconds", Scope: Scope{GameID: "game_a", Serial: "S1"}},
		} {
			res, err := f.state.Clear(req)
			if err != nil || res.Cleared != 1 {
				t.Fatalf("clear %s: %+v %v", req.Flag, res, err)
			}
		}
		if f.state.Stats().ScopeValues != 0 {
			t.Fatal("values remain")
		}
		res, err := f.state.Clear(ClearRequest{Set: "interventions", Flag: "volume_override", Scope: Scope{GameID: "game_a"}})
		if err != nil || res.Cleared != 0 {
			t.Fatalf("a repeat clear: %+v %v", res, err)
		}
	})
}

// UC: UC-053 main A6 A10
// NFR: NFR-051
func TestGameScopeBounds(t *testing.T) {
	t.Run("UC-053/A6 the number of live game scopes is capped: one more registration is 409 too_many_games (NFR-051)", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{maxGames: 2})
		f.register("game_a", "c/1")
		f.register("game_b", "c/1")
		_, err := f.state.RegisterGame(Game{ID: "game_c", CoordinatorInstance: "c/1"})
		wantReason(t, err, http.StatusConflict, fserr.TooManyGames)
		_, err = f.state.Put(WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "game_c"}})
		wantReason(t, err, http.StatusConflict, fserr.GameNotLive)
		if _, err := f.state.RegisterGame(Game{ID: "game_a", CoordinatorInstance: "c/1"}); err != nil {
			t.Fatalf("re-registering a live game must not count against the cap: %v", err)
		}
	})
	t.Run("UC-053/A6 a registration above 64 serials is 400 and registers nothing (NFR-051)", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		serials := make([]string, MaxSerialsPerGame+1)
		for p := range serials {
			serials[p] = fmt.Sprintf("MOCK%04d", p)
		}
		_, err := f.state.RegisterGame(Game{ID: "game_a", CoordinatorInstance: "c/1", Serials: serials})
		wantReason(t, err, http.StatusBadRequest, fserr.Malformed)
		if f.state.Game("game_a") != nil {
			t.Fatal("a rejected registration left a game scope")
		}
		if _, err := f.state.RegisterGame(Game{ID: "game_a", CoordinatorInstance: "c/1", Serials: serials[:MaxSerialsPerGame]}); err != nil {
			t.Fatalf("exactly 64 serials must be accepted: %v", err)
		}
	})
	t.Run("UC-053/A10 a reconcile over the cap is rejected whole (NFR-051)", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{maxGames: 3})
		f.register("game_x", "other/1")
		f.register("game_a", "c/1")
		// c/1 keeps game_a and adds two: 1 + 3 = 4 > 3.
		_, err := f.state.Reconcile("c/1", []Game{{ID: "game_a"}, {ID: "game_b"}, {ID: "game_c"}})
		wantReason(t, err, http.StatusConflict, fserr.TooManyGames)
		if got := len(f.state.Games()); got != 2 {
			t.Fatalf("a rejected reconcile changed the games: %d live", got)
		}
		// Dropping its own game makes room: c/1 replaces game_a by game_b and game_c (3 live).
		if _, err := f.state.Reconcile("c/1", []Game{{ID: "game_b"}, {ID: "game_c"}}); err != nil {
			t.Fatalf("a reconcile within the cap: %v", err)
		}
		if got := len(f.state.Games()); got != 3 {
			t.Fatalf("live games = %d, want 3", got)
		}
		if f.state.Game("game_x") == nil || f.state.Game("game_a") != nil {
			t.Fatal("the reconcile must keep the other instance's game_x and drop its own game_a")
		}
	})
	t.Run("UC-053/main a re-registration without a serial drops that player's values", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a", "c/1", "MOCK0001", "MOCK0002")
		f.put(WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("3"), Scope: Scope{GameID: "game_a", Serial: "MOCK0002"}})
		f.register("game_a", "c/1", "MOCK0001")
		payload, _, _ := f.state.Snapshot("")
		if containsStr(payload, "MOCK0002") {
			t.Fatal("the removed serial still has a rung")
		}
	})
}

// UC: UC-042 main
func TestDryRunRendersWithoutApplying(t *testing.T) {
	t.Run("UC-042/main a dry run returns the flag before and after and changes nothing", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		select {
		case <-f.sub.C: // the initial wake-up
		default:
		}
		version := f.state.Version()
		before, _, _ := f.state.Snapshot("")
		res := f.put(WriteRequest{Set: "game", Flag: "nonstop.respawn_seconds", Value: raw("4"),
			Scope: Scope{ExperimentID: "exp_1", Arm: "experimental"}, DryRun: true})
		if !res.DryRun || len(res.RenderedBefore) == 0 || len(res.Rendered) == 0 {
			t.Fatalf("dry run result incomplete: %+v", res)
		}
		if !containsStr(res.Rendered, "exp_1") || containsStr(res.RenderedBefore, "exp_1") {
			t.Fatalf("rendered after must carry the rung, before must not:\n%s\n%s", res.RenderedBefore, res.Rendered)
		}
		after, _, _ := f.state.Snapshot("")
		if f.state.Version() != version || string(before) != string(after) {
			t.Fatal("a dry run changed the served configuration")
		}
		select {
		case <-f.sub.C:
			t.Fatal("a dry run notified the sync stream")
		default:
		}
	})
	t.Run("UC-042/main a dry run over an existing value restores it", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.put(WriteRequest{Set: "game", Flag: "nonstop.respawn_seconds", Value: raw("4"), Scope: Scope{ExperimentID: "exp_1", Arm: "experimental"}})
		before, _, _ := f.state.Snapshot("")
		f.put(WriteRequest{Set: "game", Flag: "nonstop.respawn_seconds", Value: raw("6"), Scope: Scope{ExperimentID: "exp_1", Arm: "experimental"}, DryRun: true})
		after, _, _ := f.state.Snapshot("")
		if string(before) != string(after) {
			t.Fatal("the dry run did not restore the previous value")
		}
	})
	t.Run("UC-042/main a dry run cannot persist", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		_, err := f.state.Put(WriteRequest{Set: "game", Flag: "sensitivity", Variant: "slow", Persist: true, DryRun: true})
		wantReason(t, err, http.StatusBadRequest, fserr.Malformed)
	})
}

// UC: UC-053 A3
// NFR: NFR-051
func TestExpireExperiments(t *testing.T) {
	arm := func(id string) WriteRequest {
		return WriteRequest{Set: "game", Flag: "nonstop.respawn_seconds", Value: raw("4"), Scope: Scope{ExperimentID: id, Arm: "experimental"}}
	}
	t.Run("UC-053/A3 an experiment no game references is dropped after the idle time (NFR-051)", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.put(arm("exp_orphan"))
		f.put(arm("exp_live"))
		if _, err := f.state.RegisterGame(Game{ID: "game_a", CoordinatorInstance: "c/1", Kind: "shadow", ExperimentID: "exp_live", Arm: "experimental"}); err != nil {
			t.Fatal(err)
		}
		f.clock.Advance(DefaultExperimentIdle / 2)
		if n := f.state.ExpireExperiments(); n != 0 {
			t.Fatalf("expired %d before the idle time", n)
		}
		f.clock.Advance(DefaultExperimentIdle/2 + time.Second)
		if n := f.state.ExpireExperiments(); n != 1 {
			t.Fatalf("expired %d, want 1 (the orphan only)", n)
		}
		payload, _, _ := f.state.Snapshot("")
		if containsStr(payload, "exp_orphan") || !containsStr(payload, "exp_live") {
			t.Fatalf("want exp_live kept and exp_orphan dropped: %s", payload)
		}
	})
	t.Run("UC-053/A3 an experiment whose games ended expires one idle time after the last one", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.put(arm("exp_done"))
		if _, err := f.state.RegisterGame(Game{ID: "game_a", CoordinatorInstance: "c/1", Kind: "shadow", ExperimentID: "exp_done", Arm: "experimental"}); err != nil {
			t.Fatal(err)
		}
		f.clock.Advance(DefaultExperimentIdle)
		f.state.ExpireExperiments() // the live game refreshes it
		if _, ok := f.state.EndGame("game_a", EndReasonEnded); !ok {
			t.Fatal("game_a was not live")
		}
		f.clock.Advance(DefaultExperimentIdle - time.Second)
		if n := f.state.ExpireExperiments(); n != 0 {
			t.Fatalf("expired %d right after its last game", n)
		}
		f.clock.Advance(2 * time.Second)
		if n := f.state.ExpireExperiments(); n != 1 {
			t.Fatalf("expired %d, want 1", n)
		}
	})
}

// UC: UC-053 A2 BR-004
func TestClearWriter_KeepsPersistedAdminValues(t *testing.T) {
	t.Run("UC-053/A2 a writer reset never removes the host's persisted setting", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{stateFile: filepath.Join(t.TempDir(), "admin.json")})
		f.put(WriteRequest{Set: "game", Flag: "sensitivity", Variant: "slow", Persist: true, Writer: "menu"})
		f.put(WriteRequest{Set: "game", Flag: "num_teams", Variant: "four", Writer: "menu"})
		n, err := f.state.ClearWriter("game", "menu")
		if err != nil || n != 1 {
			t.Fatalf("cleared %d (%v), want only the in-memory overlay", n, err)
		}
		if got := f.snapshotFlag("sensitivity")["defaultVariant"]; got != "slow" {
			t.Fatalf("sensitivity %v after the reset, want the persisted slow", got)
		}
	})
}

// UC: UC-053 A12
func TestStateFileCorruptCopiesAreCapped(t *testing.T) {
	t.Run("UC-053/A12 a corrupt state file is moved aside and at most three .corrupt copies are kept", func(t *testing.T) {
		dir := t.TempDir()
		path := filepath.Join(dir, "admin.json")
		for i := 0; i < 5; i++ {
			if err := os.WriteFile(fmt.Sprintf("%s.corrupt-%d", path, 1600000000+i), []byte("x"), 0o600); err != nil {
				t.Fatal(err)
			}
		}
		if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
			t.Fatal(err)
		}
		newFixture(t, fixtureOpts{stateFile: path, epoch: "e1"})
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatalf("the corrupt state file was not moved aside: %v", err)
		}
		matches, _ := filepath.Glob(path + ".corrupt-*")
		if len(matches) != 3 {
			t.Fatalf("%d corrupt copies, want 3: %v", len(matches), matches)
		}
	})
}

// UC: UC-053 A12
func TestUnreadableStateFileBlocksPersistence(t *testing.T) {
	t.Run("UC-053/A12 a read error keeps the file and refuses persisted admin writes", func(t *testing.T) {
		// A directory at the state-file path: ReadFile fails with an I/O error, not "not exist".
		path := filepath.Join(t.TempDir(), "admin.json")
		if err := os.Mkdir(path, 0o755); err != nil {
			t.Fatal(err)
		}
		f := newFixture(t, fixtureOpts{stateFile: path})
		if _, err := f.state.Put(WriteRequest{Set: "game", Flag: "sensitivity", Variant: "slow", Persist: true, Writer: "menu"}); err == nil || err.Status != 500 {
			t.Fatalf("err = %v, want 500 while persistence is blocked", err)
		}
		if f.state.PersistErrors() != 1 {
			t.Fatalf("persist errors = %d, want 1", f.state.PersistErrors())
		}
		if fi, err := os.Stat(path); err != nil || !fi.IsDir() {
			t.Fatalf("the unreadable path was touched: %v", err)
		}
		if m, _ := filepath.Glob(path + ".corrupt-*"); len(m) != 0 {
			t.Fatalf("an unreadable file was moved aside as corrupt: %v", m)
		}
		if got := f.snapshotFlag("sensitivity")["defaultVariant"]; got == "slow" {
			t.Fatalf("defaultVariant = %v: the refused write still applies", got)
		}
	})
	t.Run("UC-053/A12 a read error still lets the kill switch apply in memory (#2368)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		if err := os.Mkdir(path, 0o755); err != nil {
			t.Fatal(err)
		}
		f := newFixture(t, fixtureOpts{stateFile: path})
		res, err := f.state.Put(WriteRequest{Set: "agent", Flag: "interventions_allowed", Variant: "none", Persist: true, Writer: "menu"})
		if err != nil {
			t.Fatalf("err = %v: the kill switch must work while persistence is blocked", err)
		}
		if res.Persisted || f.state.PersistErrors() != 1 {
			t.Fatalf("persisted %v errors %d, want false and 1", res.Persisted, f.state.PersistErrors())
		}
		if got := f.snapshotFlag("interventions_allowed")["defaultVariant"]; got != "none" {
			t.Fatalf("defaultVariant = %v, want the kill switch none", got)
		}
		if st := f.state.Stats(); st.PersistedValues != 0 {
			t.Fatalf("persisted overlays %d: the kill switch lives in memory only", st.PersistedValues)
		}
	})
	t.Run("UC-053/A12 a read error refuses a persisted clear with nothing in memory (#2368)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		if err := os.Mkdir(path, 0o755); err != nil {
			t.Fatal(err)
		}
		f := newFixture(t, fixtureOpts{stateFile: path})
		res, err := f.state.Clear(ClearRequest{Set: "game", Flag: "sensitivity", Persist: true})
		if err == nil || err.Status != 500 || err.Reason != fserr.ReasonPersistFailed {
			t.Fatalf("err = %v, want 500 persist_failed: the unreadable file may still hold the value", err)
		}
		if res.Persisted || res.Cleared != 0 || f.state.PersistErrors() != 1 {
			t.Fatalf("result %+v errors %d, want nothing persisted and 1 error", res, f.state.PersistErrors())
		}
	})
	t.Run("UC-053/A12 a read error leaves a persisted clear of a flag without persist at 204 (#2368)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		if err := os.Mkdir(path, 0o755); err != nil {
			t.Fatal(err)
		}
		f := newFixture(t, fixtureOpts{stateFile: path})
		res, err := f.state.Clear(ClearRequest{Set: "agent", Flag: "enabled", Persist: true})
		if err != nil {
			t.Fatalf("err = %v: a flag that takes no persisted value cannot be in the file", err)
		}
		if res.Persisted || res.PersistFailed || f.state.PersistErrors() != 0 {
			t.Fatalf("result %+v errors %d, want a plain clear and no counted error", res, f.state.PersistErrors())
		}
	})
	t.Run("UC-053/A12 a read error answers a kill switch's persisted clear as not persisted (#2368)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		if err := os.Mkdir(path, 0o755); err != nil {
			t.Fatal(err)
		}
		f := newFixture(t, fixtureOpts{stateFile: path})
		res, err := f.state.Clear(ClearRequest{Set: "agent", Flag: "interventions_allowed", Persist: true})
		if err != nil {
			t.Fatalf("err = %v: a kill switch clear is never refused", err)
		}
		if res.Persisted || !res.PersistFailed || f.state.PersistErrors() != 1 {
			t.Fatalf("result %+v errors %d, want persist failed and 1 error", res, f.state.PersistErrors())
		}
	})
}

// UC: UC-053 A2 UC-015 BR-018
func TestHoldShieldKeysPersist(t *testing.T) {
	holdKeys := map[string]string{
		"shield.energy_max_seconds":     "7",
		"shield.energy_budget_seconds":  "12",
		"shield.energy_initial_seconds": "3",
		"shield.recharge_per_second":    "0.4",
		"shield.min_burst_seconds":      "0.6",
		"shield.hold_rumble_intensity":  "90",
		"shield.empty_pulse_ms":         "300",
	}
	for key, value := range holdKeys {
		t.Run("UC-053/A2 "+key+" persisted write survives a restart (#2452)", func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "admin.json")
			f := newFixture(t, fixtureOpts{stateFile: path, epoch: "e1"})
			res := f.put(WriteRequest{Set: "game", Flag: key, Value: raw(value), Persist: true, Writer: "operator"})
			if !res.Persisted {
				t.Fatal("the result does not say persisted")
			}
			g := newFixture(t, fixtureOpts{stateFile: path, epoch: "e2"})
			var want any
			if err := json.Unmarshal([]byte(value), &want); err != nil {
				t.Fatal(err)
			}
			if _, got := g.resolve(key, map[string]any{}); got != want {
				t.Fatalf("%s after restart %v, want %v", key, got, want)
			}
		})
	}
}

// UC: UC-053 A2 UC-043 A17
func TestHoldShieldKeysPersistRungs(t *testing.T) {
	const key = "shield.energy_max_seconds"
	t.Run("UC-053/A2 a persisted stack value outranks a real overlay (#2452)", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{stateFile: filepath.Join(t.TempDir(), "admin.json"), epoch: "e1"})
		f.put(WriteRequest{Set: "game", Flag: key, Value: raw("7"), Persist: true, Writer: "operator"})
		f.put(WriteRequest{Set: "game", Flag: key, Value: raw("4"), Scope: Scope{GameKind: "real"}, Writer: "agent"})
		if _, got := f.resolve(key, map[string]any{}); got != float64(7) {
			t.Fatalf("%s = %v, want the persisted 7 over the real overlay", key, got)
		}
	})
	refused := map[string]WriteRequest{
		"experiment+arm": {Set: "game", Flag: key, Value: raw("4"), Scope: Scope{ExperimentID: "exp_1", Arm: "experimental"}, Persist: true, Writer: "agent"},
		"real":           {Set: "game", Flag: key, Value: raw("4"), Scope: Scope{GameKind: "real"}, Persist: true, Writer: "agent"},
	}
	for name, req := range refused {
		t.Run("UC-053/A5 a persisted "+name+" write is refused (#2452)", func(t *testing.T) {
			f := newFixture(t, fixtureOpts{stateFile: filepath.Join(t.TempDir(), "admin.json"), epoch: "e1"})
			_, err := f.state.Put(req)
			wantReason(t, err, http.StatusBadRequest, fserr.Malformed)
		})
	}
}
