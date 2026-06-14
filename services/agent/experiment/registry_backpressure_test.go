package experiment

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"
)

// registry_backpressure_test.go covers the #998 fix: the game-coordinator runs
// ONE game at a time and rejects concurrent shadow starts with "Game already in
// progress". The registry must treat that as BACKPRESSURE (release the
// reservation, retry next tick, no spawn_failed WARN) and must bound its
// effective in-flight concurrency so it stops over-spawning doomed starts.

// recordingHandler is a minimal slog.Handler that records every emitted record
// (message + level) so a test can assert what was (and was NOT) logged.
type recordingHandler struct {
	mu      sync.Mutex
	records []slog.Record
}

func (h *recordingHandler) Enabled(context.Context, slog.Level) bool { return true }

func (h *recordingHandler) Handle(_ context.Context, r slog.Record) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.records = append(h.records, r)
	return nil
}

func (h *recordingHandler) WithAttrs([]slog.Attr) slog.Handler { return h }
func (h *recordingHandler) WithGroup(string) slog.Handler      { return h }

func (h *recordingHandler) messages() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	msgs := make([]string, len(h.records))
	for i, r := range h.records {
		msgs[i] = r.Message
	}
	return msgs
}

func (h *recordingHandler) has(msg string) bool {
	for _, m := range h.messages() {
		if m == msg {
			return true
		}
	}
	return false
}

func newRecordingLogger() (*slog.Logger, *recordingHandler) {
	h := &recordingHandler{}
	return slog.New(h), h
}

// EffectiveConcurrencyFromEnv honors the env override and falls back safely.
func TestEffectiveConcurrencyFromEnv(t *testing.T) {
	t.Setenv(effectiveConcurrencyEnv, "3")
	if got := EffectiveConcurrencyFromEnv(); got != 3 {
		t.Fatalf("EffectiveConcurrencyFromEnv = %d, want 3", got)
	}
	t.Setenv(effectiveConcurrencyEnv, "0")
	if got := EffectiveConcurrencyFromEnv(); got != DefaultEffectiveConcurrency {
		t.Fatalf("EffectiveConcurrencyFromEnv(0) = %d, want default %d", got, DefaultEffectiveConcurrency)
	}
	t.Setenv(effectiveConcurrencyEnv, "garbage")
	if got := EffectiveConcurrencyFromEnv(); got != DefaultEffectiveConcurrency {
		t.Fatalf("EffectiveConcurrencyFromEnv(garbage) = %d, want default", got)
	}
}

// The effective cap bounds in-flight spawns to the coordinator's real
// concurrency even when MaxShadowGames (capacity bookkeeping) is much larger, so
// the registry does not over-spawn doomed concurrent starts (#998).
func TestEffectiveCapBoundsInFlightBelowMaxShadowGames(t *testing.T) {
	spawner := &fakeSpawner{}
	// NOTE: construct via NewRegistry directly (NOT newTestRegistry) so the test
	// exercises the real effective-cap default path, not the helper's back-compat
	// shim that mirrors EffectiveConcurrency to MaxShadowGames.
	r := NewRegistry(RegistryConfig{
		Root:                 t.TempDir(),
		MaxShadowGames:       10,
		EffectiveConcurrency: 1,
		Spawner:              spawner,
		Verdict:              fakeVerdict{concludeAtN: 1000},
		Clock:                fixedClock(),
	})
	ctx := context.Background()
	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	// A single tick must spawn at most the effective cap (1), NOT MaxShadowGames.
	n := r.AllocateAndSpawn(ctx)
	if n != 1 {
		t.Fatalf("spawned %d in one tick, want 1 (effective cap)", n)
	}
	if total := r.TotalInFlight(); total != 1 {
		t.Fatalf("in-flight %d, want 1 (effective cap bounds it)", total)
	}
	// While the slot is occupied, another tick spawns nothing more.
	if again := r.AllocateAndSpawn(ctx); again != 0 {
		t.Fatalf("spawned %d while at effective cap, want 0", again)
	}
}

// EffectiveConcurrency is clamped to MaxShadowGames: a misconfigured larger value
// can never make the registry hold more in-flight than its capacity bookkeeping.
func TestEffectiveCapClampedToMaxShadowGames(t *testing.T) {
	r := NewRegistry(RegistryConfig{
		Root:                 t.TempDir(),
		MaxShadowGames:       2,
		EffectiveConcurrency: 99,
		Clock:                fixedClock(),
	})
	if got := r.EffectiveCapacity(); got != 2 {
		t.Fatalf("EffectiveCapacity = %d, want 2 (clamped to MaxShadowGames)", got)
	}
}

