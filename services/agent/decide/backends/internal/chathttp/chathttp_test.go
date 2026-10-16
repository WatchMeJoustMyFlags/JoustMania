package chathttp

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestServerAddress(t *testing.T) {
	cases := map[string]struct {
		host string
		port int
	}{
		"https://api.openai.com/v1": {"api.openai.com", 443},
		"http://proxy.local/x/":     {"proxy.local", 80},
		"http://127.0.0.1:8080/v1":  {"127.0.0.1", 8080},
		"::bad":                     {"", 0},
		"":                          {"", 0},
	}
	for in, want := range cases {
		if h, p := ServerAddress(in); h != want.host || p != want.port {
			t.Errorf("ServerAddress(%q) = %s:%d, want %s:%d", in, h, p, want.host, want.port)
		}
	}
}

func TestPost(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Test") != "1" || r.Header.Get("Content-Type") != "application/json" {
			w.WriteHeader(400)
			return
		}
		if r.URL.Path == "/big" {
			_, _ = w.Write([]byte(strings.Repeat("x", 11)))
			return
		}
		if r.URL.Path == "/slow" {
			<-r.Context().Done()
			return
		}
		w.WriteHeader(201)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()
	hdr := http.Header{"X-Test": []string{"1"}}

	status, body, err := Post(context.Background(), srv.Client(), srv.URL+"/ok", hdr, []byte(`{}`), 10+1)
	if err != nil || status != 201 || string(body) != `{"ok":true}` {
		t.Errorf("ok = %d %q %v", status, body, err)
	}
	if _, _, err := Post(context.Background(), srv.Client(), srv.URL+"/big", hdr, nil, 10); !errors.Is(err, ErrTooLarge) {
		t.Errorf("big err = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	_, _, err = Post(ctx, srv.Client(), srv.URL+"/slow", hdr, nil, 10)
	if err == nil || TransportErrorType(ctx, err) != "timeout" {
		t.Errorf("slow err = %v (%s)", err, TransportErrorType(ctx, err))
	}
	canceled, cancel2 := context.WithCancel(context.Background())
	cancel2()
	_, _, err = Post(canceled, srv.Client(), srv.URL+"/ok", hdr, nil, 10)
	if TransportErrorType(canceled, err) != "canceled" {
		t.Errorf("canceled = %s", TransportErrorType(canceled, err))
	}
	if TransportErrorType(context.Background(), errors.New("x")) != "_OTHER" {
		t.Error("other")
	}
	if StatusErrorType(429) != "429" {
		t.Error("status")
	}
}

func TestMetricsNoPanic(t *testing.T) {
	m := NewMetrics("test")
	c := Call{Provider: "openai", RequestModel: "m", ResponseModel: "m2", Host: "h", Port: 443}
	m.RecordDuration(context.Background(), c, "", time.Second)
	m.RecordDuration(context.Background(), Call{Provider: "jev"}, "timeout", time.Second)
	m.RecordTokens(context.Background(), c, 10, 5)
}
