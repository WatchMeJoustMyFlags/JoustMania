package main

// Forwarding tests for the Connect -> gRPC proxies (#1525): the dashboard's
// Connect calls go through a real Connect handler (httptest) to in-process
// fake gRPC backends (bufconn). Each test proves the request reaches the right
// backend with its payload, the backend's answer comes back intact, and a
// backend gRPC error surfaces as a Connect error.

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	controllerpb "github.com/joustmania/connect-proxy/gen/controller_manager"
	"github.com/joustmania/connect-proxy/gen/controller_manager/controller_managerconnect"
	gamepb "github.com/joustmania/connect-proxy/gen/game_coordinator"
	"github.com/joustmania/connect-proxy/gen/game_coordinator/game_coordinatorconnect"
	menupb "github.com/joustmania/connect-proxy/gen/menu"
	"github.com/joustmania/connect-proxy/gen/menu/menuconnect"
)

// callLog records which backend RPC was hit and with which request.
type callLog struct {
	mu    sync.Mutex
	calls []string
	reqs  []proto.Message
}

func (l *callLog) record(method string, req proto.Message) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.calls = append(l.calls, method)
	l.reqs = append(l.reqs, proto.Clone(req))
}

func (l *callLog) only(t *testing.T, method string) proto.Message {
	t.Helper()
	l.mu.Lock()
	defer l.mu.Unlock()
	if len(l.calls) != 1 || l.calls[0] != method {
		t.Fatalf("backend calls = %v, want exactly [%s]", l.calls, method)
	}
	return l.reqs[0]
}

func (l *callLog) none(t *testing.T) {
	t.Helper()
	l.mu.Lock()
	defer l.mu.Unlock()
	if len(l.calls) != 0 {
		t.Fatalf("backend calls = %v, want none (wrong backend reached)", l.calls)
	}
}

// --- fake backends ---------------------------------------------------------

type fakeMenu struct {
	menupb.UnimplementedMenuServiceServer
	log    callLog
	err    error // returned by every unary RPC when set
	events []*menupb.MenuEvent
	endErr error // returned after the stream events when set
}

func (f *fakeMenu) StartMenu(_ context.Context, r *menupb.StartMenuRequest) (*menupb.StartMenuResponse, error) {
	f.log.record("StartMenu", r)
	if f.err != nil {
		return nil, f.err
	}
	return &menupb.StartMenuResponse{Success: true}, nil
}

func (f *fakeMenu) StopMenu(_ context.Context, r *menupb.StopMenuRequest) (*menupb.StopMenuResponse, error) {
	f.log.record("StopMenu", r)
	if f.err != nil {
		return nil, f.err
	}
	return &menupb.StopMenuResponse{Success: true}, nil
}

func (f *fakeMenu) ProcessInput(_ context.Context, r *menupb.ProcessInputRequest) (*menupb.ProcessInputResponse, error) {
	f.log.record("ProcessInput", r)
	if f.err != nil {
		return nil, f.err
	}
	if r.GetData()["mode"] == "NoSuchMode" {
		return &menupb.ProcessInputResponse{Success: false, Error: "Unknown game mode: NoSuchMode"}, nil
	}
	return &menupb.ProcessInputResponse{Success: true}, nil
}

func (f *fakeMenu) ListGameModes(_ context.Context, r *menupb.ListGameModesRequest) (*menupb.ListGameModesResponse, error) {
	f.log.record("ListGameModes", r)
	if f.err != nil {
		return nil, f.err
	}
	return &menupb.ListGameModesResponse{GameModes: []string{"JoustFFA", "JoustTeams"}, DefaultMode: "JoustFFA"}, nil
}

// menuStatus is the snapshot fakeMenu answers GetMenuStatus with: every field set.
var menuStatus = &menupb.GetMenuStatusResponse{
	MenuState:           menupb.MenuState_RUNNING,
	LifecycleLive:       true,
	LifecycleGeneration: 7,
	Selection:           "Werewolf",
	ReadyCount:          2,
	ConnectedCount:      3,
	AdminSerial:         "adm",
	AgentEnabled:        true,
	AgentLevel:          "standard",
	AgentSavedLevel:     "full",
	AgentBackend:        "claude",
	Sensitivity:         3,
	NumTeams:            4,
	Instructions:        true,
	ForceAllStart:       true,
	AudioVolumeMaster:   "quiet",
	AgentEnabledState:   "on",
}

func (f *fakeMenu) GetMenuStatus(_ context.Context, r *menupb.GetMenuStatusRequest) (*menupb.GetMenuStatusResponse, error) {
	f.log.record("GetMenuStatus", r)
	if f.err != nil {
		return nil, f.err
	}
	return menuStatus, nil
}

func (f *fakeMenu) StreamMenuEvents(r *menupb.StreamMenuEventsRequest, s grpc.ServerStreamingServer[menupb.MenuEvent]) error {
	f.log.record("StreamMenuEvents", r)
	for _, ev := range f.events {
		if err := s.Send(ev); err != nil {
			return err
		}
	}
	return f.endErr
}

type fakeGame struct {
	gamepb.UnimplementedGameCoordinatorServiceServer
	log    callLog
	err    error
	events []*gamepb.GameEvent
	endErr error
}

func (f *fakeGame) ForceEndGame(_ context.Context, r *gamepb.ForceEndGameRequest) (*gamepb.ForceEndGameResponse, error) {
	f.log.record("ForceEndGame", r)
	if f.err != nil {
		return nil, f.err
	}
	if r.GameId != nil && r.GetGameId() != "game_live" {
		return &gamepb.ForceEndGameResponse{Success: false, Error: "Unknown game_id: " + r.GetGameId()}, nil
	}
	return &gamepb.ForceEndGameResponse{Success: true}, nil
}

