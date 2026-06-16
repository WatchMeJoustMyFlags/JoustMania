package experiment

import (
	"fmt"
	"strings"
	"sync"
)

// thesis_proposer.go closes the agent's research LOOP (#1184, epic #1181 incr. 4):
//
//	game → retro (#1179) → THESIS → experiment → verdict → (promote/refine → new thesis)
//
// The post-game retro (decision/retro_capture.go) already produces structured
// RetroSuggestions — {intervention_type, emphasis, reason} — describing what the
// analyst thinks should change for the next session. Today those land ONLY on the
// agent.llm.retro span (fire-and-forget); nothing turns them back into an
// experiment. The ThesisProposer is that missing seam: it CONSUMES recent retro
// suggestions and EMITS a candidate experiment Intent whose Hypothesis (the
// experiment's explicit THESIS — what idea + expected effect, #1184) is seeded
// from the retro's own reasoning. The next experiment therefore TESTS what the
// last game's retro proposed.
//
// This is the #960 Proposer→Intents path made CONCRETE without an inference
// backend: #960's LLM Proposer is wired but inert (it degrades to no-proposal
// until #738/#742 land a real Ollama/cloud transport). The ThesisProposer is the
// deterministic counterpart — it needs no backend, so the loop closes NOW, the
// same way the #995 dynamic declarer delivered autonomous declaration ahead of the
// LLM proposer.
//
// SAFETY / SCOPE — adds NO new write surface. It does NOT choose a flag/value
// itself; it DELEGATES that to an injected FlagSelector (the #995 dynamic
// declarer's Declare in production), so every Intent it emits flows through the
// SAME #977 Gate + #978 Registry caps as the env seed and the dynamic declarer.
// Its ONLY distinct contribution is the THESIS provenance: it stamps the retro's
// intervention_type / emphasis / reason onto the Intent.Hypothesis so the
// experiment records WHY it was run (the retro that motivated it), and biases the
// objective from the retro's emphasis where the analyst signalled a direction.
//
// GATING — default OFF. The caller only invokes Propose when the opt-in is on
// (the loop gates it on AGENT_EXPERIMENT_PROPOSER_ENABLED, subordinate to
// AGENT_EXPERIMENTS_ENABLED + the kill-switch), so it changes the autonomous
// experiment cadence for NOBODY who has not opted in.
//
// DETERMINISM — pure given (recorded suggestions, selector output): no clock, no
// randomness, no map-iteration-order dependence. Safe for concurrent use (the
// suggestion ring is mutex-guarded — the retro coordinator records from its own
// goroutines while the loop reads from the tick goroutines, like the shared
// Proposer/Resolver).

// RetroSuggestion is the structured post-game recommendation the ThesisProposer
// consumes (#1184). It MIRRORS llm.RetroSuggestion field-for-field rather than
// importing it, to keep the experiment package free of an llm dependency on the
// record path (the LLM Proposer's #960 Backend seam already imports llm; the
// thesis seam stays dependency-light so the retro side — which decodes the
// llm.RetroSuggestion — can hand it across with a trivial copy). Captured as-is;
// no field is validated against an allow-list (the retro is recorded-only).
type RetroSuggestion struct {
	// InterventionType is the allow-list intervention the analyst recommends tuning
	// (e.g. "adjust_global_difficulty"). Carried into the thesis text as provenance.
	InterventionType string
	// Emphasis is the direction the analyst recommends (enable / weight_up /
	// weight_down / disable). Used to bias the experiment objective and recorded in
	// the thesis as the EXPECTED effect.
	Emphasis string
	// Reason is the analyst's one-sentence justification tying the change to session
	// evidence. It becomes the human-readable core of the experiment's thesis.
	Reason string
}

