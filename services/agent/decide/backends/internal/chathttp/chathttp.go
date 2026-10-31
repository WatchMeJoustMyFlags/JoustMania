// Package chathttp is the plain net/http plumbing shared by the HTTP decision
// backends of epic #1225 that do not use a vendor SDK (openai, jev): one POST
// with W3C trace-context injection and a bounded body read, the low-cardinality
// error.type of a failed call, server.address/server.port from a base URL, and
// the OTel GenAI client metrics (gen_ai.client.operation.duration,
// gen_ai.client.token.usage, semconv v1.41.0).
//
// It mirrors what the claude backend does inline (that package is SDK based and
// keeps its own copies). It reads no flags and does not import package decision.
package chathttp

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	semconv "go.opentelemetry.io/otel/semconv/v1.41.0"
	"go.opentelemetry.io/otel/semconv/v1.41.0/genaiconv"

	"github.com/joustmania/agent/internal/anthropichttp"
)

// MaxResponseBytes caps the HTTP response body a backend reads. A legitimate
// chat completion carrying one FFA answer is a few KiB; llmio.Parse caps the
// model text itself at 64 KiB.
const MaxResponseBytes = 1 << 20

// ErrTooLarge is returned by Post when the response body exceeds the limit.
var ErrTooLarge = errors.New("chathttp: response body too large")

// DefaultHTTPClient is shared by every backend without an injected client, so
// the TLS connection to the vendor is pooled across the per-decision New calls
// (that matters at a 2.5s budget). Its timeout is a backstop; the Decide ctx
// deadline is the real budget.
var DefaultHTTPClient = anthropichttp.NewHTTPClient(30 * time.Second)

// Post sends body as a JSON POST to url with the given extra headers, injects
// W3C traceparent from ctx (the caller's chat span) via the global propagator,
// as inference/claude.go does, and reads at most maxBody bytes of the
// response. It returns the status and body of any HTTP response (2xx or not);
// err is set only for transport failures and an oversized body (ErrTooLarge).
func Post(ctx context.Context, client *http.Client, url string, header http.Header, body []byte, maxBody int64) (int, []byte, error) {
	r, err := PostResponse(ctx, client, url, header, body, maxBody)
	return r.Status, r.Body, err
}

// Response is what PostResponse read: the status, the (size-capped) body and
// the response headers (for a request id the vendor sends only as a header).
type Response struct {
	Status int
	Body   []byte
	Header http.Header
}

// PostResponse is Post that also returns the response headers. Status and
// Header are set whenever an HTTP response arrived, even when err is
// ErrTooLarge or a body read error.
func PostResponse(ctx context.Context, client *http.Client, url string, header http.Header, body []byte, maxBody int64) (Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return Response{}, err
	}
	for k, vs := range header {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(req.Header))

	resp, err := client.Do(req)
	if err != nil {
		return Response{}, err
	}
	defer func() { _ = resp.Body.Close() }()
	out := Response{Status: resp.StatusCode, Header: resp.Header}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxBody+1))
	if err != nil {
		return out, err
	}
	if int64(len(raw)) > maxBody {
		return out, ErrTooLarge
	}
	out.Body = raw
	return out, nil
}

// TransportErrorType is the error.type of a call that got no HTTP response:
// "timeout", "canceled" or "_OTHER".
func TransportErrorType(ctx context.Context, err error) string {
	switch {
	case errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded):
		return "timeout"
	case errors.Is(err, context.Canceled):
		return "canceled"
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return "timeout"
	}
	return semconv.ErrorTypeOther.Value.AsString()
}

// StatusErrorType is the error.type of a non-2xx response: the status code, the
// convention for HTTP clients.
func StatusErrorType(status int) string { return strconv.Itoa(status) }

// ServerAddress splits a base URL into server.address / server.port.
func ServerAddress(base string) (string, int) {
	u, err := url.Parse(base)
	if err != nil || u.Hostname() == "" {
		return "", 0
	}
	if p := u.Port(); p != "" {
		if n, err := strconv.Atoi(p); err == nil {
			return u.Hostname(), n
		}
	}
	if u.Scheme == "http" {
		return u.Hostname(), 80
	}
	return u.Hostname(), 443
}

