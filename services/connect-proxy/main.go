// Connect-Go Proxy for JoustMania
//
// This service acts as a bridge between the browser (Connect protocol over HTTP/1.1)
// and the backend gRPC services. It enables real-time streaming to the web dashboard.
package main

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"os"

	"connectrpc.com/connect"
	"github.com/rs/cors"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

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
	controllerConn, err := grpc.NewClient(controllerManagerAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		slog.Error("Failed to connect to controller manager", "error", err)
		os.Exit(1)
	}
	defer controllerConn.Close()

	gameConn, err := grpc.NewClient(gameCoordinatorAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		slog.Error("Failed to connect to game coordinator", "error", err)
		os.Exit(1)
	}
	defer gameConn.Close()

	menuConn, err := grpc.NewClient(menuAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
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

	// Chaos fault injection endpoints (for presentation demos)
	registerChaosHandlers(mux)

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

	// Wrap with OTEL HTTP tracing
	tracedHandler := otelhttp.NewHandler(corsHandler, "connect-proxy")

	// Start server
	slog.Info("Connect proxy listening", "addr", listenAddr)
	if err := http.ListenAndServe(listenAddr, tracedHandler); err != nil {
		slog.Error("Server failed", "error", err)
		os.Exit(1)
	}
}

// ControllerManagerProxy implements the Connect handler by proxying to gRPC
type ControllerManagerProxy struct {
	client controllerpb.ControllerManagerServiceClient
}

func (p *ControllerManagerProxy) StreamButtonEvents(
	ctx context.Context,
	stream *connect.BidiStream[controllerpb.ButtonEventStreamControl, controllerpb.ButtonEvent],
) error {
	// For bidirectional streaming, we need to handle both directions
	grpcStream, err := p.client.StreamButtonEvents(ctx)
	if err != nil {
		return connect.NewError(connect.CodeInternal, err)
	}

	// Forward messages in both directions
	errCh := make(chan error, 2)

	// Client -> gRPC
	go func() {
		for {
			msg, err := stream.Receive()
			if err == io.EOF {
				grpcStream.CloseSend()
				errCh <- nil
				return
			}
			if err != nil {
				errCh <- err
				return
			}
			if err := grpcStream.Send(msg); err != nil {
				errCh <- err
				return
			}
		}
	}()

	// gRPC -> Client
	go func() {
		for {
			msg, err := grpcStream.Recv()
			if err == io.EOF {
				errCh <- nil
				return
			}
			if err != nil {
				errCh <- err
				return
			}
			if err := stream.Send(msg); err != nil {
				errCh <- err
				return
			}
		}
	}()

	// Wait for either direction to finish
	return <-errCh
}

func (p *ControllerManagerProxy) StreamGameplayData(
	ctx context.Context,
	stream *connect.BidiStream[controllerpb.GameplayStreamControl, controllerpb.GameplayDataUpdate],
) error {
	grpcStream, err := p.client.StreamGameplayData(ctx)
	if err != nil {
		return connect.NewError(connect.CodeInternal, err)
	}

	errCh := make(chan error, 2)

	// Client -> gRPC
	go func() {
		for {
			msg, err := stream.Receive()
			if err == io.EOF {
				grpcStream.CloseSend()
				errCh <- nil
				return
			}
			if err != nil {
				errCh <- err
				return
			}
			if err := grpcStream.Send(msg); err != nil {
				errCh <- err
				return
			}
		}
	}()

	// gRPC -> Client
	go func() {
		for {
			msg, err := grpcStream.Recv()
			if err == io.EOF {
				errCh <- nil
				return
			}
			if err != nil {
				errCh <- err
				return
			}
			if err := stream.Send(msg); err != nil {
				errCh <- err
				return
			}
		}
	}()

	return <-errCh
}

func (p *ControllerManagerProxy) RenameController(
	ctx context.Context,
	req *connect.Request[controllerpb.RenameControllerRequest],
) (*connect.Response[controllerpb.RenameControllerResponse], error) {
	resp, err := p.client.RenameController(ctx, req.Msg)
	if err != nil {
		return nil, connect.NewError(connect.CodeInternal, err)
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
		return nil, connect.NewError(connect.CodeInternal, err)
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
		return connect.NewError(connect.CodeInternal, err)
	}

	for {
		msg, err := grpcStream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return connect.NewError(connect.CodeInternal, err)
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
		return nil, connect.NewError(connect.CodeInternal, err)
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
		return nil, connect.NewError(connect.CodeInternal, err)
	}
	return connect.NewResponse(resp), nil
}

func (p *MenuProxy) StopMenu(
	ctx context.Context,
	req *connect.Request[menupb.StopMenuRequest],
) (*connect.Response[menupb.StopMenuResponse], error) {
	resp, err := p.client.StopMenu(ctx, req.Msg)
	if err != nil {
		return nil, connect.NewError(connect.CodeInternal, err)
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
		return connect.NewError(connect.CodeInternal, err)
	}

	for {
		msg, err := grpcStream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return connect.NewError(connect.CodeInternal, err)
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
		return nil, connect.NewError(connect.CodeInternal, err)
	}
	return connect.NewResponse(resp), nil
}

