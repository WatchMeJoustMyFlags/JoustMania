package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"sort"
	"strings"
)

// observability flag names in flagd performance.json
var observabilityFlags = []string{
	"trace_sampling_rate",
	"verbose_logging",
	"span_enrichment_config",
	"metrics_filter_pattern",
}

// observabilityDefaults maps each observability flag to its safe default variant.
// Used by obsResetHandler (bulk reset) and getDefaultForFlag (per-flag fallback).
var observabilityDefaults = map[string]string{
	"trace_sampling_rate":    "full",
	"verbose_logging":        "off",
	"span_enrichment_config": "off",
	"metrics_filter_pattern": "none",
}

// registerObservabilityHandlers adds /observability/* endpoints to the mux.
func registerObservabilityHandlers(mux *http.ServeMux) {
	mux.HandleFunc("/observability/status", obsStatusHandler)
	mux.HandleFunc("/observability/sampling/", obsSamplingHandler)
	mux.HandleFunc("/observability/verbose-logging/", obsVerboseLoggingHandler)
	mux.HandleFunc("/observability/enrichment/", obsEnrichmentHandler)
	mux.HandleFunc("/observability/metrics-filter/", obsMetricsFilterHandler)
	mux.HandleFunc("/observability/reset", obsResetHandler)
	mux.HandleFunc("/observability/preset/debug-controller", obsPresetDebugController)
	mux.HandleFunc("/observability/preset/quiet", obsPresetQuiet)
	mux.HandleFunc("/observability/preset/full-blast", obsPresetFullBlast)

	slog.Info("Observability handlers registered", "flagd_config", flagdConfigPath)
}

// obsStatusHandler returns the current state of all observability flags.
func obsStatusHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	flagdMu.Lock()
	defer flagdMu.Unlock()

	cfg, err := readFlagdConfig()
	if err != nil {
		slog.Error("observability: failed to read config", "error", err)
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	status := make(map[string]interface{})
	for _, name := range observabilityFlags {
		if flag, ok := cfg.Flags[name]; ok {
			status[name] = map[string]interface{}{
				"defaultVariant": flag.DefaultVariant,
				"targeting":      flag.Targeting,
			}
		}
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(status)
}

// obsSamplingHandler sets trace_sampling_rate. Path: /observability/sampling/{rate}
func obsSamplingHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	variant := strings.TrimPrefix(r.URL.Path, "/observability/sampling/")
	if variant == "" {
		http.Error(w, "variant required: off, low, half, full", http.StatusBadRequest)
		return
	}
	if err := validateVariant(variant); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	service := r.URL.Query().Get("service")
	serial := r.URL.Query().Get("serial")
	// TODO: normalize serial format (e.g. case, separator) for consistent targeting

	if service != "" {
		if err := validateContextValue(service); err != nil {
			http.Error(w, "invalid service: "+err.Error(), http.StatusBadRequest)
			return
		}
	}
	if serial != "" {
		if err := validateContextValue(serial); err != nil {
			http.Error(w, "invalid serial: "+err.Error(), http.StatusBadRequest)
			return
		}
	}

	var targeting interface{}
	if serial != "" {
		targeting = buildContextTargeting("controller_serial", serial, variant)
	} else if service != "" {
		targeting = buildContextTargeting("service_name", service, variant)
	}

	if err := setObsFlag("trace_sampling_rate", variant, targeting); err != nil {
		slog.Error("observability: failed to set sampling", "error", err)
		http.Error(w, err.Error(), obsFlagErrorStatus(err))
		return
	}

	respondOK(w, "trace_sampling_rate", variant)
}

// obsVerboseLoggingHandler toggles verbose_logging. Path: /observability/verbose-logging/{on|off}
func obsVerboseLoggingHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	variant := strings.TrimPrefix(r.URL.Path, "/observability/verbose-logging/")
	if err := validateVariant(variant); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if variant != "on" && variant != "off" {
		http.Error(w, "variant must be 'on' or 'off'", http.StatusBadRequest)
		return
	}

	service := r.URL.Query().Get("service")
	gameMode := r.URL.Query().Get("game_mode")

	if service != "" {
		if err := validateContextValue(service); err != nil {
			http.Error(w, "invalid service: "+err.Error(), http.StatusBadRequest)
			return
		}
	}
	if gameMode != "" {
		if err := validateContextValue(gameMode); err != nil {
			http.Error(w, "invalid game_mode: "+err.Error(), http.StatusBadRequest)
			return
		}
	}

	var targeting interface{}
	if gameMode != "" && service != "" {
		targeting = buildAndTargeting(
			map[string]string{"service_name": service, "game_mode": gameMode},
			variant,
		)
	} else if service != "" {
		targeting = buildContextTargeting("service_name", service, variant)
	} else if gameMode != "" {
		targeting = buildContextTargeting("game_mode", gameMode, variant)
	}

	if err := setObsFlag("verbose_logging", variant, targeting); err != nil {
		slog.Error("observability: failed to set verbose logging", "error", err)
		http.Error(w, err.Error(), obsFlagErrorStatus(err))
		return
	}

	respondOK(w, "verbose_logging", variant)
}

