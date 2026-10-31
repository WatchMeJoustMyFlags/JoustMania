package main

import (
	"context"
	"encoding/json"
	"sort"
	"strings"
	"sync"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/flagservice"
)

// dynamic_declarer.go is the HEURISTIC (no-LLM) dynamic experiment declarer
// (#995): instead of only the experiment seed (the experiment_seed flag, one
// operator-named flag), the agent CHOOSES which existing game.json flag to
// experiment on by itself, using observed signals — so the parameterized surface
// gets explored autonomously. It is the unblocked autonomy increment: the LLM
// Proposer→Intents path (#960) is deferred (inert until the inference backend
// #738/#742 lands); this delivers dynamic declaration NOW.
//
// SAFETY BOUNDARY — the declarer ADDS NO new write surface. It only ever picks:
//   - an EXISTING game.json flag (never invents flags — the Gate's ReasonUnknownFlag
//     still rejects a non-existent key), from a configurable candidate set;
//   - an experimental value that is one of the flag's OWN existing variant values
//     (a bounded, pre-vetted, JSON-kind-compatible perturbation of the default —
//     it passes the Gate's checkTypeCompatible by construction); and
//   - an EXPERIMENTABLE objective (decision.IsExperimentable; chaos is never
//     chosen and is rejected at Registry.Declare anyway).
//
// Every Intent it builds therefore flows through the SAME #977 Gate + #978
// Registry caps as the experiment seed; the declarer is a SELECTION heuristic in front
// of the unchanged declare path, not a new privilege.
//
// GATING — default-off, byte-identical for everyone who does not opt in. The
// declarer only runs when the experiment_dynamic_enabled flag is on AND the rest
// of the experiment loop's gates are satisfied (experiments_enabled + the
// agent.json kill-switch — the loop only invokes the declarer from
// allocateIfEnabled, which already self-aborts when either is off). Both flags
// fail closed when unresolved; there is no env fallback (#1907).

// The declarer's settings (candidate list, denylist additions, default objective,
// target N) are the experiment_dynamic_* agent flags (#1907, flags.md §15), resolved
// per tick in flags.ExperimentConfig and applied with configure under mu before every
// Declare. They replaced the AGENT_EXPERIMENT_DYNAMIC_* env vars read once at start.

// defaultDynamicDenylist is the set of game.json flag keys the autonomous declarer
// never experiments on: agent/experiment-control
// knobs that are the declarer's OWN plumbing, not game-tuning parameters. The
// declarer must never experiment on its own machinery (e.g. shadow_policy=block
// would block the very shadow games the experiment needs to accrue samples).
// It is a FLOOR (UC-070 BR-008, C-034, #1907): it always applies, also to an explicit
// candidate list, and the experiment_dynamic_denylist flag can only ADD keys to it.
//
// #1316 widened it to the platform plumbing, lobby/admin and ceremony timing flags:
// they are not game tuning, an experiment on them measures nothing about play, and
// in the talk's screen-only fallback (UC-070 A9) the audience sees the experiment
// names. UC-070 BR-008.
var defaultDynamicDenylist = map[string]struct{}{
	// shadow_policy (#837): gates whether shadow games may start at all — tuning it
	// to "block" would starve the declarer's own experiments. Pure agent plumbing.
	"shadow_policy": {},
	// Session capacity (#1018): how many games may run at once, not how one plays.
	"max_concurrent_games": {},
	"max_shadow_games":     {},
	// Lobby / admin start controls: when the menu force-starts, not the game itself.
	"force_all_start":         {},
	"force_all_start_seconds": {},
	// Ceremony timing around the game (countdown, winner animation).
	"countdown_phase_duration_ms": {},
	"winner_rainbow_duration_ms":  {},
}

// pressureFunc returns the current per-objective failing-fitness pressure
// (objective → pressure in [0,1], higher = more failing). It is the selection
// SIGNAL: the declarer targets the objective under highest pressure when one is
// available. nil (or an empty map) ⇒ no signal, so the declarer falls back to the
// configured default objective and pure round-robin flag rotation. Injected so it
// is deterministic / testable (the production wiring may pass nil until a live
// pressure source is wired — the declarer is correct either way).
type pressureFunc func() map[string]float64

