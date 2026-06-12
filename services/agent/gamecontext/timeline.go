package gamecontext

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// timeline.go is the bounded per-game rolling NARRATIVE (#916). The pre-#916
// agent kept only a LIVE SNAPSHOT per partition: every signal OVERWRITES the
// matching GameContext field, so the LLM prompt (#739) and the post-game retro
// (#844) saw point-in-time state with no trend — "player X has been fading for
// 90s" or "the last tempo bump changed nothing" were invisible. The project
// pitch promised "spans aggregated into rolling game narratives"; this is that
// narrative.
//
// It is a FIXED-CAPACITY ring buffer (timelineCap) living ON the Store, so it is
// automatically per-partition and evicted with the partition (#845): two
// concurrent games keep two independent timelines, and a drained partition's
// timeline disappears with its Store. Nothing global is introduced — the ring
// buffer is just another field guarded by the existing Store mutex.
//
// Determinism contract (the prompt golden tests depend on it): entries carry an
// injected timestamp (Store.now, never time.Now), render in append order, one
// compact line each, with fixed formatting. The same event stream always renders
// byte-identically.

// timelineCap is the FIXED ring-buffer capacity per partition (#916). It bounds
// memory to a constant per game — once full, each append evicts the oldest
// entry, so the timeline always reflects the most RECENT window of activity.
// 64 entries comfortably covers the recent narrative the prompt cares about
// (a few minutes of ~periodic state deltas plus eliminations and phase changes)
// while keeping the rendered section small enough to stay cheap in the prompt.
const timelineCap = 64

// TimelineEventKind is the compact, typed category of a narrative entry. Keeping
// it a small enum (rather than free text) means the renderer can format each
// kind consistently and a future consumer can filter without string parsing.
type TimelineEventKind string

const (
	// EventStateDelta is a periodic snapshot of the session-level aggregates that
	// matter for trend reasoning (active player count, mean movement intensity).
	// It is appended only when a tracked aggregate CHANGED versus the last delta,
	// so a steady game does not fill the ring with identical lines (#916).
	EventStateDelta TimelineEventKind = "state"
	// EventElimination records a player leaving the game, with the serial and the
	// 1-based elimination order — the trend the retro's winner/outcome derivation
	// already reasons over, now timestamped in the narrative.
	EventElimination TimelineEventKind = "elimination"
	// EventPhase records a session phase transition: game start and game end.
	EventPhase TimelineEventKind = "phase"
	// EventIntervention records a dispatched OR blocked intervention. The Store
	// does not observe interventions itself (the decision Loop does); this kind
	// exists so a clean seam (Store.AppendInterventionEvent) can layer them in
	// without a second structure. See AppendInterventionEvent / #916 deferral note.
	EventIntervention TimelineEventKind = "intervention"
)

// TimelineEvent is one compact, timestamped narrative entry. Payload fields are
// kind-specific and only the relevant ones are set; the renderer reads exactly
// the fields its kind defines, so an unset field never leaks into the wrong line.
type TimelineEvent struct {
	At   time.Time
	Kind TimelineEventKind

	// Detail is the kind-specific short payload:
	//   EventPhase        -> "start" | "end"
	//   EventElimination  -> the eliminated serial
	//   EventIntervention -> the intervention id (e.g. "grant_shield")
	//   EventStateDelta   -> unused (the numeric fields below carry it)
	Detail string

	// Serial scopes an event to a player when relevant (elimination target, or a
	// player-targeted intervention); empty for session-scoped events.
	Serial string

	// Order is the 1-based elimination order for EventElimination; 0 otherwise.
	Order int

	// ActivePlayers / MeanIntensity carry the EventStateDelta aggregates. Pointers
	// distinguish "not part of this delta" (nil) from a genuine zero so the
	// renderer can omit an unobserved aggregate rather than print a misleading 0.
	ActivePlayers *int
	MeanIntensity *float64

	// Blocked marks an EventIntervention that a permission gate rejected (true) vs
	// one that dispatched (false); unused for other kinds.
	Blocked bool
}

// timeline is the fixed-capacity ring buffer. It is NOT independently locked:
// every method is called while the owning Store holds s.mu, so the timeline
// reuses that mutex (the issue's "reuse the existing mutex" guidance) and adds
// no second lock to reason about.
type timeline struct {
	buf   []TimelineEvent // len == cap once warmed; pre-grown to timelineCap
	start int             // index of the OLDEST entry within buf
	size  int             // number of live entries (0..timelineCap)

	// lastActivePlayers / lastMeanIntensity dedupe EventStateDelta: an aggregate
	// snapshot is only appended when one of these CHANGED, so a steady game does
	// not churn the ring. They track the last APPENDED delta, not every signal.
	lastActivePlayers *int
	lastMeanIntensity *float64
}

