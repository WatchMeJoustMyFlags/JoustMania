package promote

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/joustmania/agent/decision"
)

// substrate.go is the shadow substrate validity bar on autonomous promotion
// (UC-043 A23, FR-152 g, FR-203, #2541). An autonomous real-default write is made
// only for an objective an operator attested as validated for the running release
// (env AGENT_AUTONOMOUS_VALIDATED_OBJECTIVES, a C-034 ceiling the agent cannot
// write). The bar is set once, in buildPromoter, before the loop starts; a
// Promoter without a bar refuses every autonomous write (fail closed). It is
// checked in promoteAutonomous and again in writeTracked, the one place every
// real-default write passes. Reverts and the kill switch are never gated.

// ReasonSubstrateNotValidated prefixes the reason of an autonomous promotion
// refused by the validity bar: "substrate_not_validated objective=<obj>".
const ReasonSubstrateNotValidated = "substrate_not_validated"

// substrateNotValidatedError is the refusal of the bar; its message is the
// promotion reason.
type substrateNotValidatedError struct{ objective string }

func (e *substrateNotValidatedError) Error() string {
	return fmt.Sprintf("%s objective=%s", ReasonSubstrateNotValidated, e.objective)
}

// SetSubstrateBar sets the objectives whose substrate is validated for the
// running release (UC-043 A23). It takes effect once: a later call is ignored, so
// the bar cannot change while promotions run. A nil or empty map validates
// nothing. The map is copied.
func (pr *Promoter) SetSubstrateBar(validated map[string]bool) {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	if pr.substrateSet {
		pr.log.Error("substrate validity bar already set; ignoring a second SetSubstrateBar (UC-043 A23)")
		return
	}
	pr.substrateSet = true
	pr.substrate = make(map[string]bool, len(validated))
	for obj, ok := range validated {
		if ok && obj != "" {
			pr.substrate[obj] = true
		}
	}
}

// SubstrateValidated reports whether objective meets the validity bar. The empty
// objective never does, and neither does any objective while no bar is set.
func (pr *Promoter) SubstrateValidated(objective string) bool {
	if objective == "" {
		return false
	}
	pr.mu.Lock()
	defer pr.mu.Unlock()
	return pr.substrate[objective]
}

// substrateRefusal returns the bar's refusal for objective, or nil when it is
// validated.
func (pr *Promoter) substrateRefusal(objective string) error {
	if pr.SubstrateValidated(objective) {
		return nil
	}
	return &substrateNotValidatedError{objective: objective}
}

// releaseVersion is the only SERVICE_VERSION an attestation can bind to: a plain
// semver release tag as release-please writes it (IMAGE_TAG=0.14.0). Moving tags
// (latest, main, dev), a commit sha or a v-prefix never match.
var releaseVersion = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`)

// ParseValidatedObjectives parses AGENT_AUTONOMOUS_VALIDATED_OBJECTIVES, a comma
// list of objective@release entries, against the running SERVICE_VERSION. An entry
// counts only when its objective has a fitness function (endurance, balanced,
// accelerate; case-insensitive) and its release equals serviceVersion exactly,
// and only when serviceVersion is a release (X.Y.Z). It returns the validated
// set and one warning per rejected entry (logged at startup).
func ParseValidatedObjectives(env, serviceVersion string) (map[string]bool, []string) {
	validated := map[string]bool{}
	var warnings []string
	entries := strings.Split(env, ",")
	nonEmpty := 0
	for _, e := range entries {
		if strings.TrimSpace(e) != "" {
			nonEmpty++
		}
	}
	if nonEmpty == 0 {
		return validated, nil
	}
	if !releaseVersion.MatchString(serviceVersion) {
		return validated, []string{fmt.Sprintf(
			"SERVICE_VERSION %q is not a release (X.Y.Z); no objective counts as validated", serviceVersion)}
	}
	for _, raw := range entries {
		entry := strings.TrimSpace(raw)
		if entry == "" {
			continue
		}
		obj, release, ok := strings.Cut(entry, "@")
		obj = strings.ToLower(strings.TrimSpace(obj))
		release = strings.TrimSpace(release)
		switch {
		case !ok || obj == "" || release == "":
			warnings = append(warnings, fmt.Sprintf("entry %q is not objective@release; ignored", entry))
		case !decision.IsExperimentable(obj):
			warnings = append(warnings, fmt.Sprintf("entry %q names no objective with a fitness function; ignored", entry))
		case release != serviceVersion:
			warnings = append(warnings, fmt.Sprintf("entry %q is attested for %s, running %s; ignored", entry, release, serviceVersion))
		default:
			validated[obj] = true
		}
	}
	return validated, warnings
}

// ValidatedObjectiveList renders a validated set for the startup log, sorted.
func ValidatedObjectiveList(validated map[string]bool) []string {
	out := make([]string, 0, len(validated))
	for obj, ok := range validated {
		if ok {
			out = append(out, obj)
		}
	}
	sort.Strings(out)
	return out
}
