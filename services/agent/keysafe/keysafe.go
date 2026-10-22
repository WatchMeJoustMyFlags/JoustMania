// Package keysafe holds the one rule for a base URL a secret (API key, token) is sent to.
package keysafe

import (
	"net"
	"net/url"
	"strings"
)

// keysafe.go holds the one rule for a base URL an API key is sent to (#1774
// AGENT_INFERENCE_BASE_URL, #1814 decision.openai.base_url / decision.jev.base_url, #1900 decision.claude.base_url,
// #1886 GITHUB_API_URL):
// https, or plain http only to a loopback host, with a host and without
// credentials, query or fragment. A URL that breaks it makes its backend
// unavailable before any request exists, so the key is sent nowhere.
// UC: UC-044 BR-003, C-016.

// Reasons BaseURL gives for a rejected URL. They are for logs only: the
// URL itself is never logged or put on a span.
const (
	ReasonMalformed       = "malformed"
	ReasonCleartextRemote = "cleartext_remote"
)

// BaseURL reports whether raw (trimmed) is a base URL an API key may be
// sent to. On false, reason is ReasonMalformed (not a plain http(s) URL with a
// host, or it carries credentials, a query or a fragment) or
// ReasonCleartextRemote (plain http to a host that is not loopback). It parses
// only (no I/O), so it is cheap enough for the per-cycle decide factories.
func BaseURL(raw string) (reason string, ok bool) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" ||
		u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return ReasonMalformed, false
	}
	if u.Scheme == "http" && !IsLoopbackHost(u.Hostname()) {
		return ReasonCleartextRemote, false
	}
	return "", true
}

// IsLoopbackHost reports whether host is localhost or a loopback IP (127.0.0.0/8, ::1).
func IsLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
