package main

import (
	"encoding/json"
	"os"
	"sort"
	"strings"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
)

// dynamic_declarer.go is the HEURISTIC (no-LLM) dynamic experiment declarer
// (#995): instead of only the static AGENT_EXPERIMENT_SEED_FLAG env seed (one
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
// Registry caps as the env seed; the declarer is a SELECTION heuristic in front
// of the unchanged declare path, not a new privilege.
//
// GATING — default-off, byte-identical for everyone who does not opt in. The
// declarer only runs when AGENT_EXPERIMENT_DYNAMIC_ENABLED=true AND the rest of
// the experiment loop's gates are satisfied (AGENT_EXPERIMENTS_ENABLED + the
// agent.json kill-switch — the loop only invokes the declarer from
// allocateIfEnabled, which already self-aborts when the kill-switch is off).

const (
	// envDynamicEnabled is the opt-in for the heuristic dynamic declarer (#995).
	// Default false ⇒ the loop declares ONLY the static env seed (if any),
	// byte-identical to pre-#995 behavior. Subordinate to AGENT_EXPERIMENTS_ENABLED
	// and the kill-switch.
	envDynamicEnabled = "AGENT_EXPERIMENT_DYNAMIC_ENABLED"

	// envDynamicCandidates overrides the candidate game.json flag set the declarer
	// rotates over: a comma-separated list of flag keys. Empty/unset ⇒ ALL tunable
	// game.json flags (every flag with at least two variants). A key that is not
	// present in game.json is skipped (the Gate would reject it anyway).
	envDynamicCandidates = "AGENT_EXPERIMENT_DYNAMIC_CANDIDATES"

	// envDynamicObjective overrides the default objective the declarer uses when no
	// fitness-pressure signal is available. Default balanced (experimentable). A
	// non-experimentable value (chaos) falls back to balanced.
	envDynamicObjective = "AGENT_EXPERIMENT_DYNAMIC_OBJECTIVE"

	// envDynamicTargetN overrides the target games-per-arm for dynamically declared
	// experiments. Non-positive/unset ⇒ 0 (the Registry reconciles it up to the
	// verdict min-N, #1001).
	envDynamicTargetN = "AGENT_EXPERIMENT_DYNAMIC_TARGET_N"
)

// dynamicEnabled reports whether the heuristic dynamic declarer opt-in is on.
// This is the distinct, default-off #995 gate — independent of (and subordinate
// to) the experiment-loop opt-in and the kill-switch.
func dynamicEnabled() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv(envDynamicEnabled)), "true")
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
// the live game.json flag surface (#995). It holds a persisted round-robin cursor
// so successive declarations cover DIFFERENT candidate flags deterministically (no
// wall-clock / rand — variety comes from the cursor, the #995 testability rule).
type dynamicDeclarer struct {
	// gameFlagPath is the live game.json the declarer reads candidate flags +
	// their current defaults/variants from (re-read each call so a hot-reloaded
	// file is reflected).
	gameFlagPath string
	// candidates, when non-empty, restricts/orders the flags the declarer rotates
	// over. Empty ⇒ all tunable flags discovered in game.json (sorted, stable).
	candidates []string
	// defaultObjective is the experimentable objective used when no pressure signal
	// is available.
	defaultObjective string
	// targetN is the target games-per-arm stamped on each declared Intent (0 ⇒ the
	// Registry reconciles to min-N).
	targetN int
	// pressure is the optional fitness-pressure selection signal (may be nil).
	pressure pressureFunc

	// cursor is the persisted round-robin position across candidate flags. It only
	// advances when a declaration is actually emitted, so a tick that finds no
	// declarable flag does not skip the surface. NOT a clock — pure rotation.
	cursor int
}

