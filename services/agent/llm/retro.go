package llm

// retro.go builds the POST-GAME retrospective prompt (#844): the prompt the
// agent would send to an offline LLM analyst when a game ENDS, asking it to
// suggest calibration tweaks for the NEXT game. It is the sibling of the in-game
// builder in prompt.go and shares its helpers (floatPtrOrUnknown,
// summarizeObjectives, joinInterventions, stringOrUnknown, the `unknown`
// literal) and its determinism contract:
//
//   - No wall-clock reads — RetroInput.Now is injected.
//   - No randomness.
//   - Players are sorted by serial; no map-iteration-order dependence.
//   - Floats render at fixed precision (floatPtrOrUnknown's 2 decimals).
//
// The same RetroInput (with a fixed Now) always yields a byte-identical
// RetroPrompt. Unlike Build, there is NO Variant: the offline analyst makes a
// handful of human-reviewed recommendations, so there is no in-game
// aggressiveness dial.
//
// Capture-first, like #739: nothing calls a backend yet. decision/retro_capture.go
// records the built prompt on an `agent.llm.retro` span for offline replay; a
// follow-up wires a real backend once the fallback chain (#741) exists.

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamesummary"
	"github.com/joustmania/agent/goal"
)

// RetroPrompt is the System/User prompt pair the agent would send to the offline
// analyst at game end, plus the capability model flag for attribution. There is
// deliberately no Variant: a post-game analyst has no in-game aggressiveness
// variant.
type RetroPrompt struct {
	System string // role + calibration surface + policy + response contract
	User   string // serialized full-session summary
	Model  string // model that serves the retro (attribution only; set by the caller)
}

// RetroInput is everything BuildRetro needs to render a RetroPrompt at game end.
type RetroInput struct {
	Snapshot flags.Snapshot
	Context  gamecontext.GameContext
	// Summary is the already-built per-game narrative (#1158). The retro renders the
	// rich per-player PlayerArc stanzas (survival / elimination-offset / sparkline /
	// activity / near-death) the agent ALREADY computes in gamesummary.BuildSummary,
	// instead of re-deriving a flat terminal snapshot. It is built by the caller from
	// the SAME finished-game GameContext (gamesummary.BuildSummary is a pure fold), so
	// no clock/disk is touched here and the prompt stays deterministic (PlayerArcs are
	// serial-sorted, all offsets relative to game start). A zero Summary (no arcs)
	// falls back to the room-level outcome block alone.
	Summary gamesummary.Summary
	Now     time.Time // injected for determinism — BuildRetro must never read the wall clock
}

// BuildRetro renders the deterministic retrospective prompt for one finished
// session. The same RetroInput (with a fixed Now) always yields a byte-identical
// RetroPrompt. The System prompt is grounded in the SAME physics + mode mechanics
// the in-game prompt uses (#1158: physicalModel + modePromptFragment), and the
// suggestion vocabulary is the REAL intervention allow-list — there are no
// init-time "calibration flags"; the agent only ever acts through interventions.
func BuildRetro(in RetroInput) RetroPrompt {
	mode := stringOrUnknown(in.Context.Session.GameMode)
	return RetroPrompt{
		System: buildRetroSystem(mode, in.Snapshot.InterventionsAllowed, len(in.Summary.Predictions) > 0),
		User:   buildRetroUser(in.Context, in.Summary, in.Now),
	}
}

// RetroResponse is the DECODED post-game analyst reply (#1179): the structured
// shape of the JSON object buildRetroSystem's RESPONSE CONTRACT asks the offline
// analyst to return (llm/retro.go:146-158). It is the retro sibling of the in-game
// Response (decode.go): the offline analyst gives a session assessment, a handful of
// tuning suggestions, and the session focus the next game should lean toward. Unlike
// the in-game Response it dispatches NOTHING — the retro is RECORDED ONLY (the agent
// never auto-applies a suggestion), so DecodeRetro is deliberately FORGIVING about
// the suggestion vocabulary (it does not reject an unknown intervention_type/emphasis):
// a recorded-only conclusion is captured as-is for a human to read, not gated like a
// dispatchable action. The only hard requirement is that the reply be a single JSON
// object — anything else is unparseable and stamped as a parse failure on the span.
type RetroResponse struct {
	// SessionAssessment is the analyst's one-sentence read on how the game went.
	SessionAssessment string `json:"session_assessment"`
	// Suggestions are the (possibly empty) tuning recommendations. A healthy session
	// legitimately yields an empty list — that is a VALID, contract-following reply.
	Suggestions []RetroSuggestion `json:"suggestions"`
	// SessionFocus is the goal the next session should lean toward (one of
	// endurance/balanced/accelerate/chaos per the contract). Captured verbatim — not
	// validated against a vocabulary, since the retro is recorded-only.
	SessionFocus string `json:"session_focus"`
}