func (f *fakeGame) GetGameState(_ context.Context, r *gamepb.GetGameStateRequest) (*gamepb.GetGameStateResponse, error) {
	f.log.record("GetGameState", r)
	if f.err != nil {
		return nil, f.err
	}
	return &gamepb.GetGameStateResponse{Success: true, GameInfo: &gamepb.GameInfo{GameId: r.GetGameId(), GameMode: "JoustFFA"}}, nil
}

func (f *fakeGame) ListGames(_ context.Context, r *gamepb.ListGamesRequest) (*gamepb.ListGamesResponse, error) {
	f.log.record("ListGames", r)
	if f.err != nil {
		return nil, f.err
	}
	return &gamepb.ListGamesResponse{Success: true, Games: []*gamepb.GameInfo{{GameId: "game_live"}, {GameId: "game_shadow"}}}, nil
}

func (f *fakeGame) StreamGameEvents(r *gamepb.StreamEventsRequest, s grpc.ServerStreamingServer[gamepb.GameEvent]) error {
	f.log.record("StreamGameEvents", r)
	for _, ev := range f.events {
		if err := s.Send(ev); err != nil {
			return err
		}
	}
	return f.endErr
}

type fakeController struct {
	controllerpb.UnimplementedControllerManagerServiceServer
	log     callLog
	err     error
	updates []*controllerpb.GameplayDataUpdate

	// Gameplay stream knobs for WatchGameplayData (#1595): holdOpen keeps the
	// stream open after the updates until the caller goes away, gameplayEndErr
	// ends it with that status, and gameplayDone (when set) is closed once the
	// backend's stream handler has returned, i.e. the upstream stream is closed.
	holdOpen       bool
	gameplayEndErr error
	gameplayDone   chan struct{}
	// recordControls keeps receiving after the updates and records every
	// further control message (a filter or colours) until the stream ends.
	recordControls bool
}

// StreamGameplayData records the first control message, sends the canned
// updates and then ends the stream from the backend side.
func (f *fakeController) StreamGameplayData(s grpc.BidiStreamingServer[controllerpb.GameplayStreamControl, controllerpb.GameplayDataUpdate]) error {
	if f.gameplayDone != nil {
		defer close(f.gameplayDone)
	}
	first, err := s.Recv()
	if err != nil {
		return err
	}
	f.log.record("StreamGameplayData", first)
	for _, u := range f.updates {
		if err := s.Send(u); err != nil {
			return err
		}
	}
	if f.recordControls {
		for {
			next, err := s.Recv()
			if err != nil {
				return nil
			}
			f.log.record("StreamGameplayData.control", next)
		}
	}
	if f.holdOpen {
		<-s.Context().Done()
		return s.Context().Err()
	}
	return f.gameplayEndErr
}

func (f *fakeController) RenameController(_ context.Context, r *controllerpb.RenameControllerRequest) (*controllerpb.RenameControllerResponse, error) {
	f.log.record("RenameController", r)
	if f.err != nil {
		return nil, f.err
	}
	return &controllerpb.RenameControllerResponse{Success: true}, nil
}

func (f *fakeController) IdentifyController(_ context.Context, r *controllerpb.IdentifyControllerRequest) (*controllerpb.IdentifyControllerResponse, error) {
	f.log.record("IdentifyController", r)
	if f.err != nil {
		return nil, f.err
	}
	if r.GetSerial() == "gone" {
		return &controllerpb.IdentifyControllerResponse{Success: false, Outcome: "not_connected", Error: "Controller not connected"}, nil
	}
	return &controllerpb.IdentifyControllerResponse{Success: true, Outcome: "started", DurationMs: r.GetDurationMs()}, nil
}

func (f *fakeController) GetConnectedControllers(_ context.Context, r *controllerpb.GetConnectedControllersRequest) (*controllerpb.GetConnectedControllersResponse, error) {
	f.log.record("GetConnectedControllers", r)
	if f.err != nil {
		return nil, f.err
	}
	return &controllerpb.GetConnectedControllersResponse{
		ConnectedSerials: []string{"aa:bb"},
		Controllers:      []*controllerpb.ConnectedController{{Serial: "aa:bb", Name: "Red", Battery: 4}},
	}, nil
}

// --- harness ---------------------------------------------------------------

// proxyRig wires the three proxies exactly as main() does, in front of
// in-process fake backends, and exposes generated Connect clients.
type proxyRig struct {
	menu       *fakeMenu
	game       *fakeGame
	controller *fakeController

	menuClient       menuconnect.MenuServiceClient
	gameClient       game_coordinatorconnect.GameCoordinatorServiceClient
	controllerClient controller_managerconnect.ControllerManagerServiceClient

	url        string
	httpClient *http.Client
}