// FlagSelector is the flag/value/objective CHOICE the ThesisProposer delegates to.
// It returns a partially-filled Intent (FlagKey + ExperimentalValue + a default
// Objective + TargetNPerArm) for a flag that has no active experiment, or ok=false
// when nothing is declarable. The ThesisProposer keeps the FlagKey/value/targetN
// and OVERWRITES the Hypothesis (with the retro-seeded thesis) and, when the retro
// signalled a direction, the Objective — so the candidate flag selection stays the
// proven, bounded #995 logic and the proposer adds only the thesis provenance.
//
// It is a FUNC type (not an interface) so the production selector — the #995
// dynamic declarer, whose Declare takes the main package's DEFINED activeFlags type
// — can be adapted with a one-line closure (a defined-type method cannot satisfy an
// interface whose method takes the underlying map type). The active-flag set is the
// flags that already have a live experiment (the per-flag dedup), opaque to the
// proposer; only the selector interprets it.
type FlagSelector func(active map[string]struct{}) (Intent, bool)

// DefaultMaxRetroBacklog bounds the recent-suggestion ring so an LLM that emits a
// suggestion every game cannot grow the proposer's memory without limit. The ring
// keeps the MOST RECENT suggestions (a freshly-observed problem matters more than a
// stale one); older entries are dropped as new ones arrive. Bounded-memory
// discipline, mirroring the journal's recent-tail (#983 §9.2).
const DefaultMaxRetroBacklog = 16

// ThesisProposer turns recent retro suggestions into candidate experiment theses
// (#1184). It owns a bounded ring of un-consumed suggestions; the retro side feeds
// it via Record and the experiment loop drains it via Propose. Safe for concurrent
// use.
type ThesisProposer struct {
	maxBacklog int

	mu sync.Mutex
	// backlog is the FIFO of un-consumed retro suggestions, oldest first. Propose
	// consumes from the front (oldest actionable suggestion); Record appends to the
	// back and evicts the front when over maxBacklog.
	backlog []RetroSuggestion
}

// NewThesisProposer builds a ThesisProposer with the given backlog bound (a
// non-positive value falls back to DefaultMaxRetroBacklog).
func NewThesisProposer(maxBacklog int) *ThesisProposer {
	if maxBacklog <= 0 {
		maxBacklog = DefaultMaxRetroBacklog
	}
	return &ThesisProposer{maxBacklog: maxBacklog}
}

// Record adds a retro suggestion to the backlog (the seam the retro coordinator
// calls when a post-game retro decodes a suggestion). A suggestion with no Reason
// AND no InterventionType is dropped (nothing to seed a thesis from). The ring is
// bounded: appending past maxBacklog evicts the oldest. Concurrency-safe.
func (tp *ThesisProposer) Record(s RetroSuggestion) {
	if strings.TrimSpace(s.Reason) == "" && strings.TrimSpace(s.InterventionType) == "" {
		return
	}
	tp.mu.Lock()
	defer tp.mu.Unlock()
	tp.backlog = append(tp.backlog, s)
	if over := len(tp.backlog) - tp.maxBacklog; over > 0 {
		// Drop the oldest `over` entries (keep the most recent maxBacklog).
		tp.backlog = append(tp.backlog[:0], tp.backlog[over:]...)
	}
}

// Backlog reports the number of un-consumed suggestions (for telemetry/tests).
func (tp *ThesisProposer) Backlog() int {
	tp.mu.Lock()
	defer tp.mu.Unlock()
	return len(tp.backlog)
}

