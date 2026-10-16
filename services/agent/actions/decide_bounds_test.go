package actions

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decision"
)

// boundsSource ties each entry of the decide FFA bounds table (#1225) to the
// writer default and the interventions.json flag it was derived from, so the
// table cannot drift from the code it documents.
var boundsSource = map[string]struct {
	flag    string
	neutral string
	def     float64
}{
	decision.InterventionAdjustMusicTempo:        {flagMusicTempoOverride, neutralNone, defaultMusicTempo},
	decision.InterventionAdjustVolume:            {flagVolumeOverride, neutralNone, defaultVolume},
	decision.InterventionAdjustGlobalSensitivity: {flagGlobalSensitivityOverride, neutralNone, defaultGlobalSensitivity},
	decision.InterventionAdjustPlayerSensitivity: {flagPlayerSensitivityFactor, neutralDefault, defaultPlayerSensitivity},
	decision.InterventionAdjustGlobalDifficulty:  {flagGlobalDifficultyFactor, neutralDefault, defaultGlobalDifficulty},
	decision.InterventionSetPlayerHandicap:       {flagPlayerHandicapFactor, neutralDefault, defaultPlayerHandicap},
	decision.InterventionGrantShield:             {flagShieldSeconds, neutralNone, defaultShieldSeconds},
	decision.InterventionPartialShield:           {flagPartialShieldSeconds, neutralNone, partialShieldDefaultSeconds(defaultPartialShield)},
}

func partialShieldDefaultSeconds(directive string) float64 {
	v, _ := strconv.ParseFloat(strings.SplitN(directive, ":", 2)[0], 64)
	return v
}

func TestDecideBounds_MatchWriterDefaultsAndFlagVariants(t *testing.T) {
	if got, want := len(decide.MagnitudeInterventions()), len(boundsSource); got != want {
		t.Fatalf("decide bounds table has %d entries, source map %d: keep them in sync", got, want)
	}

	// The real flag file (not the testdata copy, which can lag behind it).
	raw, err := os.ReadFile(filepath.Join("..", "..", "flagd", "interventions.json"))
	if err != nil {
		t.Skipf("services/flagd/interventions.json not available: %v", err)
	}
	var doc struct {
		Flags map[string]struct {
			Variants map[string]any `json:"variants"`
		} `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parse interventions.json: %v", err)
	}

	for _, iv := range decide.MagnitudeInterventions() {
		src, ok := boundsSource[iv]
		if !ok {
			t.Errorf("%s: bounds entry without a documented source", iv)
			continue
		}
		b, _ := decide.MagnitudeBounds(iv)
		if b.Default != src.def {
			t.Errorf("%s: bounds default %v, writer default %v", iv, b.Default, src.def)
		}
		flag, ok := doc.Flags[src.flag]
		if !ok {
			t.Errorf("%s: flag %q missing from interventions.json", iv, src.flag)
			continue
		}
		lo, hi := math.Inf(1), math.Inf(-1)
		for name, v := range flag.Variants {
			if name == src.neutral {
				continue
			}
			n, ok := variantNumber(v)
			if !ok {
				t.Fatalf("%s: variant %s=%v is not numeric", src.flag, name, v)
			}
			lo, hi = math.Min(lo, n), math.Max(hi, n)
		}
		if b.Min != lo || b.Max != hi {
			t.Errorf("%s: bounds [%v,%v], %s non-neutral variants span [%v,%v]", iv, b.Min, b.Max, src.flag, lo, hi)
		}
		if b.Default < b.Min || b.Default > b.Max {
			t.Errorf("%s: default %v outside [%v,%v]", iv, b.Default, b.Min, b.Max)
		}
	}
}

// variantNumber reads a numeric variant, or the seconds of a "<seconds>:<boost>"
// partial_shield directive.
func variantNumber(v any) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case string:
		n, err := strconv.ParseFloat(strings.SplitN(x, ":", 2)[0], 64)
		return n, err == nil
	}
	return 0, false
}
