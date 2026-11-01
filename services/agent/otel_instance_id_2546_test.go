package main

import (
	"regexp"
	"testing"
)

// Issue #2546: service.instance.id is "<prefix>-<8 hex>", fresh per draw, stable per process.
func TestInstanceID2546(t *testing.T) {
	re := regexp.MustCompile(`^abc123-[0-9a-f]{8}$`)
	a, b := newInstanceID("abc123"), newInstanceID("abc123")
	if !re.MatchString(a) || !re.MatchString(b) {
		t.Fatalf("bad format: %q %q", a, b)
	}
	if a == b {
		t.Fatalf("two draws gave the same id %q", a)
	}
}

func TestServiceInstanceIDStablePerProcess2546(t *testing.T) {
	t.Setenv("HOSTNAME", "")
	first := serviceInstanceID("host")
	if second := serviceInstanceID("other"); first != second {
		t.Fatalf("id changed within a process: %q vs %q", first, second)
	}
}