// dynamicDeclarer selects and builds a fresh experiment Intent on its own from
// the live game.json flag surface (#995). It holds an in-memory round-robin cursor
// (resets on restart) so successive declarations cover DIFFERENT candidate flags
// deterministically (no wall-clock / rand — variety comes from the cursor, the
// #995 testability rule).
//
// CONCURRENCY (#1040 review): the whole select→budget-check→dedup→declare path is
// reached CONCURRENTLY from several experiment-loop goroutines (tick, immediate
// allocate, per-partition onGameEnd, onGameTerminal drive, fireCompletedGrace
// timer). The declarer exposes mu so the loop can serialize that ENTIRE sequence
// (LiveCount + ActiveFlags + cursor-advance + Declare) atomically — closing the
// TOCTOU over-declaration window and the d.cursor data race. mu wraps the registry
// calls (never the reverse) and the registry never calls back into the declarer, so
// there is no lock-ordering deadlock with registry.mu.
type dynamicDeclarer struct {
	// mu serializes the loop's declare decision path (see the type doc). It is held
	// by the caller (declareDynamicIfEnabled) around LiveCount/ActiveFlags/Declare so
	// only ONE dynamic declare is ever in flight; it also guards d.cursor, which
	// Declare mutates.
	mu sync.Mutex

	// gameFlags reads the game flag set from the flag service: the experiment
	// catalog (flags whose registry row allows experiment+arm, flags.md §13.3)
	// with their current defaults and variants (R2, #1415; read each call).
	gameFlags gameFlagReader
	// candidates, when non-empty, restricts/orders the flags the declarer rotates
	// over. Empty ⇒ all tunable flags discovered in game.json (sorted, stable).
	// Either way the effective denylist filters it (UC-070 BR-008).
	candidates []string
	// denylist holds the keys the experiment_dynamic_denylist flag ADDS to
	// defaultDynamicDenylist. The built-in list always applies on top of it, so
	// this field can only tighten the rotation (UC-070 BR-008, C-034, #1907).
	denylist map[string]struct{}
	// defaultObjective is the experimentable objective used when no pressure signal
	// is available.
	defaultObjective string
	// targetN is the target games-per-arm stamped on each declared Intent (0 ⇒ the
	// Registry reconciles to min-N).
	targetN int
	// pressure is the optional fitness-pressure selection signal (may be nil).
	pressure pressureFunc
	// hold gates the hold-mode shield keys (UC-043 A17); nil offers none of them.
	hold *holdGate

	// cursor is the in-memory round-robin position across candidate flags (resets to
	// 0 on restart — not journaled). It only advances when a declaration is actually
	// emitted, so a tick that finds no declarable flag does not skip the surface. NOT
	// a clock — pure rotation. Mutated only under mu (held by the loop's declare path).
	cursor int
}

// newDynamicDeclarer builds the declarer with the compiled defaults (every tunable
// flag, only the built-in denylist, objective balanced, target 0). pressure is the
// optional live signal seam (nil ⇒ default-objective fallback).
//
// #1044: the declarer is ALWAYS built — the on/off gate is the LIVE
// experiment_dynamic_enabled flag, checked PER TICK by the loop
// (declareDynamicIfEnabled); an unresolved flag reads false (#1907). Its settings
// are the experiment_dynamic_* flags, applied per tick with configure (#1907).
func newDynamicDeclarer(gameFlags gameFlagReader, pressure pressureFunc) *dynamicDeclarer {
	return &dynamicDeclarer{
		gameFlags:        gameFlags,
		defaultObjective: decision.ObjectiveBalanced,
		pressure:         pressure,
	}
}

