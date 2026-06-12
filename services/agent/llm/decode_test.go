package llm

import (
	"errors"
	"testing"
)

// decode_test.go exhaustively table-tests the defensive response parser (#739).
// The safety property under test: Decode returns a usable Response ONLY for a
// well-formed, complete, in-vocab reply; EVERY other input yields a typed error so
// the decision loop falls back to rules and never dispatches untrusted output.

func TestDecode_Valid(t *testing.T) {
	tests := []struct {
		name string
		raw  string
		want Response
	}{
		{
			name: "minimal session-scoped",
			raw:  `{"intervention":"grant_shield","target_serial":"","value":"","reason":"even the field","objective_served":"balanced"}`,
			want: Response{Intervention: "grant_shield", Reason: "even the field", ObjectiveServed: "balanced"},
		},
		{
			name: "player-targeted with value",
			raw:  `{"intervention":"send_controller_effect","target_serial":"AAAA1111","value":"rumble","reason":"hype","objective_served":"chaos"}`,
			want: Response{Intervention: "send_controller_effect", TargetSerial: "AAAA1111", Value: "rumble", Reason: "hype", ObjectiveServed: "chaos"},
		},
		{
			name: "noop is valid",
			raw:  `{"intervention":"noop","target_serial":"","value":"","reason":"nothing actionable","objective_served":"endurance"}`,
			want: Response{Intervention: "noop", Reason: "nothing actionable", ObjectiveServed: "endurance"},
		},
		{
			name: "wrapped in prose and a json fence",
			raw:  "Sure! Here is my decision:\n```json\n{\"intervention\":\"adjust_music_tempo\",\"value\":\"1.2\",\"reason\":\"raise energy\",\"objective_served\":\"accelerate\"}\n```\nHope that helps.",
			want: Response{Intervention: "adjust_music_tempo", Value: "1.2", Reason: "raise energy", ObjectiveServed: "accelerate"},
		},
		{
			name: "extra unknown fields are ignored",
			raw:  `{"intervention":"grant_shield","reason":"r","objective_served":"balanced","confidence":0.9,"notes":"extra"}`,
			want: Response{Intervention: "grant_shield", Reason: "r", ObjectiveServed: "balanced"},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Decode(tc.raw)
			if err != nil {
				t.Fatalf("Decode error = %v, want nil", err)
			}
			if got != tc.want {
				t.Errorf("Decode = %+v, want %+v", got, tc.want)
			}
		})
	}
}

func TestDecode_Rejects(t *testing.T) {
	tests := []struct {
		name    string
		raw     string
		wantErr error
	}{
		{"empty", "", ErrEmptyResponse},
		{"whitespace only", "   \n\t ", ErrEmptyResponse},
		{"no json at all", "I refuse to answer.", ErrNotJSON},
		{"malformed json", `{"intervention":"grant_shield", "reason":}`, ErrNotJSON},
		{"bare json array no object", `["grant_shield","play_audio_cue"]`, ErrNotJSON},
		{"missing intervention", `{"reason":"r","objective_served":"balanced"}`, ErrMissingField},
		{"empty intervention", `{"intervention":"  ","reason":"r","objective_served":"balanced"}`, ErrMissingField},
		{"missing reason", `{"intervention":"grant_shield","objective_served":"balanced"}`, ErrMissingField},
		{"empty reason", `{"intervention":"grant_shield","reason":"","objective_served":"balanced"}`, ErrMissingField},
		{"missing objective", `{"intervention":"grant_shield","reason":"r"}`, ErrMissingField},
		{"out-of-vocab objective", `{"intervention":"grant_shield","reason":"r","objective_served":"maximize_fun"}`, ErrInvalidObjective},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Decode(tc.raw)
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("Decode error = %v, want %v", err, tc.wantErr)
			}
			// The cardinal safety property: a rejected response yields the zero
			// Response, so no field can leak into a dispatched action.
			if got != (Response{}) {
				t.Errorf("rejected Decode returned non-zero Response %+v", got)
			}
		})
	}
}

// TestDecode_OutOfVocabInterventionIsStructurallyValid documents the boundary
// between the two validation layers: an UNKNOWN intervention id is STRUCTURALLY
// valid here (Decode does not know the per-session allow-list), so Decode accepts
// it — the decision package's permission gate is what blocks it (decision.blocked).
// This keeps the allow-list check in ONE place (evaluatePermission), applied
// identically to rules and llm choices.
func TestDecode_OutOfVocabInterventionIsStructurallyValid(t *testing.T) {
	got, err := Decode(`{"intervention":"launch_nukes","reason":"chaos reigns","objective_served":"chaos"}`)
	if err != nil {
		t.Fatalf("Decode error = %v, want nil (allow-list is enforced downstream, not here)", err)
	}
	if got.Intervention != "launch_nukes" {
		t.Errorf("intervention = %q, want launch_nukes (decode is allow-list-agnostic)", got.Intervention)
	}
}

func TestResponse_IsNoop(t *testing.T) {
	if !(Response{Intervention: "noop"}).IsNoop() {
		t.Error("noop intervention must report IsNoop")
	}
	if (Response{Intervention: "grant_shield"}).IsNoop() {
		t.Error("non-noop intervention must not report IsNoop")
	}
}
