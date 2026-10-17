package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"sync"
)

// flags is the flag service the canary and chaos endpoints write through
// (epic #1407). Tests replace it.
var (
	flags   flagService = newFlagServiceFromEnv()
	flagsMu sync.Mutex
)

// registerChaosHandlers adds chaos fault injection endpoints to the mux.
func registerChaosHandlers(mux *http.ServeMux) {
	faults := []string{"poll-drop", "accel-spike", "led-failure", "disconnect"}
	for _, fault := range faults {
		f := fault
		mux.HandleFunc("/chaos/"+f, sameOriginOnly(chaosSetHandler(f)))
	}
	mux.HandleFunc("/chaos/reset", sameOriginOnly(chaosResetHandler))
	mux.HandleFunc("/chaos/status", chaosStatusHandler)

	slog.Info("Chaos handlers registered", "flag_set", controllerFlagSet)
}

// faultRouteToVariant maps URL path segments to flagd variant names.
func faultRouteToVariant(route string) string {
	switch route {
	case "poll-drop":
		return "poll_drop"
	case "accel-spike":
		return "accel_spike"
	case "led-failure":
		return "led_failure"
	case "disconnect":
		return "disconnect"
	default:
		return "none"
	}
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

// chaosSetHandler returns a handler that activates a specific fault type.
// Supports ?fraction=N (1-100) to target only a percentage of controllers.
func chaosSetHandler(fault string) http.HandlerFunc {
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

		variant := faultRouteToVariant(fault)
		if err := setChaosVariant(r.Context(), variant, fraction); err != nil {
			slog.Error("chaos: failed to set fault", "variant", variant, "error", err)
			http.Error(w, err.Error(), statusFor(err))
			return
		}

		slog.Info("chaos: activated fault", "variant", variant, "fraction", fraction)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"status":   "ok",
			"fault":    variant,
			"fraction": fraction,
		})
	}
}

// chaosResetHandler clears all chaos faults.
func chaosResetHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	if err := setChaosVariant(r.Context(), "none", 100); err != nil {
		slog.Error("chaos: failed to reset", "error", err)
		http.Error(w, err.Error(), statusFor(err))
		return
	}

	slog.Info("chaos: reset to none")
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"status": "ok",
		"fault":  "none",
	})
}

// chaosStatusHandler returns the flag service's view of chaos_fault_type.
func chaosStatusHandler(w http.ResponseWriter, r *http.Request) {
	flagStatus(w, r, "chaos_fault_type")
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

// setChaosVariant changes chaos_fault_type through the flag service.
//   - "none": both overlays are cleared, the baseline (none) returns.
//   - fraction 100: a stack overlay; any fraction overlay is cleared.
//   - fraction < 100: a fraction overlay, so that share of serials resolves the
//     fault and the rest the default (no stack overlay: the baseline none).
func setChaosVariant(ctx context.Context, variant string, fraction int) error {
	flagsMu.Lock()
	defer flagsMu.Unlock()
	const flag = "chaos_fault_type"

	switch {
	case variant == "none":
		if err := flags.Clear(ctx, flag, true); err != nil {
			return err
		}
		return flags.Clear(ctx, flag, false)
	case fraction >= 100:
		if err := flags.PutVariant(ctx, flag, variant, 0); err != nil {
			return err
		}
		return flags.Clear(ctx, flag, true)
	default:
		if err := flags.Clear(ctx, flag, false); err != nil {
			return err
		}
		return flags.PutVariant(ctx, flag, variant, fraction)
	}
}
