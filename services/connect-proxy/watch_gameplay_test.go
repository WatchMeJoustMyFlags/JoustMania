package main

// WatchGameplayData (#1595): the browser's server-streaming view of the bidi
// StreamGameplayData. connect-proxy opens the bidi stream to the controller
// manager with the browser's config, relays the updates and closes the
// upstream stream when the browser goes away or the stream ends.

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"

	controllerpb "github.com/joustmania/connect-proxy/gen/controller_manager"
	"github.com/joustmania/connect-proxy/gen/controller_manager/controller_managerconnect"
)

const watchGameplayPath = "/joustmania.controller_manager.ControllerManagerService/WatchGameplayData"

// watchSpy wraps the real proxy and closes returned when its
// WatchGameplayData handler has returned.
type watchSpy struct {
	*ControllerManagerProxy
	returned chan struct{}
}

func (w *watchSpy) WatchGameplayData(
	ctx context.Context,
	req *connect.Request[controllerpb.GameplayStreamConfig],
	stream *connect.ServerStream[controllerpb.GameplayDataUpdate],
) error {
	defer close(w.returned)
	return w.ControllerManagerProxy.WatchGameplayData(ctx, req, stream)
}

func spyOn(returned chan struct{}) func(*ControllerManagerProxy) controller_managerconnect.ControllerManagerServiceHandler {
	return func(p *ControllerManagerProxy) controller_managerconnect.ControllerManagerServiceHandler {
		return &watchSpy{ControllerManagerProxy: p, returned: returned}
	}
}

func waitClosed(t *testing.T, ch <-chan struct{}, what string) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(3 * time.Second):
		t.Fatalf("%s after 3 s", what)
	}
}

// waitUpstreamOpened waits until the fake controller manager has received the
// stream's first control message.
func waitUpstreamOpened(t *testing.T, f *fakeController) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		f.log.mu.Lock()
		n := len(f.log.calls)
		f.log.mu.Unlock()
		if n > 0 {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("upstream StreamGameplayData never opened")
}

func gameplayConfigControl(cfg *controllerpb.GameplayStreamConfig) *controllerpb.GameplayStreamControl {
	return &controllerpb.GameplayStreamControl{Control: &controllerpb.GameplayStreamControl_Config{Config: cfg}}
}

