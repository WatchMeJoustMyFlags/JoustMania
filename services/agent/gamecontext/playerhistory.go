package gamecontext

import (
	"fmt"
	"math"
	"strings"
	"time"
)

// playerhistory.go is the bounded per-player movement TIME-SERIES (#1082). The
// #916 session timeline records only session-level aggregates (mean_intensity +
// active_players) — a single number that averages every player into one band and
// cannot answer the questions a tuning decision turns on: WHO was active vs idle,
// WHEN, and WHO was close to death. This adds a per-`serial` movement series plus
// a session-level game-speed / death-threshold track, mirroring the #916 ring-
// buffer pattern but per player, so the prompt and the retro can render a compact
// sparkline + a proximity-to-death track per player read against the tempo.
//
// Memory + token bound: each player keeps a FIXED-CAPACITY ring of bucketed
// samples (playerHistoryCap), and recording is dedupe-bucketed to ~1Hz (a sample
// is only appended once its bucket-second advances or the value changes
// meaningfully), so the 60Hz/10Hz signal firehose can never churn the ring. The
// rendered form is a single sparkline string per player — never an array of
// floats — so it stays cheap in the prompt.
//
// Determinism contract (the golden tests depend on it): samples carry the
// injected Store.now timestamp, the sparkline buckets render in time order with
// fixed glyphs, and the proximity track is a pure function of the samples + the
// observed threshold. The same sample stream always renders byte-identically.

// playerHistoryCap is the FIXED ring-buffer capacity per player (#1082). 32
// bucketed (~1Hz) samples comfortably cover the recent window the prompt cares
// about (the sparkline is the most-recent ~30s of movement) while keeping the
// per-player memory and the rendered sparkline width bounded to a constant.
const playerHistoryCap = 32

// movementBucket is the dedupe granularity for per-player samples: at most one
// sample per bucket-second per player (the value at bucket roll-over). This is
// the linchpin of the memory bound — the raw signal arrives at up to 60Hz, so
// without bucketing the ring would churn in well under a second and bury the
// trend. 1s matches the #916 narrative cadence and the proximity track's "@-Ns"
// resolution.
const movementBucket = time.Second

// MovementSample is one bucketed per-player observation: the player's movement
// intensity at an instant, with the variance when it was observed alongside.
// Variance is a pointer so "intensity seen, variance not yet" stays distinct from
// a genuine zero variance.
type MovementSample struct {
	At        time.Time
	Intensity float64
	Variance  *float64
}

// SpeedSample is one bucketed session-level observation of the game-speed /
// death-threshold reference frame (#1082): the applied music tempo and the
// effective (tempo-interpolated) death threshold at an instant. Both are pointers
// so a track that only ever observed one of the two does not fabricate the other.
type SpeedSample struct {
	At             time.Time
	Tempo          *float64
	DeathThreshold *float64
}

// playerHistory is a fixed-capacity per-player ring of MovementSamples plus the
// bucket-dedupe memory. Like the #916 timeline it is NOT independently locked:
// every method runs while the owning Store holds s.mu, so it reuses that mutex.
type playerHistory struct {
	buf   []MovementSample // len == cap once warmed; pre-grown to playerHistoryCap
	start int              // index of the OLDEST entry
	size  int              // number of live entries (0..playerHistoryCap)

	// lastBucket is the bucket-second of the last APPENDED sample, so a within-
	// bucket update overwrites the tail rather than appending a new entry.
	lastBucket time.Time
}

// record appends or coalesces a sample. Within the same bucket-second it
// OVERWRITES the tail (keeping the latest value for that second); on a new bucket
// it appends, evicting the oldest entry once at capacity. The bound is the cap.
func (h *playerHistory) record(s MovementSample) {
	if h.buf == nil {
		h.buf = make([]MovementSample, playerHistoryCap)
	}
	bucket := s.At.Truncate(movementBucket)
	if h.size > 0 && bucket.Equal(h.lastBucket) {
		// Same second: replace the tail so the bucket holds its latest value.
		tail := (h.start + h.size - 1) % playerHistoryCap
		h.buf[tail] = s
		return
	}
	h.lastBucket = bucket
	end := (h.start + h.size) % playerHistoryCap
	h.buf[end] = s
	if h.size == playerHistoryCap {
		h.start = (h.start + 1) % playerHistoryCap
	} else {
		h.size++
	}
}

