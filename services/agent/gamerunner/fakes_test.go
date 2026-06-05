package gamerunner

import (
	"context"
	"fmt"
	"net"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	mockpb "github.com/joustmania/agent/gen/controller_manager_mock"
	gcpb "github.com/joustmania/agent/gen/game_coordinator"
)

// fakeMock is an in-process MockControllerService recording the calls the
// runner makes. Behavior is configurable per-test via the func fields.
type fakeMock struct {
	mockpb.UnimplementedMockControllerServiceServer

	mu sync.Mutex

	// added is the ordered list of serials the fake has minted (not yet removed).
	added []string
	// removed records serials passed to RemoveController.
	removed []string
	// deaths records serials passed to SimulateDeath, in order.
	deaths []string
	// movements counts SimulateMovement calls.
	movements int
	// reservedTag records the tag of the last AddControllers call.
	reservedTag string

	// addControllers overrides AddControllers. Default mints "fake-<tag>-<i>".
	addControllers func(*mockpb.AddControllersRequest) (*mockpb.AddControllersResponse, error)
	// list overrides ListMockControllers. Default reports `added` as reserved.
	list func() (*mockpb.ListResponse, error)
}

func (f *fakeMock) AddControllers(_ context.Context, req *mockpb.AddControllersRequest) (*mockpb.AddControllersResponse, error) {
	f.mu.Lock()
	f.reservedTag = req.GetTag()
	f.mu.Unlock()
	if f.addControllers != nil {
		return f.addControllers(req)
	}
	serials := make([]string, req.GetCount())
	for i := range serials {
		serials[i] = fmt.Sprintf("fake-%s-%d", req.GetTag(), i)
	}
	f.mu.Lock()
	f.added = append(f.added, serials...)
	f.mu.Unlock()
	return &mockpb.AddControllersResponse{Success: true, Serials: serials}, nil
}

func (f *fakeMock) RemoveController(_ context.Context, req *mockpb.RemoveControllerRequest) (*mockpb.RemoveControllerResponse, error) {
	f.mu.Lock()
	f.removed = append(f.removed, req.GetSerial())
	f.mu.Unlock()
	return &mockpb.RemoveControllerResponse{Success: true}, nil
}

func (f *fakeMock) SimulateMovement(_ context.Context, _ *mockpb.MovementRequest) (*mockpb.MovementResponse, error) {
	f.mu.Lock()
	f.movements++
	f.mu.Unlock()
	return &mockpb.MovementResponse{Success: true}, nil
}

func (f *fakeMock) SimulateDeath(_ context.Context, req *mockpb.DeathRequest) (*mockpb.DeathResponse, error) {
	f.mu.Lock()
	f.deaths = append(f.deaths, req.GetSerial())
	f.mu.Unlock()
	return &mockpb.DeathResponse{Success: true, AccelMagnitude: 7.07}, nil
}

func (f *fakeMock) ListMockControllers(_ context.Context, _ *mockpb.ListRequest) (*mockpb.ListResponse, error) {
	if f.list != nil {
		return f.list()
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	controllers := make([]*mockpb.MockControllerInfo, len(f.added))
	for i, s := range f.added {
		controllers[i] = &mockpb.MockControllerInfo{Serial: s, Reserved: true, Tag: f.reservedTag}
	}
	return &mockpb.ListResponse{
		Serials:     append([]string(nil), f.added...),
		Count:       int32(len(f.added)),
		Controllers: controllers,
	}, nil
}

func (f *fakeMock) snapshot() (added, removed, deaths []string, movements int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.added...),
		append([]string(nil), f.removed...),
		append([]string(nil), f.deaths...),
		f.movements
}

// fakeCoord is an in-process GameCoordinatorService. StreamGameEvents emits the
// scripted `events` (each stamped with gameID) then either blocks until the
// stream context is cancelled or, if streamErr is set, returns it after the
// scripted events.
type fakeCoord struct {
	gcpb.UnimplementedGameCoordinatorServiceServer

	gameID string
	// events is the scripted sequence sent on StreamGameEvents.
	events []*gcpb.GameEvent
	// streamErr, when set, is returned after the scripted events (simulating a
	// mid-stream failure). When nil, the stream blocks after the events until
	// the client/context cancels.
	streamErr error
	// emitTerminalOnForceEnd, when set, makes ForceEndGame push a terminal event
	// onto an extra channel consumed by the live stream.
	emitTerminalOnForceEnd string

	mu             sync.Mutex
	forceEndCalls  []string // game_ids passed to ForceEndGame
	listGames      func() (*gcpb.ListGamesResponse, error)
	forceEndSignal chan *gcpb.GameEvent
}