// dialFake starts a gRPC server on a bufconn listener and returns a client
// connection to it. Everything is torn down at test cleanup.
func dialFake(t *testing.T, register func(*grpc.Server)) *grpc.ClientConn {
	t.Helper()
	lis := bufconn.Listen(1 << 20)
	srv := grpc.NewServer()
	register(srv)
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)

	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return lis.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("dial fake backend: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

func newProxyRig(t *testing.T) *proxyRig {
	t.Helper()
	return newProxyRigWith(t, &fakeController{}, nil)
}

// newProxyRigWith is newProxyRig with a given controller backend and an
// optional wrapper around the controller proxy (to observe its handlers).
func newProxyRigWith(
	t *testing.T,
	controller *fakeController,
	wrap func(*ControllerManagerProxy) controller_managerconnect.ControllerManagerServiceHandler,
) *proxyRig {
	t.Helper()
	rig := &proxyRig{menu: &fakeMenu{}, game: &fakeGame{}, controller: controller}

	menuConn := dialFake(t, func(s *grpc.Server) { menupb.RegisterMenuServiceServer(s, rig.menu) })
	gameConn := dialFake(t, func(s *grpc.Server) { gamepb.RegisterGameCoordinatorServiceServer(s, rig.game) })
	ctrlConn := dialFake(t, func(s *grpc.Server) { controllerpb.RegisterControllerManagerServiceServer(s, rig.controller) })

	mux := http.NewServeMux()
	ctrlProxy := &ControllerManagerProxy{client: controllerpb.NewControllerManagerServiceClient(ctrlConn)}
	var ctrlHandler controller_managerconnect.ControllerManagerServiceHandler = ctrlProxy
	if wrap != nil {
		ctrlHandler = wrap(ctrlProxy)
	}
	mux.Handle(controller_managerconnect.NewControllerManagerServiceHandler(ctrlHandler))
	mux.Handle(game_coordinatorconnect.NewGameCoordinatorServiceHandler(
		&GameCoordinatorProxy{client: gamepb.NewGameCoordinatorServiceClient(gameConn)}))
	mux.Handle(menuconnect.NewMenuServiceHandler(
		&MenuProxy{client: menupb.NewMenuServiceClient(menuConn)}))

	// HTTP/2 over TLS so the bidirectional gameplay stream works too; the
	// unary and server-streaming calls behave as over the browser's HTTP/1.1.
	ts := httptest.NewUnstartedServer(mux)
	ts.EnableHTTP2 = true
	ts.StartTLS()
	t.Cleanup(ts.Close)

	rig.url = ts.URL
	rig.httpClient = ts.Client()
	httpClient := ts.Client()
	rig.menuClient = menuconnect.NewMenuServiceClient(httpClient, ts.URL)
	rig.gameClient = game_coordinatorconnect.NewGameCoordinatorServiceClient(httpClient, ts.URL)
	rig.controllerClient = controller_managerconnect.NewControllerManagerServiceClient(httpClient, ts.URL)
	return rig
}

// otherBackendsUntouched fails if a backend other than want saw a call.
func (r *proxyRig) otherBackendsUntouched(t *testing.T, want string) {
	t.Helper()
	if want != "menu" {
		r.menu.log.none(t)
	}
	if want != "game" {
		r.game.log.none(t)
	}
	if want != "controller" {
		r.controller.log.none(t)
	}
}

func testCtx(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	return ctx
}

// assertBackendError checks that a backend gRPC status surfaces as the Connect
// error with the matching code and the backend message (#1560).
func assertBackendError(t *testing.T, err error, wantCode connect.Code, backendDesc string) {
	t.Helper()
	if err == nil {
		t.Fatal("expected a Connect error, got nil")
	}
	var cerr *connect.Error
	if !errors.As(err, &cerr) {
		t.Fatalf("error %T is not a *connect.Error: %v", err, err)
	}
	if cerr.Code() != wantCode {
		t.Errorf("Connect code = %v, want %v", cerr.Code(), wantCode)
	}
	if !strings.Contains(cerr.Message(), backendDesc) {
		t.Errorf("Connect message %q does not carry the backend error %q", cerr.Message(), backendDesc)
	}
}

func eq(t *testing.T, got, want proto.Message) {
	t.Helper()
	if !proto.Equal(got, want) {
		t.Errorf("backend request = %v, want %v", got, want)
	}
}

// --- menu ------------------------------------------------------------------

// UC: UC-010 A6
// UC: UC-011 BR-002
func TestProxyProcessInputStartGame(t *testing.T) {
	t.Run("UC-010/A6 start_game reaches the menu with its mode", func(t *testing.T) {
		rig := newProxyRig(t)
		req := &menupb.ProcessInputRequest{
			InputType: "web_command",
			Data:      map[string]string{"command": "start_game", "mode": "JoustFFA"},
		}
		resp, err := rig.menuClient.ProcessInput(testCtx(t), connect.NewRequest(req))
		if err != nil {
			t.Fatalf("ProcessInput: %v", err)
		}
		if !resp.Msg.GetSuccess() {
			t.Errorf("success = false, want true")
		}
		eq(t, rig.menu.log.only(t, "ProcessInput"), req)
		rig.otherBackendsUntouched(t, "menu")
	})

	t.Run("UC-011/BR-002 rejected mode returns success=false with the menu's error", func(t *testing.T) {
		rig := newProxyRig(t)
		resp, err := rig.menuClient.ProcessInput(testCtx(t), connect.NewRequest(&menupb.ProcessInputRequest{
			InputType: "web_command",
			Data:      map[string]string{"command": "start_game", "mode": "NoSuchMode"},
		}))
		if err != nil {
			t.Fatalf("ProcessInput: %v", err)
		}
		if resp.Msg.GetSuccess() || resp.Msg.GetError() != "Unknown game mode: NoSuchMode" {
			t.Errorf("response = %v, want success=false with the menu's error", resp.Msg)
		}
	})

	t.Run("UC-010/A6 menu gRPC error surfaces as a Connect error", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.menu.err = status.Error(codes.Unavailable, "menu restarting")
		_, err := rig.menuClient.ProcessInput(testCtx(t), connect.NewRequest(&menupb.ProcessInputRequest{InputType: "web_command"}))
		assertBackendError(t, err, connect.CodeUnavailable, "menu restarting")
	})
}

