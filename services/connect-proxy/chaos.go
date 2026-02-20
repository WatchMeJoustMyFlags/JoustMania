package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"sync"
)

// flagdConfig represents the top-level flagd JSON file structure.
type flagdConfig struct {
	Metadata map[string]interface{} `json:"metadata"`
	Flags    map[string]*flagdFlag  `json:"flags"`
}

// flagdFlag represents a single feature flag in flagd.
type flagdFlag struct {
	State          string                 `json:"state"`
	Variants       map[string]interface{} `json:"variants"`
	DefaultVariant string                 `json:"defaultVariant"`
	Targeting      interface{}            `json:"targeting,omitempty"`
}

var (
	flagdConfigPath string
	flagdMu         sync.Mutex
)

func init() {
	flagdConfigPath = getEnv("FLAGD_CONFIG_PATH", "/etc/flagd/flags/performance.json")
}

// registerChaosHandlers adds chaos fault injection endpoints to the mux.
func registerChaosHandlers(mux *http.ServeMux) {
	faults := []string{"poll-drop", "accel-spike", "led-failure", "disconnect"}
	for _, fault := range faults {
		f := fault
		mux.HandleFunc("/chaos/"+f, chaosSetHandler(f))
	}
	mux.HandleFunc("/chaos/reset", chaosResetHandler)
	mux.HandleFunc("/chaos/status", chaosStatusHandler)

	log.Printf("Chaos handlers registered (flagd config: %s)", flagdConfigPath)
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
		if err := setChaosVariant(variant, fraction); err != nil {
			log.Printf("chaos: failed to set %s: %v", variant, err)
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}

		log.Printf("chaos: activated fault %s (fraction=%d%%)", variant, fraction)
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

	if err := setChaosVariant("none", 100); err != nil {
		log.Printf("chaos: failed to reset: %v", err)
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	log.Printf("chaos: reset to none")
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"status": "ok",
		"fault":  "none",
	})
}

// chaosStatusHandler returns the current chaos_fault_type flag config.
func chaosStatusHandler(w http.ResponseWriter, r *http.Request) {
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

	flag, ok := cfg.Flags["chaos_fault_type"]
	if !ok {
		http.Error(w, "chaos_fault_type flag not found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(flag)
}

// buildFractionalTargeting creates flagd fractional targeting rules.
// With fraction=100, returns nil (use defaultVariant for all).
// With fraction<100, returns a fractional rule that hashes targetingKey (serial)
// to give `fraction`% of controllers the fault and the rest "none".
func buildFractionalTargeting(variant string, fraction int) interface{} {
	if fraction >= 100 {
		return nil
	}
	// flagd fractional operator: deterministic bucketing by targetingKey
	// https://flagd.dev/reference/custom-operations/fractional-operation/
	return map[string]interface{}{
		"fractional": []interface{}{
			[]interface{}{variant, fraction},
			[]interface{}{"none", 100 - fraction},
		},
	}
}

// setChaosVariant updates the chaos_fault_type flag in the flagd config file.
// fraction=100 targets all controllers (defaultVariant only).
// fraction<100 uses flagd fractional targeting to affect a subset of controllers.
func setChaosVariant(variant string, fraction int) error {
	flagdMu.Lock()
	defer flagdMu.Unlock()

	cfg, err := readFlagdConfig()
	if err != nil {
		return fmt.Errorf("read config: %w", err)
	}

	flag, ok := cfg.Flags["chaos_fault_type"]
	if !ok {
		return fmt.Errorf("chaos_fault_type flag not found in %s", flagdConfigPath)
	}

	if _, ok := flag.Variants[variant]; !ok {
		return fmt.Errorf("unknown variant %q", variant)
	}

	if variant == "none" {
		// Reset: clear targeting and set default to none
		flag.DefaultVariant = "none"
		flag.Targeting = nil
	} else if fraction >= 100 {
		// All controllers: set default, clear targeting
		flag.DefaultVariant = variant
		flag.Targeting = nil
	} else {
		// Fractional: keep default as "none" (fallback), set targeting
		flag.DefaultVariant = "none"
		flag.Targeting = buildFractionalTargeting(variant, fraction)
	}

	return writeFlagdConfig(cfg)
}

// readFlagdConfig reads and parses the flagd JSON config file.
func readFlagdConfig() (*flagdConfig, error) {
	data, err := os.ReadFile(flagdConfigPath)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", flagdConfigPath, err)
	}

	var cfg flagdConfig
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse %s: %w", flagdConfigPath, err)
	}

	return &cfg, nil
}

// writeFlagdConfig writes the flagd config back to disk with consistent formatting.
func writeFlagdConfig(cfg *flagdConfig) error {
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal config: %w", err)
	}

	// Append newline for POSIX compliance
	data = append(data, '\n')

	if err := os.WriteFile(flagdConfigPath, data, 0644); err != nil {
		return fmt.Errorf("write %s: %w", flagdConfigPath, err)
	}

	return nil
}
