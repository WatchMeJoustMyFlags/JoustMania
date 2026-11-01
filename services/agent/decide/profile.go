package decide

import "strings"

// Prompt profiles (decision.prompt_profile, #2467 PR 5, decide-contract.md §3,
// UC-040 BR-036): the wording a remote backend renders a Request with. v1 is the
// shipped prompt, byte for byte; neutral describes the levers by their effects only.
const (
	PromptProfileV1      = "v1"
	PromptProfileNeutral = "neutral"
)

// NormalizePromptProfile maps a decision.prompt_profile value to a known profile:
// neutral (trimmed, case-insensitive) is neutral, anything else, the empty string
// included, is v1 (the shipped prompt is the fallback).
func NormalizePromptProfile(profile string) string {
	if strings.EqualFold(strings.TrimSpace(profile), PromptProfileNeutral) {
		return PromptProfileNeutral
	}
	return PromptProfileV1
}

// IsNeutral reports whether r is rendered under the neutral profile.
func (r Request) IsNeutral() bool {
	return NormalizePromptProfile(r.PromptProfile) == PromptProfileNeutral
}

// NoopDescriptionNeutral is noop's lever text under the neutral profile: what doing
// nothing does, no verdict on when it is right (UC-040 BR-036).
const NoopDescriptionNeutral = "Do nothing and leave the game as it is."