// The #998 core: a spawner that reports "Game already in progress"
// (ErrSpawnBackpressure) must NOT count as a failure. The registry releases the
// reservation (no leak / no over-spawn), does NOT emit experiment.spawn_failed,
// and retries on the next tick (which succeeds once the coordinator frees up).
func TestSpawnBackpressureReleasesAndRetriesWithoutFailureLog(t *testing.T) {
	spawner := &fakeSpawner{backpressNow: true}
	logger, rec := newRecordingLogger()
	r := NewRegistry(RegistryConfig{
		Root:                 t.TempDir(),
		MaxShadowGames:       4,
		EffectiveConcurrency: 1,
		Spawner:              spawner,
		Verdict:              fakeVerdict{concludeAtN: 1000},
		Clock:                fixedClock(),
		Log:                  logger,
	})
	ctx := context.Background()
	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	// Tick 1: coordinator at capacity → backpressure. Nothing spawns, no leak.
	if n := r.AllocateAndSpawn(ctx); n != 0 {
		t.Fatalf("spawned %d under backpressure, want 0", n)
	}
	if total := r.TotalInFlight(); total != 0 {
		t.Fatalf("in-flight %d after backpressure, want 0 (reservation released, no over-spawn)", total)
	}
	if spawner.attemptCount() != 1 {
		t.Fatalf("spawn attempts = %d after one backpressured tick, want 1 (not hammering the coordinator)", spawner.attemptCount())
	}

	// Crucially: NO spawn_failed WARN for benign backpressure.
	if rec.has("experiment.spawn_failed") {
		t.Fatalf("backpressure must NOT log experiment.spawn_failed; got messages %v", rec.messages())
	}
	// It IS recorded as backpressure (debug) for observability.
	if !rec.has("experiment.spawn_backpressure") {
		t.Fatalf("backpressure should be logged as experiment.spawn_backpressure; got %v", rec.messages())
	}

	// Tick 2: coordinator freed up → the retry now succeeds (no slot was leaked).
	spawner.mu.Lock()
	spawner.backpressNow = false
	spawner.mu.Unlock()
	if n := r.AllocateAndSpawn(ctx); n != 1 {
		t.Fatalf("retry spawned %d, want 1 (backpressure did not leak the slot)", n)
	}
	if total := r.TotalInFlight(); total != 1 {
		t.Fatalf("in-flight %d after successful retry, want 1", total)
	}
}

// A GENUINE spawn error (not backpressure) still WARNs as experiment.spawn_failed
// — the fix narrows the WARN to real errors, it does not silence all of them.
func TestGenuineSpawnErrorStillWarns(t *testing.T) {
	spawner := &fakeSpawner{failNow: true}
	logger, rec := newRecordingLogger()
	r := NewRegistry(RegistryConfig{
		Root:                 t.TempDir(),
		MaxShadowGames:       4,
		EffectiveConcurrency: 1,
		Spawner:              spawner,
		Verdict:              fakeVerdict{concludeAtN: 1000},
		Clock:                fixedClock(),
		Log:                  logger,
	})
	ctx := context.Background()
	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if n := r.AllocateAndSpawn(ctx); n != 0 {
		t.Fatalf("spawned %d on a failing spawner, want 0", n)
	}
	if total := r.TotalInFlight(); total != 0 {
		t.Fatalf("in-flight %d after failed spawn, want 0 (reservation released)", total)
	}
	if !rec.has("experiment.spawn_failed") {
		t.Fatalf("a genuine spawn error must still log experiment.spawn_failed; got %v", rec.messages())
	}
	if rec.has("experiment.spawn_backpressure") {
		t.Fatalf("a genuine error must NOT be logged as backpressure; got %v", rec.messages())
	}
}

// ErrSpawnBackpressure is recognizable through fmt.Errorf("%w") wrapping (the
// shape the real shadowSpawner produces), so the registry's errors.Is check fires.
func TestErrSpawnBackpressureIsWrappable(t *testing.T) {
	wrapped := errors.New("coordinator rejected start: map[error:Game already in progress]")
	err := errors.Join(ErrSpawnBackpressure, wrapped)
	if !errors.Is(err, ErrSpawnBackpressure) {
		t.Fatal("errors.Is must detect ErrSpawnBackpressure through wrapping")
	}
	if !strings.Contains(err.Error(), "backpressure") {
		t.Fatalf("ErrSpawnBackpressure message should mention backpressure, got %q", err.Error())
	}
}
