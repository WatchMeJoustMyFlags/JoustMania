package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
)

// flagservice.go is the connect-proxy's client of the flag service (epic #1407,
// UC-053 A1): the canary and chaos endpoints change the `controller` flag set
// only through its HTTP API (docs/specs/services.md §7.2). The flag files are the
// read-only baseline (C-033).

const (
	controllerFlagSet   = "controller"
	proxyWriter         = "connect-proxy"
	flagServiceDeadline = 2 * time.Second
)

// flagService is the part of the flag-service API the canary and chaos
// handlers use.
type flagService interface {
	// PutVariant sets a stack-wide overlay (fraction 0) or a fraction overlay
	// (fraction 1..99: that share of serials resolves variant).
	PutVariant(ctx context.Context, flag, variant string, fraction int) error
	// Clear removes the stack overlay (fraction false) or the fraction overlay.
	Clear(ctx context.Context, flag string, fraction bool) error
	// Get returns the flag's view (GET /controller/{flag}).
	Get(ctx context.Context, flag string) (map[string]any, error)
}

// flagServiceError is a refused or failed request; Status is the HTTP status
// (0 when the service was unreachable).
type flagServiceError struct {
	Status int
	Reason string
	Detail string
}

func (e *flagServiceError) Error() string {
	if e.Status == 0 {
		return "flag service unreachable: " + e.Detail
	}
	return fmt.Sprintf("flag service: HTTP %d %s: %s", e.Status, e.Reason, e.Detail)
}

type httpFlagService struct {
	base   string
	client *http.Client
}

func newFlagServiceFromEnv() *httpFlagService {
	return &httpFlagService{
		base:   strings.TrimRight(getEnv("FLAG_SERVICE_URL", "http://flag-service:8070"), "/"),
		client: &http.Client{Transport: otelhttp.NewTransport(http.DefaultTransport), Timeout: flagServiceDeadline},
	}
}

func (s *httpFlagService) flagURL(flag string) string {
	return s.base + "/" + controllerFlagSet + "/" + url.PathEscape(flag)
}

func (s *httpFlagService) do(ctx context.Context, method, target string, body any) (map[string]any, error) {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, target, reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.client.Do(req)
	if err != nil {
		return nil, &flagServiceError{Detail: err.Error()}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var out map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	if resp.StatusCode/100 != 2 {
		e := &flagServiceError{Status: resp.StatusCode}
		if out != nil {
			e.Reason, _ = out["error"].(string)
			e.Detail, _ = out["detail"].(string)
		}
		return nil, e
	}
	return out, nil
}

func (s *httpFlagService) PutVariant(ctx context.Context, flag, variant string, fraction int) error {
	body := map[string]any{"variant": variant, "writer": proxyWriter}
	if fraction > 0 {
		body["scope"] = map[string]any{"fraction_percent": fraction}
	}
	_, err := s.do(ctx, http.MethodPut, s.flagURL(flag), body)
	return err
}

func (s *httpFlagService) Clear(ctx context.Context, flag string, fraction bool) error {
	target := s.flagURL(flag) + "?writer=" + proxyWriter
	if fraction {
		target += "&fraction=true"
	}
	_, err := s.do(ctx, http.MethodDelete, target, nil)
	return err
}

func (s *httpFlagService) Get(ctx context.Context, flag string) (map[string]any, error) {
	return s.do(ctx, http.MethodGet, s.flagURL(flag), nil)
}

// variantFor returns the declared variant whose value is the string want.
func variantFor(view map[string]any, want string) (string, bool) {
	values, _ := view["variant_values"].(map[string]any)
	names, _ := view["variants"].([]any)
	for _, n := range names {
		name, _ := n.(string)
		if s, ok := values[name].(string); ok && s == want {
			return name, true
		}
	}
	return "", false
}

// statusFor maps a flag-service error to the proxy's HTTP status: a refusal
// keeps its 4xx, an outage is 502 Bad Gateway, anything else 500.
func statusFor(err error) int {
	var fe *flagServiceError
	if errors.As(err, &fe) {
		switch {
		case fe.Status == 0:
			return http.StatusBadGateway
		case fe.Status >= 400 && fe.Status < 500:
			return fe.Status
		case fe.Status == http.StatusServiceUnavailable:
			return http.StatusServiceUnavailable
		}
	}
	return http.StatusInternalServerError
}