func (c *fakeCoord) StreamGameEvents(_ *gcpb.StreamEventsRequest, stream grpc.ServerStreamingServer[gcpb.GameEvent]) error {
	for _, ev := range c.events {
		ev.GameId = c.gameID
		if err := stream.Send(ev); err != nil {
			return err
		}
	}
	if c.streamErr != nil {
		return c.streamErr
	}
	// Block until force-end pushes a terminal event or the stream context ends.
	c.mu.Lock()
	signal := c.forceEndSignal
	c.mu.Unlock()
	for {
		select {
		case ev := <-signal:
			ev.GameId = c.gameID
			if err := stream.Send(ev); err != nil {
				return err
			}
		case <-stream.Context().Done():
			return stream.Context().Err()
		}
	}
}

func (c *fakeCoord) ForceEndGame(_ context.Context, req *gcpb.ForceEndGameRequest) (*gcpb.ForceEndGameResponse, error) {
	c.mu.Lock()
	c.forceEndCalls = append(c.forceEndCalls, req.GetGameId())
	signal := c.forceEndSignal
	terminal := c.emitTerminalOnForceEnd
	c.mu.Unlock()
	if terminal != "" && signal != nil {
		select {
		case signal <- &gcpb.GameEvent{EventType: terminal, Timestamp: time.Now().UnixMilli()}:
		default:
		}
	}
	return &gcpb.ForceEndGameResponse{Success: true}, nil
}

func (c *fakeCoord) ListGames(_ context.Context, _ *gcpb.ListGamesRequest) (*gcpb.ListGamesResponse, error) {
	if c.listGames != nil {
		return c.listGames()
	}
	return &gcpb.ListGamesResponse{Success: true}, nil
}

func (c *fakeCoord) forceEndGameIDs() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]string(nil), c.forceEndCalls...)
}

// testHarness wires fake servers over bufconn and injects a dialer into a Runner.
type testHarness struct {
	runner *Runner
	mock   *fakeMock
	coord  *fakeCoord
	stop   func()
}

// newHarness starts both fakes on in-memory listeners and returns a Runner whose
// dialer resolves "coordinator"/"mock" targets to those listeners. Test configs
// use short timers so tests run fast.
func newHarness(t *testing.T, mock *fakeMock, coord *fakeCoord) *testHarness {
	t.Helper()
	if coord.forceEndSignal == nil {
		coord.forceEndSignal = make(chan *gcpb.GameEvent, 1)
	}

	mockLis := bufconn.Listen(1 << 20)
	coordLis := bufconn.Listen(1 << 20)

	mockSrv := grpc.NewServer()
	mockpb.RegisterMockControllerServiceServer(mockSrv, mock)
	coordSrv := grpc.NewServer()
	gcpb.RegisterGameCoordinatorServiceServer(coordSrv, coord)

	go func() { _ = mockSrv.Serve(mockLis) }()
	go func() { _ = coordSrv.Serve(coordLis) }()

	dial := func(target string) (grpc.ClientConnInterface, func() error, error) {
		var lis *bufconn.Listener
		switch target {
		case "coordinator":
			lis = coordLis
		case "mock":
			lis = mockLis
		default:
			return nil, nil, fmt.Errorf("unknown target %q", target)
		}
		conn, err := grpc.NewClient(
			"passthrough:///bufnet",
			grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
				return lis.DialContext(ctx)
			}),
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		)
		if err != nil {
			return nil, nil, err
		}
		return conn, conn.Close, nil
	}

	r := New(Config{
		CoordinatorAddr:  "coordinator",
		MockAddr:         "mock",
		GameTimeout:      2 * time.Second,
		MovementInterval: 5 * time.Millisecond,
		KillInterval:     10 * time.Millisecond,
		RPCTimeout:       2 * time.Second,
	}, nil)
	r.dial = dial

	return &testHarness{
		runner: r,
		mock:   mock,
		coord:  coord,
		stop: func() {
			mockSrv.Stop()
			coordSrv.Stop()
		},
	}
}
