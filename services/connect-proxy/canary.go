package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
)

// registerCanaryHandlers adds canary rollout control endpoints to the mux.
// These mirror the chaos endpoint pattern: HTTP POST -> modify flagd JSON -> hot reload.
func registerCanaryHandlers(mux *http.ServeMux) {
	mux.HandleFunc("/canary/python", canarySetHandler("python"))
	mux.HandleFunc("/canary/rust", canarySetHandler("rust"))
	mux.HandleFunc("/canary/rollback", canaryRollbackHandler)
	mux.HandleFunc("/canary/status", canaryStatusHandler)

	slog.Info("Canary handlers registered", "flagd_config", flagdConfigPath)
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

		if err := setCanaryRouting(backend, fraction); err != nil {
			slog.Error("canary: failed to set routing", "backend", backend, "error", err)
			http.Error(w, err.Error(), http.StatusInternalServerError)
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

	if err := setCanaryRouting("python", 100); err != nil {
		slog.Error("canary: failed to rollback", "error", err)
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	slog.Info("canary: rolled back to python")
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"status":  "ok",
		"backend": "python",
	})
}

// canaryStatusHandler returns the current controller_adapter_routing flag config.
func canaryStatusHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	flagdMu.Lock()
	defer flagdMu.Unlock()

	cfg, err := readFlagdConfig()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	flag, ok := cfg.Flags["controller_adapter_routing"]
	if !ok {
		http.Error(w, "controller_adapter_routing flag not found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(flag)
}

// setCanaryRouting updates the controller_adapter_routing flag in the flagd config.
// fraction=100 routes all controllers to the backend (defaultVariant only).
// fraction<100 uses flagd fractional targeting for gradual rollout.
func setCanaryRouting(backend string, fraction int) error {
	flagdMu.Lock()
	defer flagdMu.Unlock()

	cfg, err := readFlagdConfig()
	if err != nil {
		return fmt.Errorf("read config: %w", err)
	}

	flag, ok := cfg.Flags["controller_adapter_routing"]
	if !ok {
		return fmt.Errorf("controller_adapter_routing flag not found in %s", flagdConfigPath)
	}

	// Find the variant key for this backend value
	variantKey := ""
	for k, v := range flag.Variants {
		if str, ok := v.(string); ok && str == backend {
			variantKey = k
			break
		}
	}
	if variantKey == "" {
		return fmt.Errorf("no variant with value %q in controller_adapter_routing", backend)
	}

	// Find the "other" backend for fractional targeting
	otherKey := ""
	for k := range flag.Variants {
		if k != variantKey {
			otherKey = k
			break
		}
	}

	if fraction >= 100 {
		// All controllers: set default, clear targeting
		flag.DefaultVariant = variantKey
		flag.Targeting = nil
	} else {
		// Fractional rollout: default stays as other, targeting routes fraction% to backend
		flag.DefaultVariant = otherKey
		flag.Targeting = buildFractionalTargeting(variantKey, fraction)
	}

	return writeFlagdConfig(cfg)
}
