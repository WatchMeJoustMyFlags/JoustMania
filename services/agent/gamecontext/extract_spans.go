package gamecontext

import (
	"time"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

// Span attribute keys and event names.
const (
	spanAttrSerial   = "player.serial"
	spanAttrGameMode = "game.mode"
	spanAttrGameID   = "game.id"
	spanAttrGameKind = "game.kind"
	// spanAttrGameName is set only by the game root span together with game.id and
	// game.kind (game_session.py), which identifies the root span (#1494).
	spanAttrGameName = "game.name"
	// Experiment attribution span attributes (#975). Spans are the PRIMARY
	// attribution channel — the game span carries experiment.id / experiment.arm
	// unconditionally (empty when not in an experiment).
	spanAttrExperimentID = "experiment.id"
	spanAttrArm          = "experiment.arm"
	spanEventDeath       = "player_death"

	// resourceAttrServiceName mirrors semconv service.name, used for the
	// own-telemetry skip.
	resourceAttrServiceName = "service.name"
)

// spanGameIDOf reads the game.id span attribute; empty when absent. It is the
// span-path routing key the Multiplexer (#845 PR B) partitions on, mirroring
// gameIDOf for metric datapoints.
func spanGameIDOf(attrs pcommon.Map) string {
	if v, ok := attrs.Get(spanAttrGameID); ok {
		return v.AsString()
	}
	return ""
}

// spanGameLabels resolves the game identity from a span's attributes: the
// game.id / game.kind attributes carried by the game-session span (always) and
// the player_lifecycle span (since #845's producer change). Mirrors
// metricGameLabels so the metric and span paths share the gameLabels routing
// key PR B builds its multiplexer on.
func spanGameLabels(attrs pcommon.Map) gameLabels {
	var labels gameLabels
	if v, ok := attrs.Get(spanAttrGameID); ok {
		labels.GameID = v.AsString()
	}
	if v, ok := attrs.Get(spanAttrGameKind); ok {
		labels.GameKind = NormalizeGameKind(v.AsString()) // "primary" -> "real" (#1249)
	}
	if v, ok := attrs.Get(spanAttrExperimentID); ok {
		labels.ExperimentID = v.AsString()
	}
	if v, ok := attrs.Get(spanAttrArm); ok {
		labels.Arm = v.AsString()
	}
	return labels
}

// isOwnResource reports whether a resource belongs to this agent itself
// (service.name matches ownService) — its telemetry is skipped to break the
// otlp/agent fan-out feedback loop.
func (s *Store) isOwnResource(res pcommon.Resource) bool {
	if s.ownService == "" {
		return false
	}
	v, ok := res.Attributes().Get(resourceAttrServiceName)
	return ok && v.AsString() == s.ownService
}

// ApplySpans applies trace data to the store. Spans are a LATE signal in this
// system: player_lifecycle spans only end at game end, and the collector batches
// traces (~10s), so this path is used for identity enrichment and elimination
// confirmation, and as the second game-end signal (the game root span, #1494),
// never as a primary live signal (UC-041 BR-001: metrics drive decisions, spans
// are late and serve audit and elimination order). It returns true if anything
// was recognized.
func (s *Store) ApplySpans(td ptrace.Traces) bool {
	updated := false
	rss := td.ResourceSpans()
	for i := 0; i < rss.Len(); i++ {
		if s.isOwnResource(rss.At(i).Resource()) {
			continue
		}
		sss := rss.At(i).ScopeSpans()
		for j := 0; j < sss.Len(); j++ {
			spans := sss.At(j).Spans()
			for k := 0; k < spans.Len(); k++ {
				if s.applySpan(spans.At(k)) {
					updated = true
				}
			}
		}
	}
	return updated
}

func (s *Store) applySpan(span ptrace.Span) bool {
	attrs := span.Attributes()
	updated := false

	// Resolve the span's game identity once and thread it (PR-B routing key, #845).
	labels := spanGameLabels(attrs)

	var serial string
	if sv, ok := attrs.Get(spanAttrSerial); ok {
		serial = sv.AsString()
		if serial != "" {
			// Ensure identity exists (intensity-less player record).
			s.ensurePlayer(serial)
			updated = true
		}
	}
	if mv, ok := attrs.Get(spanAttrGameMode); ok {
		s.SetGameMode(mv.AsString())
		updated = true
	}
	if labels.GameID != "" {
		s.AdoptSessionID(labels.GameID)
		updated = true
	}
	if labels.GameKind != "" {
		s.SetGameKind(labels.GameKind)
		updated = true
	}
	if labels.ExperimentID != "" {
		s.SetExperimentID(labels.ExperimentID)
		updated = true
	}
	if labels.Arm != "" {
		s.SetArm(labels.Arm)
		updated = true
	}

	events := span.Events()
	for e := 0; e < events.Len(); e++ {
		ev := events.At(e)
		if ev.Name() == spanEventDeath && serial != "" {
			s.RecordElimination(serial)
			updated = true
		}
	}

	// Game end from the root span (#1494, UC-041 A4): OTLP exports a span only
	// once it ended, and the game root span ends when its session ends (natural
	// end, force-end or error), so it reaching the agent means the game is over
	// even when its game_active 0 datapoint was never exported. Applied after the
	// identity above so the end snapshot carries the game's id, kind and arm.
	if isGameRootSpan(attrs, labels) {
		var end time.Time
		if ts := span.EndTimestamp(); ts != 0 {
			end = ts.AsTime()
		}
		s.EndGameFromRootSpan(end)
		updated = true
	}
	return updated
}

// isGameRootSpan reports whether a span is a game's root span: the only span
// that carries game.name together with game.id and game.kind (the
// StreamGameEvents server span has game.name + game.id but no game.kind; the
// player_lifecycle span has game.id + game.kind but no game.name).
func isGameRootSpan(attrs pcommon.Map, labels gameLabels) bool {
	if labels.GameID == "" || labels.GameKind == "" {
		return false
	}
	_, ok := attrs.Get(spanAttrGameName)
	return ok
}

// ensurePlayer creates a player record for identity enrichment without setting
// any signal value. Caller must NOT hold s.mu.
func (s *Store) ensurePlayer(serial string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.player(serial)
	s.touch(p)
}
