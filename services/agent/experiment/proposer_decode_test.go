package experiment

import (
	"errors"
	"testing"
)

// proposer_decode_test.go exhaustively table-tests the pure proposal decoder,
// mirroring services/agent/llm/decode_test.go. The contract under test is the
// #931/#739 untrusted-output rule: ONLY a well-formed, complete, in-vocab reply
// yields a Proposal; everything else returns a typed error and (in the Proposer)
// produces no proposal. No flagd, no network, no clock.

// vocab is the known-flag set the decoder validates against — a small stand-in
// for the live game.json flag keys.
var vocab = map[string]struct{}{
	"death_grace_period_seconds": {},
	"invincibility_seconds":      {},
	"random_assignment":          {},
}

func TestDecodeProposal(t *testing.T) {
	tests := []struct {
		name     string
		raw      string
		wantErr  error  // sentinel the returned error must wrap; nil = expect success
		wantFlag string // asserted only on success
	}{
		{
			name:     "valid bare JSON",
			raw:      `{"flag":"death_grace_period_seconds","value":0.75,"rationale":"longer grace may extend sessions"}`,
			wantFlag: "death_grace_period_seconds",
		},
		{
			name:     "valid wrapped in prose and fences",
			raw:      "Sure! Here is my proposal:\n```json\n{\"flag\":\"invincibility_seconds\",\"value\":5.0,\"rationale\":\"more mercy windows\"}\n```\nHope that helps.",
			wantFlag: "invincibility_seconds",
		},
		{
			name:     "valid boolean value",
			raw:      `{"flag":"random_assignment","value":false,"rationale":"try deterministic team assignment"}`,
			wantFlag: "random_assignment",
		},
		{
			name:    "empty",
			raw:     "   \n\t ",
			wantErr: errEmptyProposal,
		},
		{
			name:    "not json",
			raw:     "I think you should tune the grace period higher.",
			wantErr: errNotJSONProposal,
		},
		{
			name:    "malformed json object",
			raw:     `{"flag":"death_grace_period_seconds","value":}`,
			wantErr: errNotJSONProposal,
		},
		{
			name:    "missing flag",
			raw:     `{"value":0.75,"rationale":"why"}`,
			wantErr: errMissingProposalField,
		},
		{
			name:    "empty flag",
			raw:     `{"flag":"  ","value":0.75,"rationale":"why"}`,
			wantErr: errMissingProposalField,
		},
		{
			name:    "missing value",
			raw:     `{"flag":"death_grace_period_seconds","rationale":"why"}`,
			wantErr: errMissingProposalField,
		},
		{
			name:    "explicit null value",
			raw:     `{"flag":"death_grace_period_seconds","value":null,"rationale":"why"}`,
			wantErr: errMissingProposalField,
		},
		{
			name:    "missing rationale",
			raw:     `{"flag":"death_grace_period_seconds","value":0.75}`,
			wantErr: errMissingProposalField,
		},
		{
			name:    "unknown flag (out of vocab)",
			raw:     `{"flag":"totally_made_up_flag","value":1,"rationale":"hallucinated"}`,
			wantErr: errUnknownProposalFlag,
		},
		{
			name:    "agent.json smuggle attempt is out of vocab",
			raw:     `{"flag":"enabled","value":true,"rationale":"turn myself off? no"}`,
			wantErr: errUnknownProposalFlag,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p, err := decodeProposal(tt.raw, vocab)
			if tt.wantErr != nil {
				if !errors.Is(err, tt.wantErr) {
					t.Fatalf("decodeProposal err = %v, want wrap of %v", err, tt.wantErr)
				}
				if p.FlagKey != "" {
					t.Fatalf("decodeProposal returned a non-empty Proposal on error: %+v", p)
				}
				return
			}
			if err != nil {
				t.Fatalf("decodeProposal unexpected err: %v", err)
			}
			if p.FlagKey != tt.wantFlag {
				t.Fatalf("FlagKey = %q, want %q", p.FlagKey, tt.wantFlag)
			}
			if p.ExperimentalValue == nil {
				t.Fatalf("ExperimentalValue is nil on a valid proposal")
			}
			if p.Rationale == "" {
				t.Fatalf("Rationale is empty on a valid proposal")
			}
		})
	}
}

// TestDecodeProposal_EmptyVocabRejectsEverything pins the fail-closed reading: with
// no known flags, every flag is out of vocab (nothing is proposable), so even a
// structurally valid reply is rejected.
func TestDecodeProposal_EmptyVocabRejectsEverything(t *testing.T) {
	raw := `{"flag":"death_grace_period_seconds","value":0.75,"rationale":"why"}`
	if _, err := decodeProposal(raw, nil); !errors.Is(err, errUnknownProposalFlag) {
		t.Fatalf("empty vocab: err = %v, want errUnknownProposalFlag", err)
	}
	if _, err := decodeProposal(raw, map[string]struct{}{}); !errors.Is(err, errUnknownProposalFlag) {
		t.Fatalf("empty-map vocab: err = %v, want errUnknownProposalFlag", err)
	}
}

// TestDecodeProposal_PreservesNumberAndObjectValues confirms the experimental
// value round-trips through the decoder with its JSON shape intact (numbers as
// float64, objects as maps), so the Writer re-marshals the same value the model
// proposed.
func TestDecodeProposal_PreservesNumberAndObjectValues(t *testing.T) {
	objVocab := map[string]struct{}{"nonstop.scoring": {}}
	raw := `{"flag":"nonstop.scoring","value":{"base":120,"death_penalty":5},"rationale":"reward survival more"}`
	p, err := decodeProposal(raw, objVocab)
	if err != nil {
		t.Fatalf("decodeProposal: %v", err)
	}
	m, ok := p.ExperimentalValue.(map[string]any)
	if !ok {
		t.Fatalf("ExperimentalValue type = %T, want map[string]any", p.ExperimentalValue)
	}
	if m["base"].(float64) != 120 {
		t.Fatalf("base = %v, want 120", m["base"])
	}
}
