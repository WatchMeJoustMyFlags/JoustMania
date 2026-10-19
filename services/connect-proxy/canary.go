package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
)

// registerCanaryHandlers adds canary rollout control endpoints to the mux.
// HTTP POST -> flag-service write -> flagd serves it at once over the gRPC sync
// stream (epic #1407).
func registerCanaryHandlers(mux *http.ServeMux) {
	mux.HandleFunc("/canary/python", canarySetHandler("python"))
	mux.HandleFunc("/canary/rust", canarySetHandler("rust"))
	mux.HandleFunc("/canary/rollback", canaryRollbackHandler)
	mux.HandleFunc("/canary/status", canaryStatusHandler)

	slog.Info("Canary handlers registered", "flag_set", controllerFlagSet)
}

// parseFraction reads the ?fraction= query param (1-100, default 100).
func parseFraction(r *http.Request) (int, error) {
	q := r.URL.Query().Get("fraction")
	if q == "" {
		return 100, nil
	}
	v, err := strconv.Atoi(q)
	if err != nil || v < 1 || v > 100 {
		return 0, fmt.Errorf("fraction must be 1-100, got %q", q)
	}
	return v, nil
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

// canaryRollbackHandler clears the canary's stack and fraction overlays, so
// every serial resolves the baseline default of bluetooth_backend again (rust
// since #1371). It needs no python adapter (#1402 S3).
func canaryRollbackHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	backend, err := clearCanaryRouting(r.Context())
	if err != nil {
		slog.Error("canary: failed to rollback", "error", err)
		http.Error(w, err.Error(), statusFor(err))
		return
	}

	slog.Info("canary: rolled back to the baseline default", "backend", backend)
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"status":  "ok",
		"backend": backend,
	})
}

// clearCanaryRouting removes both bluetooth_backend overlays and returns the
// value the flag resolves to afterwards.
func clearCanaryRouting(ctx context.Context) (string, error) {
	flagsMu.Lock()
	defer flagsMu.Unlock()
	const flag = "bluetooth_backend"
	if err := flags.Clear(ctx, flag, true); err != nil {
		return "", err
	}
	if err := flags.Clear(ctx, flag, false); err != nil {
		return "", err
	}
	view, err := flags.Get(ctx, flag)
	if err != nil {
		return "", fmt.Errorf("read %s: %w", flag, err)
	}
	return effectiveValue(view), nil
}

// canaryStatusHandler returns the flag service's view of bluetooth_backend.
func canaryStatusHandler(w http.ResponseWriter, r *http.Request) {
	flagStatus(w, r, "bluetooth_backend")
}

// flagStatus serves GET /controller/{flag} from the flag service.
func flagStatus(w http.ResponseWriter, r *http.Request, flag string) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	view, err := flags.Get(r.Context(), flag)
	if err != nil {
		http.Error(w, err.Error(), statusFor(err))
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(view)
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

	if backend == "python" || (fraction < 100 && canaryCounterpart[backend] == "python") {
		if err := requirePythonAdapter(ctx); err != nil {
			return err
		}
	}

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

// pythonNotLoadedError refuses a canary that routes serials to python while
// the `backend` flag loads no python adapter: the controller manager would
// silently keep those serials on rust (#1402 S3, UC-052 A3).
type pythonNotLoadedError struct{ backend string }

func (e *pythonNotLoadedError) Error() string {
	return fmt.Sprintf("python adapter not loaded: the controller backend flag is %q; "+
		"a python canary would silently stay on rust. Opt in first (UC-002 BR-003): "+
		"backend=python_rust, the python-hid compose profile, and restart controller-manager",
		e.backend)
}

// requirePythonAdapter checks that the `backend` flag the flag service serves
// names python. The controller manager reads that flag only at startup, so
// this is the configured state, not proof of a running python adapter. An
// unreadable flag refuses the canary (fail closed).
func requirePythonAdapter(ctx context.Context) error {
	view, err := flags.Get(ctx, "backend")
	if err != nil {
		return fmt.Errorf("read backend: %w", err)
	}
	value := effectiveValue(view)
	for _, name := range strings.Split(value, ",") {
		if strings.TrimSpace(name) == "python" {
			return nil
		}
	}
	return &pythonNotLoadedError{backend: value}
}

// effectiveValue returns the string value of the view's effective default
// variant ("" when it has none).
func effectiveValue(view map[string]any) string {
	variant, _ := view["effective_default"].(string)
	values, _ := view["variant_values"].(map[string]any)
	value, _ := values[variant].(string)
	return value
}
