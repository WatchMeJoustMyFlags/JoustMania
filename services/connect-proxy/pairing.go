package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
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
// no authentication (UC-005 BR-003), like the chaos and canary endpoints.
func registerPairingHandlers(mux *http.ServeMux) {
	client := &http.Client{Timeout: 35 * time.Second}
	mux.HandleFunc("/pairing/unpair", unpairHandler(pairingDaemonURL, client))
	slog.Info("Pairing handlers registered", "pairing_daemon", pairingDaemonURL)
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