// RetroSuggestion is one recorded tuning recommendation from the analyst (#1179):
// which intervention TYPE to change, how (emphasis), and why. Captured as-is for a
// human to review; never auto-applied, so no field is validated against an allow-list.
type RetroSuggestion struct {
	// InterventionType is the allow-list intervention the analyst recommends tuning.
	InterventionType string `json:"intervention_type"`
	// Emphasis is the direction of the change (enable/weight_up/weight_down/disable).
	Emphasis string `json:"emphasis"`
	// Reason is the one-sentence justification tying the change to session evidence.
	Reason string `json:"reason"`
}

// Retro-decode rejection reasons (#1179). The retro path is recorded-only, so the
// ONLY failure mode is "the reply was not a single JSON object" — there are no
// required-field or vocabulary checks (a healthy session yields an empty, but valid,
// reply). Both reasons are stamped as the parse-failure marker on the agent.llm.retro
// span (parse_ok=false + llm.infer.error).
var (
	// ErrRetroEmptyResponse: the analyst returned nothing usable (empty/whitespace).
	ErrRetroEmptyResponse = errors.New("llm: empty retro response")
	// ErrRetroNotJSON: no single JSON object could be extracted/parsed from the reply.
	ErrRetroNotJSON = errors.New("llm: retro response is not a single JSON object")
)

// DecodeRetro parses a raw analyst reply into a RetroResponse, or returns a typed
// error (#1179). It is the retro sibling of the in-game Decode (decode.go) and shares
// its DEFENSIVE JSON extraction: a real model wraps its object in prose or ```json
// fences despite the "no prose" instruction, so DecodeRetro locates the first '{' and
// the matching last '}' and parses the span between them (extractJSONObject).
//
// It is intentionally LESS strict than Decode: the in-game Decode validates required
// fields and a closed objective vocabulary because its output DISPATCHES an action;
// the retro is RECORDED ONLY (never auto-applied), so DecodeRetro captures whatever
// well-formed JSON the analyst returns — an empty suggestions list, an unknown
// emphasis, a missing focus are all captured verbatim for a human to read. The single
// hard requirement is parseability: a non-JSON reply is unparseable and the caller
// stamps parse_ok=false on the span.
//
// CONTRACT CAVEAT: the ONLY thing this guarantees is "a parseable JSON object". A
// structurally-valid but contentless object (e.g. {"foo":1}) parses fine — it yields
// parse_ok=true with an empty conclusion (empty assessment/focus, no suggestions),
// INDISTINGUISHABLE from a genuinely empty healthy reply ({}). Callers cannot infer
// "the analyst said nothing useful" vs "the analyst saw nothing to flag" from parse_ok
// alone; both are valid, recorded-only outcomes.
func DecodeRetro(raw string) (RetroResponse, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return RetroResponse{}, ErrRetroEmptyResponse
	}

	obj, err := extractJSONObject(trimmed)
	if err != nil {
		// Normalize the in-game ErrNotJSON to the retro-namespaced reason so callers
		// log a coherent "retro" cause; the underlying defensive extraction is shared.
		return RetroResponse{}, ErrRetroNotJSON
	}

	var r RetroResponse
	// DisallowUnknownFields is intentionally NOT used: a compliant analyst may add
	// commentary keys, and we only consume the contracted ones (mirrors Decode).
	if err := json.Unmarshal(obj, &r); err != nil {
		return RetroResponse{}, fmt.Errorf("%w: %v", ErrRetroNotJSON, err)
	}
	return r, nil
}

