package decision

import (
	"crypto/sha256"
	"fmt"
	"log/slog"
	"sync"
)

// prompt_fingerprint.go is the #1168 root-cause fix for the #1167 traces-pipeline
// collector OOM. The agent.llm.prompt (#739) and agent.llm.retro (#844) spans used
// to carry the FULL, UNCAPPED System prompt text on EVERY emission. That text is
// LARGE (physics model #1091, mode fragment, impact magnitudes #1102, allow-list;
// retro got physics in #1160) and effectively CONSTANT for a given prompt_variant +
// mode + interventions_allowed (the cross-game PRIOR-GAMES block #929 and operator
// note #930 do vary it — see below), so shipping it per span (4 shadow games/tick)
// is redundant bulk that drove span size, not span count, into the collector OOM.
//
// The fix replaces the full System TEXT on the span with a short FINGERPRINT
// (llm.prompt.system_sha256 / llm.retro.system_sha256) and emits the full System
// text exactly ONCE per distinct fingerprint on a dedicated LOG line, so any span's
// hash is resolvable back to the exact text without paying per span.
//
// LOG, not span: the OOM is the TRACES pipeline. A reference SPAN would add back to
// that very pipeline; a log line rides the logs pipeline (or stdout), keeping the
// traces pipeline lean — the stated goal. The reference is low-rate (once per
// distinct hash for the process lifetime), so even on a busy logs backend it is
// negligible.

// systemPromptSHALen is how many hex characters of the SHA-256 digest the span
// fingerprint carries. 16 hex chars = 64 bits of the digest — enough to make a
// collision between the handful of distinct system prompts a process ever renders
// astronomically unlikely, while staying tiny on the span.
const systemPromptSHALen = 16

// maxUserPromptBytes caps the user-prompt text emitted on a span (#1168). The user
// prompt is the VARIABLE, valuable reasoning input (per-player arcs, timeline), so
// it is KEPT — but bounded at a generous 32 KiB so a pathological game can never
// reintroduce the per-span bloat the system prompt used to cause. A capped value
// carries the userPromptTruncatedMarker so it is never silently mistaken for the
// whole prompt. 32 KiB comfortably exceeds any real per-cycle user prompt.
const maxUserPromptBytes = 32 * 1024

// userPromptTruncatedMarker is appended to a user prompt that exceeded
// maxUserPromptBytes so the truncation is explicit on the span.
const userPromptTruncatedMarker = "\n...[truncated]"

// SpanLLMSystemPromptRef is the message of the once-per-fingerprint reference LOG
// line (#1168). It is deliberately a LOG, not a span (the OOM is the traces
// pipeline — see file header). It carries {system_sha256, prompt_variant, mode,
// interventions_allowed, system} so any span's llm.prompt.system_sha256 /
// llm.retro.system_sha256 is resolvable to the exact full text. The name mirrors
// the issue's suggested agent.llm.system_prompt reference.
const SpanLLMSystemPromptRef = "agent.llm.system_prompt"

// systemPromptSHA returns the short hex fingerprint of a system prompt: the first
// systemPromptSHALen hex chars of its SHA-256 digest. It is the value stamped on
// the span in place of the full text. A given system prompt always hashes to the
// same fingerprint, so the reference log line (emitted once per distinct hash) is
// resolvable for any span carrying it.
func systemPromptSHA(system string) string {
	return fmt.Sprintf("%x", sha256.Sum256([]byte(system)))[:systemPromptSHALen]
}

// responseSHA returns the short hex fingerprint of an LLM raw RESPONSE (#1169/#1179),
// the same scheme systemPromptSHA uses for prompts: the first systemPromptSHALen hex
// chars of the SHA-256 digest. It is stamped on the span as llm.retro.response_sha256
// in place of the full raw text; the once-per-hash reference log resolves it.
func responseSHA(raw string) string {
	return fmt.Sprintf("%x", sha256.Sum256([]byte(raw)))[:systemPromptSHALen]
}

// capUserPrompt bounds the user-prompt text emitted on a span to maxUserPromptBytes,
// appending userPromptTruncatedMarker when it has to cut. The content is preserved
// up to the bound — the user prompt is the variable reasoning input we keep (#1168),
// only its worst case is capped. It cuts on a byte boundary (the user prompt is
// rendered ASCII/JSON-ish snapshot text; a stray split rune is harmless on a log/
// span string and far cheaper than rune-walking 32 KiB every cycle).
func capUserPrompt(user string) string {
	if len(user) <= maxUserPromptBytes {
		return user
	}
	return user[:maxUserPromptBytes] + userPromptTruncatedMarker
}

