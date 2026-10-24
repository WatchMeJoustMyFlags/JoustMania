package baseline

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func flagdDir(t *testing.T, sub string) string {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(file), "..", "..", "flagd", sub)
}

func writeFiles(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// UC: UC-053 main BR-007
func TestLoad(t *testing.T) {
	for _, sub := range []string{"", "ci"} {
		t.Run("UC-053/main the nine baseline files of services/flagd/"+sub+" load and merge", func(t *testing.T) {
			b, err := Load(flagdDir(t, sub), DefaultFiles)
			if err != nil {
				t.Fatal(err)
			}
			if len(b.Order) != 9 {
				t.Fatalf("sets %v", b.Order)
			}
			for _, id := range b.Order {
				for key := range b.Sets[id].Flags {
					if b.SetOf(key) != id {
						t.Fatalf("%s maps to %q, want %q", key, b.SetOf(key), id)
					}
				}
			}
		})
	}
	t.Run("UC-053/main a duplicate flag key across sets refuses the start", func(t *testing.T) {
		dir := writeFiles(t, map[string]string{
			"a.json": `{"metadata":{"flagSetId":"a"},"flags":{"k":{"state":"ENABLED","variants":{"x":1},"defaultVariant":"x"}}}`,
			"b.json": `{"metadata":{"flagSetId":"b"},"flags":{"k":{"state":"ENABLED","variants":{"x":1},"defaultVariant":"x"}}}`,
		})
		if _, err := Load(dir, []string{"a.json", "b.json"}); err == nil || !strings.Contains(err.Error(), "duplicate flag key") {
			t.Fatalf("got %v", err)
		}
	})
	t.Run("UC-053/main a second file for the same set is refused", func(t *testing.T) {
		dir := writeFiles(t, map[string]string{
			"controller.json":         `{"metadata":{"flagSetId":"controller"},"flags":{"a":{"state":"ENABLED","variants":{"x":1},"defaultVariant":"x"}}}`,
			"controller.ci-rust.json": `{"metadata":{"flagSetId":"controller"},"flags":{"b":{"state":"ENABLED","variants":{"x":1},"defaultVariant":"x"}}}`,
		})
		if _, err := Load(dir, []string{"controller.json", "controller.ci-rust.json"}); err == nil {
			t.Fatal("a second controller set must be refused")
		}
	})
	t.Run("UC-053/main malformed baselines are refused", func(t *testing.T) {
		bad := map[string]string{
			"mismatch":   `{"metadata":{"flagSetId":"other"},"flags":{"k":{"variants":{"x":1},"defaultVariant":"x"}}}`,
			"nodefault":  `{"flags":{"k":{"variants":{"x":1},"defaultVariant":"y"}}}`,
			"novariants": `{"flags":{"k":{"variants":{},"defaultVariant":"y"}}}`,
			"empty":      `{"flags":{}}`,
			"notjson":    `{`,
		}
		for name, body := range bad {
			dir := writeFiles(t, map[string]string{name + ".json": body})
			if _, err := Load(dir, []string{name + ".json"}); err == nil {
				t.Errorf("%s: loaded", name)
			}
		}
		dir := writeFiles(t, map[string]string{"games.json": `{"flags":{"k":{"variants":{"x":1},"defaultVariant":"x"}}}`})
		if _, err := Load(dir, []string{"games.json"}); err == nil {
			t.Error("a reserved set id loaded")
		}
		if _, err := Load(dir, []string{"missing.json"}); err == nil {
			t.Error("a missing file loaded")
		}
	})
}

func TestVariantOrderIsTheDeclaredOrder(t *testing.T) {
	b, err := Load(flagdDir(t, "ci"), DefaultFiles)
	if err != nil {
		t.Fatal(err)
	}
	got := b.Flag("game", "sensitivity").VariantNames()
	want := []string{"ultra_slow", "slow", "medium", "fast", "ultra_fast"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("sensitivity variants %v, want the file order %v", got, want)
	}
}
