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
// The loop reads the flag on every enabled tick (flags.ExperimentConfig.Seed)
// and acts on each distinct value at most once per process:
//   - a declare, a skip because the seed's flag already has a live experiment
//     (no stacking) and a refused or failed declare all LATCH the value;
//   - an unresolved flag (eval error) and `off` ({}) latch nothing;
//   - a malformed value declares nothing (never half-applied), latches and
//     logs one WARN.
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
		return n, n >= 0
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

// maybeDeclareSeed declares the experiment seed of this tick's config, at most
// once per distinct value (see the file comment). It runs under the declarer
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

	key := seedLatchKey(cfg.Seed)
	if _, done := e.seedLatched[key]; done {
		return
	}
	intent, kind, reason := parseExperimentSeed(cfg.Seed)
	switch kind {
	case seedOff:
		return // {}: no seed, nothing latched
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

// latchSeed records a seed value as acted on; the caller holds declarerLock.
func (e *experimentLoop) latchSeed(key string) {
	if e.seedLatched == nil {
		e.seedLatched = make(map[string]struct{})
	}
	e.seedLatched[key] = struct{}{}
}