// Propose drains the OLDEST recorded retro suggestion and turns it into a candidate
// experiment Intent whose Hypothesis is the retro-seeded THESIS (#1184). It
// delegates the flag/value choice to the injected FlagSelector (so the bounded
// #995 selection logic is reused) and then OVERWRITES the Intent's Hypothesis with
// the thesis built from the suggestion, biasing the Objective from the suggestion's
// emphasis when a direction was signalled.
//
// It returns ok=false (consuming NOTHING) when:
//   - there is no recorded suggestion to act on, OR
//   - the FlagSelector finds nothing declarable (no free candidate flag) — the
//     suggestion stays in the backlog for a later tick when capacity frees up.
//
// A consumed suggestion is removed from the backlog ONLY when an Intent is emitted,
// so a suggestion is never silently dropped because the flag surface was momentarily
// full. Pure given (backlog, selector output); no clock, no randomness.
func (tp *ThesisProposer) Propose(selector FlagSelector, active map[string]struct{}) (Intent, bool) {
	if selector == nil {
		return Intent{}, false
	}
	tp.mu.Lock()
	defer tp.mu.Unlock()
	if len(tp.backlog) == 0 {
		return Intent{}, false
	}
	// Delegate flag/value selection to the proven bounded selector. If it finds
	// nothing declarable, keep the suggestion for a later tick (do NOT consume it).
	intent, ok := selector(active)
	if !ok {
		return Intent{}, false
	}
	suggestion := tp.backlog[0]
	tp.backlog = append(tp.backlog[:0], tp.backlog[1:]...)

	// The proposer's distinct contribution: stamp the retro-seeded THESIS and bias the
	// objective from the suggestion's direction. The flag/value/targetN stay as the
	// selector chose them (bounded, Gate-safe by construction).
	intent.Hypothesis = thesisFromSuggestion(suggestion, intent)
	if obj, ok := objectiveFromEmphasis(suggestion.Emphasis); ok {
		intent.Objective = obj
	}
	return intent, true
}

// thesisFromSuggestion renders the explicit, human-readable THESIS (#1184) for an
// experiment seeded by a retro suggestion: the IDEA (the flag/value under test) +
// the EXPECTED EFFECT (the retro's intervention emphasis and reason). It records
// the provenance ("seeded by the post-game retro") so a human reading the dossier
// or the trace sees WHY this experiment exists — closing the game→retro→thesis
// loop visibly. Deterministic given its inputs.
func thesisFromSuggestion(s RetroSuggestion, intent Intent) string {
	var b strings.Builder
	b.WriteString("retro-seeded thesis (#1184): the post-game retro suggested ")
	if t := strings.TrimSpace(s.InterventionType); t != "" {
		b.WriteString(t)
		if e := strings.TrimSpace(s.Emphasis); e != "" {
			fmt.Fprintf(&b, " (%s)", e)
		}
	} else if e := strings.TrimSpace(s.Emphasis); e != "" {
		b.WriteString(e)
	} else {
		b.WriteString("a calibration change")
	}
	if r := strings.TrimSpace(s.Reason); r != "" {
		fmt.Fprintf(&b, " — \"%s\"", r)
	}
	fmt.Fprintf(&b, "; testing %s=%v as the closest tunable proxy", intent.FlagKey, intent.ExperimentalValue)
	if intent.Objective != "" {
		fmt.Fprintf(&b, " for objective %s", intent.Objective)
	}
	b.WriteString(".")
	return b.String()
}

// objectiveFromEmphasis maps a retro suggestion's EMPHASIS to the experimentable
// objective the seeded experiment should optimize, when the direction implies one.
// The retro's emphasis vocabulary (enable / weight_up / weight_down / disable)
// signals whether the analyst wants MORE or LESS intervention pressure:
//
//   - weight_up / enable  → the analyst wants the agent to act MORE: the room was
//     fading, so optimize for ENGAGEMENT pace (accelerate).
//   - weight_down / disable → the analyst wants the agent to act LESS: play was too
//     chaotic/over-managed, so optimize for ENDURANCE (let games breathe).
//
// An unrecognized / empty emphasis returns ok=false, leaving the selector's default
// objective untouched (the proposer never invents a non-experimentable objective —
// chaos is never returned, and Registry.Declare rejects it regardless). The mapping
// is advisory: it only BIASES which objective the bounded experiment scores against.
func objectiveFromEmphasis(emphasis string) (string, bool) {
	switch strings.ToLower(strings.TrimSpace(emphasis)) {
	case "weight_up", "enable":
		return "accelerate", true
	case "weight_down", "disable":
		return "endurance", true
	default:
		return "", false
	}
}
