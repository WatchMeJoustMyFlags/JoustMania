// Command flag-service holds JoustMania's runtime flag state in memory and
// serves it to a stock flagd as a flagd.sync.v1 gRPC sync source (epic #1407,
// UC-053, services.md §7). Writers use the HTTP/JSON API on 8070; flagd
// connects to 50064.
//
// It loads the baseline flag files once at start and never writes them
// (C-030, C-033); only admin values written with "persist": true go to its own
// state file.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"

	"github.com/joustmania/flag-service/api"
	"github.com/joustmania/flag-service/baseline"
	"github.com/joustmania/flag-service/registry"
	"github.com/joustmania/flag-service/state"
	"github.com/joustmania/flag-service/syncserver"
	"github.com/joustmania/flag-service/telemetry"
)

const defaultServiceName = "flag-service"

func getEnv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func resolveServiceName() string { return getEnv("OTEL_SERVICE_NAME", defaultServiceName) }

// config is the service configuration (services.md §7, "Config").
type config struct {
	BaselineDir    string
	BaselineFiles  []string
	Registry       string
	StateFile      string
	Served         []string
	EventListLen   int
	MaxGameAge     time.Duration
	ExperimentIdle time.Duration
	MaxGames       int
	GRPCPort       string
	HTTPPort       string
	SweepInterval  time.Duration
}

func splitList(s string) []string {
	var out []string
	for _, part := range strings.Split(s, ",") {
		if p := strings.TrimSpace(part); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func loadConfig() (config, error) {
	cfg := config{
		BaselineDir:    getEnv("FLAG_SERVICE_BASELINE_DIR", "/etc/flagd"),
		BaselineFiles:  splitList(getEnv("FLAG_SERVICE_BASELINE_FILES", strings.Join(baseline.DefaultFiles, ","))),
		Registry:       getEnv("FLAG_SERVICE_REGISTRY", "/etc/flag-service/registry.yaml"),
		StateFile:      getEnv("FLAG_SERVICE_STATE_FILE", "/var/lib/flag-service/admin.json"),
		Served:         splitList(os.Getenv("FLAG_SERVICE_SETS")),
		EventListLen:   state.DefaultEventListLen,
		MaxGameAge:     state.DefaultMaxGameAge,
		MaxGames:       state.DefaultMaxGames,
		ExperimentIdle: state.DefaultExperimentIdle,
		GRPCPort:       getEnv("FLAG_SERVICE_GRPC_PORT", "50064"),
		HTTPPort:       getEnv("FLAG_SERVICE_HTTP_PORT", "8070"),
		SweepInterval:  5 * time.Second,
	}
	if cfg.StateFile == "off" {
		cfg.StateFile = ""
	}
	if v := os.Getenv("FLAG_SERVICE_EVENT_LIST_LEN"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 || n > 256 {
			return cfg, fmt.Errorf("FLAG_SERVICE_EVENT_LIST_LEN=%q: want 1..256", v)
		}
		cfg.EventListLen = n
	}
	if v := os.Getenv("FLAG_SERVICE_MAX_GAMES"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 || n > 4096 {
			return cfg, fmt.Errorf("FLAG_SERVICE_MAX_GAMES=%q: want 1..4096", v)
		}
		cfg.MaxGames = n
	}
	if v := os.Getenv("FLAG_SERVICE_EXPERIMENT_IDLE"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil || d <= 0 {
			return cfg, fmt.Errorf("FLAG_SERVICE_EXPERIMENT_IDLE=%q: want a positive duration such as 2h", v)
		}
		cfg.ExperimentIdle = d
	}
	if v := os.Getenv("FLAG_SERVICE_MAX_GAME_AGE"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil || d <= 0 {
			return cfg, fmt.Errorf("FLAG_SERVICE_MAX_GAME_AGE=%q: want a positive duration such as 1h", v)
		}
		cfg.MaxGameAge = d
	}
	return cfg, nil
}

func newEpoch() string {
	b := make([]byte, 4)
	if _, err := rand.Read(b); err != nil {
		return strconv.FormatInt(time.Now().UnixNano()%0xffffffff, 16)
	}
	return hex.EncodeToString(b)
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		os.Exit(healthcheck())
	}
	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: parseLevel(os.Getenv("LOG_LEVEL"))}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("flag service stopped", "error", err)
		os.Exit(1)
	}
}

func parseLevel(s string) slog.Level {
	switch strings.ToLower(s) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	}
	return slog.LevelInfo
}

