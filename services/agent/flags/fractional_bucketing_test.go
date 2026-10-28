package flags

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	flagd "github.com/open-feature/go-sdk-contrib/providers/flagd/pkg"
	"github.com/open-feature/go-sdk/openfeature"
)

// fractionalFixture is tests/contract/fractional_buckets.json (#1945): the
// pinned bucket of each serial under a 50 % bluetooth_backend fraction overlay.
const fractionalFixture = "../../../tests/contract/fractional_buckets.json"

// UC: UC-052 main
//
// TestFractionalBucketsMatchThePinnedTable checks that the flagd core the agent
// links (in-process evaluation) buckets the pinned serials exactly as the
// Python provider and the flagd image do (tests/contract/test_fractional_bucketing.py).
// A failure means a flagd component moved to another fractional hash on its own.
func TestFractionalBucketsMatchThePinnedTable(t *testing.T) {
	raw, err := os.ReadFile(fractionalFixture)
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var fx struct {
		FlagKey        string            `json:"flag_key"`
		Variants       map[string]string `json:"variants"`
		DefaultVariant string            `json:"default_variant"`
		Targeting      json.RawMessage   `json:"targeting"`
		Cases          map[string]string `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatalf("parse fixture: %v", err)
	}
	doc := map[string]any{"flags": map[string]any{fx.FlagKey: map[string]any{
		"state":          "ENABLED",
		"variants":       fx.Variants,
		"defaultVariant": fx.DefaultVariant,
		"targeting":      fx.Targeting,
	}}}
	body, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "flags.json")
	if err := os.WriteFile(path, body, 0o600); err != nil {
		t.Fatal(err)
	}

	provider, err := flagd.NewProvider(flagd.WithFileResolver(), flagd.WithOfflineFilePath(path))
	if err != nil {
		t.Fatalf("provider: %v", err)
	}
	domain := "fractional-bucketing-" + t.Name()
	if err := openfeature.SetNamedProviderAndWait(domain, provider); err != nil {
		t.Fatalf("set provider: %v", err)
	}
	t.Cleanup(func() { provider.Shutdown() })
	client := openfeature.NewClient(domain)

	for serial, want := range fx.Cases {
		t.Run("UC-052/main "+serial, func(t *testing.T) {
			got, err := client.StringValue(context.Background(), fx.FlagKey, "",
				openfeature.NewTargetlessEvaluationContext(map[string]any{
					openfeature.TargetingKey: serial,
				}))
			if err != nil {
				t.Fatalf("evaluate: %v", err)
			}
			if got != want {
				t.Errorf("serial %s: got %q, pinned %q", serial, got, want)
			}
		})
	}
}