// buildRetroSystem renders the System prompt: the post-game analyst role, the
// game-physics + mode grounding (#1158: the SAME physicalModel + modePromptFragment
// the in-game buildSystem got via #1091, so the offline analyst reasons within the
// real mechanics and cannot hallucinate combat/respawn/init-only "calibration
// flags"), the REAL intervention-type vocabulary (the intervention allow-list — the
// agent has no init-time calibration surface; it only ever acts through
// interventions), the smallest-change policy, and the JSON response contract.
//
// #1160 review fix: the allow-list is the agent's REACTIVE in-game vocabulary, not a
// game-setup surface — so the analyst recommends which of these intervention TYPES to
// ENABLE / EMPHASIZE (or de-emphasize) for the next session, not how the next game
// opens or is paced.
//
// #1158 fix B: the four phantom flags this prompt previously enumerated
// (global_difficulty_factor / pacing_profile / threshold_table / objective_variant)
// are GONE. Verified against the real schema: threshold_table and objective_variant
// exist in NO flagd source; global_difficulty_factor and pacing_profile DO exist but
// only as game_coordinator (interventions domain) flags the agent writes INDIRECTLY
// via the adjust_global_difficulty / set_pacing_profile INTERVENTIONS — there is no
// agent-side "read at game INIT" calibration surface. So the analyst recommends from
// the live intervention allow-list (the agent's only acting surface). The former
// objective_variant enum collided with the in-game objective_served enum; the retro's
// advisory goal field is renamed session_focus to remove the collision.
//
// #1160 review fix: the allow-list is the set of REAL-TIME REACTIVE in-game actions
// (with the full / shadow_experimental variants it includes eliminate_player /
// revive_player / end_game / send_controller_effect). Recommending the next game
// "OPEN by eliminating a player" is incoherent — those are reactions, not setup. So
// the recommendation surface is reframed: the analyst recommends which intervention
// TYPES the agent should ENABLE or EMPHASIZE (weight up) — and which to de-emphasize
// — for the next session, i.e. tuning the agent's reactive vocabulary and emphasis,
// NOT the game's opening configuration. Under that framing the whole allow-list,
// reactive actions included, reads coherently. Still advisory / recorded-only /
// human-reviewed.
//
// #2479: a game with scored predictions adds one closing sentence asking for a
// comment on calibration (UC-042 A12); a game without any gets the prompt it always
// had. The in-game prompt is not touched.
func buildRetroSystem(mode string, allowed []string, withPredictions bool) string {
	var b strings.Builder
	b.WriteString(`You are the JoustMania post-game analyst. A physical movement party game has just
ended. You receive a full session summary and must suggest how the agent should TUNE
its in-game reactions for the NEXT session to make play more fun. You are NOT
controlling a live game — you make at most a handful of recommendations, which a
human reviews. Suggestions are RECORDED ONLY and never auto-applied.

`)
	// Physics + current-mode grounding (#1158), shared verbatim with the in-game
	// prompt (#1091): the analyst must reason within the game's actual mechanics —
	// motion-controlled, tempo-scaled elimination, no health/damage/combat — so it
	// never invents mechanics the system does not have.
	b.WriteString(physicalModel)
	b.WriteString("\n\n")
	b.WriteString(modePromptFragment(mode))
	b.WriteString(`

YOUR RECOMMENDATION SURFACE — the agent has NO init-time "calibration flags"; it
only ever acts through INTERVENTIONS, and these are REAL-TIME REACTIVE in-game
actions, not a game-setup or opening configuration. So do NOT recommend how the next
game opens or is paced. Instead, recommend which of these intervention TYPES the
agent should ENABLE or EMPHASIZE (weight up) — and which to de-emphasize — for the
next session, tuning its reactive vocabulary and emphasis (only those the live
allow-list below permits):
`)
	b.WriteString("  " + joinInterventions(allowed))
	b.WriteString(`

POLICY: suggest the SMALLEST tuning change that addresses an observed problem,
grounded in the session evidence below (who faded when, who was near death, the
engagement trend) — e.g. enable or weight up an intervention type that would have
helped, or de-emphasize one that over-fired. If the session looked healthy, return an
empty suggestions list. Do not invent intervention types outside the allow-list above.

RESPONSE CONTRACT — reply with EXACTLY ONE JSON object, no prose, matching:
{
  "session_assessment": "<one short sentence: how did this game go?>",
  "suggestions": [
    {
      "intervention_type": "<one of the allow-list intervention types above>",
      "emphasis": "<one of: enable, weight_up, weight_down, disable>",
      "reason": "<one short sentence tying the tuning change to session evidence>"
    }
  ],
  "session_focus": "<one of: endurance, balanced, accelerate, chaos — the goal the next session should lean toward>"
}
If no tuning is warranted, return "suggestions": [].`)
	if withPredictions {
		b.WriteString(`

The session also lists the agent's predictions against what happened. In
"session_assessment", say briefly whether they were well calibrated: compare each hit
rate with its base rate from the controls and ignore unscorable ones.`)
	}
	return b.String()
}

