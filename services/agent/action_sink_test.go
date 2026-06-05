package main

import (
	"io"
	"log/slog"
	"testing"

	"github.com/joustmania/agent/actions"
)

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// TestActionSinkDisabledByDefault: with the env var unset the agent stays inert
// (nil sink -> loop keeps NoopActions), so the scaffold applies nothing.
func TestActionSinkDisabledByDefault(t *testing.T) {
	t.Setenv("AGENT_INTERVENTIONS_ENABLED", "")
	if sink := actionSink(discardLogger()); sink != nil {
		t.Fatalf("expected nil sink when disabled, got %T", sink)
	}
}

func TestActionSinkDisabledOnFalse(t *testing.T) {
	t.Setenv("AGENT_INTERVENTIONS_ENABLED", "false")
	if sink := actionSink(discardLogger()); sink != nil {
		t.Fatalf("expected nil sink when false, got %T", sink)
	}
}

// TestActionSinkEnabled: AGENT_INTERVENTIONS_ENABLED=true wires the real Writer.
func TestActionSinkEnabled(t *testing.T) {
	t.Setenv("AGENT_INTERVENTIONS_ENABLED", "true")
	t.Setenv("INTERVENTIONS_FLAG_PATH", "/tmp/does-not-matter.json")
	sink := actionSink(discardLogger())
	if _, ok := sink.(*actions.Writer); !ok {
		t.Fatalf("expected *actions.Writer when enabled, got %T", sink)
	}
}

func TestActionSinkEnabledCaseInsensitive(t *testing.T) {
	t.Setenv("AGENT_INTERVENTIONS_ENABLED", "TRUE")
	if sink := actionSink(discardLogger()); sink == nil {
		t.Fatal("expected non-nil sink for TRUE")
	}
}