// systemPromptReference emits the full System-prompt text exactly ONCE per distinct
// fingerprint for the process lifetime (#1168). It is the recoverability half of the
// fingerprint scheme: the span carries only the hash, this carries the text the hash
// maps to. seen is a process-lifetime set of hashes already emitted; on first sight
// of a hash the full text is logged, subsequent sights are no-ops. When the system
// prompt changes (new variant / mode / allow-list / context block / note) its hash
// changes and the new text is re-emitted.
type systemPromptReference struct {
	// seen maps a system-prompt fingerprint -> struct{} once it has been logged.
	// sync.Map suits the write-once-then-read-mostly access pattern across the
	// concurrent per-game Loops (#845) and the retro coordinator that share it.
	seen sync.Map
}

// emit logs the full system prompt on the dedicated reference line the FIRST time it
// sees a given fingerprint, and is a no-op on every subsequent sight of the same
// fingerprint. It returns true when it actually emitted (first sight), which the
// tests use to assert the once-per-fingerprint semantics. The log carries the hash,
// the low-cardinality determinants (variant / mode / allow-list summary) and the
// full text, so a hash on any span is resolvable to its text. A nil log uses
// slog.Default so the reference is never silently dropped.
func (r *systemPromptReference) emit(log *slog.Logger, hash, variant, mode, allowed, system string) bool {
	if _, loaded := r.seen.LoadOrStore(hash, struct{}{}); loaded {
		return false // already emitted this fingerprint
	}
	if log == nil {
		log = slog.Default()
	}
	log.Info(SpanLLMSystemPromptRef,
		"system_sha256", hash,
		"prompt_variant", variant,
		"mode", mode,
		"interventions_allowed", allowed,
		"system", system,
	)
	return true
}

// systemPromptRef is the process-lifetime singleton shared by the decision-capture
// (llm_capture.go) and retro-capture (retro_capture.go) paths, so a system prompt
// rendered by either path is logged once for the whole process — not once per Loop
// (there is one Loop per concurrent game, #845) and not once per path. Tests reset
// it via resetSystemPromptRef.
var systemPromptRef = &systemPromptReference{}

// resetSystemPromptRef clears the process-lifetime seen-set. It exists for tests
// that assert the once-per-fingerprint emission semantics and must start from a
// clean slate (the singleton otherwise persists across tests in one binary).
func resetSystemPromptRef() { systemPromptRef = &systemPromptReference{} }

// retroResponseReference emits the analyst's full RAW reply text exactly ONCE per
// distinct fingerprint for the process lifetime (#1179), the response counterpart of
// systemPromptReference. The agent.llm.retro span carries only response_sha256; this
// carries the full text the hash maps to, on a dedicated LOG line (the OOM is the
// TRACES pipeline — keep the big recorded-only text off it). On first sight of a hash
// the full text is logged; subsequent sights are no-ops.
type retroResponseReference struct {
	// seen maps a response fingerprint -> struct{} once it has been logged.
	seen sync.Map
}

// emit logs the full raw response on the dedicated reference line the FIRST time it
// sees a given fingerprint, and is a no-op on every subsequent sight. It returns true
// when it actually emitted (first sight), which tests use to assert the once-per-hash
// semantics. The log carries the hash, the session id (so a reply is attributable),
// and the full raw text. A nil log uses slog.Default so the reference is never dropped.
func (r *retroResponseReference) emit(log *slog.Logger, hash, sessionID, raw string) bool {
	if _, loaded := r.seen.LoadOrStore(hash, struct{}{}); loaded {
		return false // already emitted this fingerprint
	}
	if log == nil {
		log = slog.Default()
	}
	log.Info(SpanLLMRetroResponseRef,
		"response_sha256", hash,
		"session_id", sessionID,
		"response", raw,
	)
	return true
}

// retroResponseRef is the process-lifetime singleton for the retro raw-response
// reference (#1179), shared across all concurrent game-end retros so a given raw reply
// is logged once for the whole process. Tests reset it via resetRetroResponseRef.
var retroResponseRef = &retroResponseReference{}

// resetRetroResponseRef clears the process-lifetime seen-set for tests that assert
// the once-per-fingerprint emission semantics.
func resetRetroResponseRef() { retroResponseRef = &retroResponseReference{} }
