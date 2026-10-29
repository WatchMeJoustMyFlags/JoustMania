package state

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"

	"github.com/joustmania/flag-service/fserr"
	"github.com/joustmania/flag-service/registry"
)

// The precondition matrix of Put and Clear (#2370): every refusal and its
// order, pinned as (status, reason) per case. Each case lists the checks it
// trips; the ones with two or more pin their precedence. A refactor of
// Put / Clear must keep every row. Any change of an answer below is a spec
// change (services.md §7.2, UC-053) and not a refactor.
//
// A row pins the answer AND the side effects (checkEffects): a refusal leaves
// every slot and the snapshot version untouched, a success leaves the addressed
// slot holding the value (or empty for a clear), bumps the version iff it
// changed something, and carries the persisted marker (flag and state file).

type matrixCase struct {
	name string
	// opts and setup build the state; breakFile breaks the state file after
	// startup (the next save fails).
	opts      fixtureOpts
	persist   bool // a state file in a temp dir
	futureVer bool // the state file is from a newer version (persistBlocked)
	breakFile bool
	setup     func(t *testing.T, f *fixture)
	put       *WriteRequest
	clr       *ClearRequest
	status    int // 0 = success
	reason    string
	cleared   int
	persisted bool // success only: res.Persisted
	failed    bool // clear only: res.PersistFailed
	// marked: a Put's slot value carries Persisted=true (the in-memory marker
	// flagservice_overlays counts); false for a kill switch that is not on disk.
	marked bool
	// extra: more checks on the state after a successful call.
	extra func(t *testing.T, f *fixture)
}

var (
	realSc    = Scope{GameKind: "real"}
	expSc     = Scope{ExperimentID: "e1", Arm: "a"}
	longID    = strings.Repeat("x", MaxIDLength+1)
	guardPut  = WriteRequest{Set: "game", Flag: "shield.energy_budget_seconds", Value: raw("20"), Scope: realSc, Writer: "agent"}
	stackPut  = WriteRequest{Set: "game", Flag: "num_teams", Variant: "four", Writer: "menu"}
	killPut   = WriteRequest{Set: "agent", Flag: "interventions_allowed", Variant: "full", Persist: true, Writer: "menu"}
	plainPers = WriteRequest{Set: "game", Flag: "num_teams", Variant: "four", Persist: true, Writer: "menu"}
)

func withReq(r WriteRequest, mod func(*WriteRequest)) *WriteRequest { mod(&r); return &r }
func clearReq(r ClearRequest) *ClearRequest                         { return &r }

func liveReal(t *testing.T, f *fixture) { f.registerKind("g-real", "real") }