// UC: UC-035 MSS, BR-006
func TestProxyWatchGameplayData(t *testing.T) {
	cfg := &controllerpb.GameplayStreamConfig{UpdateFrequencyHz: 30}

	t.Run("UC-035/MSS config opens the controller manager stream and its updates are relayed", func(t *testing.T) {
		rig := newProxyRigWith(t, &fakeController{
			updates: []*controllerpb.GameplayDataUpdate{{Timestamp: 1}, {Timestamp: 2}},
		}, nil)
		stream, err := rig.controllerClient.WatchGameplayData(testCtx(t), connect.NewRequest(cfg))
		if err != nil {
			t.Fatalf("WatchGameplayData: %v", err)
		}
		defer stream.Close()
		for i, want := range rig.controller.updates {
			if !stream.Receive() {
				t.Fatalf("Receive %d: %v", i, stream.Err())
			}
			eq(t, stream.Msg(), want)
		}
		if stream.Receive() {
			t.Fatalf("unexpected extra update %v", stream.Msg())
		}
		if err := stream.Err(); err != nil {
			t.Errorf("after the backend ended: err = %v, want a clean end", err)
		}
		eq(t, rig.controller.log.only(t, "StreamGameplayData"), gameplayConfigControl(cfg))
		rig.otherBackendsUntouched(t, "controller")
	})

	t.Run("UC-035/MSS dashboard connect+json over HTTP/1.1 gets the updates and a clean end-stream frame", func(t *testing.T) {
		rig := newProxyRigWith(t, &fakeController{
			updates: []*controllerpb.GameplayDataUpdate{
				{Timestamp: 1, Controllers: []*controllerpb.GameplayData{{Serial: "AA:01"}}},
				{Timestamp: 2},
			},
		}, nil)
		// Exactly what services/dashboard/src/main.ts sends.
		resp, frames := postConnectStream(t, rig, watchGameplayPath, map[string]any{"updateFrequencyHz": 30})
		if resp.StatusCode != 200 {
			t.Fatalf("status = %d, want 200 (a server stream works over HTTP/1.1)", resp.StatusCode)
		}
		if len(frames) != 3 {
			t.Fatalf("frames = %d, want 2 updates + end-stream", len(frames))
		}
		for i, want := range rig.controller.updates {
			if frames[i].flags != 0 {
				t.Fatalf("frame %d flags = %#x, want a data frame", i, frames[i].flags)
			}
			got := &controllerpb.GameplayDataUpdate{}
			if err := protojson.Unmarshal(frames[i].payload, got); err != nil {
				t.Fatalf("frame %d payload %s: %v", i, frames[i].payload, err)
			}
			eq(t, got, want)
		}
		end := frames[2]
		if end.flags&0x02 == 0 {
			t.Fatalf("last frame flags = %#x, want end-stream", end.flags)
		}
		var endMsg map[string]any
		if err := json.Unmarshal(end.payload, &endMsg); err != nil {
			t.Fatalf("end-stream payload %s: %v", end.payload, err)
		}
		if _, hasErr := endMsg["error"]; hasErr {
			t.Errorf("end-stream frame = %s, want no error", end.payload)
		}
		eq(t, rig.controller.log.only(t, "StreamGameplayData"), gameplayConfigControl(cfg))
		rig.otherBackendsUntouched(t, "controller")
	})

	t.Run("UC-035/MSS browser disconnect closes the upstream stream and ends the handler", func(t *testing.T) {
		upstreamDone := make(chan struct{})
		returned := make(chan struct{})
		rig := newProxyRigWith(t, &fakeController{
			updates:      []*controllerpb.GameplayDataUpdate{{Timestamp: 1}},
			holdOpen:     true,
			gameplayDone: upstreamDone,
		}, spyOn(returned))
		ctx, cancel := context.WithCancel(testCtx(t))
		defer cancel()
		stream, err := rig.controllerClient.WatchGameplayData(ctx, connect.NewRequest(cfg))
		if err != nil {
			t.Fatalf("WatchGameplayData: %v", err)
		}
		defer stream.Close()
		if !stream.Receive() {
			t.Fatalf("Receive: %v", stream.Err())
		}
		cancel()
		waitClosed(t, upstreamDone, "upstream StreamGameplayData still open after the browser left")
		waitClosed(t, returned, "WatchGameplayData handler still running after the browser left")
	})

	t.Run("UC-035/MSS browser disconnect before any update closes the upstream stream", func(t *testing.T) {
		upstreamDone := make(chan struct{})
		returned := make(chan struct{})
		fake := &fakeController{holdOpen: true, gameplayDone: upstreamDone}
		rig := newProxyRigWith(t, fake, spyOn(returned))
		ctx, cancel := context.WithCancel(testCtx(t))
		defer cancel()
		// Response headers go out with the first update, so the client call
		// itself blocks until the stream ends: run it aside.
		callDone := make(chan struct{})
		go func() {
			defer close(callDone)
			if stream, err := rig.controllerClient.WatchGameplayData(ctx, connect.NewRequest(cfg)); err == nil {
				_ = stream.Close()
			}
		}()
		// The handler is blocked in the upstream Recv with nothing to relay.
		waitUpstreamOpened(t, fake)
		cancel()
		waitClosed(t, upstreamDone, "idle upstream StreamGameplayData still open after the browser left")
		waitClosed(t, returned, "WatchGameplayData handler still running after the browser left")
		waitClosed(t, callDone, "client call still blocked after cancel")
	})

	t.Run("UC-035/MSS repeated browser reconnects leave no upstream stream or handler behind", func(t *testing.T) {
		for i := 0; i < 20; i++ {
			upstreamDone := make(chan struct{})
			returned := make(chan struct{})
			rig := newProxyRigWith(t, &fakeController{
				updates:      []*controllerpb.GameplayDataUpdate{{Timestamp: int64(i)}},
				holdOpen:     true,
				gameplayDone: upstreamDone,
			}, spyOn(returned))
			ctx, cancel := context.WithCancel(testCtx(t))
			stream, err := rig.controllerClient.WatchGameplayData(ctx, connect.NewRequest(cfg))
			if err != nil {
				cancel()
				t.Fatalf("cycle %d: WatchGameplayData: %v", i, err)
			}
			if !stream.Receive() {
				cancel()
				t.Fatalf("cycle %d: Receive: %v", i, stream.Err())
			}
			cancel()
			waitClosed(t, upstreamDone, "upstream StreamGameplayData still open")
			waitClosed(t, returned, "WatchGameplayData handler still running")
			_ = stream.Close()
		}
	})

	t.Run("UC-035/BR-006 browser colours, filter and rate are never forwarded", func(t *testing.T) {
		cases := []struct {
			name   string
			sent   *controllerpb.GameplayStreamConfig
			wantHz int32
		}{
			{"colours dropped", &controllerpb.GameplayStreamConfig{
				UpdateFrequencyHz: 30,
				Colors: []*controllerpb.ControllerColorConfig{
					{Serial: "AA:01", Color: &controllerpb.RGB{R: 255}},
				},
			}, 30},
			{"rate above the cap", &controllerpb.GameplayStreamConfig{UpdateFrequencyHz: 1000}, 30},
			{"negative rate", &controllerpb.GameplayStreamConfig{UpdateFrequencyHz: -5}, 1},
			{"no rate", &controllerpb.GameplayStreamConfig{}, 1},
		}
		for _, tc := range cases {
			t.Run(tc.name, func(t *testing.T) {
				rig := newProxyRigWith(t, &fakeController{
					updates: []*controllerpb.GameplayDataUpdate{{Timestamp: 1}},
				}, nil)
				stream, err := rig.controllerClient.WatchGameplayData(testCtx(t), connect.NewRequest(tc.sent))
				if err != nil {
					t.Fatalf("WatchGameplayData: %v", err)
				}
				defer stream.Close()
				for stream.Receive() {
				}
				eq(t, rig.controller.log.only(t, "StreamGameplayData"),
					gameplayConfigControl(&controllerpb.GameplayStreamConfig{UpdateFrequencyHz: tc.wantHz}))
			})
		}
	})

	t.Run("UC-035/MSS browser disconnect over HTTP/1.1 closes the upstream stream", func(t *testing.T) {
		// Production path: envoy talks HTTP/1.1 to connect-proxy.
		for i := 0; i < 10; i++ {
			upstreamDone := make(chan struct{})
			returned := make(chan struct{})
			rig := newProxyRigWith(t, &fakeController{
				updates:      []*controllerpb.GameplayDataUpdate{{Timestamp: int64(i)}},
				holdOpen:     true,
				gameplayDone: upstreamDone,
			}, spyOn(returned))
			ctx, cancel := context.WithCancel(testCtx(t))
			client := controller_managerconnect.NewControllerManagerServiceClient(
				http1Client(t, rig), rig.url, connect.WithProtoJSON())
			stream, err := client.WatchGameplayData(ctx, connect.NewRequest(cfg))
			if err != nil {
				cancel()
				t.Fatalf("cycle %d: WatchGameplayData: %v", i, err)
			}
			if !stream.Receive() {
				cancel()
				t.Fatalf("cycle %d: Receive: %v", i, stream.Err())
			}
			cancel()
			waitClosed(t, upstreamDone, "upstream StreamGameplayData still open after an HTTP/1.1 browser left")
			waitClosed(t, returned, "WatchGameplayData handler still running after an HTTP/1.1 browser left")
			_ = stream.Close()
		}
	})

	t.Run("UC-035/MSS upstream error ends the browser stream with the error", func(t *testing.T) {
		returned := make(chan struct{})
		rig := newProxyRigWith(t, &fakeController{
			updates:        []*controllerpb.GameplayDataUpdate{{Timestamp: 1}},
			gameplayEndErr: status.Error(codes.Unavailable, "controller manager gone"),
		}, spyOn(returned))
		stream, err := rig.controllerClient.WatchGameplayData(testCtx(t), connect.NewRequest(cfg))
		if err != nil {
			t.Fatalf("WatchGameplayData: %v", err)
		}
		defer stream.Close()
		if !stream.Receive() {
			t.Fatalf("Receive: %v", stream.Err())
		}
		if stream.Receive() {
			t.Fatalf("unexpected extra update %v", stream.Msg())
		}
		var cerr *connect.Error
		if !errors.As(stream.Err(), &cerr) || cerr.Code() != connect.CodeInternal ||
			!strings.Contains(cerr.Message(), "controller manager gone") {
			t.Fatalf("err = %v, want a CodeInternal Connect error carrying the upstream message", stream.Err())
		}
		waitClosed(t, returned, "WatchGameplayData handler still running after the upstream error")
	})
}
