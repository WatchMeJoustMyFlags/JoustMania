package experiment

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/telemetry/keys"
)

// experiment.progress (#2059, docs/specs/telemetry.md §4.1): a registry snapshot of
// one experiment as a one-span trace. The agent.experiment root is stored by Jaeger
// only when it ends (teardown) and its trace holds every shadow game, so a reader
// cannot follow a live experiment there. experiment.progress is its OWN root with a
// Link to the agent.experiment span context, emitted under r.mu after every state
// change and on a heartbeat.
const (
	// SpanExperimentProgress is the span name.
	SpanExperimentProgress = "experiment.progress"
	// EventExperimentArm is the per-arm span event; it carries
	// keys.AttrExperimentArm plus the three AttrExperimentArm* values.
	EventExperimentArm = "experiment.arm"
	// AttrExperimentProgressTrigger is one of the ProgressTrigger* values.
	AttrExperimentProgressTrigger = "experiment.progress.trigger"
	// AttrExperimentEndReason is the teardown reason, on the terminal snapshot only.
	AttrExperimentEndReason = "experiment.end_reason"
	// AttrExperimentValue is the experimental arm's flag value as JSON (#2291); the
	// control arm resolves the flag's current value.
	AttrExperimentValue = "experiment.experimental_value"
	// AttrExperimentArmAssigned is a point-in-time count of the games bound to the arm
	// and still in flight (not concluded, not released; a spawn reservation does not
	// count). Not cumulative: 0 on the terminal snapshot, teardown clears the in-flight
	// map. The arm_stats namespace keeps experiment.arm a plain attribute, never also a
	// prefix (OTel attribute naming).
	AttrExperimentArmAssigned = "experiment.arm_stats.assigned"
	// AttrExperimentArmSamples / AttrExperimentArmMeanFitness are the journal
	// summary's per-arm count and mean: only games the journal folded count.
	AttrExperimentArmSamples     = "experiment.arm_stats.samples"
	AttrExperimentArmMeanFitness = "experiment.arm_stats.mean_fitness"

	// attrLinkKind / linkKindExperiment label the Link to agent.experiment.
	attrLinkKind       = "link.kind"
	linkKindExperiment = "experiment"

	ProgressTriggerStarted       = "started"
	ProgressTriggerGameAssigned  = "game_assigned"
	ProgressTriggerGameConcluded = "game_concluded"
	ProgressTriggerGameReleased  = "game_released"
	ProgressTriggerConcluded     = "concluded"
	ProgressTriggerPromoting     = "promoting"
	ProgressTriggerTerminal      = "terminal"
	ProgressTriggerRehydrate     = "rehydrate"
	ProgressTriggerHeartbeat     = "heartbeat"
	// ProgressTriggerRecommendation: a deferred promotion was refused after the
	// experiment ended (UC-043 A19, #2276); the snapshot carries the recommendation.
	ProgressTriggerRecommendation = "recommendation"

	// AttrExperimentRecommendationFlag / Value / Owner: the winning value as a
	// recommendation when the promotion was refused because another writer owns the
	// real overlay (UC-043 A19, #2276). Value is the JSON encoding of the value.
	AttrExperimentRecommendationFlag  = "experiment.recommendation.flag"
	AttrExperimentRecommendationValue = "experiment.recommendation.value"
	AttrExperimentRecommendationOwner = "experiment.recommendation.owner"

	// ProgressHeartbeatInterval is how often every live experiment re-emits its
	// snapshot, so an idle experiment stays visible.
	ProgressHeartbeatInterval = 60 * time.Second
)