// samples returns the live entries oldest-first as a fresh slice, safe to hand
// out in a snapshot (shares no backing array with the ring, so later records
// never mutate a returned snapshot).
func (h *playerHistory) samples() []MovementSample {
	if h.size == 0 {
		return nil
	}
	out := make([]MovementSample, h.size)
	for i := 0; i < h.size; i++ {
		out[i] = h.buf[(h.start+i)%playerHistoryCap]
	}
	return out
}

// speedTrackCap is the FIXED ring capacity of the session game-speed track
// (#1082). 32 bucketed (~1Hz) samples cover the recent tempo ramp the prompt
// renders while keeping the track memory- and token-bounded.
const speedTrackCap = 32

// speedTrack is the session-level fixed-capacity ring of SpeedSamples. Like the
// player histories it reuses the owning Store's mutex (called only under s.mu).
type speedTrack struct {
	buf        []SpeedSample
	start      int
	size       int
	lastBucket time.Time
}

// record appends or coalesces a session speed/threshold sample, bucketed to ~1Hz
// exactly like the per-player ring. A within-bucket update overwrites the tail.
func (t *speedTrack) record(s SpeedSample) {
	if t.buf == nil {
		t.buf = make([]SpeedSample, speedTrackCap)
	}
	bucket := s.At.Truncate(movementBucket)
	if t.size > 0 && bucket.Equal(t.lastBucket) {
		tail := (t.start + t.size - 1) % speedTrackCap
		// Coalesce: keep whichever field this sample carries, preserving the other
		// dimension already observed in this bucket (tempo and threshold arrive on
		// separate metrics, so a bucket may be filled by two distinct samples).
		prev := t.buf[tail]
		if s.Tempo != nil {
			prev.Tempo = s.Tempo
		}
		if s.DeathThreshold != nil {
			prev.DeathThreshold = s.DeathThreshold
		}
		prev.At = s.At
		t.buf[tail] = prev
		return
	}
	t.lastBucket = bucket
	end := (t.start + t.size) % speedTrackCap
	t.buf[end] = s
	if t.size == speedTrackCap {
		t.start = (t.start + 1) % speedTrackCap
	} else {
		t.size++
	}
}

// samples returns the live track entries oldest-first as a fresh slice (shares no
// backing array with the ring).
func (t *speedTrack) samples() []SpeedSample {
	if t.size == 0 {
		return nil
	}
	out := make([]SpeedSample, t.size)
	for i := 0; i < t.size; i++ {
		out[i] = t.buf[(t.start+i)%speedTrackCap]
	}
	return out
}

// reset clears the session speed track (new session / grace exit).
func (t *speedTrack) reset() {
	t.start = 0
	t.size = 0
	t.lastBucket = time.Time{}
}

// sparkGlyphs is the 8-level block ramp used by RenderSparkline, lowest-first.
// A bucket with no movement (a true gap) renders as the separate idle glyph.
var sparkGlyphs = []rune("▁▂▃▄▅▆▇█")

// idleGlyph marks a bucket whose intensity is at/below the idle floor — a player
// standing still — so a flat-low stretch reads as idleness, not just "lowest bar".
const idleGlyph = '_'

// idleFloor is the intensity at/below which a bucket is rendered as idle (`_`).
// JoustMania accel magnitude sits near ~1.0g even when a controller is merely
// held still (gravity), so a small absolute floor distinguishes "not moving"
// from "moving a little". Tuned conservatively — only near-zero reads as idle.
const idleFloor = 0.15

// RenderSparkline renders a movement series as a compact Unicode block sparkline
// scaled against scaleMax (the death threshold when known, so a full bar means
// "at the elimination threshold"). When scaleMax is nil/non-positive the series
// auto-scales to its own observed peak so the SHAPE still reads. Buckets at/below
// the idle floor render as `_`. An empty series renders the empty string.
//
// It is exported so the llm and gamesummary packages render the identical glyphs
// from the bounded slice carried on the GameContext snapshot.
func RenderSparkline(samples []MovementSample, scaleMax *float64) string {
	if len(samples) == 0 {
		return ""
	}
	// Determine the scale: the threshold when positive, else the observed peak.
	scale := 0.0
	if scaleMax != nil && *scaleMax > 0 {
		scale = *scaleMax
	} else {
		for _, s := range samples {
			if s.Intensity > scale {
				scale = s.Intensity
			}
		}
	}
	var b strings.Builder
	for _, s := range samples {
		b.WriteRune(glyphFor(s.Intensity, scale))
	}
	return b.String()
}