// newDynamicDeclarerFromEnv builds the declarer from the AGENT_EXPERIMENT_DYNAMIC_*
// env vars, or returns nil when the opt-in is off (so the caller wires nothing).
// pressure is the optional live signal seam (nil ⇒ default-objective fallback).
func newDynamicDeclarerFromEnv(gameFlagPath string, pressure pressureFunc) *dynamicDeclarer {
	if !dynamicEnabled() {
		return nil
	}
	objective := strings.TrimSpace(os.Getenv(envDynamicObjective))
	if !decision.IsExperimentable(objective) {
		objective = decision.ObjectiveBalanced
	}
	if objective == "" {
		objective = decision.ObjectiveBalanced
	}
	return &dynamicDeclarer{
		gameFlagPath:     gameFlagPath,
		candidates:       parseCandidates(os.Getenv(envDynamicCandidates)),
		defaultObjective: objective,
		targetN:          parseDynamicTargetN(os.Getenv(envDynamicTargetN)),
		pressure:         pressure,
	}
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
// The caller (the loop) is responsible for the capacity check (registry caps) and
// for actually calling Registry.Declare/Start with the returned Intent; this
// keeps the heuristic pure and unit-testable in isolation from the registry.
func (d *dynamicDeclarer) Declare(active activeFlags) (experiment.Intent, bool) {
	flags := d.readCandidateFlags()
	if len(flags) == 0 {
		return experiment.Intent{}, false
	}
	order := d.candidateOrder(flags)
	if len(order) == 0 {
		return experiment.Intent{}, false
	}

	objective := d.selectObjective()

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
		value, ok := perturbValue(flags[key])
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
// the configured candidate list (filtered to flags actually present + tunable), or
// — when no list is configured — every discovered tunable flag in a STABLE sorted
// order (deterministic rotation independent of map iteration).
func (d *dynamicDeclarer) candidateOrder(flags map[string]candidateFlag) []string {
	if len(d.candidates) > 0 {
		order := make([]string, 0, len(d.candidates))
		for _, k := range d.candidates {
			if _, ok := flags[k]; ok {
				order = append(order, k)
			}
		}
		return order
	}
	order := make([]string, 0, len(flags))
	for k := range flags {
		order = append(order, k)
	}
	sort.Strings(order)
	return order
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

// readCandidateFlags reads the live game.json and returns every TUNABLE flag (one
// with at least two variants — a single-variant flag has no alternate value to
// experiment toward) as a candidateFlag (its default value + variants). A
// read/parse error returns an empty map (fail-closed: nothing is declarable until
// the file is readable again), mirroring gameFlagVocab.
func (d *dynamicDeclarer) readCandidateFlags() map[string]candidateFlag {
	raw, err := os.ReadFile(d.gameFlagPath)
	if err != nil {
		return nil
	}
	var doc struct {
		Flags map[string]struct {
			Variants       map[string]json.RawMessage `json:"variants"`
			DefaultVariant string                     `json:"defaultVariant"`
		} `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil
	}
	out := make(map[string]candidateFlag, len(doc.Flags))
	for key, f := range doc.Flags {
		if len(f.Variants) < 2 {
			continue // not tunable: no alternate value to perturb toward.
		}
		dv, ok := f.Variants[f.DefaultVariant]
		if !ok {
			continue // malformed: default points at a missing variant — skip.
		}
		var def any
		if err := json.Unmarshal(dv, &def); err != nil {
			continue
		}
		out[key] = candidateFlag{defaultValue: def, variants: f.Variants}
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
// declared Intent (mirrors seedIntentFromEnv's shape). It records that this was
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

// parseCandidates splits a comma-separated candidate-flag override into a trimmed,
// non-empty key list (order preserved). Empty input ⇒ nil (all tunable flags).
func parseCandidates(raw string) []string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if k := strings.TrimSpace(p); k != "" {
			out = append(out, k)
		}
	}
	return out
}

// parseDynamicTargetN parses the dynamic target-N override; non-positive/invalid ⇒
// 0 (the Registry reconciles target_n up to the verdict min-N).
func parseDynamicTargetN(raw string) int {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return 0
	}
	n := 0
	for _, c := range raw {
		if c < '0' || c > '9' {
			return 0
		}
		n = n*10 + int(c-'0')
	}
	return n
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
