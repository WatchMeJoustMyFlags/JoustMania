package promote

import (
	"fmt"
	"strings"

	"github.com/joustmania/agent/experiment"
)

// body.go renders the STRUCTURED issue/PR bodies (#936 AC: "PR description is
// structured: fitness before/after, synthetic results, reasoning summary, flag
// state"). The bodies are deterministic Markdown so they are golden-testable and a
// reviewer (or a downstream parser) reads the same fields every time.

// renderIssueBody renders the ModeIssue body: the hypothesis + the implicated flag
// + the fitness evidence that motivated proposing the experiment. NO change is
// applied in issue mode, so the body frames this as a PROPOSAL for a human to act
// on (#936: "open an issue describing the hypothesis and the implicated flag").
func renderIssueBody(p experiment.Proposal, ev Evidence) string {
	var b strings.Builder
	b.WriteString("## Agent flag-experiment hypothesis\n\n")
	b.WriteString("The agent's self-improvement loop validated a shadow-scoped flag ")
	b.WriteString("experiment and is surfacing it for human review (mode=issue: no ")
	b.WriteString("code or flag change has been applied).\n\n")

	b.WriteString("### Implicated flag\n\n")
	fmt.Fprintf(&b, "- **Flag:** `%s`\n", p.FlagKey)
	fmt.Fprintf(&b, "- **Proposed experimental value:** `%v`\n\n", p.ExperimentalValue)

	b.WriteString("### Hypothesis (proposer reasoning)\n\n")
	b.WriteString(reasoningOrFallback(p, ev))
	b.WriteString("\n\n")

	b.WriteString(renderFitnessSection(ev))
	b.WriteString(renderFlagStateSection(ev))
	return b.String()
}

// renderPRBody renders the ModePR body: the full machine-readable evidence the
// #936 AC enumerates — fitness before/after, synthetic game results, the
// proposer/coding-agent reasoning summary, and the flag state at improvement time.
// Structured headings so it parses predictably and reviews at a glance.
func renderPRBody(p experiment.Proposal, ev Evidence) string {
	var b strings.Builder
	b.WriteString("## Agent flag-experiment promotion\n\n")
	b.WriteString("This PR was opened by the agent's self-improvement loop after a ")
	b.WriteString("shadow-scoped flag experiment cleared synthetic validation. ")
	b.WriteString("Nothing is merged automatically — a human reviews + merges.\n\n")

	b.WriteString("### Proposed change\n\n")
	fmt.Fprintf(&b, "- **Flag:** `%s`\n", p.FlagKey)
	fmt.Fprintf(&b, "- **Experimental value:** `%v`\n\n", p.ExperimentalValue)

	b.WriteString(renderFitnessSection(ev))

	b.WriteString("### Synthetic game results\n\n")
	fmt.Fprintf(&b, "- **Synthetic games run:** %d\n", ev.SyntheticGames)
	fmt.Fprintf(&b, "- **Fitness measured over the synthetic suite (after):** %.4f\n\n", ev.FitnessAfter)

	b.WriteString("### Reasoning summary\n\n")
	b.WriteString(reasoningOrFallback(p, ev))
	b.WriteString("\n\n")

	b.WriteString(renderFlagStateSection(ev))
	return b.String()
}

// renderFitnessSection renders the fitness before/after/delta block shared by the
// issue and PR bodies (#936 AC: "fitness before/after").
func renderFitnessSection(ev Evidence) string {
	var b strings.Builder
	b.WriteString("### Fitness before/after\n\n")
	fmt.Fprintf(&b, "- **Before (real-game baseline):** %.4f\n", ev.FitnessBefore)
	fmt.Fprintf(&b, "- **After (synthetic suite):** %.4f\n", ev.FitnessAfter)
	fmt.Fprintf(&b, "- **Delta:** %+.4f\n\n", ev.FitnessDelta)
	return b.String()
}

// renderFlagStateSection renders the flag-state-at-improvement-time block (#936
// AC: "flag state"). The caller assembles ev.FlagState from the live flag snapshot;
// an empty value renders an explicit marker rather than a blank section, so the
// body is never ambiguous about whether the context was captured.
func renderFlagStateSection(ev Evidence) string {
	var b strings.Builder
	b.WriteString("### Flag state at improvement time\n\n")
	if strings.TrimSpace(ev.FlagState) == "" {
		b.WriteString("_(flag state not captured)_\n")
	} else {
		fmt.Fprintf(&b, "```\n%s\n```\n", strings.TrimSpace(ev.FlagState))
	}
	return b.String()
}

// reasoningOrFallback returns the proposer reasoning, falling back to the
// Proposal's rationale, then to an explicit marker — the body always has a
// reasoning section even when the proposer side (#931) supplied nothing.
func reasoningOrFallback(p experiment.Proposal, ev Evidence) string {
	if s := strings.TrimSpace(ev.Reasoning); s != "" {
		return s
	}
	if s := strings.TrimSpace(p.Rationale); s != "" {
		return s
	}
	return "_(no reasoning summary supplied)_"
}

// branchName derives a deterministic, filesystem/git-safe head branch name for a
// github-target PR from the flag key.
func branchName(p experiment.Proposal) string {
	return "agent/experiment-" + sanitize(p.FlagKey)
}

// evidenceFilePath is the repo-relative path a TargetLocal commit writes the
// evidence body to (a record of the experiment in the working tree; the real
// flag-change wiring is a follow-up — here the committed artifact is the evidence).
func evidenceFilePath(p experiment.Proposal) string {
	return "agent-experiments/" + sanitize(p.FlagKey) + ".md"
}

// sanitize lowercases a flag key and replaces any non-alphanumeric run with a
// single hyphen, so it is safe in a branch name / file path.
func sanitize(s string) string {
	var b strings.Builder
	lastHyphen := false
	for _, r := range strings.ToLower(s) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
			lastHyphen = false
		} else if !lastHyphen {
			b.WriteByte('-')
			lastHyphen = true
		}
	}
	return strings.Trim(b.String(), "-")
}
