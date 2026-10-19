package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"google.golang.org/grpc"

	controllerpb "github.com/joustmania/connect-proxy/gen/controller_manager"
	"github.com/joustmania/connect-proxy/gen/controller_manager/controller_managerconnect"
)

// endlessController is a controller-manager backend whose bidi streams never
// end on their own: after the first control message it sends updates until
// the proxy cancels the upstream call. The proxy's backend->client goroutine
// is therefore always mid-Send when the client half-closes (#1543).
type endlessController struct {
	controllerpb.UnimplementedControllerManagerServiceServer
}

func (endlessController) StreamGameplayData(s grpc.BidiStreamingServer[controllerpb.GameplayStreamControl, controllerpb.GameplayDataUpdate]) error {
	if _, err := s.Recv(); err != nil {
		return err
	}
	for ts := int64(1); ; ts++ {
		if err := s.Send(&controllerpb.GameplayDataUpdate{Timestamp: ts}); err != nil {
			return err
		}
	}
}

func (endlessController) StreamButtonEvents(s grpc.BidiStreamingServer[controllerpb.ButtonEventStreamControl, controllerpb.ButtonEvent]) error {
	if _, err := s.Recv(); err != nil {
		return err
	}
	for ts := int64(1); ; ts++ {
		if err := s.Send(&controllerpb.ButtonEvent{Serial: "AA:01", Timestamp: ts}); err != nil {
			return err
		}
	}
}

func newEndlessControllerClient(t *testing.T) controller_managerconnect.ControllerManagerServiceClient {
	t.Helper()
	conn := dialFake(t, func(s *grpc.Server) {
		controllerpb.RegisterControllerManagerServiceServer(s, endlessController{})
	})
	mux := http.NewServeMux()
	mux.Handle(controller_managerconnect.NewControllerManagerServiceHandler(
		&ControllerManagerProxy{client: controllerpb.NewControllerManagerServiceClient(conn)}))
	ts := httptest.NewUnstartedServer(mux)
	ts.EnableHTTP2 = true
	ts.StartTLS()
	t.Cleanup(ts.Close)
	return controller_managerconnect.NewControllerManagerServiceClient(ts.Client(), ts.URL)
}

// clientClosesFirst runs one stream where the client half-closes while the
// backend is still streaming, then drains the response until the proxy ends it.
// Without the join in relayBidi the handler returns while its backend->client
// goroutine still calls Send, which `go test -race` reports.
func clientClosesFirst(
	t *testing.T,
	send func() error,
	receive func() error,
	closeRequest, closeResponse func() error,
) {
	t.Helper()
	if err := send(); err != nil {
		t.Fatalf("Send: %v", err)
	}
	if err := receive(); err != nil {
		t.Fatalf("Receive before the half-close: %v", err)
	}
	if err := closeRequest(); err != nil {
		t.Fatalf("CloseRequest: %v", err)
	}
	// The proxy ends the response once the client half-closed; drain it.
	for receive() == nil {
	}
	_ = closeResponse()
}

// Repeated so the race detector sees the handler return while a Send is in flight.
const clientClosesFirstRounds = 20

// UC: UC-035 MSS
func TestProxyStreamGameplayDataClientClosesFirst(t *testing.T) {
	t.Run("UC-035/MSS client half-close ends the relay without a send after the handler returned", func(t *testing.T) {
		client := newEndlessControllerClient(t)
		for range clientClosesFirstRounds {
			ctx, cancel := context.WithCancel(testCtx(t))
			stream := client.StreamGameplayData(ctx)
			cfg := &controllerpb.GameplayStreamControl{Control: &controllerpb.GameplayStreamControl_Config{
				Config: &controllerpb.GameplayStreamConfig{UpdateFrequencyHz: 30},
			}}
			clientClosesFirst(t,
				func() error { return stream.Send(cfg) },
				func() error { _, err := stream.Receive(); return err },
				stream.CloseRequest, stream.CloseResponse)
			cancel()
		}
	})
}

// The menu's lobby stream; not a dashboard path, so no use-case step of its own.
func TestProxyStreamButtonEventsClientClosesFirst(t *testing.T) {
	t.Run("client half-close ends the relay without a send after the handler returned", func(t *testing.T) {
		client := newEndlessControllerClient(t)
		for range clientClosesFirstRounds {
			ctx, cancel := context.WithCancel(testCtx(t))
			stream := client.StreamButtonEvents(ctx)
			cfg := &controllerpb.ButtonEventStreamControl{Control: &controllerpb.ButtonEventStreamControl_Config{
				Config: &controllerpb.ButtonEventStreamConfig{},
			}}
			clientClosesFirst(t,
				func() error { return stream.Send(cfg) },
				func() error { _, err := stream.Receive(); return err },
				stream.CloseRequest, stream.CloseResponse)
			cancel()
		}
	})
}
