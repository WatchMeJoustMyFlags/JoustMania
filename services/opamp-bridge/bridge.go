package main

import (
	"context"
	"log"
	"net/http"
	"sync"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
	"github.com/open-telemetry/opamp-go/protobufs"
	"github.com/open-telemetry/opamp-go/server"
	"github.com/open-telemetry/opamp-go/server/types"
)

// FlagValues holds the current collector-level flag values.
type FlagValues struct {
	TailSamplingRate  float64
	LogFilterSeverity string
}

// Bridge watches flagd for collector-level flag changes and pushes
// config updates to connected OTel Collector agents via OpAMP.
type Bridge struct {
	client       *openfeature.Client
	listenAddr   string
	pollInterval time.Duration

	srv     server.OpAMPServer
	mu      sync.Mutex
	conns   []types.Connection
	current FlagValues
	cancel  context.CancelFunc
}

// NewBridge creates a new OpAMP bridge.
func NewBridge(client *openfeature.Client, listenAddr string, pollInterval time.Duration) *Bridge {
	return &Bridge{
		client:       client,
		listenAddr:   listenAddr,
		pollInterval: pollInterval,
	}
}

// Start begins the OpAMP server and flag polling loop.
func (b *Bridge) Start(ctx context.Context) error {
	b.srv = server.New(newLogger())

	settings := server.StartSettings{
		Settings: server.Settings{
			Callbacks: types.Callbacks{
				OnConnecting: func(_ *http.Request) types.ConnectionResponse {
					return types.ConnectionResponse{
						Accept: true,
						ConnectionCallbacks: types.ConnectionCallbacks{
							OnConnected: func(connCtx context.Context, conn types.Connection) {
								log.Println("agent connected")
								b.mu.Lock()
								b.conns = append(b.conns, conn)
								current := b.current
								b.mu.Unlock()

								// Send current config to newly connected agent.
								cfg := RenderCollectorConfig(current)
								_ = conn.Send(connCtx, &protobufs.ServerToAgent{
									RemoteConfig: &protobufs.AgentRemoteConfig{
										Config: &protobufs.AgentConfigMap{
											ConfigMap: map[string]*protobufs.AgentConfigFile{
												"": {
													Body:        cfg,
													ContentType: "text/yaml",
												},
											},
										},
									},
								})
							},
							OnMessage: func(_ context.Context, conn types.Connection, msg *protobufs.AgentToServer) *protobufs.ServerToAgent {
								return &protobufs.ServerToAgent{}
							},
							OnConnectionClose: func(conn types.Connection) {
								log.Println("agent disconnected")
								b.mu.Lock()
								defer b.mu.Unlock()
								for i, c := range b.conns {
									if c == conn {
										b.conns = append(b.conns[:i], b.conns[i+1:]...)
										break
									}
								}
							},
						},
					}
				},
			},
		},
		ListenEndpoint: b.listenAddr,
	}

	if err := b.srv.Start(settings); err != nil {
		return err
	}

	pollCtx, cancel := context.WithCancel(ctx)
	b.cancel = cancel

	// Initial flag read.
	b.current = b.readFlags(pollCtx)
	log.Printf("initial flags: sampling_rate=%.2f, log_severity=%s",
		b.current.TailSamplingRate, b.current.LogFilterSeverity)

	go b.pollLoop(pollCtx)
	return nil
}

// Stop shuts down the bridge.
func (b *Bridge) Stop() {
	if b.cancel != nil {
		b.cancel()
	}
	if b.srv != nil {
		b.srv.Stop(context.Background())
	}
}

func (b *Bridge) pollLoop(ctx context.Context) {
	ticker := time.NewTicker(b.pollInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			newValues := b.readFlags(ctx)
			if newValues != b.current {
				log.Printf("flag change detected: sampling_rate=%.2f->%.2f, log_severity=%s->%s",
					b.current.TailSamplingRate, newValues.TailSamplingRate,
					b.current.LogFilterSeverity, newValues.LogFilterSeverity)
				b.current = newValues
				b.pushConfig(ctx)
			}
		}
	}
}

func (b *Bridge) readFlags(ctx context.Context) FlagValues {
	evalCtx := openfeature.NewEvaluationContext("", map[string]interface{}{})

	samplingRate, _ := b.client.FloatValue(ctx, "collector_tail_sampling_rate", 1.0, evalCtx)
	logSeverity, _ := b.client.StringValue(ctx, "collector_log_filter_severity", "INFO", evalCtx)

	return FlagValues{
		TailSamplingRate:  samplingRate,
		LogFilterSeverity: logSeverity,
	}
}

func (b *Bridge) pushConfig(ctx context.Context) {
	cfg := RenderCollectorConfig(b.current)
	msg := &protobufs.ServerToAgent{
		RemoteConfig: &protobufs.AgentRemoteConfig{
			Config: &protobufs.AgentConfigMap{
				ConfigMap: map[string]*protobufs.AgentConfigFile{
					"": {
						Body:        cfg,
						ContentType: "text/yaml",
					},
				},
			},
		},
	}

	b.mu.Lock()
	conns := make([]types.Connection, len(b.conns))
	copy(conns, b.conns)
	b.mu.Unlock()

	for _, conn := range conns {
		if err := conn.Send(ctx, msg); err != nil {
			log.Printf("failed to send config to agent: %v", err)
		}
	}
	log.Printf("pushed config to %d agent(s)", len(conns))
}

// opampLogger adapts Go's log package to the OpAMP server logger interface.
type opampLogger struct{}

func newLogger() *opampLogger { return &opampLogger{} }

func (l *opampLogger) Debugf(_ context.Context, format string, args ...interface{}) {
	log.Printf("[DEBUG] "+format, args...)
}

func (l *opampLogger) Errorf(_ context.Context, format string, args ...interface{}) {
	log.Printf("[ERROR] "+format, args...)
}
