package main

import (
	"net"
	"net/url"
	"strings"
)

// keyurl.go holds the one rule for a base URL an API key is sent to (#1774
// AGENT_INFERENCE_BASE_URL, #1814 decision.openai.base_url / decision.jev.base_url):
// https, or plain http only to a loopback host, with a host and without
// credentials, query or fragment. A URL that breaks it makes its backend
// unavailable before any request exists, so the key is sent nowhere.
// UC: UC-044 BR-003, C-016.

// Reasons keySafeBaseURL gives for a rejected URL. They are for logs only: the
// URL itself is never logged or put on a span.
const (
	reasonMalformed       = "malformed"
	reasonCleartextRemote = "cleartext_remote"
)

// keySafeBaseURL reports whether raw (trimmed) is a base URL an API key may be
// sent to. On false, reason is reasonMalformed (not a plain http(s) URL with a
// host, or it carries credentials, a query or a fragment) or
// reasonCleartextRemote (plain http to a host that is not loopback). It parses
// only (no I/O), so it is cheap enough for the per-cycle decide factories.
func keySafeBaseURL(raw string) (reason string, ok bool) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" ||
		u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return reasonMalformed, false
	}
	if u.Scheme == "http" && !isLoopbackHost(u.Hostname()) {
		return reasonCleartextRemote, false
	}
	return "", true
}

// isLoopbackHost reports whether host is localhost or a loopback IP (127.0.0.0/8, ::1).
func isLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