// UC: UC-011 BR-002
func TestProxyListGameModes(t *testing.T) {
	t.Run("UC-011/BR-002 mode list and default come back from the menu", func(t *testing.T) {
		rig := newProxyRig(t)
		resp, err := rig.menuClient.ListGameModes(testCtx(t), connect.NewRequest(&menupb.ListGameModesRequest{}))
		if err != nil {
			t.Fatalf("ListGameModes: %v", err)
		}
		eq(t, resp.Msg, &menupb.ListGameModesResponse{GameModes: []string{"JoustFFA", "JoustTeams"}, DefaultMode: "JoustFFA"})
		rig.menu.log.only(t, "ListGameModes")
		rig.otherBackendsUntouched(t, "menu")
	})

	t.Run("UC-011/BR-002 menu error surfaces so the dashboard falls back to FFA", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.menu.err = status.Error(codes.Unavailable, "menu down")
		_, err := rig.menuClient.ListGameModes(testCtx(t), connect.NewRequest(&menupb.ListGameModesRequest{}))
		assertBackendError(t, err, connect.CodeUnavailable, "menu down")
	})
}

// UC: UC-015 A5
func TestProxyGetMenuStatus(t *testing.T) {
	t.Run("UC-015/A5 every snapshot field comes back from the menu", func(t *testing.T) {
		rig := newProxyRig(t)
		resp, err := rig.menuClient.GetMenuStatus(testCtx(t), connect.NewRequest(&menupb.GetMenuStatusRequest{}))
		if err != nil {
			t.Fatalf("GetMenuStatus: %v", err)
		}
		eq(t, resp.Msg, menuStatus)
		rig.menu.log.only(t, "GetMenuStatus")
		rig.otherBackendsUntouched(t, "menu")
	})

	t.Run("UC-015/A5 menu error keeps its code so the pad treats the status as unknown", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.menu.err = status.Error(codes.Unavailable, "menu restarting")
		_, err := rig.menuClient.GetMenuStatus(testCtx(t), connect.NewRequest(&menupb.GetMenuStatusRequest{}))
		assertBackendError(t, err, connect.CodeUnavailable, "menu restarting")
	})
}

// UC: UC-010 BR-003
func TestProxyStartStopMenu(t *testing.T) {
	t.Run("UC-010/BR-003 StartMenu and StopMenu reach the menu", func(t *testing.T) {
		rig := newProxyRig(t)
		if _, err := rig.menuClient.StartMenu(testCtx(t), connect.NewRequest(&menupb.StartMenuRequest{})); err != nil {
			t.Fatalf("StartMenu: %v", err)
		}
		rig.menu.log.only(t, "StartMenu")
		rig.menu.log = callLog{}
		resp, err := rig.menuClient.StopMenu(testCtx(t), connect.NewRequest(&menupb.StopMenuRequest{}))
		if err != nil {
			t.Fatalf("StopMenu: %v", err)
		}
		if !resp.Msg.GetSuccess() {
			t.Error("StopMenu success = false, want true")
		}
		rig.menu.log.only(t, "StopMenu")
		rig.otherBackendsUntouched(t, "menu")
	})

	t.Run("UC-010/BR-003 menu errors surface as Connect errors", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.menu.err = status.Error(codes.FailedPrecondition, "menu busy")
		_, err := rig.menuClient.StartMenu(testCtx(t), connect.NewRequest(&menupb.StartMenuRequest{}))
		assertBackendError(t, err, connect.CodeFailedPrecondition, "menu busy")
		_, err = rig.menuClient.StopMenu(testCtx(t), connect.NewRequest(&menupb.StopMenuRequest{}))
		assertBackendError(t, err, connect.CodeFailedPrecondition, "menu busy")
	})
}

// UC: UC-011 BR-002
func TestProxyStreamMenuEvents(t *testing.T) {
	t.Run("UC-011/BR-002 menu events are relayed in order", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.menu.events = []*menupb.MenuEvent{
			{EventType: "selection_changed", Data: map[string]string{"mode": "JoustTeams"}, Timestamp: 1},
			{EventType: "game_start_rejected", Data: map[string]string{"mode": "NoSuchMode"}, Timestamp: 2},
		}
		stream, err := rig.menuClient.StreamMenuEvents(testCtx(t), connect.NewRequest(&menupb.StreamMenuEventsRequest{}))
		if err != nil {
			t.Fatalf("StreamMenuEvents: %v", err)
		}
		defer stream.Close()
		var got []*menupb.MenuEvent
		for stream.Receive() {
			got = append(got, stream.Msg())
		}
		if err := stream.Err(); err != nil {
			t.Fatalf("stream ended with %v, want a clean end", err)
		}
		if len(got) != len(rig.menu.events) {
			t.Fatalf("got %d events, want %d", len(got), len(rig.menu.events))
		}
		for i := range got {
			eq(t, got[i], rig.menu.events[i])
		}
		rig.otherBackendsUntouched(t, "menu")
	})

	t.Run("UC-011/BR-002 backend stream error after events surfaces", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.menu.events = []*menupb.MenuEvent{{EventType: "menu_started"}}
		rig.menu.endErr = status.Error(codes.Internal, "menu stream broke")
		stream, err := rig.menuClient.StreamMenuEvents(testCtx(t), connect.NewRequest(&menupb.StreamMenuEventsRequest{}))
		if err != nil {
			t.Fatalf("StreamMenuEvents: %v", err)
		}
		defer stream.Close()
		n := 0
		for stream.Receive() {
			n++
		}
		if n != 1 {
			t.Errorf("got %d events before the error, want 1", n)
		}
		assertBackendError(t, stream.Err(), connect.CodeInternal, "menu stream broke")
	})
}

// --- game coordinator ------------------------------------------------------

