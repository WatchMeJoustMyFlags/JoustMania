# Connect Proxy

gRPC-to-Connect protocol bridge for browser access to JoustMania services.

## Overview

The Connect Proxy is a Go service that translates between the [Connect protocol](https://connectrpc.com/) (HTTP/1.1 + JSON, used by the web dashboard) and the native gRPC services running in the cluster. It proxies requests to the Controller Manager, Game Coordinator, and Menu services, enabling the browser to use streaming RPCs that would otherwise require HTTP/2.

Written in Go with [Connect-Go](https://github.com/connectrpc/connect-go) and standard `net/http`.

## Proxied Services

| Backend | Default Address | RPCs |
|---------|-----------------|------|
| Controller Manager | `controller-manager:50052` | `StreamButtonEvents`, `StreamGameplayData`, `RenameController` |
| Game Coordinator | `game-coordinator:50053` | `StreamGameEvents`, `ForceEndGame`, `GetGameState` |
| Menu | `menu:50054` | `StartMenu`, `StopMenu`, `StreamMenuEvents`, `ProcessInput` |

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTROLLER_MANAGER_SERVICE` | `controller-manager:50052` | Controller Manager gRPC address |
| `GAME_COORDINATOR_SERVICE` | `game-coordinator:50053` | Game Coordinator gRPC address |
| `MENU_SERVICE` | `menu:50054` | Menu gRPC address |
| `LISTEN_ADDR` | `:8080` | HTTP listen address |

| Property | Value |
|----------|-------|
| **Container port** | 8080 |
| **Container** | `joustmania-connect-proxy` |
| **Health check** | `GET /health` |
| **Memory limit** | 128 MB |

## How It Works

Each backend service has a proxy struct (e.g., `ControllerManagerProxy`) that implements the Connect service interface. Unary RPCs forward the request and return the response. Streaming RPCs spawn goroutines to relay messages in both directions between the Connect stream and the gRPC stream.

CORS is enabled for all origins to allow browser requests from the dashboard.

## Files

```
services/connect-proxy/
├── Dockerfile    # Three-stage: buf proto gen, Go build, alpine runtime
├── main.go       # Proxy handlers, gRPC client setup, HTTP server
├── go.mod
└── go.sum
```

Proto Go bindings are generated at Docker build time from `proto/` using `buf generate`.

## See Also

- [Dashboard](../dashboard/) -- the web UI that talks through this proxy
- [Proto Definitions](../../proto/) -- source .proto files
- [Architecture](../../docs/ARCHITECTURE.md)
