package llm

import (
	"errors"
	"testing"
)

// retro_decode_test.go covers DecodeRetro (#1179): the defensive parser for the
// offline analyst's post-game reply. Unlike the in-game Decode it is recorded-only,
// so the ONLY hard requirement is that the reply be a single JSON object — there is no
// required-field or vocabulary validation. The tests assert: a clean object decodes; a
// fenced / prose-wrapped object decodes (the shared defensive extraction); an empty
// suggestions list is a VALID reply; and non-JSON / empty input return a typed error.

func TestDecodeRetro_Valid(t *testing.T) {
	raw := `{
	  "session_assessment": "tight endgame, two strong survivors",
	  "suggestions": [
	    {"intervention_type": "grant_shield", "emphasis": "weight_up", "reason": "early dropouts faded fast"},
	    {"intervention_type": "play_audio_cue", "emphasis": "enable", "reason": "lull mid-game"}
	  ],
	  "session_focus": "endurance"
	}`
	got, err := DecodeRetro(raw)
	if err != nil {
		t.Fatalf("DecodeRetro returned error: %v", err)
	}
	if got.SessionAssessment != "tight endgame, two strong survivors" {
		t.Errorf("session_assessment = %q", got.SessionAssessment)
	}
	if got.SessionFocus != "endurance" {
		t.Errorf("session_focus = %q, want endurance", got.SessionFocus)
	}
	if len(got.Suggestions) != 2 {
		t.Fatalf("suggestions = %d, want 2", len(got.Suggestions))
	}
	if got.Suggestions[0].InterventionType != "grant_shield" ||
		got.Suggestions[0].Emphasis != "weight_up" ||
		got.Suggestions[0].Reason != "early dropouts faded fast" {
		t.Errorf("suggestion[0] = %+v", got.Suggestions[0])
	}
}

// A model commonly wraps the JSON in a ```json fence and surrounding prose despite the
// "no prose" instruction; DecodeRetro must still recover the object (shared defensive
// extraction with the in-game Decode).
func TestDecodeRetro_FencedAndProseWrapped(t *testing.T) {
	raw := "Sure! Here is my analysis:\n```json\n" +
		`{"session_assessment":"healthy","suggestions":[],"session_focus":"balanced"}` +
		"\n```\nLet me know if you need more."
	got, err := DecodeRetro(raw)
	if err != nil {
		t.Fatalf("DecodeRetro on fenced/prose-wrapped input returned error: %v", err)
	}
	if got.SessionFocus != "balanced" {
		t.Errorf("session_focus = %q, want balanced", got.SessionFocus)
	}
	// An empty suggestions list is a VALID, healthy-session reply.
	if len(got.Suggestions) != 0 {
		t.Errorf("suggestions = %d, want 0 (empty list is valid)", len(got.Suggestions))
	}
}

// An empty suggestions list with a non-empty object is valid (the contract's
// healthy-session case), distinct from a parse failure.
func TestDecodeRetro_EmptySuggestionsValid(t *testing.T) {
	got, err := DecodeRetro(`{"session_assessment":"all good","suggestions":[],"session_focus":"chaos"}`)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got.Suggestions) != 0 {
		t.Errorf("suggestions = %d, want 0", len(got.Suggestions))
	}
}

func TestDecodeRetro_GarbageAndEmpty(t *testing.T) {
	tests := []struct {
		name    string
		raw     string
		wantErr error
	}{
		{"empty", "", ErrRetroEmptyResponse},
		{"whitespace only", "   \n\t ", ErrRetroEmptyResponse},
		{"prose with no object", "the game went well, no suggestions", ErrRetroNotJSON},
		{"unbalanced braces", "{not valid json at all", ErrRetroNotJSON},
		{"a JSON array, not object", `["session_assessment"]`, ErrRetroNotJSON},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := DecodeRetro(tc.raw)
			if err == nil {
				t.Fatalf("DecodeRetro(%q) = nil error, want %v", tc.raw, tc.wantErr)
			}
			if !errors.Is(err, tc.wantErr) {
				t.Errorf("DecodeRetro(%q) error = %v, want errors.Is %v", tc.raw, err, tc.wantErr)
			}
		})
	}
}