// UC: UC-027 MSS A1
func TestProxyForceEndGame(t *testing.T) {
	t.Run("UC-027/MSS dashboard Stop reaches the coordinator with its reason", func(t *testing.T) {
		rig := newProxyRig(t)
		req := &gamepb.ForceEndGameRequest{Reason: "Dashboard stop button"}
		resp, err := rig.gameClient.ForceEndGame(testCtx(t), connect.NewRequest(req))
		if err != nil {
			t.Fatalf("ForceEndGame: %v", err)
		}
		if !resp.Msg.GetSuccess() {
			t.Error("success = false, want true")
		}
		got := rig.game.log.only(t, "ForceEndGame").(*gamepb.ForceEndGameRequest)
		eq(t, got, req)
		if got.GameId != nil {
			t.Errorf("game_id = %q, want unset (primary game)", got.GetGameId())
		}
		rig.otherBackendsUntouched(t, "game")
	})

	t.Run("UC-027/MSS a given game id is forwarded", func(t *testing.T) {
		rig := newProxyRig(t)
		req := &gamepb.ForceEndGameRequest{Reason: "operator", GameId: proto.String("game_live")}
		if _, err := rig.gameClient.ForceEndGame(testCtx(t), connect.NewRequest(req)); err != nil {
			t.Fatalf("ForceEndGame: %v", err)
		}
		eq(t, rig.game.log.only(t, "ForceEndGame"), req)
	})

	t.Run("UC-027/A1 unknown game id returns the coordinator's success=false", func(t *testing.T) {
		rig := newProxyRig(t)
		resp, err := rig.gameClient.ForceEndGame(testCtx(t), connect.NewRequest(
			&gamepb.ForceEndGameRequest{Reason: "operator", GameId: proto.String("game_nope")}))
		if err != nil {
			t.Fatalf("ForceEndGame: %v", err)
		}
		if resp.Msg.GetSuccess() || resp.Msg.GetError() != "Unknown game_id: game_nope" {
			t.Errorf("response = %v, want success=false with the coordinator's error", resp.Msg)
		}
	})

	t.Run("ForceEndGame: coordinator gRPC error surfaces as a Connect error", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.game.err = status.Error(codes.Unavailable, "coordinator down")
		_, err := rig.gameClient.ForceEndGame(testCtx(t), connect.NewRequest(&gamepb.ForceEndGameRequest{Reason: "x"}))
		assertBackendError(t, err, connect.CodeUnavailable, "coordinator down")
	})
}

// Generic forwarding behaviour, no use-case step of its own.
func TestProxyGameStateAndList(t *testing.T) {
	t.Run("GetGameState forwards the game id", func(t *testing.T) {
		rig := newProxyRig(t)
		req := &gamepb.GetGameStateRequest{GameId: proto.String("game_live")}
		resp, err := rig.gameClient.GetGameState(testCtx(t), connect.NewRequest(req))
		if err != nil {
			t.Fatalf("GetGameState: %v", err)
		}
		if resp.Msg.GetGameInfo().GetGameId() != "game_live" || resp.Msg.GetGameInfo().GetGameMode() != "JoustFFA" {
			t.Errorf("game_info = %v, want game_live / JoustFFA", resp.Msg.GetGameInfo())
		}
		eq(t, rig.game.log.only(t, "GetGameState"), req)
		rig.otherBackendsUntouched(t, "game")
	})

	t.Run("ListGames returns every game", func(t *testing.T) {
		rig := newProxyRig(t)
		resp, err := rig.gameClient.ListGames(testCtx(t), connect.NewRequest(&gamepb.ListGamesRequest{}))
		if err != nil {
			t.Fatalf("ListGames: %v", err)
		}
		if n := len(resp.Msg.GetGames()); n != 2 {
			t.Errorf("got %d games, want 2", n)
		}
		rig.game.log.only(t, "ListGames")
		rig.otherBackendsUntouched(t, "game")
	})

	t.Run("coordinator errors surface as Connect errors", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.game.err = status.Error(codes.NotFound, "no such game")
		_, err := rig.gameClient.GetGameState(testCtx(t), connect.NewRequest(&gamepb.GetGameStateRequest{}))
		assertBackendError(t, err, connect.CodeNotFound, "no such game")
		_, err = rig.gameClient.ListGames(testCtx(t), connect.NewRequest(&gamepb.ListGamesRequest{}))
		assertBackendError(t, err, connect.CodeNotFound, "no such game")
	})
}

// UC: UC-035 main, step 2
func TestProxyStreamGameEvents(t *testing.T) {
	t.Run("events are relayed in order with the game id filter forwarded", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.game.events = []*gamepb.GameEvent{
			{EventType: "game_start", GameId: "game_live", Timestamp: 1},
			{EventType: "game_force_ended", GameId: "game_live", Data: map[string]string{"reason": "Dashboard stop button"}, Timestamp: 2},
		}
		req := &gamepb.StreamEventsRequest{GameId: proto.String("game_live")}
		stream, err := rig.gameClient.StreamGameEvents(testCtx(t), connect.NewRequest(req))
		if err != nil {
			t.Fatalf("StreamGameEvents: %v", err)
		}
		defer stream.Close()
		var got []*gamepb.GameEvent
		for stream.Receive() {
			got = append(got, stream.Msg())
		}
		if err := stream.Err(); err != nil {
			t.Fatalf("stream ended with %v, want a clean end", err)
		}
		if len(got) != len(rig.game.events) {
			t.Fatalf("got %d events, want %d", len(got), len(rig.game.events))
		}
		for i := range got {
			eq(t, got[i], rig.game.events[i])
		}
		eq(t, rig.game.log.only(t, "StreamGameEvents"), req)
		rig.otherBackendsUntouched(t, "game")
	})

	t.Run("backend stream error surfaces", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.game.endErr = status.Error(codes.Unavailable, "coordinator gone")
		stream, err := rig.gameClient.StreamGameEvents(testCtx(t), connect.NewRequest(&gamepb.StreamEventsRequest{}))
		if err != nil {
			t.Fatalf("StreamGameEvents: %v", err)
		}
		defer stream.Close()
		for stream.Receive() {
		}
		assertBackendError(t, stream.Err(), connect.CodeUnavailable, "coordinator gone")
	})
}