// newTimeline builds an empty ring pre-grown to capacity so appends never
// allocate after construction (allocation-bounded, per the issue).
func newTimeline() timeline {
	return timeline{buf: make([]TimelineEvent, timelineCap)}
}

// append adds e to the ring, evicting the oldest entry once at capacity. The
// length stays clamped at timelineCap forever — this is the memory bound.
func (t *timeline) append(e TimelineEvent) {
	if t.buf == nil {
		t.buf = make([]TimelineEvent, timelineCap)
	}
	end := (t.start + t.size) % timelineCap
	t.buf[end] = e
	if t.size == timelineCap {
		// Full: overwrite the oldest and advance start so it points at the new
		// oldest. size stays at cap.
		t.start = (t.start + 1) % timelineCap
	} else {
		t.size++
	}
}

// events returns the live entries in append (oldest-first) order as a fresh
// slice, safe to hand out in a snapshot (it shares no backing array with the
// ring, so later appends never mutate a returned snapshot).
func (t *timeline) events() []TimelineEvent {
	if t.size == 0 {
		return nil
	}
	out := make([]TimelineEvent, t.size)
	for i := 0; i < t.size; i++ {
		out[i] = t.buf[(t.start+i)%timelineCap]
	}
	return out
}

// reset clears the ring AND the state-delta dedupe memory. The Store calls this
// on session reset (grace exit) so a finished game's narrative does not bleed
// into the next session on the same partition (the lobby persists, but the
// timeline is session-scoped, matching EliminationSequence).
func (t *timeline) reset() {
	t.start = 0
	t.size = 0
	t.lastActivePlayers = nil
	t.lastMeanIntensity = nil
}

// RenderTimeline formats a slice of timeline events into the compact, one-line
// per-event section shared by the in-game prompt (#739) and the retro (#844).
// It is exported so the llm package renders the exact same lines for both
// prompts from the bounded slice carried on the GameContext snapshot.
//
// The reference time `now` makes each line carry a RELATIVE age (e.g. "-12s")
// rather than an absolute clock, so the narrative reads as "how long ago" and
// stays stable regardless of the session's wall-clock start. Events are assumed
// oldest-first (events() order); they render in that order, one per line.
func RenderTimeline(events []TimelineEvent, now time.Time) []string {
	lines := make([]string, 0, len(events))
	for _, e := range events {
		lines = append(lines, renderEvent(e, now))
	}
	return lines
}

// renderEvent formats one event as "<age> <kind>: <detail>". age is the whole
// seconds between the event and now, negative for the past (the normal case),
// so a reader scans "-90s ... -12s ... -0s" as the recent window.
func renderEvent(e TimelineEvent, now time.Time) string {
	age := int(e.At.Sub(now).Round(time.Second) / time.Second)
	prefix := fmt.Sprintf("%+ds", age)
	switch e.Kind {
	case EventPhase:
		return prefix + " phase: " + e.Detail
	case EventElimination:
		return fmt.Sprintf("%s elimination: %s (#%d)", prefix, e.Serial, e.Order)
	case EventIntervention:
		outcome := "dispatched"
		if e.Blocked {
			outcome = "blocked"
		}
		if e.Serial != "" {
			return fmt.Sprintf("%s intervention: %s %s -> %s", prefix, e.Detail, outcome, e.Serial)
		}
		return fmt.Sprintf("%s intervention: %s %s", prefix, e.Detail, outcome)
	case EventStateDelta:
		return prefix + " state: " + renderStateDelta(e)
	default:
		return prefix + " " + string(e.Kind)
	}
}

// renderStateDelta formats the aggregates of a state-delta event, omitting any
// aggregate not part of this delta (nil) so a line never prints a misleading 0.
func renderStateDelta(e TimelineEvent) string {
	parts := make([]string, 0, 2)
	if e.ActivePlayers != nil {
		parts = append(parts, "active_players="+strconv.Itoa(*e.ActivePlayers))
	}
	if e.MeanIntensity != nil {
		parts = append(parts, "mean_intensity="+strconv.FormatFloat(*e.MeanIntensity, 'f', 2, 64))
	}
	return strings.Join(parts, " ")
}