// OperationDurationBuckets and TokenUsageBuckets are the bucket boundaries the
// GenAI semantic conventions advise for gen_ai.client.operation.duration and
// gen_ai.client.token.usage. genaiconv does not apply them, so without these the
// SDK defaults (0, 5, 10, 25, ...) are used (#1434).
var (
	OperationDurationBuckets = []float64{0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92}
	TokenUsageBuckets        = []float64{1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864}
)

// OperationDurationBucketsOption sets OperationDurationBuckets on a histogram.
func OperationDurationBucketsOption() metric.Float64HistogramOption {
	return metric.WithExplicitBucketBoundaries(OperationDurationBuckets...)
}

// TokenUsageBucketsOption sets TokenUsageBuckets on a histogram.
func TokenUsageBucketsOption() metric.Int64HistogramOption {
	return metric.WithExplicitBucketBoundaries(TokenUsageBuckets...)
}

// Metrics records the GenAI client metrics of one backend instrumentation
// scope. Instruments are created lazily once from the global meter provider,
// which delegates to the real provider once otel.go installs it, so creation
// order does not matter. Metrics are best effort: a failed constructor yields
// a no-op instrument and never fails a decision.
type Metrics struct {
	scope      string
	once       sync.Once
	opDuration genaiconv.ClientOperationDuration
	tokenUsage genaiconv.ClientTokenUsage
}

// NewMetrics returns the metrics of instrumentation scope scope.
func NewMetrics(scope string) *Metrics { return &Metrics{scope: scope} }

func (m *Metrics) instruments() {
	m.once.Do(func() {
		meter := otel.Meter(m.scope)
		m.opDuration, _ = genaiconv.NewClientOperationDuration(meter, OperationDurationBucketsOption())
		m.tokenUsage, _ = genaiconv.NewClientTokenUsage(meter, TokenUsageBucketsOption())
	})
}

// Call identifies one model call for the metric attributes.
type Call struct {
	Provider      string
	RequestModel  string
	ResponseModel string
	Host          string
	Port          int
}

func (m *Metrics) attrs(c Call) []attribute.KeyValue {
	var attrs []attribute.KeyValue
	if c.RequestModel != "" {
		attrs = append(attrs, m.opDuration.AttrRequestModel(c.RequestModel))
	}
	if c.Host != "" {
		attrs = append(attrs, m.opDuration.AttrServerAddress(c.Host), m.opDuration.AttrServerPort(c.Port))
	}
	if c.ResponseModel != "" {
		attrs = append(attrs, m.opDuration.AttrResponseModel(c.ResponseModel))
	}
	return attrs
}

// RecordDuration records gen_ai.client.operation.duration for one call.
func (m *Metrics) RecordDuration(ctx context.Context, c Call, errType string, d time.Duration) {
	m.instruments()
	attrs := m.attrs(c)
	if errType != "" {
		attrs = append(attrs, m.opDuration.AttrErrorType(genaiconv.ErrorTypeAttr(errType)))
	}
	m.opDuration.Record(context.WithoutCancel(ctx), d.Seconds(),
		genaiconv.OperationNameChat, genaiconv.ProviderNameAttr(c.Provider), attrs...)
}

// RecordTokens records gen_ai.client.token.usage (input and output) for one
// call that returned usage.
func (m *Metrics) RecordTokens(ctx context.Context, c Call, input, output int) {
	m.instruments()
	attrs := m.attrs(c)
	ctx = context.WithoutCancel(ctx)
	provider := genaiconv.ProviderNameAttr(c.Provider)
	m.tokenUsage.Record(ctx, int64(input), genaiconv.OperationNameChat, provider, genaiconv.TokenTypeInput, attrs...)
	m.tokenUsage.Record(ctx, int64(output), genaiconv.OperationNameChat, provider, genaiconv.TokenTypeOutput, attrs...)
}