// --- controller manager ----------------------------------------------------

// UC: UC-002 A2
func TestProxyRenameController(t *testing.T) {
	t.Run("UC-002/A2 rename reaches the controller manager with serial and name", func(t *testing.T) {
		rig := newProxyRig(t)
		req := &controllerpb.RenameControllerRequest{Serial: "aa:bb", Name: "Red Baron"}
		resp, err := rig.controllerClient.RenameController(testCtx(t), connect.NewRequest(req))
		if err != nil {
			t.Fatalf("RenameController: %v", err)
		}
		if !resp.Msg.GetSuccess() {
			t.Error("success = false, want true")
		}
		eq(t, rig.controller.log.only(t, "RenameController"), req)
		rig.otherBackendsUntouched(t, "controller")
	})

	t.Run("UC-002/A2 controller manager error surfaces", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.controller.err = status.Error(codes.InvalidArgument, "name too long")
		_, err := rig.controllerClient.RenameController(testCtx(t), connect.NewRequest(&controllerpb.RenameControllerRequest{Serial: "aa:bb"}))
		assertBackendError(t, err, connect.CodeInvalidArgument, "name too long")
	})
}

// UC: UC-004 main A1
func TestProxyIdentifyController(t *testing.T) {
	t.Run("UC-004/main identify forwards serial and duration", func(t *testing.T) {
		rig := newProxyRig(t)
		req := &controllerpb.IdentifyControllerRequest{Serial: "aa:bb", DurationMs: 3000}
		resp, err := rig.controllerClient.IdentifyController(testCtx(t), connect.NewRequest(req))
		if err != nil {
			t.Fatalf("IdentifyController: %v", err)
		}
		eq(t, resp.Msg, &controllerpb.IdentifyControllerResponse{Success: true, Outcome: "started", DurationMs: 3000})
		eq(t, rig.controller.log.only(t, "IdentifyController"), req)
		rig.otherBackendsUntouched(t, "controller")
	})

	t.Run("UC-004/A1 not-connected outcome comes back intact", func(t *testing.T) {
		rig := newProxyRig(t)
		resp, err := rig.controllerClient.IdentifyController(testCtx(t), connect.NewRequest(&controllerpb.IdentifyControllerRequest{Serial: "gone"}))
		if err != nil {
			t.Fatalf("IdentifyController: %v", err)
		}
		if resp.Msg.GetSuccess() || resp.Msg.GetOutcome() != "not_connected" {
			t.Errorf("response = %v, want success=false outcome=not_connected", resp.Msg)
		}
	})

	t.Run("UC-004/main controller manager error surfaces", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.controller.err = status.Error(codes.Unavailable, "cm down")
		_, err := rig.controllerClient.IdentifyController(testCtx(t), connect.NewRequest(&controllerpb.IdentifyControllerRequest{Serial: "aa:bb"}))
		assertBackendError(t, err, connect.CodeUnavailable, "cm down")
	})
}

// UC: UC-070 BR-006
func TestProxyGetConnectedControllers(t *testing.T) {
	t.Run("connected controllers come back from the controller manager", func(t *testing.T) {
		rig := newProxyRig(t)
		resp, err := rig.controllerClient.GetConnectedControllers(testCtx(t), connect.NewRequest(&controllerpb.GetConnectedControllersRequest{}))
		if err != nil {
			t.Fatalf("GetConnectedControllers: %v", err)
		}
		eq(t, resp.Msg, &controllerpb.GetConnectedControllersResponse{
			ConnectedSerials: []string{"aa:bb"},
			Controllers:      []*controllerpb.ConnectedController{{Serial: "aa:bb", Name: "Red", Battery: 4}},
		})
		rig.controller.log.only(t, "GetConnectedControllers")
		rig.otherBackendsUntouched(t, "controller")
	})

	t.Run("controller manager error surfaces", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.controller.err = status.Error(codes.Unavailable, "cm down")
		_, err := rig.controllerClient.GetConnectedControllers(testCtx(t), connect.NewRequest(&controllerpb.GetConnectedControllersRequest{}))
		assertBackendError(t, err, connect.CodeUnavailable, "cm down")
	})
}

// Generic forwarding behaviour, no use-case step of its own.
func TestProxyBackendUnreachable(t *testing.T) {
	t.Run("a backend that is not listening surfaces as a Connect error", func(t *testing.T) {
		// A connection to a closed bufconn listener fails at call time, as
		// a stopped menu container would.
		lis := bufconn.Listen(1 << 10)
		_ = lis.Close()
		conn, err := grpc.NewClient("passthrough:///bufnet",
			grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return lis.DialContext(ctx) }),
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		)
		if err != nil {
			t.Fatalf("NewClient: %v", err)
		}
		t.Cleanup(func() { _ = conn.Close() })
		mux := http.NewServeMux()
		mux.Handle(menuconnect.NewMenuServiceHandler(&MenuProxy{client: menupb.NewMenuServiceClient(conn)}))
		ts := httptest.NewServer(mux)
		t.Cleanup(ts.Close)

		client := menuconnect.NewMenuServiceClient(ts.Client(), ts.URL)
		_, err = client.ProcessInput(testCtx(t), connect.NewRequest(&menupb.ProcessInputRequest{InputType: "web_command"}))
		var cerr *connect.Error
		if !errors.As(err, &cerr) || cerr.Code() != connect.CodeUnavailable {
			t.Fatalf("err = %v, want a CodeUnavailable Connect error", err)
		}
	})
}

