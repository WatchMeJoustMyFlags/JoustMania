package gamecontext

import (
	"go.opentelemetry.io/collector/pdata/ptrace"
)

// Span attribute keys and event names.
const (
	spanAttrSerial   = "player.serial"
	spanAttrGameMode = "game.mode"
	spanAttrGameID   = "game.id"
	spanEventDeath   = "player_death"
)

// ApplySpans applies trace data to the store. Spans are a LATE signal in this
// system: player_lifecycle spans only end at game end, and the collector batches
// traces (~10s), so this path is used for identity enrichment and elimination
// confirmation only, never as a primary live signal. It returns true if anything
// was recognized.
func (s *Store) ApplySpans(td ptrace.Traces) bool {
	updated := false
	rss := td.ResourceSpans()
	for i := 0; i < rss.Len(); i++ {
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
	if gv, ok := attrs.Get(spanAttrGameID); ok {
		s.AdoptSessionID(gv.AsString())
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
	return updated
}

// ensurePlayer creates a player record for identity enrichment without setting
// any signal value. Caller must NOT hold s.mu.
func (s *Store) ensurePlayer(serial string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.player(serial)
	s.touch(p)
}
