package flags

import (
	"context"
	"log/slog"
	"net"
	"strconv"
	"strings"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
)

// ObservabilityDomain is the domain / flagSetId of the observability set
// (services/flagd/observability.json), of which the agent reads only log_level (#1788).
const ObservabilityDomain = "observability"

// KeyLogLevel is the log level flag (flags.md §15, FR-150, FR-151).
const KeyLogLevel = "log_level"

// logLevelPollInterval is the safety net under the configuration-change handler.
const logLevelPollInterval = 5 * time.Second

// ParseLogLevel maps a level name to a slog level; ok is false for an unknown or empty name.
func ParseLogLevel(s string) (level slog.Level, ok bool) {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "debug":
		return slog.LevelDebug, true
	case "info":
		return slog.LevelInfo, true
	case "warn", "warning":
		return slog.LevelWarn, true
	case "error":
		return slog.LevelError, true
	}
	return slog.LevelInfo, false
}

// LogLevelFollower keeps a slog.LevelVar equal to the log_level flag at runtime.
// Resolution, the flag always wins: the flag when flagd resolves it; only when it cannot
// be resolved (flagd unreachable, flag missing, no first sync yet, unknown value) the
// deprecated LOG_LEVEL env value when it names a level; else info.
type LogLevelFollower struct {
	client  Evaluator
	level   *slog.LevelVar
	service string
	env     string
	log     *slog.Logger
}

// NewLogLevelFollower builds a follower over client. env is the raw LOG_LEVEL value.
func NewLogLevelFollower(client Evaluator, level *slog.LevelVar, service, env string, log *slog.Logger) *LogLevelFollower {
	if log == nil {
		log = slog.Default()
	}
	return &LogLevelFollower{client: client, level: level, service: service, env: env, log: log}
}

// Resolve returns the level now: flag, else env, else info.
func (f *LogLevelFollower) Resolve(ctx context.Context) slog.Level {
	evalCtx := openfeature.NewEvaluationContext("", map[string]any{"service": f.service})
	if v, err := f.client.StringValue(ctx, KeyLogLevel, "", evalCtx); err == nil {
		if level, ok := ParseLogLevel(v); ok {
			return level
		}
	}
	if level, ok := ParseLogLevel(f.env); ok {
		return level
	}
	return slog.LevelInfo
}

// Refresh applies the resolved level.
func (f *LogLevelFollower) Refresh(ctx context.Context) {
	next := f.Resolve(ctx)
	if prev := f.level.Level(); prev != next {
		f.level.Set(next)
		f.log.Info("log level changed (log_level flag, #1788)", "from", prev.String(), "to", next.String())
	}
}

// SetupLogLevel registers the observability flagd domain (in-process, non-blocking like
// SetupFlagd) and keeps level following the log_level flag: on provider ready, on every
// configuration change, and every few seconds as the safety net (#1709). It returns at
// once; until the first sync the env level (or info) applies.
func SetupLogLevel(ctx context.Context, cfg ProviderConfig, service, env string, level *slog.LevelVar, log *slog.Logger) error {
	if log == nil {
		log = slog.Default()
	}
	probe := tcpProbe(net.JoinHostPort(cfg.Host, strconv.Itoa(int(cfg.Port))))
	client, err := registerDomain(ObservabilityDomain, flagdFactory(cfg, ObservabilityDomain), probe, defaultSyncTiming, log)
	if err != nil {
		return err
	}
	f := NewLogLevelFollower(client, level, service, env, log)
	f.Refresh(context.Background())
	// context.Background(): a refresh racing shutdown must not evaluate with a cancelled ctx.
	onEvent := openfeature.EventCallback(func() *func(openfeature.EventDetails) {
		cb := func(openfeature.EventDetails) { f.Refresh(context.Background()) }
		return &cb
	}())
	client.AddHandler(openfeature.ProviderReady, onEvent)
	client.AddHandler(openfeature.ProviderConfigChange, onEvent)
	go func() {
		ticker := time.NewTicker(logLevelPollInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				f.Refresh(context.Background())
			}
		}
	}()
	return nil
}