// UC: UC-053 main
func TestPreconditionMatrix_Put(t *testing.T) {
	cases := []matrixCase{
		{name: "unknown set", put: withReq(stackPut, func(r *WriteRequest) { r.Set = "nope" }), status: 404, reason: fserr.UnknownFlagSet},
		{name: "unknown flag", put: withReq(stackPut, func(r *WriteRequest) { r.Flag = "nope" }), status: 404, reason: fserr.UnknownFlag},
		{name: "flag of another set", put: withReq(stackPut, func(r *WriteRequest) { r.Set = "agent" }), status: 404, reason: fserr.UnknownFlag},
		{name: "event flag, also no body", put: &WriteRequest{Set: "interventions", Flag: "end_game"}, status: 400, reason: fserr.Malformed},
		{name: "neither variant nor value", put: &WriteRequest{Set: "game", Flag: "num_teams"}, status: 400, reason: fserr.Malformed},
		{name: "both variant and value", put: withReq(stackPut, func(r *WriteRequest) { r.Value = raw(`"four"`) }), status: 400, reason: fserr.Malformed},
		{name: "variant+value beats long id", put: withReq(stackPut, func(r *WriteRequest) { r.Variant = ""; r.Writer = longID }), status: 400, reason: fserr.Malformed},
		{name: "long writer", put: withReq(stackPut, func(r *WriteRequest) { r.Writer = longID }), status: 400, reason: fserr.Malformed},
		{name: "long game id beats scope fit", put: withReq(stackPut, func(r *WriteRequest) { r.Scope = Scope{GameID: longID} }), status: 400, reason: fserr.Malformed},
		{name: "scope combination fits nothing", put: withReq(stackPut, func(r *WriteRequest) { r.Scope = Scope{Serial: "S1"} }), status: 400, reason: fserr.Malformed},
		{name: "scope not allowed for the flag", put: withReq(stackPut, func(r *WriteRequest) { r.Scope = realSc }), status: 400, reason: fserr.Malformed},
		{name: "game_kind shadow fits no scope", put: withReq(stackPut, func(r *WriteRequest) { r.Scope = Scope{GameKind: "shadow"} }), status: 400, reason: fserr.Malformed},
		{name: "experiment id without an arm fits no scope", put: withReq(stackPut, func(r *WriteRequest) { r.Scope = Scope{ExperimentID: "e1"} }), status: 400, reason: fserr.Malformed},
		{name: "scope fit beats a live game", setup: liveReal, put: withReq(guardPut, func(r *WriteRequest) { r.Scope = Scope{GameID: "g-real"} }), status: 400, reason: fserr.Malformed},
		{name: "ttl on a guarded real write beats the live game", setup: liveReal, put: withReq(guardPut, func(r *WriteRequest) { r.TTLSeconds = ttl(5) }), status: 400, reason: fserr.Malformed},
		{name: "ttl on a guarded real write", put: withReq(guardPut, func(r *WriteRequest) { r.TTLSeconds = ttl(5) }), status: 400, reason: fserr.Malformed},
		{name: "ttl zero", put: withReq(stackPut, func(r *WriteRequest) { r.TTLSeconds = ttl(0) }), status: 400, reason: fserr.Malformed},
		{name: "ttl negative", put: withReq(stackPut, func(r *WriteRequest) { r.TTLSeconds = ttl(-1) }), status: 400, reason: fserr.Malformed},
		{name: "ttl on an experiment arm", put: withReq(guardPut, func(r *WriteRequest) { r.Scope = expSc; r.TTLSeconds = ttl(5) }), status: 400, reason: fserr.Malformed},
		{name: "ttl on a stack write is fine", put: withReq(stackPut, func(r *WriteRequest) { r.TTLSeconds = ttl(5) })},
		{name: "ttl on an unguarded real write is fine", put: &WriteRequest{Set: "game", Flag: "sensitivity", Variant: "fast", Scope: realSc, TTLSeconds: ttl(5)}},
		{name: "if_writer_in outside real", put: withReq(stackPut, func(r *WriteRequest) { r.IfWriterIn = []string{""} }), status: 400, reason: fserr.Malformed},
		{name: "if_writer_in too many entries", put: withReq(guardPut, func(r *WriteRequest) { r.IfWriterIn = make([]string, MaxIfWriterIn+1) }), status: 400, reason: fserr.Malformed},
		{name: "if_writer_in entry too long", put: withReq(guardPut, func(r *WriteRequest) { r.IfWriterIn = []string{longID} }), status: 400, reason: fserr.Malformed},
		{name: "persist of a flag that cannot persist", put: &WriteRequest{Set: "agent", Flag: "enabled", Variant: "on", Persist: true}, status: 400, reason: fserr.Malformed},
		{name: "persist outside the stack scope", put: withReq(guardPut, func(r *WriteRequest) { r.Value = nil; r.Variant = "hold"; r.Persist = true }), status: 400, reason: fserr.Malformed},
		{name: "persist with ttl", persist: true, put: withReq(plainPers, func(r *WriteRequest) { r.TTLSeconds = ttl(5) }), status: 400, reason: fserr.Malformed},
		{name: "persist with dry run", persist: true, put: withReq(plainPers, func(r *WriteRequest) { r.DryRun = true }), status: 400, reason: fserr.Malformed},
		{name: "persist-fit beats set not served", opts: fixtureOpts{served: []string{"agent"}}, put: &WriteRequest{Set: "agent", Flag: "enabled", Variant: "on", Persist: true}, status: 400, reason: fserr.Malformed},
		{name: "set not served", opts: fixtureOpts{served: []string{"agent"}}, put: &stackPut, status: 409, reason: fserr.SetNotServed},
		{name: "set not served beats an invalid value", opts: fixtureOpts{served: []string{"agent"}}, put: withReq(stackPut, func(r *WriteRequest) { r.Variant = "nope" }), status: 409, reason: fserr.SetNotServed},
		{name: "game not live", put: &WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "gx"}}, status: 409, reason: fserr.GameNotLive},
		{name: "game not live beats an invalid value", put: &WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("99"), Scope: Scope{GameID: "gx"}}, status: 409, reason: fserr.GameNotLive},
		{name: "serial not in game", setup: func(t *testing.T, f *fixture) { f.register("g1", "c1", "S1") }, put: &WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"), Scope: Scope{GameID: "g1", Serial: "S9"}}, status: 422, reason: fserr.SerialNotInGame},
		{name: "real game live", setup: liveReal, put: &guardPut, status: 409, reason: fserr.RealGameLive},
		{name: "real game live, dry run", setup: liveReal, put: withReq(guardPut, func(r *WriteRequest) { r.DryRun = true }), status: 409, reason: fserr.RealGameLive},
		{name: "real game live beats an invalid value", setup: liveReal, put: withReq(guardPut, func(r *WriteRequest) { r.Value = raw("999") }), status: 409, reason: fserr.RealGameLive},
		{name: "real game live beats if_writer_in", setup: liveReal, put: withReq(guardPut, func(r *WriteRequest) { r.IfWriterIn = []string{"nobody"} }), status: 409, reason: fserr.RealGameLive},
		{name: "shadow game does not block", setup: func(t *testing.T, f *fixture) { f.registerKind("g-s", "shadow") }, put: &guardPut},
		{name: "unregistered kind blocks (fails closed)", setup: func(t *testing.T, f *fixture) { f.registerKind("g-k", "") }, put: &guardPut, status: 409, reason: fserr.RealGameLive},
		{name: "invalid value", put: withReq(stackPut, func(r *WriteRequest) { r.Variant = "nope" }), status: 422, reason: fserr.OutOfBounds},
		{name: "value out of range", put: withReq(guardPut, func(r *WriteRequest) { r.Value = raw("999") }), status: 422, reason: fserr.OutOfBounds},
		{name: "invalid value beats no stream", opts: fixtureOpts{noStream: true}, put: withReq(stackPut, func(r *WriteRequest) { r.Variant = "nope" }), status: 422, reason: fserr.OutOfBounds},
		{name: "fraction below bounds", put: &WriteRequest{Set: "controller", Flag: "bluetooth_backend", Variant: "rust", Scope: Scope{FractionPercent: intp(0)}}, status: 422, reason: fserr.OutOfBounds},
		{name: "fraction above bounds", put: &WriteRequest{Set: "controller", Flag: "bluetooth_backend", Variant: "rust", Scope: Scope{FractionPercent: intp(101)}}, status: 422, reason: fserr.OutOfBounds},
		{name: "fraction in bounds", put: &WriteRequest{Set: "controller", Flag: "bluetooth_backend", Variant: "rust", Scope: Scope{FractionPercent: intp(10)}}},
		{name: "no sync stream", opts: fixtureOpts{noStream: true}, put: &stackPut, status: 503, reason: fserr.NoSyncStream},
		{name: "no sync stream beats if_writer_in", opts: fixtureOpts{noStream: true}, put: withReq(guardPut, func(r *WriteRequest) { r.IfWriterIn = []string{"nobody"} }), status: 503, reason: fserr.NoSyncStream},
		{name: "if_writer_in mismatch, no overlay", put: withReq(guardPut, func(r *WriteRequest) { r.IfWriterIn = []string{"agent"} }), status: 412, reason: fserr.WriterMismatch},
		{name: "if_writer_in mismatch, dry run too", put: withReq(guardPut, func(r *WriteRequest) { r.IfWriterIn = []string{"agent"}; r.DryRun = true }), status: 412, reason: fserr.WriterMismatch},
		{name: "if_writer_in matches the empty entry", put: withReq(guardPut, func(r *WriteRequest) { r.IfWriterIn = []string{""} })},
		{name: "if_writer_in matches the stored writer", setup: func(t *testing.T, f *fixture) { f.put(guardPut) }, put: withReq(guardPut, func(r *WriteRequest) { r.IfWriterIn = []string{"x", "agent"} })},
		{name: "if_writer_in mismatch, other writer", setup: func(t *testing.T, f *fixture) { f.put(*withReq(guardPut, func(r *WriteRequest) { r.Writer = "menu" })) }, put: withReq(guardPut, func(r *WriteRequest) { r.IfWriterIn = []string{"agent"} }), status: 412, reason: fserr.WriterMismatch},
		{name: "event flag in a dry run", put: &WriteRequest{Set: "interventions", Flag: "end_game", Variant: "x", DryRun: true}, status: 400, reason: fserr.Malformed},
		// successes
		{name: "stack write", put: &stackPut},
		{name: "game write", setup: func(t *testing.T, f *fixture) { f.register("g1", "c1") }, put: &WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "g1"}}},
		{name: "game+serial write", setup: func(t *testing.T, f *fixture) { f.register("g1", "c1", "S1") }, put: &WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"), Scope: Scope{GameID: "g1", Serial: "S1"}}},
		{name: "game+serial write into a game that already has another serial", setup: func(t *testing.T, f *fixture) {
			f.register("g1", "c1", "S1", "S2")
			f.put(WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("3"), Scope: Scope{GameID: "g1", Serial: "S1"}})
		}, put: &WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"), Scope: Scope{GameID: "g1", Serial: "S2"}},
			extra: func(t *testing.T, f *fixture) {
				if len(f.state.flags["shield_seconds"].serials["g1"]) != 2 {
					t.Fatalf("the earlier serial was lost: %v", f.state.flags["shield_seconds"].serials["g1"])
				}
			}},
		{name: "experiment write", put: withReq(guardPut, func(r *WriteRequest) { r.Scope = expSc })},
		{name: "real write", put: &guardPut},
		{name: "dry run", put: withReq(stackPut, func(r *WriteRequest) { r.DryRun = true })},
		// persistence (#2368, #2396, UC-053 A2/A12)
		{name: "persisted write", persist: true, put: &plainPers, persisted: true, marked: true},
		{name: "persisted write, no state file configured", put: &plainPers, marked: true},
		{name: "persisted write, save fails: refused", persist: true, breakFile: true, put: &plainPers, status: 500, reason: fserr.ReasonPersistFailed},
		{name: "persisted kill switch, save succeeds: applied and persisted", persist: true, put: &killPut, persisted: true, marked: true},
		{name: "persisted kill switch, save fails: applied, not persisted", persist: true, breakFile: true, put: &killPut},
		{name: "persisted write, state file from a newer version: refused", persist: true, futureVer: true, put: &plainPers, status: 500, reason: fserr.ReasonPersistFailed},
		{name: "persisted kill switch, newer state file: applied, not persisted", persist: true, futureVer: true, put: &killPut},
	}
	for _, c := range cases {
		c := c
		t.Run(c.name, func(t *testing.T) { runMatrixCase(t, c) })
	}
}

