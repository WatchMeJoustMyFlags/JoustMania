package flagservice

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

// recordingServer answers every request with the next status/body of its
// script (the last entry repeats) and records when each request arrived.
type recordingServer struct {
	mu       sync.Mutex
	arrivals []time.Time
	script   []func(w http.ResponseWriter, r *http.Request)
}

func (s *recordingServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.arrivals = append(s.arrivals, time.Now())
	n := len(s.arrivals)
	step := s.script[min(n, len(s.script))-1]
	s.mu.Unlock()
	step(w, r)
}

func (s *recordingServer) calls() []time.Time {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]time.Time(nil), s.arrivals...)
}

func reply(status int, body string) func(http.ResponseWriter, *http.Request) {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}
}

const gameNotLiveBody = `{"error":"game_not_live","detail":"game g is not registered"}`

func newRecordingServer(t *testing.T, script ...func(http.ResponseWriter, *http.Request)) (*recordingServer, *Client) {
	t.Helper()
	rec := &recordingServer{script: script}
	srv := httptest.NewServer(rec)
	t.Cleanup(srv.Close)
	return rec, New(srv.URL)
}

// UC: UC-053 A6
// UC: UC-040 A27
func TestClient_GameNotLiveRetriesOnce(t *testing.T) {
	gameWrite := Write{Variant: "on", Scope: Scope{GameID: "g"}}

	t.Run("UC-053/A6 a second game_not_live is returned after exactly one retry 100 ms later", func(t *testing.T) {
		rec, c := newRecordingServer(t, reply(http.StatusConflict, gameNotLiveBody))
		_, err := c.Put(context.Background(), "interventions", "shield_seconds", gameWrite)
		if !IsReason(err, ReasonGameNotLive) {
			t.Fatalf("err = %v, want the game_not_live rejection", err)
		}
		calls := rec.calls()
		if len(calls) != 2 {
			t.Fatalf("server saw %d requests, want 2 (one retry, no third call)", len(calls))
		}
		if gap := calls[1].Sub(calls[0]); gap < GameNotLiveBackoff || gap > GameNotLiveBackoff+RequestTimeout {
			t.Fatalf("retry came %v after the first call, want %v (bounded by %v)", gap, GameNotLiveBackoff, GameNotLiveBackoff+RequestTimeout)
		}
	})

	t.Run("UC-053/A6 a registration that lands before the retry is written", func(t *testing.T) {
		rec, c := newRecordingServer(t,
			reply(http.StatusConflict, gameNotLiveBody),
			reply(http.StatusOK, `{"flag_set":"interventions","flag":"shield_seconds","variant":"on"}`))
		res, err := c.Put(context.Background(), "interventions", "shield_seconds", gameWrite)
		if err != nil || res.Variant != "on" {
			t.Fatalf("res=%+v err=%v, want the retried write accepted", res, err)
		}
		if n := len(rec.calls()); n != 2 {
			t.Fatalf("server saw %d requests, want 2", n)
		}
	})

	t.Run("UC-053/A6 a game event is retried once too", func(t *testing.T) {
		rec, c := newRecordingServer(t, reply(http.StatusConflict, gameNotLiveBody))
		_, err := c.Post(context.Background(), "interventions", "eliminate_player", Event{Scope: Scope{GameID: "g"}})
		if !IsReason(err, ReasonGameNotLive) || len(rec.calls()) != 2 {
			t.Fatalf("err=%v calls=%d, want game_not_live after 2 calls", err, len(rec.calls()))
		}
	})

	t.Run("UC-053/A6 a write without a game scope is not retried", func(t *testing.T) {
		rec, c := newRecordingServer(t, reply(http.StatusConflict, gameNotLiveBody))
		_, err := c.Put(context.Background(), "agent", "decision.backend", Write{Variant: "jev"})
		if !IsReason(err, ReasonGameNotLive) || len(rec.calls()) != 1 {
			t.Fatalf("err=%v calls=%d, want one call", err, len(rec.calls()))
		}
	})
}