// glyphFor maps one intensity to its sparkline glyph against scale. At/below the
// idle floor -> idle glyph; otherwise the value's position in [0,scale] selects a
// block, clamped to the top block when it meets/exceeds the scale.
func glyphFor(intensity, scale float64) rune {
	if intensity <= idleFloor {
		return idleGlyph
	}
	if scale <= 0 {
		return sparkGlyphs[0]
	}
	frac := intensity / scale
	if frac >= 1 {
		return sparkGlyphs[len(sparkGlyphs)-1]
	}
	idx := int(frac * float64(len(sparkGlyphs)))
	if idx >= len(sparkGlyphs) {
		idx = len(sparkGlyphs) - 1
	}
	return sparkGlyphs[idx]
}

// Activity is the coarse per-window activity classification derived from a
// player's recent movement series (#1082): the cheap "what was this player doing"
// label that lets an elimination be read against its lead-up.
type Activity string

const (
	// ActivityUnknown is the classification when there is no movement history to
	// classify (an empty series).
	ActivityUnknown Activity = "unknown"
	// ActivityIdle means the recent window was mostly at/below the idle floor —
	// the player was standing still.
	ActivityIdle Activity = "idle"
	// ActivityActive means steady, meaningful movement without wild swings.
	ActivityActive Activity = "active"
	// ActivityErratic means high swing between samples — bursty/jerky movement,
	// the signature of a player flailing or thrashing.
	ActivityErratic Activity = "erratic"
)

// erraticStdFrac is the swing-relative-to-mean ratio above which a window is
// classified erratic: when the sample standard deviation exceeds this fraction of
// the window mean, the movement is bursty rather than steady. 0.5 (swings of half
// the mean) is a deliberately coarse, explainable cut.
const erraticStdFrac = 0.5

// ClassifyActivity classifies a movement window as idle / active / erratic, or
// unknown when there is nothing to classify. It is a pure function of the samples
// so the prompt and retro derive the same label deterministically. The window is
// the most-recent activityWindow samples (the recent behavior is what matters for
// a live decision; the full ring still feeds the sparkline).
func ClassifyActivity(samples []MovementSample) Activity {
	const activityWindow = 8
	if len(samples) == 0 {
		return ActivityUnknown
	}
	window := samples
	if len(window) > activityWindow {
		window = window[len(window)-activityWindow:]
	}
	var sum, sumSq float64
	for _, s := range window {
		sum += s.Intensity
		sumSq += s.Intensity * s.Intensity
	}
	n := float64(len(window))
	mean := sum / n
	if mean <= idleFloor {
		return ActivityIdle
	}
	variance := sumSq/n - mean*mean
	if variance < 0 {
		variance = 0 // floating-point guard
	}
	std := math.Sqrt(variance)
	if std > erraticStdFrac*mean {
		return ActivityErratic
	}
	return ActivityActive
}

// Proximity is the per-player proximity-to-death result (#1082): the closest the
// player's movement came to the elimination threshold over the observed window,
// expressed as a fraction of the threshold (1.0 == at the threshold), and WHEN
// (relative to a reference time) that closest call happened. It reads the threshold
// AT THE TIME of each sample when a per-sample threshold track is supplied, so a
// "0.92×thr" call is interpreted against the tempo that was actually in force.
type Proximity struct {
	// Observed is false when proximity cannot be computed — no movement samples or
	// no death-threshold observation. The renderers show "unknown" then.
	Observed bool
	// Ratio is the peak intensity/threshold over the window (the closest call).
	Ratio float64
	// At is the timestamp of the closest call (the sample that produced Ratio).
	At time.Time
	// Crossed is true when the player's intensity met or exceeded the threshold at
	// some point (Ratio >= 1.0) — i.e. it entered death territory.
	Crossed bool
}

