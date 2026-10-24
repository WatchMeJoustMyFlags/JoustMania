// Connect-Go Proxy for JoustMania
//
// This service acts as a bridge between the browser (Connect protocol over HTTP/1.1)
// and the backend gRPC services. It enables real-time streaming to the web dashboard.
package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"

	"connectrpc.com/connect"
	"github.com/rs/cors"
	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"

	// Generated proto imports
	controllerpb "github.com/joustmania/connect-proxy/gen/controller_manager"
	gamepb "github.com/joustmania/connect-proxy/gen/game_coordinator"
	menupb "github.com/joustmania/connect-proxy/gen/menu"

	// Connect handlers
	"github.com/joustmania/connect-proxy/gen/controller_manager/controller_managerconnect"
	"github.com/joustmania/connect-proxy/gen/game_coordinator/game_coordinatorconnect"
	"github.com/joustmania/connect-proxy/gen/menu/menuconnect"
)

// Service addresses from environment or defaults
var (
	controllerManagerAddr = getEnv("CONTROLLER_MANAGER_SERVICE", "controller-manager:50052")
	gameCoordinatorAddr   = getEnv("GAME_COORDINATOR_SERVICE", "game-coordinator:50053")
	menuAddr              = getEnv("MENU_SERVICE", "menu:50054")
	listenAddr            = getEnv("LISTEN_ADDR", ":8080")
)

func getEnv(key, defaultValue string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return defaultValue
}

func main() {
	ctx := context.Background()

	logger, shutdownLogs := initLogs(ctx)
	defer shutdownLogs(ctx)
	slog.SetDefault(logger)

	shutdownMetrics := initMetrics(ctx)
	defer shutdownMetrics(ctx)

	shutdownTracing := initTracing(ctx)
	defer shutdownTracing(ctx)

	slog.Info("JoustMania Connect Proxy starting...")
	slog.Info("Backend services",
		"controller_manager", controllerManagerAddr,
		"game_coordinator", gameCoordinatorAddr,
		"menu", menuAddr,
	)

	// Create gRPC connections to backend services
	controllerConn, err := grpc.NewClient(controllerManagerAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithStatsHandler(otelgrpc.NewClientHandler()),
	)
	if err != nil {
		slog.Error("Failed to connect to controller manager", "error", err)
		os.Exit(1)
	}
	defer controllerConn.Close()

	gameConn, err := grpc.NewClient(gameCoordinatorAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithStatsHandler(otelgrpc.NewClientHandler()),
	)
	if err != nil {
		slog.Error("Failed to connect to game coordinator", "error", err)
		os.Exit(1)
	}
	defer gameConn.Close()

	menuConn, err := grpc.NewClient(menuAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithStatsHandler(otelgrpc.NewClientHandler()),
	)
	if err != nil {
		slog.Error("Failed to connect to menu", "error", err)
		os.Exit(1)
	}
	defer menuConn.Close()

	// Create gRPC clients
	controllerClient := controllerpb.NewControllerManagerServiceClient(controllerConn)
	gameClient := gamepb.NewGameCoordinatorServiceClient(gameConn)
	menuClient := menupb.NewMenuServiceClient(menuConn)

	// Create HTTP mux with Connect handlers
	mux := http.NewServeMux()

	// Register Connect handlers for each service
	controllerPath, controllerHandler := controller_managerconnect.NewControllerManagerServiceHandler(
		&ControllerManagerProxy{client: controllerClient},
	)
	mux.Handle(controllerPath, controllerHandler)

	gamePath, gameHandler := game_coordinatorconnect.NewGameCoordinatorServiceHandler(
		&GameCoordinatorProxy{client: gameClient},
	)
	mux.Handle(gamePath, gameHandler)

	menuPath, menuHandler := menuconnect.NewMenuServiceHandler(
		&MenuProxy{client: menuClient},
	)
	mux.Handle(menuPath, menuHandler)

	// Canary rollout control endpoints
	registerCanaryHandlers(mux)

	// Operator unpair forwarded to the pairing daemon (UC-005)
	registerPairingHandlers(mux)

	// Health check endpoint
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("OK"))
	})

	// Serve static files for dashboard (if mounted)
	staticDir := "/app/static"
	if _, err := os.Stat(staticDir); err == nil {
		slog.Info("Serving static files", "dir", staticDir)
		mux.Handle("/", http.FileServer(http.Dir(staticDir)))
	}

	// CORS middleware for browser requests
	corsHandler := cors.New(cors.Options{
		AllowedOrigins:   []string{"*"},
		AllowedMethods:   []string{"GET", "POST", "OPTIONS"},
		AllowedHeaders:   []string{"*"},
		AllowCredentials: false,
		MaxAge:           86400,
	}).Handler(mux)

	// Wrap with OTEL HTTP tracing (skip health checks to avoid noisy spans)
	tracedHandler := otelhttp.NewHandler(corsHandler, "connect-proxy",
		otelhttp.WithFilter(func(r *http.Request) bool {
			return r.URL.Path != "/health"
		}),
	)

	// Start server
	slog.Info("Connect proxy listening", "addr", listenAddr)
	if err := http.ListenAndServe(listenAddr, tracedHandler); err != nil {
		slog.Error("Server failed", "error", err)
		os.Exit(1)
	}
}