// obsEnrichmentHandler sets span_enrichment_config. Path: /observability/enrichment/{level}
func obsEnrichmentHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	variant := strings.TrimPrefix(r.URL.Path, "/observability/enrichment/")
	if variant == "" {
		http.Error(w, "variant required: off, basic, flags, full", http.StatusBadRequest)
		return
	}
	if err := validateVariant(variant); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	lang := r.URL.Query().Get("language")
	if lang != "" {
		if err := validateContextValue(lang); err != nil {
			http.Error(w, "invalid language: "+err.Error(), http.StatusBadRequest)
			return
		}
	}
	var targeting interface{}
	if lang != "" {
		targeting = buildContextTargeting("language", lang, variant)
	}

	if err := setObsFlag("span_enrichment_config", variant, targeting); err != nil {
		slog.Error("observability: failed to set enrichment", "error", err)
		http.Error(w, err.Error(), obsFlagErrorStatus(err))
		return
	}

	respondOK(w, "span_enrichment_config", variant)
}

// obsMetricsFilterHandler sets metrics_filter_pattern. Path: /observability/metrics-filter/{pattern}
func obsMetricsFilterHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	variant := strings.TrimPrefix(r.URL.Path, "/observability/metrics-filter/")
	if variant == "" {
		http.Error(w, "variant required: none, poll_only, process_only", http.StatusBadRequest)
		return
	}
	if err := validateVariant(variant); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	service := r.URL.Query().Get("service")
	if service != "" {
		if err := validateContextValue(service); err != nil {
			http.Error(w, "invalid service: "+err.Error(), http.StatusBadRequest)
			return
		}
	}
	var targeting interface{}
	if service != "" {
		targeting = buildContextTargeting("service_name", service, variant)
	}

	if err := setObsFlag("metrics_filter_pattern", variant, targeting); err != nil {
		slog.Error("observability: failed to set metrics filter", "error", err)
		http.Error(w, err.Error(), obsFlagErrorStatus(err))
		return
	}

	respondOK(w, "metrics_filter_pattern", variant)
}

// obsResetHandler resets all observability flags to defaults.
func obsResetHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
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

	for flagName, defaultVariant := range observabilityDefaults {
		if err := setFlagInConfig(cfg, flagName, defaultVariant, nil); err != nil {
			slog.Error("observability: reset failed for flag", "flag", flagName, "error", err)
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
	}

	if err := writeFlagdConfig(cfg); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	slog.Info("observability: reset all flags to defaults")
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "ok", "action": "reset"})
}

// ── Presets ─────────────────────────────────────────────────────────

func obsPresetDebugController(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	serial := r.URL.Query().Get("serial")
	// TODO: normalize serial format (e.g. case, separator) for consistent targeting
	if serial == "" {
		http.Error(w, "serial query param required", http.StatusBadRequest)
		return
	}
	if err := validateContextValue(serial); err != nil {
		http.Error(w, "invalid serial: "+err.Error(), http.StatusBadRequest)
		return
	}

	flagdMu.Lock()
	defer flagdMu.Unlock()

	cfg, err := readFlagdConfig()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	if err := setFlagInConfig(cfg, "trace_sampling_rate", "full",
		buildContextTargeting("controller_serial", serial, "full")); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if err := setFlagInConfig(cfg, "verbose_logging", "on",
		buildContextTargeting("controller_serial", serial, "on")); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if err := setFlagInConfig(cfg, "span_enrichment_config", "full",
		buildContextTargeting("controller_serial", serial, "full")); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	if err := writeFlagdConfig(cfg); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	slog.Info("observability: preset debug-controller", "serial", serial)
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"status": "ok", "preset": "debug-controller", "serial": serial,
	})
}

func obsPresetQuiet(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
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

	if err := setFlagInConfig(cfg, "trace_sampling_rate", "low", nil); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if err := setFlagInConfig(cfg, "verbose_logging", "off", nil); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if err := setFlagInConfig(cfg, "span_enrichment_config", "off", nil); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if err := setFlagInConfig(cfg, "metrics_filter_pattern", "poll_only", nil); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	if err := writeFlagdConfig(cfg); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	slog.Info("observability: preset quiet")
	respondOK(w, "preset", "quiet")
}