// emitProgressLocked emits one experiment.progress for e. link is the agent.experiment
// span context to Link to (teardown passes the one it captured before ending the
// span); an invalid one adds no Link. endReason is set on the terminal snapshot only.
// Nil-tracer-safe. Assumes r.mu is held.
func (r *Registry) emitProgressLocked(e *entry, trigger, endReason string, link trace.SpanContext) {
	if r.tracer == nil || e == nil || e.journal == nil {
		return
	}
	intent := e.journal.Intent()
	summary := e.journal.Summary()
	attrs := []attribute.KeyValue{
		attribute.String(keys.AttrExperimentID, e.id),
		attribute.String(keys.AttrExperimentStatus, string(e.status)),
		attribute.String(keys.AttrExperimentFlagKey, intent.FlagKey),
		attribute.String(keys.AttrExperimentObjective, intent.Objective),
		attribute.Int(keys.AttrExperimentTargetN, intent.TargetNPerArm),
		attribute.String(keys.AttrExperimentArms, strings.Join(e.arms, ",")),
		attribute.String(AttrExperimentProgressTrigger, trigger),
	}
	// #2291: what is tested, for the dashboard's experiment card. The thesis and the
	// experimental arm's value are on every snapshot (the dashboard keeps the newest
	// span); each is omitted when empty.
	if intent.Hypothesis != "" {
		attrs = append(attrs, attribute.String(keys.AttrExperimentThesis, capSpanText(intent.Hypothesis)))
	}
	if intent.ExperimentalValue != nil {
		if raw, err := json.Marshal(intent.ExperimentalValue); err == nil {
			attrs = append(attrs, attribute.String(AttrExperimentValue, capSpanText(string(raw))))
		}
	}
	if v := summary.Verdict; v != nil {
		attrs = append(attrs,
			attribute.String(keys.AttrVerdictOutcome, v.Outcome),
			attribute.Float64(keys.AttrVerdictDelta, v.Delta),
			attribute.Bool(keys.AttrVerdictSignificant, v.Significant),
		)
	}
	if trigger == ProgressTriggerTerminal || (trigger == ProgressTriggerRecommendation && endReason != "") {
		attrs = append(attrs, attribute.String(AttrExperimentEndReason, endReason))
	}
	if e.recommendOwner != "" {
		value, err := json.Marshal(intent.ExperimentalValue)
		if err != nil {
			value = []byte(fmt.Sprint(intent.ExperimentalValue))
		}
		attrs = append(attrs,
			attribute.String(AttrExperimentRecommendationFlag, intent.FlagKey),
			attribute.String(AttrExperimentRecommendationValue, capSpanText(string(value))),
			attribute.String(AttrExperimentRecommendationOwner, e.recommendOwner),
		)
	}
	opts := []trace.SpanStartOption{trace.WithNewRoot(), trace.WithAttributes(attrs...)}
	if link.IsValid() {
		opts = append(opts, trace.WithLinks(trace.Link{
			SpanContext: link,
			Attributes:  []attribute.KeyValue{attribute.String(attrLinkKind, linkKindExperiment)},
		}))
	}
	_, span := r.tracer.Start(context.Background(), SpanExperimentProgress, opts...)

	inFlight := make(map[string]int, len(e.arms))
	for gameID, arm := range e.games {
		if !isReservation(gameID) {
			inFlight[arm]++
		}
	}
	for _, arm := range e.arms {
		var samples int
		var mean float64
		if st := summary.Arms[arm]; st != nil {
			samples, mean = st.Count, st.Mean
		}
		span.AddEvent(EventExperimentArm, trace.WithAttributes(
			attribute.String(keys.AttrExperimentArm, arm),
			attribute.Int(AttrExperimentArmAssigned, inFlight[arm]),
			attribute.Int(AttrExperimentArmSamples, samples),
			attribute.Float64(AttrExperimentArmMeanFitness, mean),
		))
	}
	span.End()
}

// ProgressHeartbeat emits an experiment.progress heartbeat for every non-terminal
// experiment, in id order. The experiment loop calls it every
// ProgressHeartbeatInterval.
func (r *Registry) ProgressHeartbeat() {
	if r == nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	ids := make([]string, 0, len(r.entries))
	for id, e := range r.entries {
		if !e.status.IsTerminal() {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	for _, id := range ids {
		e := r.entries[id]
		r.emitProgressLocked(e, ProgressTriggerHeartbeat, "", e.spanCtx)
	}
}

// maxSpanTextLen caps free-text span attributes at 512 bytes: experiment.thesis,
// experiment.experimental_value and experiment.recommendation.value (#2291, #2276).
const maxSpanTextLen = 512

// capSpanText cuts s to maxSpanTextLen bytes on a rune boundary and appends "..." when
// it cut, so the result stays valid UTF-8 and at most maxSpanTextLen bytes. It is the
// one cap helper for every free-text attribute of experiment.progress.
func capSpanText(s string) string {
	if len(s) <= maxSpanTextLen {
		return s
	}
	cut := maxSpanTextLen - len("...")
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + "..."
}