// healthcheck is the container health probe (the runtime image has no shell
// or wget): GET /healthz on the local HTTP port.
func healthcheck() int {
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get("http://127.0.0.1:" + getEnv("FLAG_SERVICE_HTTP_PORT", "8070") + "/healthz")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		fmt.Fprintln(os.Stderr, "healthz:", resp.Status)
		return 1
	}
	return 0
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	shutdownTracing := initTracing(ctx)
	shutdownMetrics := initMetrics(ctx)
	defer func() {
		sctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = shutdownTracing(sctx)
		_ = shutdownMetrics(sctx)
	}()
	tracer := otel.Tracer(telemetry.Name)
	epoch := newEpoch()

	st, report, err := loadState(ctx, tracer, cfg, epoch, logger)
	if err != nil {
		return err
	}
	metrics, err := telemetry.NewMetrics(otel.Meter(telemetry.Name), st.Stats)
	if err != nil {
		return fmt.Errorf("metrics: %w", err)
	}
	recordLoadReport(ctx, metrics, report)

	links := &telemetry.Links{}
	grpcServer := grpc.NewServer(syncserver.ServerOptions()...)
	(&syncserver.Server{State: st, Metrics: metrics, Tracer: tracer, Links: links}).Register(grpcServer)
	healthSrv := health.NewServer()
	healthpb.RegisterHealthServer(grpcServer, healthSrv)
	lis, err := net.Listen("tcp", ":"+cfg.GRPCPort)
	if err != nil {
		return fmt.Errorf("listen gRPC :%s: %w", cfg.GRPCPort, err)
	}
	var grpcUp atomic.Bool
	go func() {
		grpcUp.Store(true)
		if err := grpcServer.Serve(lis); err != nil {
			logger.Error("gRPC server stopped", "error", err)
			grpcUp.Store(false)
		}
	}()

	httpAPI := &api.API{State: st, Metrics: metrics, Tracer: tracer, Links: links, Logger: logger, Ready: grpcUp.Load}
	httpServer := &http.Server{
		Addr:              ":" + cfg.HTTPPort,
		Handler:           httpAPI.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
	}
	httpErr := make(chan error, 1)
	go func() { httpErr <- httpServer.ListenAndServe() }()

	go sweep(ctx, st, metrics, tracer, cfg.SweepInterval, logger)

	logger.Info("flag service started", "epoch", epoch, "served_sets", cfg.Served,
		"grpc_port", cfg.GRPCPort, "http_port", cfg.HTTPPort, "admin_values", report.AdminValues,
		"state_file", cfg.StateFile)

	select {
	case <-ctx.Done():
	case err := <-httpErr:
		if !errors.Is(err, http.ErrServerClosed) {
			return fmt.Errorf("HTTP server: %w", err)
		}
	}
	logger.Info("flag service shutting down")
	sctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_ = httpServer.Shutdown(sctx)
	grpcServer.Stop() // open SyncFlags streams never end on their own
	return nil
}

// loadState loads the baseline, the registry and the admin state file inside a
// flagservice.baseline.load span (telemetry.md §10.1).
func loadState(ctx context.Context, tracer trace.Tracer, cfg config, epoch string, logger *slog.Logger) (*state.State, state.LoadReport, error) {
	_, span := tracer.Start(ctx, telemetry.SpanBaselineLoad, trace.WithAttributes(attribute.String("flagservice.epoch", epoch)))
	defer span.End()
	fail := func(err error) (*state.State, state.LoadReport, error) {
		span.SetStatus(codes.Error, err.Error())
		span.SetAttributes(attribute.String("flagservice.result", telemetry.ResultError))
		return nil, state.LoadReport{}, err
	}
	base, err := baseline.Load(cfg.BaselineDir, cfg.BaselineFiles)
	if err != nil {
		return fail(err)
	}
	reg, err := registry.LoadFile(cfg.Registry, base)
	if err != nil {
		return fail(err)
	}
	st, report, err := state.New(base, reg, state.Options{
		Epoch: epoch, Served: cfg.Served, EventListLen: cfg.EventListLen, MaxGameAge: cfg.MaxGameAge, MaxGames: cfg.MaxGames, ExperimentIdle: cfg.ExperimentIdle,
		StateFile: cfg.StateFile, Logger: logger,
	})
	if err != nil {
		return fail(err)
	}
	flags := 0
	for _, id := range base.Order {
		n := len(base.Sets[id].Flags)
		flags += n
		span.AddEvent("flag_set", trace.WithAttributes(
			attribute.String("feature_flag.set.id", id),
			attribute.Int("flagservice.flags", n),
			attribute.Bool("flagservice.served", st.Served(id)),
		))
	}
	span.SetAttributes(
		attribute.Int("flagservice.flags", flags),
		attribute.Int("flagservice.admin_values", report.AdminValues),
		attribute.Int("flagservice.admin_dropped", len(report.Dropped)),
		attribute.Bool("flagservice.admin_state_corrupt", report.Corrupt),
		attribute.String("flagservice.result", telemetry.ResultOK),
	)
	return st, report, nil
}

// recordLoadReport counts a corrupt state file found at start (UC-053 A12).
func recordLoadReport(ctx context.Context, metrics *telemetry.Metrics, report state.LoadReport) {
	if report.Corrupt {
		metrics.AdminStateCorrupt.Add(ctx, 1)
	}
}

// sweep expires game scopes older than the maximum game age (UC-053 A11) and
// stack overlays whose TTL ran out.
func sweep(ctx context.Context, st *state.State, metrics *telemetry.Metrics, tracer trace.Tracer, every time.Duration, logger *slog.Logger) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		sweepOnce(ctx, st, metrics, tracer, logger)
	}
}

// sweepOnce runs one pass of the sweep: expired game scopes are dropped,
// logged and counted (UC-053 A11), then TTL overlays and idle experiment
// values (UC-053 A3) expire.
func sweepOnce(ctx context.Context, st *state.State, metrics *telemetry.Metrics, tracer trace.Tracer, logger *slog.Logger) {
	for _, end := range st.ExpireGames() {
		logger.Warn("flag service: game scope expired (its end was never reported)", "game_id", end.GameID,
			"coordinator_instance", end.CoordinatorInstance, "dropped_values", end.DroppedValues)
		metrics.ScopesExpired.Add(ctx, 1)
		telemetry.GameEndSpan(ctx, tracer, end)
	}
	if n := st.ExpireOverlays(); n > 0 {
		logger.Info("flag service: overlays expired (ttl)", "count", n)
	}
	if n := st.ExpireExperiments(); n > 0 {
		logger.Warn("flag service: experiment values expired (no game referenced them)", "count", n)
		metrics.ExperimentsExpired.Add(ctx, int64(n))
	}
}