// ComputeProximity finds a player's closest call to the death threshold over its
// movement window. threshold is the session death-threshold track (SpeedSamples
// carrying DeathThreshold); the nearest-in-time threshold sample at or before each
// movement sample is used, so the ratio reflects the tempo in force then. When the
// track is empty, fallbackThreshold (the last-known scalar threshold) is used for
// every sample; when neither is available, Observed is false.
func ComputeProximity(samples []MovementSample, track []SpeedSample, fallbackThreshold *float64) Proximity {
	if len(samples) == 0 {
		return Proximity{}
	}
	thrAt := thresholdLookup(track, fallbackThreshold)
	var best Proximity
	for _, s := range samples {
		thr := thrAt(s.At)
		if thr == nil || *thr <= 0 {
			continue
		}
		ratio := s.Intensity / *thr
		if !best.Observed || ratio > best.Ratio {
			best = Proximity{Observed: true, Ratio: ratio, At: s.At, Crossed: ratio >= 1.0}
		}
	}
	return best
}

// thresholdLookup returns a function mapping a sample time to the effective death
// threshold then: the most-recent track sample at-or-before t that carries a
// DeathThreshold, else the fallback scalar (the last observed threshold), else
// nil. The track is assumed oldest-first.
func thresholdLookup(track []SpeedSample, fallback *float64) func(time.Time) *float64 {
	return func(t time.Time) *float64 {
		var chosen *float64
		for _, sp := range track {
			if sp.DeathThreshold == nil {
				continue
			}
			if sp.At.After(t) {
				break
			}
			chosen = sp.DeathThreshold
		}
		if chosen != nil {
			return chosen
		}
		return fallback
	}
}

// RenderSpeedTrack renders the session game-speed series as a tempo sparkline,
// scaled to the observed tempo span so the RAMP reads as a rising sparkline (the
// accelerate rules / adjust_music_tempo step it over the game). An empty track
// renders the empty string. The threshold dimension is folded into each player's
// proximity readout, so the speed track itself shows only the tempo shape.
func RenderSpeedTrack(track []SpeedSample) string {
	tempos := make([]MovementSample, 0, len(track))
	for _, sp := range track {
		if sp.Tempo == nil {
			continue
		}
		tempos = append(tempos, MovementSample{At: sp.At, Intensity: *sp.Tempo})
	}
	if len(tempos) == 0 {
		return ""
	}
	// Auto-scale to the observed tempo span so a flat tempo reads flat and a ramp
	// reads as a rising sparkline. Tempo never goes idle, so the idle glyph never
	// applies here; pass a min-anchored copy by offsetting to the observed floor.
	lo, hi := tempos[0].Intensity, tempos[0].Intensity
	for _, t := range tempos {
		if t.Intensity < lo {
			lo = t.Intensity
		}
		if t.Intensity > hi {
			hi = t.Intensity
		}
	}
	span := hi - lo
	var b strings.Builder
	for _, t := range tempos {
		if span <= 0 {
			b.WriteRune(sparkGlyphs[len(sparkGlyphs)/2]) // flat tempo: a mid bar
			continue
		}
		frac := (t.Intensity - lo) / span
		idx := int(frac * float64(len(sparkGlyphs)-1))
		if idx < 0 {
			idx = 0
		}
		if idx >= len(sparkGlyphs) {
			idx = len(sparkGlyphs) - 1
		}
		b.WriteRune(sparkGlyphs[idx])
	}
	return b.String()
}

// RenderProximity formats a Proximity as the compact "near-death@-Ns(R×thr)"
// readout shown next to a player's sparkline, with "→elim" appended when the
// player crossed the threshold. now is the reference for the relative age. An
// unobserved proximity renders the "unknown" sentinel so the channel is never
// silently dropped.
func RenderProximity(p Proximity, now time.Time, crossedToElim bool) string {
	if !p.Observed {
		return "near-death=unknown"
	}
	age := int(p.At.Sub(now).Round(time.Second) / time.Second)
	out := fmt.Sprintf("near-death@%+ds(%.2f×thr)", age, p.Ratio)
	if crossedToElim {
		out += "→elim"
	}
	return out
}