// buildRetroUser renders the User message: the session header, the derived
// outcome (duration, final active players, elimination order, winner), the
// room-level engagement TREND, and the per-player ARC stanzas (#1158). Now is
// rendered as the captured_at timestamp in UTC RFC3339.
//
// #1158 fix A: the per-player section is now built from the rich PlayerArc the
// agent ALREADY computes in gamesummary.BuildSummary (survival_seconds /
// elimination_offset_seconds / movement_sparkline / activity / final_intensity /
// near_death_ratio + offset), instead of re-deriving a flat terminal snapshot that
// dropped most of those values. Arcs are serial-sorted and offset-based, so the
// stanza block stays deterministic. The room-level outcome / timeline / engagement
// trend / speed blocks remain as context. When the Summary carries no arcs (a
// pre-#1158 caller that never built one, or a game with no players), the stanza
// block falls back to "(none)" and the rest of the prompt is unchanged.
func buildRetroUser(ctx gamecontext.GameContext, summary gamesummary.Summary, now time.Time) string {
	var b strings.Builder
	fmt.Fprintf(&b, "SESSION RETROSPECTIVE (session=%s, kind=%s, mode=%s, captured_at=%s)\n",
		ctx.SessionID, gameKindOrUnknown(ctx.GameKind), stringOrUnknown(ctx.Session.GameMode), now.UTC().Format(time.RFC3339))

	serials := sortedSerials(ctx.Players)
	winner := winnerSerial(serials, ctx.Session.EliminationSequence)

	b.WriteString("\nOutcome:\n")
	fmt.Fprintf(&b, "  duration_seconds: %s\n", floatPtrOrUnknown(ctx.Session.DurationSeconds))
	fmt.Fprintf(&b, "  final_active_players: %s\n", intPtrOrUnknown(ctx.Session.ActivePlayerCount))
	fmt.Fprintf(&b, "  elimination_order: [%s]\n", strings.Join(ctx.Session.EliminationSequence, ", "))
	fmt.Fprintf(&b, "  winner: %s\n", winner)
	// #1771: applied and blocked are split as the game summary splits them, so a
	// fully gated game reads "0 applied, 8 blocked by a gate", never "8 interventions".
	fmt.Fprintf(&b, "  interventions: %d applied, %d blocked by a gate (permission chain or interventions_enabled)\n",
		summary.InterventionsApplied, summary.InterventionsBlocked)
	writeShieldHold(&b, summary.ShieldHold)

	// The same rolling-narrative section as the in-game prompt (#916): the retro
	// analyst sees the trend over the whole session (when players faded, when the
	// game ended), not just the terminal snapshot.
	writeTimeline(&b, ctx, now)
	// #1082: the game-speed track the per-player proximity tracks are read against.
	writeSpeedTrack(&b, ctx)
	// #1158: the room-level engagement trend (active-count + mean-intensity over the
	// game), straight off the Summary the agent already derived, kept as the
	// session-wide context the per-player arcs read against.
	writeEngagementTrend(&b, summary.EngagementTrend)

	// #1158: per-player ARC stanzas from the already-built Summary, replacing the old
	// flat snapshot. Arcs are serial-sorted by BuildSummary, so the block is
	// deterministic.
	b.WriteString("\nPlayer arcs (sorted by serial; offsets relative to game start; \"unknown\" = never observed):\n")
	if len(summary.PlayerArcs) == 0 {
		b.WriteString("  (none)")
	}
	for i, arc := range summary.PlayerArcs {
		if i > 0 {
			b.WriteString("\n")
		}
		writePlayerArc(&b, arc, ctx.Session.EliminationSequence)
	}
	writePredictions(&b, summary)
	writeGoal(&b, summary.Goal)
	return b.String()
}

