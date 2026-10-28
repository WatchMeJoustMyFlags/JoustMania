package main

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"sync"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
)

// experiment_seed.go declares the experiment seed (#1907, flags.md §3,
// UC-070 BR-007, UC-043 A1): the operator-chosen experiment in the
// `experiment_seed` object flag (agent set). It replaced the five
// AGENT_EXPERIMENT_SEED_* env vars. It is a seeded experiment INTENT, not a
// random-number seed (that is the shadow game's pair seed).
//
// The loop reads the flag on every tick. It remembers the LAST value it acted
// on (one canonical-JSON string, the latch) and acts on a value only when it
// differs from the latch:
//   - a declare, a skip because the seed's flag already has a live experiment
//     (no stacking) and a refused or failed declare all set the latch;
//   - a malformed value declares nothing (never half-applied), sets the latch
//     and logs one WARN (again only after the value changed);
//   - `off` ({}) observed on ANY tick, gates on or off, clears the latch
//     (observeSeedOff), so `agent-dryrun-enable.sh off` then `on` declares the
//     seed again; `off` must be visible for one experiment tick;
//   - an unresolved flag (eval error) neither acts nor clears the latch, so a
//     fail-closed flap never re-arms a seed.
// A->B->A declares A again (the per-flag dedup still prevents stacking). A
// kill-switch beat that leaves the seed on does not re-declare it.
//
// The latch key is the canonical JSON of the resolved object, so the same
// value under another variant name is still the same seed.

// seedDefaultHypothesis is stamped on a seed that names no hypothesis.
const seedDefaultHypothesis = "experiment_seed flag (#1907)"

// seedKind classifies a resolved experiment_seed value.
type seedKind int

const (
	seedOff       seedKind = iota // {}: no seed
	seedValid                     // a declarable Intent
	seedMalformed                 // anything else: no seed, one WARN
)

// seedFields are the only keys a seed object may carry.
var seedFields = map[string]struct{}{
	"flag": {}, "value": {}, "objective": {}, "target_n": {}, "hypothesis": {},
}

// seedObjectives are the objectives a seed may name. chaos (no fitness
// function) and any unknown name make the seed malformed: unlike the dynamic
// declarer's objective, a seed never falls back (no half-applied seed).
var seedObjectives = map[string]struct{}{
	decision.ObjectiveBalanced:   {},
	decision.ObjectiveEndurance:  {},
	decision.ObjectiveAccelerate: {},
}

// parseExperimentSeed validates one resolved experiment_seed value against the
// flags.md §3 schema. On seedMalformed the reason says which rule failed.
func parseExperimentSeed(raw any) (experiment.Intent, seedKind, string) {
	obj, ok := raw.(map[string]any)
	if !ok {
		return experiment.Intent{}, seedMalformed, fmt.Sprintf("not an object (%T)", raw)
	}
	if len(obj) == 0 {
		return experiment.Intent{}, seedOff, ""
	}
	for k := range obj {
		if _, known := seedFields[k]; !known {
			return experiment.Intent{}, seedMalformed, "unknown field " + k
		}
	}
	flagKey, ok := obj["flag"].(string)
	if !ok || strings.TrimSpace(flagKey) == "" {
		return experiment.Intent{}, seedMalformed, "flag must be a non-empty string"
	}
	value, ok := obj["value"]
	if !ok || value == nil {
		return experiment.Intent{}, seedMalformed, "value is required and must not be null"
	}
	objective := decision.ObjectiveBalanced
	if rawObjective, present := obj["objective"]; present {
		s, isString := rawObjective.(string)
		if _, known := seedObjectives[s]; !isString || !known {
			return experiment.Intent{}, seedMalformed, fmt.Sprintf("objective %v is not balanced, endurance or accelerate", rawObjective)
		}
		objective = s
	}
	targetN := 0
	if rawTarget, present := obj["target_n"]; present {
		n, valid := nonNegativeInt(rawTarget)
		if !valid {
			return experiment.Intent{}, seedMalformed, fmt.Sprintf("target_n %v is not a non-negative integer", rawTarget)
		}
		targetN = n
	}
	hypothesis := seedDefaultHypothesis
	if rawHypothesis, present := obj["hypothesis"]; present {
		s, isString := rawHypothesis.(string)
		if !isString {
			return experiment.Intent{}, seedMalformed, "hypothesis must be a string"
		}
		if strings.TrimSpace(s) != "" {
			hypothesis = strings.TrimSpace(s)
		}
	}
	return experiment.Intent{
		Hypothesis:        hypothesis,
		FlagKey:           strings.TrimSpace(flagKey),
		ExperimentalValue: value,
		Objective:         objective,
		TargetNPerArm:     targetN,
	}, seedValid, ""
}

// nonNegativeInt accepts a JSON integer (flagd decodes numbers as float64; the
// in-memory test provider may pass Go ints) that is >= 0. A fraction, a string
// or a negative number is rejected.
func nonNegativeInt(v any) (int, bool) {
	switch n := v.(type) {
	case int:
		return n, n >= 0 && n <= math.MaxInt32
	case int64:
		return int(n), n >= 0 && n <= math.MaxInt32
	case float64:
		if n < 0 || n != math.Trunc(n) || n > math.MaxInt32 {
			return 0, false
		}
		return int(n), true
	case json.Number:
		i, err := n.Int64()
		return int(i), err == nil && i >= 0 && i <= math.MaxInt32
	}
	return 0, false
}