// postJSON sends a request the way services/dashboard/src/client.ts does:
// a plain fetch POST with Content-Type application/json.
func postJSON(t *testing.T, rig *proxyRig, path string, body any, accept string) (*http.Response, []byte) {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	req, err := http.NewRequestWithContext(testCtx(t), http.MethodPost, rig.url+path, bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if accept != "" {
		req.Header.Set("Accept", accept)
	}
	resp, err := rig.httpClient.Do(req)
	if err != nil {
		t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	out, _ := io.ReadAll(resp.Body)
	return resp, out
}

// UC: UC-010 A6
// UC: UC-027 MSS
func TestProxyDashboardWireFormat(t *testing.T) {
	t.Run("UC-010/A6 dashboard Start Game JSON reaches the menu", func(t *testing.T) {
		rig := newProxyRig(t)
		resp, body := postJSON(t, rig, "/joustmania.menu.MenuService/ProcessInput",
			map[string]any{"inputType": "web_command", "data": map[string]string{"command": "start_game", "mode": "JoustFFA"}}, "")
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d (%s), want 200", resp.StatusCode, body)
		}
		var got map[string]any
		if err := json.Unmarshal(body, &got); err != nil || got["success"] != true {
			t.Errorf("body = %s, want {\"success\":true}", body)
		}
		eq(t, rig.menu.log.only(t, "ProcessInput"), &menupb.ProcessInputRequest{
			InputType: "web_command",
			Data:      map[string]string{"command": "start_game", "mode": "JoustFFA"},
		})
	})

	t.Run("UC-027/MSS dashboard Stop JSON reaches the coordinator", func(t *testing.T) {
		rig := newProxyRig(t)
		resp, body := postJSON(t, rig, "/joustmania.game_coordinator.GameCoordinatorService/ForceEndGame",
			map[string]any{"reason": "Dashboard stop button"}, "")
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d (%s), want 200", resp.StatusCode, body)
		}
		eq(t, rig.game.log.only(t, "ForceEndGame"), &gamepb.ForceEndGameRequest{Reason: "Dashboard stop button"})
	})
}

// dashboardStreamFrame is one Connect envelope of a streaming response.
type dashboardStreamFrame struct {
	flags   byte
	payload []byte
}

// postConnectStream sends a request the way services/dashboard/src/connect.ts
// does for server streams (#1542): Content-Type application/connect+json, the
// JSON request in one 5-byte envelope, over HTTP/1.1 as the browser reaches
// connect-proxy through envoy. It returns the response and its frames.
// http1Client talks HTTP/1.1 to the proxy, as envoy does in production.
func http1Client(t *testing.T, rig *proxyRig) *http.Client {
	t.Helper()
	tr := rig.httpClient.Transport.(*http.Transport).Clone()
	tr.ForceAttemptHTTP2 = false
	tr.TLSNextProto = map[string]func(string, *tls.Conn) http.RoundTripper{}
	tr.TLSClientConfig.NextProtos = []string{"http/1.1"}
	t.Cleanup(tr.CloseIdleConnections)
	return &http.Client{Transport: tr}
}

func postConnectStream(t *testing.T, rig *proxyRig, path string, body any) (*http.Response, []dashboardStreamFrame) {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	env := make([]byte, 5+len(raw))
	binary.BigEndian.PutUint32(env[1:5], uint32(len(raw)))
	copy(env[5:], raw)

	req, err := http.NewRequestWithContext(testCtx(t), http.MethodPost, rig.url+path, bytes.NewReader(env))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/connect+json")
	req.Header.Set("Connect-Protocol-Version", "1")

	resp, err := http1Client(t, rig).Do(req)
	if err != nil {
		t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	if resp.ProtoMajor != 1 {
		t.Fatalf("proto = %s, want HTTP/1.1 like the browser behind envoy", resp.Proto)
	}
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	var frames []dashboardStreamFrame
	if resp.StatusCode != http.StatusOK {
		return resp, frames
	}
	for len(data) > 0 {
		if len(data) < 5 {
			t.Fatalf("truncated frame header: % x", data)
		}
		n := int(binary.BigEndian.Uint32(data[1:5]))
		if len(data) < 5+n {
			t.Fatalf("truncated frame: want %d payload bytes, have %d", n, len(data)-5)
		}
		frames = append(frames, dashboardStreamFrame{flags: data[0], payload: data[5 : 5+n]})
		data = data[5+n:]
	}
	return resp, frames
}

// The dashboard's live event feed through connect-proxy (#1542): the wire
// format of services/dashboard/src/connect.ts is accepted and the events come
// back as enveloped frames ending in an end-stream frame.
//
// UC: UC-070 BR-005
func TestProxyDashboardStreamingJSON(t *testing.T) {
	const path = "/joustmania.game_coordinator.GameCoordinatorService/StreamGameEvents"

	t.Run("dashboard connect+json StreamGameEvents relays events and ends cleanly (#1542)", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.game.events = []*gamepb.GameEvent{
			{EventType: "game_start", GameId: "game_live", Timestamp: 1},
			{EventType: "game_started", GameId: "game_live", Data: map[string]string{"players": "2"}, Timestamp: 2},
		}
		resp, frames := postConnectStream(t, rig, path, map[string]any{})
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		if ct := resp.Header.Get("Content-Type"); ct != "application/connect+json" {
			t.Errorf("Content-Type = %q, want application/connect+json", ct)
		}
		if len(frames) != len(rig.game.events)+1 {
			t.Fatalf("got %d frames, want %d events + end-stream", len(frames), len(rig.game.events))
		}
		for i, ev := range rig.game.events {
			if frames[i].flags != 0 {
				t.Errorf("frame %d flags = %#x, want 0 (message)", i, frames[i].flags)
			}
			got := &gamepb.GameEvent{}
			if err := protojson.Unmarshal(frames[i].payload, got); err != nil {
				t.Fatalf("frame %d: %v (%s)", i, err, frames[i].payload)
			}
			eq(t, got, ev)
		}
		end := frames[len(frames)-1]
		if end.flags&0x02 == 0 {
			t.Errorf("last frame flags = %#x, want end-stream (0x02)", end.flags)
		}
		if strings.Contains(string(end.payload), "error") {
			t.Errorf("end-stream = %s, want no error", end.payload)
		}
		eq(t, rig.game.log.only(t, "StreamGameEvents"), &gamepb.StreamEventsRequest{})
		rig.otherBackendsUntouched(t, "game")
	})

	t.Run("UC-070/BR-005 dashboard game id filter reaches the coordinator", func(t *testing.T) {
		rig := newProxyRig(t)
		resp, _ := postConnectStream(t, rig, path, map[string]any{"gameId": "game_shadow"})
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		eq(t, rig.game.log.only(t, "StreamGameEvents"), &gamepb.StreamEventsRequest{GameId: proto.String("game_shadow")})
	})

	t.Run("backend error arrives in the end-stream frame the dashboard parses", func(t *testing.T) {
		rig := newProxyRig(t)
		rig.game.endErr = status.Error(codes.Unavailable, "coordinator gone")
		resp, frames := postConnectStream(t, rig, path, map[string]any{})
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200 (Connect streams report errors in the end-stream frame)", resp.StatusCode)
		}
		if len(frames) != 1 || frames[0].flags&0x02 == 0 {
			t.Fatalf("frames = %v, want one end-stream frame", frames)
		}
		var end struct {
			Error struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		}
		if err := json.Unmarshal(frames[0].payload, &end); err != nil {
			t.Fatalf("end-stream payload %s: %v", frames[0].payload, err)
		}
		if end.Error.Code != "unavailable" || !strings.Contains(end.Error.Message, "coordinator gone") {
			t.Errorf("end-stream error = %+v, want unavailable / coordinator gone", end.Error)
		}
	})
}