// maxRetroPredictionLines bounds the per-prediction lines of the retro prompt.
const maxRetroPredictionLines = 40

// writePredictions renders the "Predictions vs outcomes" block (UC-042 A12, #2479):
// one line per scored prediction, then the per-template base rates of the controls.
// Nothing for a game without predictions, so every other retro prompt is unchanged.
func writePredictions(b *strings.Builder, summary gamesummary.Summary) {
	if len(summary.Predictions) == 0 {
		return
	}
	b.WriteString("\n\nPredictions vs outcomes (p = stated probability that it comes true; unscorable is neither a hit nor a miss; acted=false is a counterfactual, never applied):\n")
	for i, p := range summary.Predictions {
		if i == maxRetroPredictionLines {
			fmt.Fprintf(b, "  ... %d more not shown\n", len(summary.Predictions)-maxRetroPredictionLines)
			break
		}
		fmt.Fprintf(b, "  +%.0fs %s/%s %s", p.OffsetSeconds, p.Backend, p.Source, p.Template)
		if p.Rule != "" {
			fmt.Fprintf(b, " rule=%s", p.Rule)
		}
		if p.Target != "" {
			fmt.Fprintf(b, " target=%s", p.Target)
		}
		fmt.Fprintf(b, " horizon=%ds p=%.2f -> %s", p.HorizonS, p.P, p.Outcome)
		if p.UnscorableReason != "" {
			fmt.Fprintf(b, " (%s)", p.UnscorableReason)
		}
		if p.Source != gamesummary.PredictionSourceControl {
			fmt.Fprintf(b, " acted=%t", p.Acted)
		}
		b.WriteString("\n")
	}
	b.WriteString("Base rates from the controls (a control predicts the same template at a random moment; hit rate over scored ones):\n")
	rates := controlBaseRates(summary.PredictionTally)
	if len(rates) == 0 {
		b.WriteString("  (no control was scored)")
		return
	}
	for i, r := range rates {
		if i > 0 {
			b.WriteString("\n")
		}
		fmt.Fprintf(b, "  %s: %.2f (%d scored, %d unscorable)", r.template, float64(r.hits)/float64(r.hits+r.misses), r.hits+r.misses, r.unscorable)
	}
}

// writeGoal renders the "Goal" block (UC-042 A12, #2479, decide-contract.md
// §14.4): the profile, each term's band, measured value and score, and the
// attainment. Nothing for a game without a goal, so every other retro prompt is
// unchanged.
func writeGoal(b *strings.Builder, g *gamesummary.Goal) {
	if g == nil {
		return
	}
	source := "goal.profile"
	if g.FromObjectives {
		source = "derived from the objectives"
	}
	attainment := "not measured"
	if g.Attainment != nil {
		attainment = strconv.FormatFloat(*g.Attainment, 'f', 2, 64)
	}
	fmt.Fprintf(b, "\n\nGoal (profile %s, %s; a term scores 1 inside its band and decays to 0 away from it; attainment = weighted mean of the measured terms): attainment %s\n",
		g.Profile, source, attainment)
	for i, t := range g.Terms {
		if i > 0 {
			b.WriteString("\n")
		}
		fmt.Fprintf(b, "  %s: ", goal.FormatBand(t.Characteristic, t.Phase, t.Min, t.Max))
		if t.Value == nil || t.Score == nil {
			b.WriteString("not measured")
			continue
		}
		fmt.Fprintf(b, "%s -> %.2f", goal.FormatValue(t.Characteristic, *t.Value), *t.Score)
	}
}

