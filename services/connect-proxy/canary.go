package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
)

// registerCanaryHandlers adds canary rollout control endpoints to the mux.
// These mirror the chaos endpoint pattern: HTTP POST -> flag-service write ->
// flagd serves it at once over the gRPC sync stream (epic #1407).
func registerCanaryHandlers(mux *http.ServeMux) {
	mux.HandleFunc("/canary/python", canarySetHandler("python"))
	mux.HandleFunc("/canary/rust", canarySetHandler("rust"))
	mux.HandleFunc("/canary/rollback", canaryRollbackHandler)
	mux.HandleFunc("/canary/status", canaryStatusHandler)

	slog.Info("Canary handlers registered", "flag_set", controllerFlagSet)
}

// canarySetHandler returns a handler that routes controllers to a specific backend.
// Supports ?fraction=N (1-100) for gradual rollout via flagd fractional targeting.
func canarySetHandler(backend string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		fraction, err := parseFraction(r)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}

		if err := setCanaryRouting(r.Context(), backend, fraction); err != nil {
			slog.Error("canary: failed to set routing", "backend", backend, "error", err)
			http.Error(w, err.Error(), statusFor(err))
			return
		}

		slog.Info("canary: routing updated", "backend", backend, "fraction", fraction)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"status":   "ok",
			"backend":  backend,
			"fraction": fraction,
		})
	}
}

// canaryRollbackHandler resets routing to the default backend (python).
func canaryRollbackHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	if err := setCanaryRouting(r.Context(), "python", 100); err != nil {
		slog.Error("canary: failed to rollback", "error", err)
		http.Error(w, err.Error(), statusFor(err))
		return
	}

	slog.Info("canary: rolled back to python")
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"status":  "ok",
		"backend": "python",
	})
}

// canaryStatusHandler returns the flag service's view of bluetooth_backend.
func canaryStatusHandler(w http.ResponseWriter, r *http.Request) {
	flagStatus(w, r, "bluetooth_backend")
}

// canaryCounterpart is the stable backend the non-canary share stays on. The
// flag has a third "unstable" variant (agent rollout work), which a canary
// split must never route the majority share to.
var canaryCounterpart = map[string]string{"python": "rust", "rust": "python"}

// setCanaryRouting changes bluetooth_backend through the flag service.
//   - fraction 100: a stack overlay on the backend's variant; any fraction
//     overlay is cleared.
//   - fraction < 100: a stack overlay on the stable counterpart (the default)
//     plus a fraction overlay, so that share of serials resolves the backend
//     and the rest the counterpart (#1386: the rest is never an undeclared
//     variant).
func setCanaryRouting(ctx context.Context, backend string, fraction int) error {
	flagsMu.Lock()
	defer flagsMu.Unlock()
	const flag = "bluetooth_backend"

	view, err := flags.Get(ctx, flag)
	if err != nil {
		return fmt.Errorf("read %s: %w", flag, err)
	}
	variant, ok := variantFor(view, backend)
	if !ok {
		return fmt.Errorf("no variant with value %q in bluetooth_backend", backend)
	}
	if fraction >= 100 {
		if err := flags.PutVariant(ctx, flag, variant, 0); err != nil {
			return err
		}
		return flags.Clear(ctx, flag, true)
	}
	other, ok := variantFor(view, canaryCounterpart[backend])
	if !ok {
		return fmt.Errorf("fractional rollout of %q unsupported: no stable counterpart variant in bluetooth_backend", backend)
	}
	if err := flags.PutVariant(ctx, flag, other, 0); err != nil {
		return err
	}
	return flags.PutVariant(ctx, flag, variant, fraction)
}
