package flags

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"testing"

	"github.com/open-feature/go-sdk/openfeature"
)

// logLevelEvaluator serves log_level: value, or err when unresolved.
type logLevelEvaluator struct {
	settableEvaluator
	mu    sync.Mutex
	value string
	err   error
	got   openfeature.EvaluationContext
}

func (e *logLevelEvaluator) StringValue(_ context.Context, flag string, def string, evalCtx openfeature.EvaluationContext, _ ...openfeature.Option) (string, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.got = evalCtx
	if flag != KeyLogLevel || e.err != nil {
		return def, e.err
	}
	return e.value, nil
}

func (e *logLevelEvaluator) set(value string, err error) {
	e.mu.Lock()
	e.value, e.err = value, err
	e.mu.Unlock()
}

func TestLogLevelFollower_Resolution(t *testing.T) {
	unresolved := errors.New("provider not ready")
	cases := []struct {
		name  string
		value string
		err   error
		env   string
		want  slog.Level
	}{
		{"flag wins over env", "debug", nil, "error", slog.LevelDebug},
		{"env when flag unresolved", "", unresolved, "warning", slog.LevelWarn},
		{"default when neither", "", unresolved, "", slog.LevelInfo},
		{"unknown flag value falls to env", "loud", nil, "error", slog.LevelError},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ev := &logLevelEvaluator{value: tc.value, err: tc.err}
			f := NewLogLevelFollower(ev, &slog.LevelVar{}, "agent", tc.env, nil)
			if got := f.Resolve(context.Background()); got != tc.want {
				t.Fatalf("got %v want %v", got, tc.want)
			}
		})
	}
}

func TestLogLevelFollower_RuntimeChangeWithoutRestart(t *testing.T) {
	ev := &logLevelEvaluator{value: "info"}
	var level slog.LevelVar
	f := NewLogLevelFollower(ev, &level, "agent", "", nil)
	f.Refresh(context.Background())
	if level.Level() != slog.LevelInfo {
		t.Fatalf("got %v", level.Level())
	}
	ev.set("debug", nil)
	f.Refresh(context.Background())
	if level.Level() != slog.LevelDebug {
		t.Fatalf("flag change not applied: %v", level.Level())
	}
	ev.set("", errors.New("sync lost"))
	f.Refresh(context.Background())
	if level.Level() != slog.LevelInfo {
		t.Fatalf("unresolved flag should fall back: %v", level.Level())
	}
}

func TestLogLevelFollower_ServiceInContext(t *testing.T) {
	ev := &logLevelEvaluator{value: "info"}
	NewLogLevelFollower(ev, &slog.LevelVar{}, "agent", "", nil).Resolve(context.Background())
	if got := ev.got.Attribute("service"); got != "agent" {
		t.Fatalf("service attr = %v", got)
	}
}
