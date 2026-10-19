package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// pairingDaemonURL is where the pairing daemon serves its control API (UC-005).
// Full stack: the daemon runs on the host network, reached through the Docker
// host gateway (extra_hosts host.docker.internal:host-gateway). Lite stack:
// http://pairing-daemon:8002 on the Compose network.
var pairingDaemonURL = getEnv("PAIRING_DAEMON_URL", "http://host.docker.internal:8002")

var bluetoothAddressRe = regexp.MustCompile(`^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$`)

// maxDaemonResponseBytes bounds the daemon's JSON answer copied back to the browser.
const maxDaemonResponseBytes = 64 << 10

// registerPairingHandlers adds the operator unpair endpoint (UC-005). There is
// no authentication (UC-005 BR-003), like the canary endpoints.
func registerPairingHandlers(mux *http.ServeMux) {
	client := &http.Client{Timeout: 35 * time.Second}
	hostnames := parseHostnames(getEnv("JOUSTMANIA_HOSTNAMES", ""))
	mux.HandleFunc("/pairing/unpair", sameOriginOnly(hostnames, unpairHandler(pairingDaemonURL, client)))
	slog.Info("Pairing handlers registered", "pairing_daemon", pairingDaemonURL, "hostnames", strings.Join(hostnames, ","))
}

// parseHostnames splits the comma-separated JOUSTMANIA_HOSTNAMES value into
// normalised host names (lower case, no trailing dot, blanks dropped).
func parseHostnames(value string) []string {
	var names []string
	for _, name := range strings.Split(value, ",") {
		if name = normaliseHostname(name); name != "" {
			names = append(names, name)
		}
	}
	return names
}

func normaliseHostname(name string) string {
	return strings.TrimSuffix(strings.ToLower(strings.TrimSpace(name)), ".")
}

// hostAllowed reports whether a request's Host names this stack: an IP
// literal, localhost, or one of the configured host names (UC-005 BR-003). A
// DNS-rebinding page reaches the Pi under the attacker's own host name, which
// is none of these.
func hostAllowed(hostHeader string, hostnames []string) bool {
	host := hostHeader
	if h, _, err := net.SplitHostPort(hostHeader); err == nil {
		host = h
	}
	host = normaliseHostname(strings.TrimSuffix(strings.TrimPrefix(host, "["), "]"))
	if host == "" {
		return false
	}
	if host == "localhost" || net.ParseIP(host) != nil {
		return true
	}
	for _, name := range hostnames {
		if host == name {
			return true
		}
	}
	return false
}

// sameOriginOnly rejects browser requests to an operator action that do not
// come from the stack's own pages (UC-005 BR-003). The CORS middleware allows
// any origin for the Connect RPCs, so without this any web page open on the
// operator's laptop could POST here:
//   - Host must be an IP literal, localhost or a configured host name
//     (DNS rebinding: the attacker's name resolves to the Pi, so Origin and
//     Host match but name the attacker's host);
//   - Sec-Fetch-Site, when sent, must be same-origin or none;
//   - Origin, when sent, must name the request's Host (CSRF).
//
// The dashboard is same-origin (served by envoy); curl and scripts send no
// Origin or Sec-Fetch-Site and pass when they use an IP, localhost or a
// configured name.
func sameOriginOnly(hostnames []string, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !hostAllowed(r.Host, hostnames) {
			slog.Warn("rejected operator request for an unknown host", "path", r.URL.Path, "host", r.Host)
			writeJSONError(w, http.StatusForbidden, "unknown host rejected (add it to JOUSTMANIA_HOSTNAMES)")
			return
		}
		if site := r.Header.Get("Sec-Fetch-Site"); site != "" && site != "same-origin" && site != "none" {
			slog.Warn("rejected cross-site operator request", "path", r.URL.Path, "sec_fetch_site", site, "host", r.Host)
			writeJSONError(w, http.StatusForbidden, "cross-site request rejected")
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" {
			parsed, err := url.Parse(origin)
			if err != nil || !strings.EqualFold(parsed.Host, r.Host) {
				slog.Warn("rejected cross-origin operator request", "path", r.URL.Path, "origin", origin, "host", r.Host)
				writeJSONError(w, http.StatusForbidden, "cross-origin request rejected")
				return
			}
		}
		next(w, r)
	}
}

type unpairRequest struct {
	Address string `json:"address"`
}

func writeJSONError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"error": message})
}

// unpairHandler forwards POST /pairing/unpair {"address": ...} to the pairing
// daemon's POST /controllers/{address}/unpair and passes its status and JSON
// answer through; 502 when the daemon cannot be reached (UC-005 A7).
func unpairHandler(daemonURL string, client *http.Client) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSONError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		var req unpairRequest
		if err := json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&req); err != nil {
			writeJSONError(w, http.StatusBadRequest, "body must be JSON {\"address\": \"<bluetooth address>\"}")
			return
		}
		address := strings.TrimSpace(req.Address)
		if !bluetoothAddressRe.MatchString(address) {
			// UC-005 A5: e.g. a mock serial; never reaches the daemon
			writeJSONError(w, http.StatusBadRequest, "not a Bluetooth address: "+address)
			return
		}

		target := strings.TrimRight(daemonURL, "/") + "/controllers/" + url.PathEscape(address) + "/unpair"
		forward, err := http.NewRequestWithContext(r.Context(), http.MethodPost, target, bytes.NewReader(nil))
		if err != nil {
			writeJSONError(w, http.StatusInternalServerError, err.Error())
			return
		}
		resp, err := client.Do(forward)
		if err != nil {
			slog.Error("pairing: daemon unreachable", "url", target, "error", err)
			writeJSONError(w, http.StatusBadGateway, "pairing daemon unreachable: "+err.Error())
			return
		}
		defer resp.Body.Close()
		body, err := io.ReadAll(io.LimitReader(resp.Body, maxDaemonResponseBytes))
		if err != nil {
			writeJSONError(w, http.StatusBadGateway, "pairing daemon answer unreadable: "+err.Error())
			return
		}

		slog.Info("pairing: unpair forwarded", "address", address, "status", resp.StatusCode)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(resp.StatusCode)
		w.Write(body)
	}
}