// ControllerManagerProxy implements the Connect handler by proxying to gRPC
//
// Browser-facing RPCs are unary or server-streaming only (#1543): the proxy owns
// any bidi gRPC stream to the backend (WatchGameplayData). StreamButtonEvents and
// StreamGameplayData are not forwarded; the embedded Unimplemented handler answers
// them with CodeUnimplemented.
type ControllerManagerProxy struct {
	controller_managerconnect.UnimplementedControllerManagerServiceHandler
	client controllerpb.ControllerManagerServiceClient
}

// WatchGameplayData is the browser's server-streaming view of the bidi
// StreamGameplayData (#1595): envoy reaches connect-proxy over HTTP/1.1, where
// connect-go refuses bidi streams (505). It opens StreamGameplayData to the
// controller manager and relays the updates. The upstream config is built
// here, never forwarded: only the rate (default 30 Hz, capped at 30 Hz), so the browser
// always gets the unfiltered observer stream and cannot claim controllers or
// set their colours (UC-035 BR-006). It runs in the handler goroutine only (no relay
// goroutines), and the deferred cancel closes the upstream stream on every
// return: browser gone (ctx cancelled), upstream error or end, or a failed send.
// The send side is never half-closed, so the controller manager keeps streaming.
func (p *ControllerManagerProxy) WatchGameplayData(
	ctx context.Context,
	req *connect.Request[controllerpb.GameplayStreamConfig],
	stream *connect.ServerStream[controllerpb.GameplayDataUpdate],
) error {
	upstreamCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	grpcStream, err := p.client.StreamGameplayData(upstreamCtx)
	if err != nil {
		return toConnectError(err)
	}
	config := &controllerpb.GameplayStreamControl{
		Control: &controllerpb.GameplayStreamControl_Config{Config: observerGameplayConfig(req.Msg)},
	}
	if err := grpcStream.Send(config); err != nil && err != io.EOF {
		return toConnectError(err)
	}
	// On io.EOF from Send the real status comes from Recv below.

	for {
		msg, err := grpcStream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			if ctx.Err() != nil {
				return connect.NewError(connect.CodeCanceled, ctx.Err())
			}
			return toConnectError(err)
		}
		if err := stream.Send(msg); err != nil {
			return err
		}
	}
}

// maxWatchGameplayHz caps the browser's requested rate (UC-035 BR-006).
const maxWatchGameplayHz = 30

// observerGameplayConfig keeps only the browser's rate and drops its colours
// (UC-035 BR-006). A missing, zero or negative rate means the default 30 Hz,
// as in the controller manager; the rate is capped at maxWatchGameplayHz.
func observerGameplayConfig(in *controllerpb.GameplayStreamConfig) *controllerpb.GameplayStreamConfig {
	hz := in.GetUpdateFrequencyHz()
	if hz <= 0 {
		hz = maxWatchGameplayHz
	}
	hz = min(hz, maxWatchGameplayHz)
	return &controllerpb.GameplayStreamConfig{UpdateFrequencyHz: hz}
}

func (p *ControllerManagerProxy) RenameController(
	ctx context.Context,
	req *connect.Request[controllerpb.RenameControllerRequest],
) (*connect.Response[controllerpb.RenameControllerResponse], error) {
	resp, err := p.client.RenameController(ctx, req.Msg)
	if err != nil {
		return nil, toConnectError(err)
	}
	return connect.NewResponse(resp), nil
}

// IdentifyController flashes and rumbles one controller (UC-004, FR-121).
func (p *ControllerManagerProxy) IdentifyController(
	ctx context.Context,
	req *connect.Request[controllerpb.IdentifyControllerRequest],
) (*connect.Response[controllerpb.IdentifyControllerResponse], error) {
	resp, err := p.client.IdentifyController(ctx, req.Msg)
	if err != nil {
		return nil, toConnectError(err)
	}
	return connect.NewResponse(resp), nil
}

func (p *ControllerManagerProxy) GetConnectedControllers(
	ctx context.Context,
	req *connect.Request[controllerpb.GetConnectedControllersRequest],
) (*connect.Response[controllerpb.GetConnectedControllersResponse], error) {
	resp, err := p.client.GetConnectedControllers(ctx, req.Msg)
	if err != nil {
		return nil, toConnectError(err)
	}
	return connect.NewResponse(resp), nil
}

// GameCoordinatorProxy implements the Connect handler by proxying to gRPC
type GameCoordinatorProxy struct {
	client gamepb.GameCoordinatorServiceClient
}