// The bidi pass-through forwarders were removed (#1543): browser-facing RPCs
// are unary or server-streaming, and the proxy owns the bidi gRPC stream
// itself (WatchGameplayData, watch_gameplay_test.go). The removed methods fall
// through to the embedded Unimplemented handler.
//
// UC: UC-035 BR-006
func TestProxyBidiRPCsUnimplemented(t *testing.T) {
	t.Run("UC-035/BR-006 StreamGameplayData is unimplemented and never reaches the controller manager", func(t *testing.T) {
		rig := newProxyRig(t)
		stream := rig.controllerClient.StreamGameplayData(testCtx(t))
		_ = stream.Send(&controllerpb.GameplayStreamControl{Control: &controllerpb.GameplayStreamControl_Config{
			Config: &controllerpb.GameplayStreamConfig{UpdateFrequencyHz: 30},
		}})
		_, err := stream.Receive()
		if connect.CodeOf(err) != connect.CodeUnimplemented {
			t.Errorf("code = %v (%v), want unimplemented", connect.CodeOf(err), err)
		}
		rig.controller.log.none(t)
		_ = stream.CloseRequest()
		_ = stream.CloseResponse()
	})
	t.Run("StreamButtonEvents is unimplemented and never reaches the controller manager", func(t *testing.T) {
		rig := newProxyRig(t)
		stream := rig.controllerClient.StreamButtonEvents(testCtx(t))
		_ = stream.Send(&controllerpb.ButtonEventStreamControl{Control: &controllerpb.ButtonEventStreamControl_Config{
			Config: &controllerpb.ButtonEventStreamConfig{},
		}})
		_, err := stream.Receive()
		if connect.CodeOf(err) != connect.CodeUnimplemented {
			t.Errorf("code = %v (%v), want unimplemented", connect.CodeOf(err), err)
		}
		rig.controller.log.none(t)
		_ = stream.CloseRequest()
		_ = stream.CloseResponse()
	})
}

// UC: UC-011 BR-002
func TestToConnectError(t *testing.T) {
	cases := []struct {
		grpc    codes.Code
		connect connect.Code
	}{
		{codes.Canceled, connect.CodeCanceled},
		{codes.Unknown, connect.CodeUnknown},
		{codes.InvalidArgument, connect.CodeInvalidArgument},
		{codes.DeadlineExceeded, connect.CodeDeadlineExceeded},
		{codes.NotFound, connect.CodeNotFound},
		{codes.AlreadyExists, connect.CodeAlreadyExists},
		{codes.PermissionDenied, connect.CodePermissionDenied},
		{codes.ResourceExhausted, connect.CodeResourceExhausted},
		{codes.FailedPrecondition, connect.CodeFailedPrecondition},
		{codes.Aborted, connect.CodeAborted},
		{codes.OutOfRange, connect.CodeOutOfRange},
		{codes.Unimplemented, connect.CodeUnimplemented},
		{codes.Internal, connect.CodeInternal},
		{codes.Unavailable, connect.CodeUnavailable},
		{codes.DataLoss, connect.CodeDataLoss},
		{codes.Unauthenticated, connect.CodeUnauthenticated},
	}
	for _, tc := range cases {
		t.Run(tc.grpc.String(), func(t *testing.T) {
			got := toConnectError(status.Error(tc.grpc, "backend said no"))
			if got.Code() != tc.connect {
				t.Errorf("code = %v, want %v", got.Code(), tc.connect)
			}
			if got.Message() != "backend said no" {
				t.Errorf("message = %q, want the status message only", got.Message())
			}
		})
	}
	t.Run("a non-status error stays Internal", func(t *testing.T) {
		got := toConnectError(errors.New("plain failure"))
		if got.Code() != connect.CodeInternal || !strings.Contains(got.Message(), "plain failure") {
			t.Errorf("got %v", got)
		}
	})
}
