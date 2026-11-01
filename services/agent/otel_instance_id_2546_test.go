package main

import (
	"context"
	"regexp"
	"testing"

	"go.opentelemetry.io/otel/attribute"
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

// The real resource must carry the per-process id, not the bare HOSTNAME.
func TestResourceInstanceIDWiring2546(t *testing.T) {
	t.Setenv("HOSTNAME", "container-x")
	res, err := newOTELResource(context.Background(), "svc", "ns")
	if err != nil {
		t.Fatal(err)
	}
	got, ok := res.Set().Value(attribute.Key("service.instance.id"))
	if !ok {
		t.Fatal("service.instance.id missing")
	}
	if got.AsString() != serviceInstanceID("ignored") {
		t.Fatalf("resource id %q != serviceInstanceID %q", got.AsString(), serviceInstanceID("ignored"))
	}
}