func obsPresetFullBlast(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
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

	if err := setFlagInConfig(cfg, "trace_sampling_rate", "full", nil); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if err := setFlagInConfig(cfg, "verbose_logging", "on", nil); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if err := setFlagInConfig(cfg, "span_enrichment_config", "full", nil); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if err := setFlagInConfig(cfg, "metrics_filter_pattern", "none", nil); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	if err := writeFlagdConfig(cfg); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	slog.Info("observability: preset full-blast")
	respondOK(w, "preset", "full-blast")
}

// ── Helpers ─────────────────────────────────────────────────────────

// validateVariant checks that a URL path variant is safe (max 64 chars, alphanumeric + _ -).
func validateVariant(variant string) error {
	if len(variant) > 64 {
		return fmt.Errorf("variant too long (max 64 chars)")
	}
	for _, c := range variant {
		if !((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-') {
			return fmt.Errorf("variant contains invalid character: %c", c)
		}
	}
	return nil
}

// validateContextValue checks that a query parameter value is safe (max 128 chars, alphanumeric + _ - . :).
func validateContextValue(value string) error {
	if len(value) > 128 {
		return fmt.Errorf("context value too long (max 128 chars)")
	}
	for _, c := range value {
		if !((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.' || c == ':') {
			return fmt.Errorf("context value contains invalid character: %c", c)
		}
	}
	return nil
}

// setObsFlag sets a single observability flag variant and optional targeting.
func setObsFlag(flagName, variant string, targeting interface{}) error {
	flagdMu.Lock()
	defer flagdMu.Unlock()

	cfg, err := readFlagdConfig()
	if err != nil {
		return err
	}

	if err := setFlagInConfig(cfg, flagName, variant, targeting); err != nil {
		return err
	}

	return writeFlagdConfig(cfg)
}

// setFlagInConfig modifies a flag in the config struct (caller holds flagdMu).
func setFlagInConfig(cfg *flagdConfig, flagName, variant string, targeting interface{}) error {
	flag, ok := cfg.Flags[flagName]
	if !ok {
		return &flagNotFoundError{flagName}
	}

	if _, ok := flag.Variants[variant]; !ok {
		return &variantNotFoundError{flagName, variant}
	}

	if targeting != nil {
		flag.DefaultVariant = getDefaultForFlag(flagName)
		flag.Targeting = targeting
	} else {
		flag.DefaultVariant = variant
		flag.Targeting = nil
	}

	return nil
}

// buildContextTargeting creates a JSONLogic "if" rule for context-based targeting.
func buildContextTargeting(contextVar, contextValue, variant string) interface{} {
	return map[string]interface{}{
		"if": []interface{}{
			map[string]interface{}{
				"==": []interface{}{
					map[string]interface{}{"var": contextVar},
					contextValue,
				},
			},
			variant,
			nil, // explicit else: flagd falls back to the flag's defaultVariant
		},
	}
}

// buildAndTargeting creates a JSONLogic "if" rule with AND conditions.
// Map keys are sorted to produce deterministic JSON output.
func buildAndTargeting(conditions map[string]string, variant string) interface{} {
	keys := make([]string, 0, len(conditions))
	for k := range conditions {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	andClauses := make([]interface{}, 0, len(conditions))
	for _, k := range keys {
		v := conditions[k]
		andClauses = append(andClauses, map[string]interface{}{
			"==": []interface{}{
				map[string]interface{}{"var": k},
				v,
			},
		})
	}

	return map[string]interface{}{
		"if": []interface{}{
			map[string]interface{}{"and": andClauses},
			variant,
			nil, // explicit else: flagd falls back to the flag's defaultVariant
		},
	}
}

// getDefaultForFlag returns the safe default variant for a flag.
func getDefaultForFlag(flagName string) string {
	if d, ok := observabilityDefaults[flagName]; ok {
		return d
	}
	return "off"
}

func respondOK(w http.ResponseWriter, flag, variant string) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"status":  "ok",
		"flag":    flag,
		"variant": variant,
	})
}

// obsFlagErrorStatus returns HTTP 400 for validation errors (flag/variant not found)
// and HTTP 500 for I/O or other internal errors.
func obsFlagErrorStatus(err error) int {
	var fnf *flagNotFoundError
	var vnf *variantNotFoundError
	if errors.As(err, &fnf) || errors.As(err, &vnf) {
		return http.StatusBadRequest
	}
	return http.StatusInternalServerError
}

type flagNotFoundError struct{ name string }

func (e *flagNotFoundError) Error() string { return "flag not found: " + e.name }

type variantNotFoundError struct{ flag, variant string }

func (e *variantNotFoundError) Error() string {
	return "unknown variant " + e.variant + " for flag " + e.flag
}