// configure applies the live experiment_dynamic_* settings of one tick (#1907). The
// caller holds d.mu (the same lock that guards Declare). An empty, blank or
// unresolved list means no candidate restriction / no denylist additions; the
// built-in denylist is applied in candidateOrder whatever cfg says (UC-070 BR-008).
// A non-experimentable objective (empty, unknown, chaos) becomes balanced, and a
// negative target N becomes 0.
func (d *dynamicDeclarer) configure(cfg flags.ExperimentConfig) {
	d.candidates = parseCandidates(cfg.DynamicCandidates)
	d.denylist = nil
	if keys := parseCandidates(cfg.DynamicDenylist); len(keys) > 0 {
		d.denylist = make(map[string]struct{}, len(keys))
		for _, k := range keys {
			d.denylist[k] = struct{}{}
		}
	}
	d.defaultObjective = strings.TrimSpace(cfg.DynamicObjective)
	// IsExperimentable accepts "" (the registry's own default), so empty is mapped here.
	if d.defaultObjective == "" || !decision.IsExperimentable(d.defaultObjective) {
		d.defaultObjective = decision.ObjectiveBalanced
	}
	d.targetN = max(cfg.DynamicTargetN, 0)
}

// activeFlags is the set of flag keys that already have a live (non-terminal)
// experiment. The declarer skips them so it never stacks a second concurrent
// experiment on a flag that is already under test (the #995 dedup rule). It is
// supplied by the loop from the Registry's live view.
type activeFlags map[string]struct{}

// Declare selects ONE fresh experiment Intent from the candidate surface, or
// returns ok=false when nothing is declarable (no readable candidate flag without
// an active experiment, or no flag has a distinct alternate value). It advances
// the round-robin cursor only when it emits an Intent, so successive Declares
// cover different flags. It performs NO writes and consults NO clock — given the
// same inputs (file, active set, cursor, pressure) it is fully deterministic.
//
// A key the shadow game's mode does not read (catalogKeyModes, #2392) is never
// offered: an arm value on it could not change a shadow game (UC-043 BR-009).
//
// The caller (the loop) is responsible for the capacity check (registry caps) and
// for actually calling Registry.Declare/Start with the returned Intent; this
// keeps the heuristic pure and unit-testable in isolation from the registry.
func (d *dynamicDeclarer) Declare(active activeFlags) (experiment.Intent, bool) {
	catalog := readGameCatalog(d.gameFlags)
	flags := candidatesOf(catalog)
	if len(flags) == 0 {
		return experiment.Intent{}, false
	}
	// UC-043 A17 step 1: a hold key only under its conditions (nil gate: none).
	offerable := d.hold.filter(context.Background(), catalog)
	order := d.candidateOrder(flags)
	if len(order) == 0 {
		return experiment.Intent{}, false
	}

	objective := d.selectObjective()
	// UC-043 BR-009 (#2392): only a key the shadow game's mode reads can change it.
	mode := shadowGameMode()

	// Round-robin scan from the persisted cursor: the FIRST candidate that has no
	// active experiment and a distinct alternate value wins. Scanning the whole
	// ring guarantees we find a declarable flag if one exists, while the cursor
	// start makes successive calls prefer the next flag (rotation/coverage).
	n := len(order)
	for i := 0; i < n; i++ {
		idx := (d.cursor + i) % n
		key := order[idx]
		if active != nil {
			if _, busy := active[key]; busy {
				continue
			}
		}
		if !offerable(key) || !offeredInShadowMode(key, mode) {
			continue
		}
		value, ok := perturbValue(flags[key])
		if key == experiment.HoldActivationKey {
			// UC-043 A17 step 1: toward hold only, never the next variant in sort order.
			value, ok = experiment.HoldValue, true
		}
		if !ok {
			continue // no distinct alternate value to try (e.g. single-variant flag).
		}
		// Emit on this flag; advance the cursor PAST it so the next Declare starts at
		// the following candidate (deterministic rotation across the surface).
		d.cursor = (idx + 1) % n
		return experiment.Intent{
			Hypothesis:        d.hypothesis(key, value, objective),
			FlagKey:           key,
			ExperimentalValue: value,
			Objective:         objective,
			TargetNPerArm:     d.targetN,
		}, true
	}
	return experiment.Intent{}, false
}

// candidateFlag is the declarer's read model of one game.json flag: its current
// default value and the full set of (variant name → value) it may perturb toward.
type candidateFlag struct {
	defaultValue any
	variants     map[string]json.RawMessage
}

