package experiment

import (
	"bytes"
	"encoding/json"
	"fmt"

	jsonlogic "github.com/diegoholiveira/jsonlogic/v3"
)

// resolved is the outcome of evaluating a flag for a given evaluation context:
// the selected variant name and its value. The Gate compares two resolved
// values (real-context before vs after a proposed write) to prove the invariant.
type resolved struct {
	Variant string
	Value   json.RawMessage
}

// resolveFlag mimics flagd's resolution for a single flag against evalCtx: run
// the targeting block (JSONLogic) to pick a variant; if it yields null, a
// non-string, or a name not in variants, fall through to defaultVariant; then
// return that variant's value. This is the same evaluation engine the agent
// already uses in tests (github.com/diegoholiveira/jsonlogic/v3, see
// actions/writer_test.go) — using it here gives the Gate a real before/after
// eval of the targeting it just wrote, not just a structural guess.
//
// It deliberately models only the parts of flagd resolution this experiment
// surface can produce: a targeting block returning a variant-name string (or
// null), plus defaultVariant fallback. It does NOT implement fractional/$ref/
// shared-evaluator features — none appear on game flags, and the Gate's
// structural checks are the primary guarantee; this eval is belt-and-suspenders.
func resolveFlag(flagRaw json.RawMessage, evalCtx map[string]any) (resolved, error) {
	var flag struct {
		Variants       map[string]json.RawMessage `json:"variants"`
		DefaultVariant string                     `json:"defaultVariant"`
		Targeting      json.RawMessage            `json:"targeting"`
	}
	if err := json.Unmarshal(flagRaw, &flag); err != nil {
		return resolved{}, fmt.Errorf("decode flag for resolution: %w", err)
	}

	variant := flag.DefaultVariant
	if len(flag.Targeting) > 0 {
		data, err := json.Marshal(evalCtx)
		if err != nil {
			return resolved{}, fmt.Errorf("marshal eval context: %w", err)
		}
		var out bytes.Buffer
		// SAFETY-CRITICAL fail-closed: jsonlogic.Apply ERRORS on an operator it does
		// not implement (flagd-specific ops like `fractional`/`sem_ver`/`starts_with`/
		// `$flagd.*`). We propagate that error so the Gate REJECTS the proposal
		// (write_simulation_error) rather than proceed on a mis-evaluated targeting.
		// This is load-bearing: if this library is ever swapped/upgraded to one that
		// returns false/null for an unknown operator instead of erroring, the gate
		// would silently weaken (a real-affecting diff could pass). The
		// TestResolveFlag_UnknownOperatorFailsClosed test guards this contract — do not
		// "handle" the error by treating an unevaluable targeting as the default branch.
		if err := jsonlogic.Apply(bytes.NewReader(flag.Targeting), bytes.NewReader(data), &out); err != nil {
			return resolved{}, fmt.Errorf("apply targeting: %w", err)
		}
		var sel any
		if err := json.Unmarshal(out.Bytes(), &sel); err != nil {
			return resolved{}, fmt.Errorf("decode targeting result: %w", err)
		}
		// flagd: a targeting result that is a known variant name selects it;
		// anything else (null, non-string, unknown name) falls through to default.
		if name, ok := sel.(string); ok {
			if _, known := flag.Variants[name]; known {
				variant = name
			}
		}
	}

	value, ok := flag.Variants[variant]
	if !ok {
		return resolved{}, fmt.Errorf("resolved variant %q not in variants", variant)
	}
	return resolved{Variant: variant, Value: value}, nil
}