// seedLatchKey is the canonical JSON of the resolved value (encoding/json sorts
// map keys), the identity the latch remembers.
func seedLatchKey(raw any) string {
	b, err := json.Marshal(raw)
	if err != nil {
		return fmt.Sprintf("%#v", raw)
	}
	return string(b)
}

// declarerLock is the mutex every declarer holds around its check-and-declare:
// the dynamic declarer's, shared with the retro proposer and the M7-4 sink.
// seedMu stands in only when no dynamic declarer is wired (test wiring).
func (e *experimentLoop) declarerLock() *sync.Mutex {
	if e.dynamic != nil {
		return &e.dynamic.mu
	}
	return &e.seedMu
}

// currentSeed re-reads experiment_seed now (#1966). The seed decisions call it
// under declarerLock, so every decision acts on a value read inside its own
// critical section: a concurrent caller holding an older config can neither
// re-declare a seed after a newer tick observed `off`, nor clear the latch of a
// newer seed with a stale `off`. nil = unresolved.
func (e *experimentLoop) currentSeed(ctx context.Context) any {
	if e.configOverride != nil {
		return e.configOverride(ctx).Seed
	}
	if e.flags == nil {
		return nil
	}
	return e.flags.ExperimentSeed(ctx)
}

// maybeDeclareSeed declares the experiment seed when it differs from the last
// value acted on (see the file comment). cfg.Seed is only a pre-check: the
// decision uses the value re-read under the lock (currentSeed, #1966). It runs under the declarer
// lock so the latch check, the per-flag dedup, the Declare and the latch update
// are one critical section: concurrent allocateIfEnabled callers (tick, game-end
// nudges) and the other declarers can never stack a second experiment on the
// seed's flag. allocateIfEnabled calls it after Reconfigure and the one-time
// rehydrate and before the dynamic declarer, so the seed takes the budget slot
// first. The seed itself is the operator's choice and is not bounded by the
// dynamic budget (as the former env seed was not).
func (e *experimentLoop) maybeDeclareSeed(ctx context.Context, cfg flags.ExperimentConfig) {
	if cfg.Seed == nil {
		return // unresolved flag (eval error): no seed, nothing latched
	}
	mu := e.declarerLock()
	mu.Lock()
	defer mu.Unlock()

	// cfg may be stale (#1966): decide on the value read under the lock.
	seed := e.currentSeed(ctx)
	if seed == nil {
		return
	}
	key := seedLatchKey(seed)
	if e.seedLatched && e.seedLast == key {
		return
	}
	intent, kind, reason := parseExperimentSeed(seed)
	switch kind {
	case seedOff:
		e.clearSeedLatch() // {}: no seed; also cleared by observeSeedOff
		return
	case seedMalformed:
		e.latchSeed(key)
		e.log.Warn("experiment: experiment_seed is malformed, no seed declared (#1907)",
			"reason", reason, "value", key)
		return
	}
	e.latchSeed(key)
	if _, live := e.registry.ActiveFlags()[intent.FlagKey]; live {
		e.log.Info("experiment: experiment_seed satisfied by a live experiment on its flag, not declared (#1907)",
			"flag_key", intent.FlagKey)
		return
	}
	if _, err := e.declareAndStart(ctx, intent); err != nil {
		e.log.Error("experiment: seed declare/start failed (#1907)",
			"flag_key", intent.FlagKey, "objective", intent.Objective, "error", err)
		return
	}
	e.log.Info("experiment: experiment_seed declared an experiment (#1907)",
		"flag_key", intent.FlagKey, "value", intent.ExperimentalValue,
		"objective", intent.Objective, "target_n", intent.TargetNPerArm)
}

// latchSeed records a seed value as the last one acted on; the caller holds
// declarerLock.
func (e *experimentLoop) latchSeed(key string) {
	e.seedLast = key
	e.seedLatched = true
}

// clearSeedLatch forgets the last acted-on seed value; the caller holds
// declarerLock.
func (e *experimentLoop) clearSeedLatch() {
	e.seedLast = ""
	e.seedLatched = false
}

// observeSeedOff clears the latch when this tick's experiment_seed is `off`
// ({}). allocateIfEnabled calls it on EVERY tick, before the gate checks, so an
// `off` set while the gates are off (agent-dryrun-enable.sh off clears the
// gates first) still re-arms the seed. An eval error (Seed == nil) and any other
// value leave the latch alone, and so does an `off` the flag no longer resolves
// to under the lock (#1966).
func (e *experimentLoop) observeSeedOff(ctx context.Context, cfg flags.ExperimentConfig) {
	if !isSeedOff(cfg.Seed) {
		return
	}
	mu := e.declarerLock()
	mu.Lock()
	defer mu.Unlock()
	// cfg may be stale (#1966): a newer seed read under the lock keeps its latch.
	if !isSeedOff(e.currentSeed(ctx)) {
		return
	}
	e.clearSeedLatch()
}

// isSeedOff reports whether a resolved experiment_seed value is `off` ({}); an
// unresolved value (nil) is not.
func isSeedOff(seed any) bool {
	if seed == nil {
		return false
	}
	_, kind, _ := parseExperimentSeed(seed)
	return kind == seedOff
}