// candidateOrder returns the candidate flag keys in the declarer's rotation order:
// the configured candidate list (filtered to flags actually present + tunable, and
// through the effective denylist), or
// — when no list is configured — every discovered tunable flag in a STABLE sorted
// order (deterministic rotation independent of map iteration).
func (d *dynamicDeclarer) candidateOrder(flags map[string]candidateFlag) []string {
	if len(d.candidates) > 0 {
		order := make([]string, 0, len(d.candidates))
		for _, k := range d.candidates {
			if _, ok := flags[k]; ok && !d.denied(k) {
				order = append(order, k)
			}
		}
		return order
	}
	order := make([]string, 0, len(flags))
	for k := range flags {
		if d.denied(k) {
			continue // agent/experiment-control knob — never tune our own machinery.
		}
		order = append(order, k)
	}
	sort.Strings(order)
	return order
}

// denied reports whether key is on the effective denylist: the built-in floor
// defaultDynamicDenylist, which always applies, plus the flag's additions
// (UC-070 BR-008, C-034: the flag can only tighten, #1907).
func (d *dynamicDeclarer) denied(key string) bool {
	if builtinDenied(key) {
		return true
	}
	_, ok := d.denylist[key]
	return ok
}

// builtinDenied reports whether key is on the built-in floor
// defaultDynamicDenylist. The declarer and the proposer vocabulary both apply it
// (UC-070 BR-008, C-034, #1961).
func builtinDenied(key string) bool {
	_, ok := defaultDynamicDenylist[key]
	return ok
}

// selectObjective picks the experimentable objective the declared experiment
// optimizes: the objective under HIGHEST fitness pressure when a pressure signal
// is available (target what is failing most), else the configured default. A
// non-experimentable objective (chaos) is never returned — it has no fitness
// function and would only fold worst-case 0 (and Registry.Declare rejects it).
func (d *dynamicDeclarer) selectObjective() string {
	if d.pressure != nil {
		if obj, ok := highestPressureObjective(d.pressure()); ok {
			return obj
		}
	}
	return d.defaultObjective
}

// highestPressureObjective returns the experimentable objective with the greatest
// positive pressure, or ok=false when no experimentable objective has positive
// pressure. Ties break by objective name (deterministic).
func highestPressureObjective(pressure map[string]float64) (string, bool) {
	best := ""
	bestP := 0.0
	for obj, p := range pressure {
		if p <= 0 || !decision.IsExperimentable(obj) {
			continue
		}
		if p > bestP || (p == bestP && (best == "" || obj < best)) {
			best = obj
			bestP = p
		}
	}
	return best, best != ""
}

// gameFlagReader reads one flag set from the flag service (flagservice.Client).
type gameFlagReader interface {
	GetSet(ctx context.Context, set string) (flagservice.SetView, error)
}

// experimentScope is the registry scope of an experiment-arm write.
const experimentScope = "experiment+arm"

// readGameCatalog returns the experiment catalog: every game flag whose registry
// row allows an experiment-arm write, keyed by flag. A read error returns nil
// (fail-closed: nothing is declarable or proposable until the flag service
// answers again).
func readGameCatalog(r gameFlagReader) map[string]flagservice.FlagView {
	catalog, err := readGameCatalogCtx(context.Background(), r)
	if err != nil {
		return nil
	}
	return catalog
}

// readGameCatalogCtx is readGameCatalog with the read error (a nil reader is an
// empty catalog, not an error).
func readGameCatalogCtx(ctx context.Context, r gameFlagReader) (map[string]flagservice.FlagView, error) {
	if r == nil {
		return nil, nil
	}
	ctx, cancel := context.WithTimeout(ctx, flagservice.RequestTimeout)
	defer cancel()
	set, err := r.GetSet(ctx, experiment.GameFlagSet)
	if err != nil {
		return nil, err
	}
	out := make(map[string]flagservice.FlagView, len(set.Flags))
	for _, f := range set.Flags {
		if f.Policy.AllowsScope(experimentScope) {
			out[f.Flag] = f
		}
	}
	return out, nil
}

// readCandidateFlags returns every TUNABLE flag of the experiment catalog (one
// with at least two variants — a single-variant flag has no alternate value to
// experiment toward) as a candidateFlag (its baseline default value + variants).
func (d *dynamicDeclarer) readCandidateFlags() map[string]candidateFlag {
	return candidatesOf(readGameCatalog(d.gameFlags))
}

