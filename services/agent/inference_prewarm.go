package main

import (
	"context"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/joustmania/agent/inference"
	"github.com/joustmania/agent/llm"
)

// warmupPromptText is the tiny throwaway prompt fired at startup to load the
// model. Its content is irrelevant — the result is discarded — so it is kept as
// short as possible to minimise the work the model does on this one call.
const warmupPromptText = "ping"

// prewarmEnabled reports whether the startup model pre-warm should run for a real
// backend. It reads AGENT_INFERENCE_PREWARM: any value other than an explicit
// "false"/"0"/"off"/"no" (case-insensitive) leaves it ON, so the default-on
// behavior holds when the var is unset. The toggle only ever gates the openai
// path — the stub backend never warms regardless (see prewarmInference).
func prewarmEnabled() bool {
	v := strings.ToLower(strings.TrimSpace(getEnv("AGENT_INFERENCE_PREWARM", "")))
	if v == "" {
		return true
	}
	if b, err := strconv.ParseBool(v); err == nil {
		return b
	}
	switch v {
	case "off", "no":
		return false
	default:
		return true
	}
}

// prewarmInference fires ONE best-effort throwaway Infer call in a BACKGROUND
// goroutine so the host Ollama model is resident before the first real
// decision/proposal eats the ~30-100s cold load (#1130). It is wired only when a
// REAL backend is configured (AGENT_INFERENCE_BACKEND=openai): the stub default
// passes a nil delegate, so this no-ops and the byte-identical default startup is
// preserved. The call is:
//   - non-blocking: it runs in its own goroutine, so startup is never delayed;
//   - best-effort / non-fatal: any failure is logged at Warn and NEVER crashes or
//     blocks the agent — it serves regardless of the warmup outcome;
//   - bounded: the goroutine's context carries warmupTimeout (the configured
//     inference timeout) so it can never hang forever, and it is also tied to the
//     parent ctx so shutdown cancels an in-flight warmup.
//
// The prompt mirrors the llm.Prompt shape every other Infer call uses, with a
// minimal User string. The model name is set for attribution only.
func prewarmInference(ctx context.Context, delegate inferFunc, model string, warmupTimeout time.Duration) {
	if delegate == nil {
		return // stub backend: no warmup, unchanged default behavior.
	}
	if !prewarmEnabled() {
		slog.Info("Inference pre-warm disabled via AGENT_INFERENCE_PREWARM (#1130)")
		return
	}
	if warmupTimeout <= 0 {
		warmupTimeout = inference.DefaultTimeout
	}
	slog.Info("Inference pre-warm started (best-effort, background) (#1130)",
		"model", model, "timeout", warmupTimeout)
	go func() {
		warmCtx, cancel := context.WithTimeout(ctx, warmupTimeout)
		defer cancel()
		start := time.Now()
		_, err := delegate(warmCtx, llm.Prompt{
			User:  warmupPromptText,
			Model: model,
		})
		elapsedMs := time.Since(start).Milliseconds()
		if err != nil {
			slog.Warn("Inference pre-warm failed (non-fatal; agent serves regardless) (#1130)",
				"model", model, "elapsed_ms", elapsedMs, "error", err)
			return
		}
		slog.Info("Inference pre-warm succeeded; model resident (#1130)",
			"model", model, "elapsed_ms", elapsedMs)
	}()
}