type controlRate struct {
	template                 string
	hits, misses, unscorable int
}

// controlBaseRates sums the control tallies per template (template order) and
// drops templates with no scored control.
func controlBaseRates(tally []gamesummary.PredictionTally) []controlRate {
	byTemplate := map[string]*controlRate{}
	for _, t := range tally {
		if t.Source != gamesummary.PredictionSourceControl {
			continue
		}
		r, ok := byTemplate[t.Template]
		if !ok {
			r = &controlRate{template: t.Template}
			byTemplate[t.Template] = r
		}
		r.hits += t.Hits
		r.misses += t.Misses
		r.unscorable += t.Unscorable
	}
	out := make([]controlRate, 0, len(byTemplate))
	for _, r := range byTemplate {
		if r.hits+r.misses > 0 {
			out = append(out, *r)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].template < out[j].template })
	return out
}

// writeEngagementTrend renders the room-level engagement series the Summary
// derived from the #916 timeline (#1158): each down-sampled (offset, active,
// mean-intensity) point, oldest-first, so the analyst sees how the WHOLE room's
// engagement moved over the game. Omitted entirely when the trend is empty (keeps
// the no-narrative prompt compact).
func writeEngagementTrend(b *strings.Builder, trend []gamesummary.EngagementPoint) {
	if len(trend) == 0 {
		return
	}
	b.WriteString("\nEngagement trend (room; offset_seconds: active / mean_intensity; oldest first):\n")
	for _, pt := range trend {
		fmt.Fprintf(b, "  +%ss: active=%s mean_intensity=%s\n",
			strconv.FormatFloat(pt.OffsetSeconds, 'f', 0, 64),
			intPtrOrUnknown(pt.ActivePlayers),
			floatPtrOrUnknown(pt.MeanIntensity))
	}
}

// writePlayerArc renders one PlayerArc as a multi-field stanza (#1158): the
// outcome + survival/elimination timing, the movement TIME-SERIES (sparkline +
// activity), the closest call to the death threshold and when it happened, and the
// final-state aggregates. Every field falls back to the "unknown" literal when the
// arc never observed it, so a never-seen player renders honestly rather than
// fabricating zeros. Pure over the arc + elimination sequence — no clock.
func writePlayerArc(b *strings.Builder, arc gamesummary.PlayerArc, eliminationSequence []string) {
	fmt.Fprintf(b, "  %s: outcome=%s survival_seconds=%s elimination_offset_seconds=%s\n",
		arc.Serial,
		playerOutcome(arc.Serial, eliminationSequence),
		floatPtrOrUnknown(arc.SurvivalSeconds),
		floatPtrOrUnknown(arc.EliminationOffsetSeconds))
	fmt.Fprintf(b, "    movement=%s activity=%s\n",
		sparklineOrUnknown(arc.MovementSparkline),
		stringValueOrUnknown(arc.Activity))
	fmt.Fprintf(b, "    near_death_ratio=%s near_death_offset_seconds=%s\n",
		floatPtrOrUnknown(arc.NearDeathRatio),
		floatPtrOrUnknown(arc.NearDeathOffsetSeconds))
	fmt.Fprintf(b, "    final_intensity=%s final_skill=%s",
		floatPtrOrUnknown(arc.FinalIntensity),
		floatPtrOrUnknown(arc.FinalSkill))
	if len(arc.ShieldTotals) > 0 {
		b.WriteString("\n    shield:" + sortedKV(arc.ShieldTotals))
	}
}

// writeShieldHold renders a hold game's latched shield values (UC-042 BR-007,
// #2096); nothing outside a hold game, so every other retro prompt is unchanged.
func writeShieldHold(b *strings.Builder, hold *gamesummary.ShieldHoldSummary) {
	if hold == nil {
		return
	}
	fmt.Fprintf(b, "  shield_activation: %s (latched for the game)\n", hold.Activation)
	if len(hold.Settings) > 0 {
		b.WriteString("  shield_settings:" + sortedKV(hold.Settings) + "\n")
	}
}