// candidatesOf is readCandidateFlags over an already-read catalog.
func candidatesOf(catalog map[string]flagservice.FlagView) map[string]candidateFlag {
	out := make(map[string]candidateFlag, len(catalog))
	for key, f := range catalog {
		if len(f.VariantValues) < 2 {
			continue // not tunable: no alternate value to perturb toward.
		}
		dv, ok := f.VariantValues[f.BaselineDefault]
		if !ok {
			continue // malformed: default points at a missing variant — skip.
		}
		var def any
		if err := json.Unmarshal(dv, &def); err != nil {
			continue
		}
		out[key] = candidateFlag{defaultValue: def, variants: f.VariantValues}
	}
	return out
}

// perturbValue picks a BOUNDED, JSON-kind-compatible experimental value for a
// flag: the variant value ADJACENT to the current default in the flag's own
// sorted variant ordering (a small, pre-vetted step off the default). Choosing an
// EXISTING variant value guarantees the value passes the Gate's type check by
// construction (it is, by definition, the same kind as the flag's variants) and
// stays within the flag's intended range — the safest possible perturbation. It
// returns ok=false when the flag has no variant value distinct from its default.
func perturbValue(f candidateFlag) (any, bool) {
	defRaw, err := json.Marshal(f.defaultValue)
	if err != nil {
		return nil, false
	}
	// Stable order over variant NAMES so the choice is deterministic regardless of
	// map iteration order.
	names := make([]string, 0, len(f.variants))
	for name := range f.variants {
		names = append(names, name)
	}
	sort.Strings(names)

	defIdx := -1
	for i, name := range names {
		if jsonRawEqual(f.variants[name], defRaw) {
			defIdx = i
			break
		}
	}
	// Prefer the variant immediately AFTER the default (wrapping), so successive
	// flags step consistently off their default; fall back to the first distinct
	// variant when the default is not found among the variant values.
	start := 0
	if defIdx >= 0 {
		start = defIdx + 1
	}
	for off := 0; off < len(names); off++ {
		name := names[(start+off)%len(names)]
		cand := f.variants[name]
		if jsonRawEqual(cand, defRaw) {
			continue // same as default — not a perturbation.
		}
		var v any
		if err := json.Unmarshal(cand, &v); err != nil {
			continue
		}
		return v, true
	}
	return nil, false
}

// hypothesis builds the human-readable hypothesis string stamped on a dynamically
// declared Intent (mirrors the experiment seed's shape). It records that this was
// the agent's own heuristic choice and what it is trying.
func (d *dynamicDeclarer) hypothesis(flagKey string, value any, objective string) string {
	vb, err := json.Marshal(value)
	v := string(vb)
	if err != nil {
		v = "<value>"
	}
	return "dynamic declarer (#995): try " + flagKey + "=" + v +
		" to improve objective " + objective
}

// parseCandidates splits a comma-separated flag-key list (the candidates or the
// denylist additions) into trimmed, non-empty keys, dropping duplicates and keeping
// the first occurrence (a duplicate candidate would bias the round-robin). Keys
// stay case-sensitive. Empty or blank input ⇒ nil.
func parseCandidates(raw string) []string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	seen := make(map[string]struct{}, len(parts))
	for _, p := range parts {
		k := strings.TrimSpace(p)
		if k == "" {
			continue
		}
		if _, dup := seen[k]; dup {
			continue
		}
		seen[k] = struct{}{}
		out = append(out, k)
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// jsonRawEqual compares two raw JSON values by their decoded form (so 4 and 4.0
// compare equal, and whitespace differences are ignored) — the same kind-aware
// equality the Gate relies on for numbers.
func jsonRawEqual(a, b json.RawMessage) bool {
	var av, bv any
	if err := json.Unmarshal(a, &av); err != nil {
		return false
	}
	if err := json.Unmarshal(b, &bv); err != nil {
		return false
	}
	ab, err1 := json.Marshal(av)
	bb, err2 := json.Marshal(bv)
	if err1 != nil || err2 != nil {
		return false
	}
	return string(ab) == string(bb)
}