// UC: UC-053 A7
func TestClient_NoSyncStreamIsNotRetried(t *testing.T) {
	t.Run("UC-053/A7 503 no_sync_stream fails the write after one call", func(t *testing.T) {
		rec, c := newRecordingServer(t, reply(http.StatusServiceUnavailable, `{"error":"no_sync_stream"}`))
		_, err := c.Put(context.Background(), "interventions", "shield_seconds", Write{Variant: "on", Scope: Scope{GameID: "g"}})
		if !IsReason(err, ReasonNoSyncStream) || len(rec.calls()) != 1 {
			t.Fatalf("err=%v calls=%d, want no_sync_stream after one call", err, len(rec.calls()))
		}
	})
}

// UC: UC-053 A8
func TestClient_UnreachableFailsClosed(t *testing.T) {
	t.Run("UC-053/A8 a request that outlasts 500 ms fails at the deadline and is not retried", func(t *testing.T) {
		release := make(chan struct{})
		hang := func(_ http.ResponseWriter, r *http.Request) {
			select {
			case <-r.Context().Done():
			case <-release:
			}
		}
		rec, c := newRecordingServer(t, hang)
		t.Cleanup(func() { close(release) }) // runs before srv.Close (LIFO)

		start := time.Now()
		_, err := c.Put(context.Background(), "interventions", "shield_seconds", Write{Variant: "on", Scope: Scope{GameID: "g"}})
		elapsed := time.Since(start)
		if err == nil {
			t.Fatal("a request past the deadline must fail (fail closed)")
		}
		if elapsed < RequestTimeout || elapsed > 4*RequestTimeout {
			t.Fatalf("Put returned after %v, want the %v deadline", elapsed, RequestTimeout)
		}
		if n := len(rec.calls()); n != 1 {
			t.Fatalf("server saw %d requests, want 1 (no retry of the same decision)", n)
		}
	})

	t.Run("UC-053/A8 a refused connection fails the write", func(t *testing.T) {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		addr := ln.Addr().String()
		_ = ln.Close()
		_, err = New("http://"+addr).Put(context.Background(), "interventions", "shield_seconds", Write{Variant: "on", Scope: Scope{GameID: "g"}})
		if err == nil {
			t.Fatal("an unreachable flag service must fail the write")
		}
		if IsReason(err, ReasonGameNotLive) {
			t.Fatalf("err %v is not a service rejection", err)
		}
	})
}

// UC: UC-043 A19
func TestClient_PutIfWriterIn(t *testing.T) {
	for name, tc := range map[string]struct {
		w         Write
		wantQuery map[string][]string
	}{
		"UC-043/A19 IfWriterIn is sent as one comma-joined if_writer_in, the empty entry kept": {
			w:         Write{Value: 3.0, Scope: Scope{GameKind: "real"}, IfWriterIn: []string{"promotion", ""}},
			wantQuery: map[string][]string{"if_writer_in": {"promotion,"}},
		},
		"UC-043/A19 without IfWriterIn no precondition is sent": {
			w:         Write{Value: 3.0, Scope: Scope{GameKind: "real"}},
			wantQuery: map[string][]string{},
		},
		"UC-043/A19 dry run and precondition together": {
			w:         Write{Value: 3.0, Scope: Scope{GameKind: "real"}, DryRun: true, IfWriterIn: []string{"promotion"}},
			wantQuery: map[string][]string{"dry_run": {"true"}, "if_writer_in": {"promotion"}},
		},
	} {
		t.Run(name, func(t *testing.T) {
			var got map[string][]string
			_, c := newRecordingServer(t, func(w http.ResponseWriter, r *http.Request) {
				got = r.URL.Query()
				reply(http.StatusOK, `{}`)(w, r)
			})
			if _, err := c.Put(context.Background(), "game", "f", tc.w); err != nil {
				t.Fatal(err)
			}
			if len(got) != len(tc.wantQuery) {
				t.Fatalf("query = %v, want %v", got, tc.wantQuery)
			}
			for k, v := range tc.wantQuery {
				if len(got[k]) != len(v) || got[k][0] != v[0] {
					t.Fatalf("query %s = %v, want %v", k, got[k], v)
				}
			}
		})
	}
}
