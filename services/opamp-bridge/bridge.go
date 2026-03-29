package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
	"github.com/open-telemetry/opamp-go/protobufs"
	"github.com/open-telemetry/opamp-go/server"
	"github.com/open-telemetry/opamp-go/server/types"
)

// FlagValues holds the current collector-level flag values.
type FlagValues struct {
	HeadSamplingRate  float64
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
// current is initialized to safe defaults so that agents connecting before the
// first flag read receive a valid config (sampling=100%, severity=INFO).
func NewBridge(client *openfeature.Client, listenAddr string, pollInterval time.Duration) *Bridge {
	return &Bridge{
		client:       client,
		listenAddr:   listenAddr,
		pollInterval: pollInterval,
		current: FlagValues{
			HeadSamplingRate:  1.0,
			LogFilterSeverity: "INFO",
		},
	}
}

// Start begins the OpAMP server and flag polling loop.
func (b *Bridge) Start(ctx context.Context) error {
	if b.pollInterval <= 0 {
		return fmt.Errorf("pollInterval must be positive, got %s", b.pollInterval)
	}

	b.srv = server.New(newLogger())

	// Read flags before starting the server so that agents connecting
	// immediately after Start() receive live values instead of defaults.
	initial := b.readFlags(ctx)
	b.mu.Lock()
	b.current = initial
	b.mu.Unlock()
	log.Printf("initial flags: sampling_rate=%.2f, log_severity=%s",
		initial.HeadSamplingRate, initial.LogFilterSeverity)

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
								cfg, err := RenderCollectorConfig(current)
								if err != nil {
									log.Printf("failed to render config for new agent: %v", err)
									return
								}
								sendCtx, sendCancel := context.WithTimeout(connCtx, 5*time.Second)
								defer sendCancel()
								if err := conn.Send(sendCtx, &protobufs.ServerToAgent{
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
								}); err != nil {
									log.Printf("failed to send initial config to agent: %v", err)
								}
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

	go b.pollLoop(pollCtx)
	return nil
}

// Stop shuts down the bridge.
func (b *Bridge) Stop() {
	if b.cancel != nil {
		b.cancel()
	}
	if b.srv != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		b.srv.Stop(ctx)
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

			b.mu.Lock()
			current := b.current
			if newValues != current {
				log.Printf("flag change detected: sampling_rate=%.2f->%.2f, log_severity=%s->%s",
					current.HeadSamplingRate, newValues.HeadSamplingRate,
					current.LogFilterSeverity, newValues.LogFilterSeverity)
				b.current = newValues
				b.mu.Unlock()
				b.pushConfig(ctx)
			} else {
				b.mu.Unlock()
			}
		}
	}
}

func (b *Bridge) readFlags(ctx context.Context) FlagValues {
	evalCtx := openfeature.NewEvaluationContext("", map[string]interface{}{})

	// Each flag read is independent; if one fails, the SDK returns the default
	// value (1.0 for sampling, "INFO" for severity), which are safe defaults.
	// This means partial failures produce a mix of live and default values,
	// which is intentional — we prefer safe defaults over rejecting all flags.
	samplingRate, err := b.client.FloatValue(ctx, "collector_tail_sampling_rate", 1.0, evalCtx)
	if err != nil {
		log.Printf("flagd: failed to read collector_tail_sampling_rate: %v", err)
	}
	logSeverity, err2 := b.client.StringValue(ctx, "collector_log_filter_severity", "INFO", evalCtx)
	if err2 != nil {
		log.Printf("flagd: failed to read collector_log_filter_severity: %v", err2)
	}

	return FlagValues{
		HeadSamplingRate:  samplingRate,
		LogFilterSeverity: strings.ToUpper(logSeverity),
	}
}

func (b *Bridge) pushConfig(ctx context.Context) {
	// Snapshot current values and connections in a single lock acquisition.
	b.mu.Lock()
	current := b.current
	conns := make([]types.Connection, len(b.conns))
	copy(conns, b.conns)
	b.mu.Unlock()

	cfg, err := RenderCollectorConfig(current)
	if err != nil {
		log.Printf("failed to render collector config: %v", err)
		return
	}

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

	for _, conn := range conns {
		sendCtx, sendCancel := context.WithTimeout(ctx, 5*time.Second)
		if err := conn.Send(sendCtx, msg); err != nil {
			log.Printf("failed to send config to agent: %v", err)
		}
		sendCancel()
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