// UC: UC-053 main
func TestPreconditionMatrix_Clear(t *testing.T) {
	guardClear := ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: realSc}
	cases := []matrixCase{
		{name: "unknown set", clr: clearReq(ClearRequest{Set: "nope", Flag: "num_teams"}), status: 404, reason: fserr.UnknownFlagSet},
		{name: "unknown flag", clr: clearReq(ClearRequest{Set: "game", Flag: "nope"}), status: 404, reason: fserr.UnknownFlag},
		{name: "scope fits nothing", clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", Scope: Scope{Serial: "S1"}}), status: 400, reason: fserr.Malformed},
		{name: "scope not allowed for the flag", clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", Scope: realSc}), status: 400, reason: fserr.Malformed},
		{name: "game_kind shadow fits no scope", clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", Scope: Scope{GameKind: "shadow"}}), status: 400, reason: fserr.Malformed},
		{name: "experiment id without an arm fits no scope", clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", Scope: Scope{ExperimentID: "e1"}}), status: 400, reason: fserr.Malformed},
		{name: "scope fit beats set not served", opts: fixtureOpts{served: []string{"agent"}}, clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", Scope: realSc}), status: 400, reason: fserr.Malformed},
		{name: "set not served", opts: fixtureOpts{served: []string{"agent"}}, clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams"}), status: 409, reason: fserr.SetNotServed},
		{name: "set not served beats no stream", opts: fixtureOpts{served: []string{"agent"}, noStream: true}, clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams"}), status: 409, reason: fserr.SetNotServed},
		{name: "no sync stream", opts: fixtureOpts{noStream: true}, clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams"}), status: 503, reason: fserr.NoSyncStream},
		{name: "no sync stream beats persist-fit", opts: fixtureOpts{noStream: true}, clr: clearReq(ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: realSc, Persist: true}), status: 503, reason: fserr.NoSyncStream},
		{name: "persist outside the stack scope", clr: clearReq(ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: realSc, Persist: true}), status: 400, reason: fserr.Malformed},
		{name: "if_writer outside real", clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", IfWriter: "menu"}), status: 400, reason: fserr.Malformed},
		{name: "if_writer outside real beats a live game", setup: liveReal, clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", IfWriter: "menu", UnlessRealGame: true}), status: 400, reason: fserr.Malformed},
		{name: "guarded revert, real game live", setup: liveReal, clr: clearReq(ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: realSc, UnlessRealGame: true}), status: 409, reason: fserr.RealGameLive},
		{name: "guarded revert beats if_writer mismatch", setup: func(t *testing.T, f *fixture) {
			f.put(guardPut)
			liveReal(t, f)
		}, clr: clearReq(ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: realSc, UnlessRealGame: true, IfWriter: "other"}), status: 409, reason: fserr.RealGameLive},
		{name: "plain clear ignores a live real game (kill switch revert)", setup: func(t *testing.T, f *fixture) {
			f.put(guardPut)
			liveReal(t, f)
		}, clr: &guardClear, cleared: 1},
		{name: "guarded revert, no real game", setup: func(t *testing.T, f *fixture) { f.put(guardPut) }, clr: clearReq(ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: realSc, UnlessRealGame: true}), cleared: 1},
		{name: "guarded revert of an unguarded flag ignores a live game", setup: liveReal, clr: clearReq(ClearRequest{Set: "game", Flag: "sensitivity", Scope: realSc, UnlessRealGame: true})},
		{name: "guarded revert in a non-real scope ignores a live real game", setup: func(t *testing.T, f *fixture) {
			f.put(*withReq(guardPut, func(r *WriteRequest) { r.Scope = expSc }))
			liveReal(t, f)
		}, clr: clearReq(ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: expSc, UnlessRealGame: true}), cleared: 1},
		{name: "if_writer mismatch", setup: func(t *testing.T, f *fixture) { f.put(guardPut) }, clr: clearReq(ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: realSc, IfWriter: "other"}), status: 412, reason: fserr.WriterMismatch},
		{name: "if_writer match", setup: func(t *testing.T, f *fixture) { f.put(guardPut) }, clr: clearReq(ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: realSc, IfWriter: "agent"}), cleared: 1},
		{name: "if_writer with no overlay is an idempotent success", clr: clearReq(ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: realSc, IfWriter: "agent"})},
		// DELETE has no id-length check and no game-live check today (answers pinned as they are)
		{name: "long id is not refused", clr: clearReq(ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: Scope{ExperimentID: longID, Arm: "a"}})},
		{name: "long writer is not refused", clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", Writer: longID})},
		{name: "game scope of a game that is not live is not refused", clr: clearReq(ClearRequest{Set: "interventions", Flag: "volume_override", Scope: Scope{GameID: "gx"}})},
		{name: "game+serial scope with a serial not in the game is not refused", setup: func(t *testing.T, f *fixture) { f.register("g1", "c1", "S1") }, clr: clearReq(ClearRequest{Set: "interventions", Flag: "shield_seconds", Scope: Scope{GameID: "g1", Serial: "S9"}})},
		{name: "game+serial flag with only a game id", clr: clearReq(ClearRequest{Set: "interventions", Flag: "shield_seconds", Scope: Scope{GameID: "g1"}}), status: 400, reason: fserr.Malformed},
		// successes
		{name: "event list clear, empty list", setup: func(t *testing.T, f *fixture) { f.register("g1", "c1") }, clr: clearReq(ClearRequest{Set: "interventions", Flag: "end_game", Scope: Scope{GameID: "g1"}})},
		{name: "stack clear, nothing there", clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams"})},
		{name: "stack clear", setup: func(t *testing.T, f *fixture) { f.put(stackPut) }, clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams"}), cleared: 1},
		{name: "experiment clear", setup: func(t *testing.T, f *fixture) { f.put(*withReq(guardPut, func(r *WriteRequest) { r.Scope = expSc })) }, clr: clearReq(ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: expSc}), cleared: 1},
		{name: "fraction clear", setup: func(t *testing.T, f *fixture) {
			f.put(WriteRequest{Set: "controller", Flag: "bluetooth_backend", Variant: "rust", Scope: Scope{FractionPercent: intp(10)}})
		}, clr: clearReq(ClearRequest{Set: "controller", Flag: "bluetooth_backend", Scope: Scope{FractionPercent: intp(10)}}), cleared: 1},
		{name: "game clear", setup: func(t *testing.T, f *fixture) {
			f.register("g1", "c1")
			f.put(WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "g1"}})
		}, clr: clearReq(ClearRequest{Set: "interventions", Flag: "volume_override", Scope: Scope{GameID: "g1"}}), cleared: 1},
		{name: "game+serial clear", setup: func(t *testing.T, f *fixture) {
			f.register("g1", "c1", "S1")
			f.put(WriteRequest{Set: "interventions", Flag: "shield_seconds", Value: raw("5"), Scope: Scope{GameID: "g1", Serial: "S1"}})
		}, clr: clearReq(ClearRequest{Set: "interventions", Flag: "shield_seconds", Scope: Scope{GameID: "g1", Serial: "S1"}}), cleared: 1},
		{name: "event list clear", setup: func(t *testing.T, f *fixture) {
			f.register("g1", "c1")
			if _, err := f.state.Append(AppendRequest{Set: "interventions", Flag: "end_game", Scope: Scope{GameID: "g1"}}); err != nil {
				t.Fatal(err)
			}
		}, clr: clearReq(ClearRequest{Set: "interventions", Flag: "end_game", Scope: Scope{GameID: "g1"}}), cleared: 1},
		// persistence
		{name: "persisted clear", persist: true, setup: func(t *testing.T, f *fixture) { f.put(plainPers) }, clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", Persist: true}), cleared: 1, persisted: true},
		{name: "persisted clear, nothing persisted", persist: true, clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", Persist: true})},
		{name: "persisted clear leaves the in-memory overlay", persist: true, setup: func(t *testing.T, f *fixture) { f.put(stackPut) }, clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", Persist: true})},
		{name: "persisted clear, save fails: refused", persist: true, breakFile: true, setup: func(t *testing.T, f *fixture) { f.put(plainPers) }, clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", Persist: true}), status: 500, reason: fserr.ReasonPersistFailed},
		{name: "persisted kill switch clear, save fails: cleared, not persisted", persist: true, breakFile: true, setup: func(t *testing.T, f *fixture) { f.put(killPut) }, clr: clearReq(ClearRequest{Set: "agent", Flag: "interventions_allowed", Persist: true}), cleared: 1, failed: true},
		{name: "persisted clear, newer state file, flag persistable: refused", persist: true, futureVer: true, clr: clearReq(ClearRequest{Set: "game", Flag: "num_teams", Persist: true}), status: 500, reason: fserr.ReasonPersistFailed},
		{name: "persisted clear, newer state file, kill switch: not persisted", persist: true, futureVer: true, clr: clearReq(ClearRequest{Set: "agent", Flag: "interventions_allowed", Persist: true}), failed: true},
		{name: "persisted clear, newer state file, flag that cannot persist: plain success", persist: true, futureVer: true, clr: clearReq(ClearRequest{Set: "agent", Flag: "enabled", Persist: true})},
	}
	for _, c := range cases {
		c := c
		t.Run(c.name, func(t *testing.T) { runMatrixCase(t, c) })
	}
}

func runMatrixCase(t *testing.T, c matrixCase) {
	t.Helper()
	opts := c.opts
	var path string
	if c.persist {
		path = filepath.Join(t.TempDir(), "admin.json")
		if c.futureVer {
			doc := `{"version":99,"values":[]}`
			if err := os.WriteFile(path, []byte(doc), 0o600); err != nil {
				t.Fatal(err)
			}
		}
		opts.stateFile = path
	}
	f := newFixture(t, opts)
	if c.setup != nil {
		c.setup(t, f)
	}
	if c.breakFile {
		if _, statErr := os.Stat(path); statErr != nil {
			if werr := os.WriteFile(path, nil, 0o600); werr != nil {
				t.Fatal(werr)
			}
		}
		breakStateFile(t, path)
	}
	beforeDump, beforeVersion := dumpState(f.state), f.state.Version()
	var beforeStack, beforeAdmin *value
	if key := c.flagKey(); f.state.flags[key] != nil {
		beforeStack, beforeAdmin = f.state.flags[key].stack, f.state.flags[key].admin
	}
	var res WriteResult
	var err *fserr.Error
	switch {
	case c.put != nil:
		res, err = f.state.Put(*c.put)
	case c.clr != nil:
		res, err = f.state.Clear(*c.clr)
	default:
		t.Fatal("case has neither put nor clear")
	}
	if c.status != 0 {
		wantReason(t, err, c.status, c.reason)
		// A refusal changes nothing: no slot, no version.
		if got := dumpState(f.state); got != beforeDump {
			t.Fatalf("a refused call changed the state:\nbefore %s\nafter  %s", beforeDump, got)
		}
		if got := f.state.Version(); got != beforeVersion {
			t.Fatalf("a refused call moved the version %d -> %d", beforeVersion, got)
		}
		return
	}
	if err != nil {
		t.Fatalf("want success, got %d %s (%s)", err.Status, err.Reason, err.Detail)
	}
	if c.clr != nil && res.Cleared != c.cleared {
		t.Fatalf("cleared %d, want %d", res.Cleared, c.cleared)
	}
	if res.Persisted != c.persisted {
		t.Fatalf("persisted %v, want %v", res.Persisted, c.persisted)
	}
	if res.PersistFailed != c.failed {
		t.Fatalf("persist failed header %v, want %v", res.PersistFailed, c.failed)
	}
	c.checkEffects(t, f, res, path, beforeDump, beforeVersion, beforeStack, beforeAdmin)
	if c.extra != nil {
		c.extra(t, f)
	}
}

func (c matrixCase) flagKey() string {
	if c.put != nil {
		return c.put.Flag
	}
	return c.clr.Flag
}

// checkEffects pins what a successful call leaves behind: the slot, the
// version and the persisted marker (in memory and in the state file).
func (c matrixCase) checkEffects(t *testing.T, f *fixture, res WriteResult, path, beforeDump string, beforeVersion uint64, beforeStack, beforeAdmin *value) {
	t.Helper()
	fl := f.state.flags[c.flagKey()]
	version := f.state.Version()
	changed := false
	switch {
	case c.put != nil && c.put.DryRun:
		// A dry run renders the write and leaves no trace.
	case c.put != nil:
		changed = true
		scope := c.put.Scope.Name()
		slot := slotOf(fl, scope, c.put.Scope)
		if scope == registry.ScopeStack && c.put.Persist {
			slot = fl.admin
			if fl.stack != nil {
				t.Fatalf("a persisted stack write must replace the in-memory overlay, stack holds %+v", *fl.stack)
			}
		}
		if slot == nil {
			t.Fatalf("the write did not land in the %s slot", scope)
		}
		if slot.Variant != res.Variant || slot.Writer != c.put.Writer {
			t.Fatalf("slot holds variant %q writer %q, want %q / %q", slot.Variant, slot.Writer, res.Variant, c.put.Writer)
		}
		if slot.Persisted != c.marked {
			t.Fatalf("slot persisted marker %v, want %v", slot.Persisted, c.marked)
		}
		c.checkFile(t, path, c.put.Flag, c.persisted)
	default:
		changed = res.Cleared > 0
		scope := c.clr.Scope.Name()
		switch {
		case scope == registry.ScopeStack && c.clr.Persist:
			if fl.admin != nil {
				t.Fatalf("the persisted value survived the clear: %+v", *fl.admin)
			}
			if fl.stack != beforeStack {
				t.Fatalf("a persisted clear touched the in-memory overlay")
			}
		case scope == registry.ScopeStack:
			if fl.stack != nil {
				t.Fatalf("the stack overlay survived the clear: %+v", *fl.stack)
			}
			if fl.admin != beforeAdmin {
				t.Fatalf("a plain clear touched the persisted value")
			}
		case fl.policy.Kind == registry.KindEvent:
			if list := fl.events[c.clr.Scope.GameID]; list != nil && len(list.Events) != 0 {
				t.Fatalf("the event list survived the clear: %+v", list.Events)
			}
		default:
			if slotOf(fl, scope, c.clr.Scope) != nil {
				t.Fatalf("the %s slot survived the clear", scope)
			}
		}
		if c.persisted {
			c.checkFile(t, path, c.clr.Flag, false)
		}
	}
	if changed {
		if version != beforeVersion+1 || res.SnapshotVersion != version {
			t.Fatalf("version %d (answer %d), want %d", version, res.SnapshotVersion, beforeVersion+1)
		}
		return
	}
	if version != beforeVersion {
		t.Fatalf("a call that changed nothing moved the version %d -> %d", beforeVersion, version)
	}
	if got := dumpState(f.state); got != beforeDump {
		t.Fatalf("a call that changed nothing changed the state:\nbefore %s\nafter  %s", beforeDump, got)
	}
}

// checkFile: the state file lists the flag (want) or does not.
func (c matrixCase) checkFile(t *testing.T, path, flag string, want bool) {
	t.Helper()
	if path == "" || !c.persisted {
		return
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if got := regexp.MustCompile(`"flag":\s*"` + flag + `"`).Match(data); got != want {
		t.Fatalf("state file lists %s: %v, want %v (%s)", flag, got, want, data)
	}
}

// slotOf is the slot a scope addresses (nil when empty).
func slotOf(fl *flagState, scope string, sc Scope) *value {
	switch scope {
	case registry.ScopeStack:
		return fl.stack
	case registry.ScopeReal:
		return fl.real
	case registry.ScopeFraction:
		return fl.fraction
	case registry.ScopeGame:
		return fl.games[sc.GameID]
	case registry.ScopeGameSerial:
		return fl.serials[sc.GameID][sc.Serial]
	case registry.ScopeExperiment:
		return fl.experiments[sc.ExperimentID][sc.Arm]
	}
	return nil
}

// dumpState renders every slot of every flag, deterministically.
func dumpState(s *State) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	dv := func(v *value) string {
		if v == nil {
			return "-"
		}
		return fmt.Sprintf("%+v", *v)
	}
	var keys []string
	for k := range s.flags {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		fl := s.flags[k]
		fmt.Fprintf(&b, "%s{stack:%s admin:%s real:%s frac:%s", k, dv(fl.stack), dv(fl.admin), dv(fl.real), dv(fl.fraction))
		for _, id := range matrixKeys(fl.games) {
			fmt.Fprintf(&b, " g[%s]:%s", id, dv(fl.games[id]))
		}
		for _, id := range matrixKeys(fl.serials) {
			for _, sn := range matrixKeys(fl.serials[id]) {
				fmt.Fprintf(&b, " s[%s/%s]:%s", id, sn, dv(fl.serials[id][sn]))
			}
		}
		for _, id := range matrixKeys(fl.experiments) {
			for _, arm := range matrixKeys(fl.experiments[id]) {
				fmt.Fprintf(&b, " x[%s/%s]:%s", id, arm, dv(fl.experiments[id][arm]))
			}
		}
		for _, id := range matrixKeys(fl.events) {
			fmt.Fprintf(&b, " e[%s]:%+v", id, *fl.events[id])
		}
		b.WriteString("}\n")
	}
	return b.String()
}

func matrixKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// Every registry-less baseline flag is read-only for both verbs, whatever else
// is wrong with the request.
// UC: UC-053 main
func TestPreconditionMatrix_ReadOnlyFlag(t *testing.T) {
	f := newFixture(t, fixtureOpts{})
	var keys []string
	for k, fl := range f.state.flags {
		if fl.policy == nil {
			keys = append(keys, k)
		}
	}
	if len(keys) == 0 {
		t.Skip("every baseline flag has a registry row")
	}
	sort.Strings(keys)
	fl := f.state.flags[keys[0]]
	_, err := f.state.Put(WriteRequest{Set: fl.set, Flag: keys[0], Variant: "x", Scope: Scope{GameID: longID}})
	wantReason(t, err, 422, fserr.FlagReadOnly)
	_, err = f.state.Clear(ClearRequest{Set: fl.set, Flag: keys[0], Scope: realSc, Persist: true})
	wantReason(t, err, 422, fserr.FlagReadOnly)
}