// sortedKV renders m as " k=v" pairs in key order (deterministic).
func sortedKV(m map[string]float64) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		b.WriteString(" " + k + "=" + strconv.FormatFloat(m[k], 'f', -1, 64))
	}
	return b.String()
}

// sparklineOrUnknown renders a movement sparkline string, or the "unknown" literal
// when empty (the player had no movement history) — mirroring stringOrUnknown for
// the non-pointer Summary string fields.
func sparklineOrUnknown(s string) string {
	if s == "" {
		return unknown
	}
	return s
}

// stringValueOrUnknown renders a plain string field, or "unknown" when empty.
func stringValueOrUnknown(s string) string {
	if s == "" {
		return unknown
	}
	return s
}

// sortedSerials returns the player serials sorted ascending. Sorting (not map
// order) is what makes the prompt deterministic.
func sortedSerials(players map[string]*gamecontext.PlayerSignals) []string {
	serials := make([]string, 0, len(players))
	for serial := range players {
		serials = append(serials, serial)
	}
	sort.Strings(serials)
	return serials
}

// playerOutcome derives a player's end-of-game outcome from its position in the
// elimination sequence: the first serial eliminated is "eliminated #1", the
// second "eliminated #2", and so on. A serial absent from the sequence survived
// the game and renders "survivor".
func playerOutcome(serial string, eliminationSequence []string) string {
	for i, s := range eliminationSequence {
		if s == serial {
			return fmt.Sprintf("eliminated #%d", i+1)
		}
	}
	return "survivor"
}

// RetroOutcome is the STRUCTURED game outcome the retro is built from (#1100):
// the same winner / final-active-players / elimination-count / duration the
// retrospective prompt renders into its "Outcome" block, but exposed as discrete
// values so retro_capture.go can stamp them as queryable span ATTRIBUTES instead
// of leaving them buried in the prompt text. Pointers distinguish "the signal was
// never observed" (nil) from a real zero, so the span omits an unobserved attr
// rather than emitting a misleading 0.
type RetroOutcome struct {
	// Winner is the sole survivor's serial, or "" when there is no single,
	// unambiguous winner (zero or multiple survivors) — see winnerSerial.
	Winner string
	// FinalActivePlayers is the session's final active-player count, or nil when
	// never observed.
	FinalActivePlayers *int
	// EliminationCount is the number of players in the elimination sequence.
	EliminationCount int
	// DurationSeconds is the final observed game duration, or nil when unknown.
	DurationSeconds *float64
}

// DeriveRetroOutcome extracts the structured game outcome from a finished-game
// GameContext (#1100). It is the SINGLE source of truth shared with the prompt:
// it derives the winner from the SAME (roster, elimination sequence) pair that
// buildRetroUser renders, so the span attributes and the prompt text can never
// disagree. It reads only already-structured GameContext fields — it NEVER parses
// the rendered prompt text. The winner is "" (not the prompt's "unknown" literal)
// when ambiguous, so the caller can cleanly omit the attribute.
func DeriveRetroOutcome(ctx gamecontext.GameContext) RetroOutcome {
	winner := winnerSerial(sortedSerials(ctx.Players), ctx.Session.EliminationSequence)
	if winner == unknown {
		winner = ""
	}
	return RetroOutcome{
		Winner:             winner,
		FinalActivePlayers: ctx.Session.ActivePlayerCount,
		EliminationCount:   len(ctx.Session.EliminationSequence),
		DurationSeconds:    ctx.Session.DurationSeconds,
	}
}

// winnerSerial returns the sole survivor's serial — a player present in the
// roster but absent from the elimination sequence — when EXACTLY one player
// survived. Zero survivors (everyone eliminated) or more than one survivor both
// render the "unknown" literal: there is no single, unambiguous winner.
func winnerSerial(serials, eliminationSequence []string) string {
	eliminated := make(map[string]bool, len(eliminationSequence))
	for _, s := range eliminationSequence {
		eliminated[s] = true
	}
	var survivors []string
	for _, s := range serials {
		if !eliminated[s] {
			survivors = append(survivors, s)
		}
	}
	if len(survivors) == 1 {
		return survivors[0]
	}
	return unknown
}