func (p *GameCoordinatorProxy) ForceEndGame(
	ctx context.Context,
	req *connect.Request[gamepb.ForceEndGameRequest],
) (*connect.Response[gamepb.ForceEndGameResponse], error) {
	resp, err := p.client.ForceEndGame(ctx, req.Msg)
	if err != nil {
		return nil, toConnectError(err)
	}
	return connect.NewResponse(resp), nil
}

func (p *GameCoordinatorProxy) StreamGameEvents(
	ctx context.Context,
	req *connect.Request[gamepb.StreamEventsRequest],
	stream *connect.ServerStream[gamepb.GameEvent],
) error {
	grpcStream, err := p.client.StreamGameEvents(ctx, req.Msg)
	if err != nil {
		return toConnectError(err)
	}

	for {
		msg, err := grpcStream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return toConnectError(err)
		}
		if err := stream.Send(msg); err != nil {
			return err
		}
	}
}

func (p *GameCoordinatorProxy) GetGameState(
	ctx context.Context,
	req *connect.Request[gamepb.GetGameStateRequest],
) (*connect.Response[gamepb.GetGameStateResponse], error) {
	resp, err := p.client.GetGameState(ctx, req.Msg)
	if err != nil {
		return nil, toConnectError(err)
	}
	return connect.NewResponse(resp), nil
}

func (p *GameCoordinatorProxy) ListGames(
	ctx context.Context,
	req *connect.Request[gamepb.ListGamesRequest],
) (*connect.Response[gamepb.ListGamesResponse], error) {
	resp, err := p.client.ListGames(ctx, req.Msg)
	if err != nil {
		return nil, toConnectError(err)
	}
	return connect.NewResponse(resp), nil
}

// MenuProxy implements the Connect handler by proxying to gRPC
type MenuProxy struct {
	client menupb.MenuServiceClient
}

func (p *MenuProxy) StartMenu(
	ctx context.Context,
	req *connect.Request[menupb.StartMenuRequest],
) (*connect.Response[menupb.StartMenuResponse], error) {
	resp, err := p.client.StartMenu(ctx, req.Msg)
	if err != nil {
		return nil, toConnectError(err)
	}
	return connect.NewResponse(resp), nil
}

func (p *MenuProxy) StopMenu(
	ctx context.Context,
	req *connect.Request[menupb.StopMenuRequest],
) (*connect.Response[menupb.StopMenuResponse], error) {
	resp, err := p.client.StopMenu(ctx, req.Msg)
	if err != nil {
		return nil, toConnectError(err)
	}
	return connect.NewResponse(resp), nil
}

func (p *MenuProxy) StreamMenuEvents(
	ctx context.Context,
	req *connect.Request[menupb.StreamMenuEventsRequest],
	stream *connect.ServerStream[menupb.MenuEvent],
) error {
	grpcStream, err := p.client.StreamMenuEvents(ctx, req.Msg)
	if err != nil {
		return toConnectError(err)
	}

	for {
		msg, err := grpcStream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return toConnectError(err)
		}
		if err := stream.Send(msg); err != nil {
			return err
		}
	}
}

func (p *MenuProxy) ProcessInput(
	ctx context.Context,
	req *connect.Request[menupb.ProcessInputRequest],
) (*connect.Response[menupb.ProcessInputResponse], error) {
	resp, err := p.client.ProcessInput(ctx, req.Msg)
	if err != nil {
		return nil, toConnectError(err)
	}
	return connect.NewResponse(resp), nil
}

func (p *MenuProxy) ListGameModes(
	ctx context.Context,
	req *connect.Request[menupb.ListGameModesRequest],
) (*connect.Response[menupb.ListGameModesResponse], error) {
	resp, err := p.client.ListGameModes(ctx, req.Msg)
	if err != nil {
		return nil, toConnectError(err)
	}
	return connect.NewResponse(resp), nil
}

// GetMenuStatus forwards the menu's read-only snapshot (#2050, UC-015 A5, services.md §3.1).
func (p *MenuProxy) GetMenuStatus(
	ctx context.Context,
	req *connect.Request[menupb.GetMenuStatusRequest],
) (*connect.Response[menupb.GetMenuStatusResponse], error) {
	resp, err := p.client.GetMenuStatus(ctx, req.Msg)
	if err != nil {
		return nil, toConnectError(err)
	}
	return connect.NewResponse(resp), nil
}

// toConnectError maps a backend gRPC error to the Connect error with the same
// code (the two code sets are identical, numbers and names), keeping the status
// message. Errors that carry no gRPC status stay CodeInternal (#1560).
func toConnectError(err error) *connect.Error {
	st, ok := status.FromError(err)
	if !ok || st.Code() == codes.OK {
		return connect.NewError(connect.CodeInternal, err)
	}
	return connect.NewError(connect.Code(st.Code()), errors.New(st.Message()))
}
