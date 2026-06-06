# JoustMania Development Guide

**Getting started with JoustMania development**

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Quick Start](#quick-start)
3. [Development Workflow](#development-workflow)
4. [Building Services](#building-services)
5. [Running Services](#running-services)
6. [Testing](#testing)
7. [Debugging](#debugging)
8. [Code Organization](#code-organization)
9. [Adding New Features](#adding-new-features)
10. [Best Practices](#best-practices)

---

## Prerequisites

### Required Software

- **Docker** (20.10+)
- **Docker Compose** (v2.0+)
- **Git**
- **Python** 3.11+ (for local development)
- **grpcurl** (for API testing)

### Optional Software

- **uv** (Python package manager)
- **make** (for convenience commands)
- **VS Code** (recommended IDE)

### Hardware (Optional)

- **PS Move Controllers** (for hardware testing)
- **USB Bluetooth Adapter** (class 1 recommended)
- **Raspberry Pi** or **Linux machine** (for full hardware testing)

### Installation

#### Docker

```bash
# Linux (using convenience script)
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER

# Log out and back in for group changes to take effect
```

#### grpcurl

```bash
# Linux
sudo apt-get install grpcurl

# macOS
brew install grpcurl

# Or download binary from https://github.com/fullstorydev/grpcurl/releases
```

#### uv (Python Package Manager)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

---

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/WatchMeJoustMyFlags/JoustMania.git
cd JoustMania
```

### 2. Build Docker Images

```bash
docker compose build --parallel
```

### 3. Start the Stack

```bash
docker compose up -d
```

> **Note:** By default, `docker compose up` uses the pinned release version from `.env` (e.g., `0.2.0`).
> For development with the latest builds, override with:
> ```bash
> IMAGE_TAG=latest docker compose up -d
> ```
> Or set `IMAGE_TAG=latest` in your local `.env` (don't commit this change).

### 4. Verify Services

```bash
# Check service status
docker compose ps

# Should show all services running (or Up)
```

### 5. Access UIs

All UIs are accessible through the unified dashboard at **http://localhost:8080**:

- **Dashboard:** http://localhost:8080
- **Jaeger:** http://localhost:8080/jaeger/
- **Grafana:** http://localhost:8080/grafana/
- **Prometheus:** http://localhost:8080/prometheus/

### 6. View Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f controller-manager
```

### 7. Stop the Stack

```bash
docker compose down
```

---

## Development Workflow

### Typical Development Cycle

```mermaid
graph LR
    A[Edit Code] --> B[Rebuild Service]
    B --> C[Restart Service]
    C --> D[Test Changes]
    D --> E{Working?}
    E -->|No| A
    E -->|Yes| F[Commit]
    F --> G[Push]
```

### Hot Reloading

Currently, services require rebuild after code changes. Future: Add volume mounts for hot reloading.

### Development Commands

```bash
# Rebuild specific service
docker compose build game-coordinator

# Restart specific service
docker compose restart game-coordinator

# Rebuild and restart
docker compose up -d --build game-coordinator

# View logs in real-time
docker compose logs -f game-coordinator
```

---

## Building Services

### Build All Services

```bash
docker compose build --parallel
```

### Build Single Service

```bash
docker compose build <service-name>
```

**Service names:**
- `controller-manager`
- `game-coordinator`
- `menu`
- `audio`
- `dashboard`

### Build Options

```bash
# No cache (clean build)
docker compose build --no-cache game-coordinator

# Pull latest base images
docker compose build --pull game-coordinator
```

### Understanding Multi-Stage Builds

Services use multi-stage Docker builds:

```dockerfile
# Stage 1: Build dependencies
FROM python:3.11-slim AS builder
RUN pip install uv
COPY pyproject.toml .
RUN uv sync

# Stage 2: Runtime
FROM python:3.11-slim
COPY --from=builder /app/.venv /app/.venv
COPY . .
CMD ["python", "server.py"]
```

**Benefits:**
- Smaller final images
- Faster builds (cached layers)
- Clean separation of build/runtime dependencies

---

## Running Services

### Run All Services

```bash
docker compose up -d
```

### Run Specific Services

```bash
# Only infrastructure
docker compose up -d jaeger otel-collector

# Only application services
docker compose up -d controller-manager game-coordinator menu
```

### Run in Foreground (with logs)

```bash
docker compose up
```

### Scale Services

```bash
# Run multiple game coordinators
docker compose up -d --scale game-coordinator=3
```

### Override Configuration

```bash
# Use custom compose file
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

---

## Testing

### Unit Tests

Each service has its own tests. Run from within the service directory:

```bash
# Run tests for a service
cd services/<service-name>
uv run pytest

# Run with verbose output
uv run pytest -v

# Run with coverage
uv run pytest --cov=.
```

### Integration Tests

Integration tests use Docker Compose:

```bash
# Run full integration test suite
make test

# Keep containers running after tests (for debugging)
SKIP_TEARDOWN=1 make test

# Clean up after SKIP_TEARDOWN
docker compose -f docker-compose.test.yml down
```

### Testing with grpcurl

#### List Services

```bash
grpcurl -plaintext localhost:50052 list
# Output: joustmania.ControllerManagerService
```

#### Test All Services

```bash
# ControllerManager (50052)
grpcurl -plaintext localhost:50052 list

# GameCoordinator (50053)
grpcurl -plaintext localhost:50053 list

# Menu (50054)
grpcurl -plaintext localhost:50054 list

# Audio (50056)
grpcurl -plaintext localhost:50056 list
```

### Hardware Testing

```bash
# Test controller pairing
python tools/manualpair.py

# Clear paired devices
python tools/clear_devices.py
```

---

## Debugging

### View Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f game-coordinator

# Last N lines
docker compose logs --tail=100 game-coordinator

# With timestamps
docker compose logs -f -t game-coordinator
```

### Access Container

```bash
# Get shell in running container
docker compose exec game-coordinator bash

# Or if bash not available
docker compose exec game-coordinator sh

# Run command in container
docker compose exec game-coordinator python -c "import sys; print(sys.version)"
```

### Inspect Container

```bash
# View container details
docker inspect joustmania-game-coordinator

# View environment variables
docker inspect joustmania-game-coordinator | jq '.[0].Config.Env'

# View mounts
docker inspect joustmania-game-coordinator | jq '.[0].Mounts'
```

### Debug gRPC Issues

```bash
# Enable gRPC debug logging
docker compose exec game-coordinator python -c "
import grpc
import logging
logging.basicConfig(level=logging.DEBUG)
"

# Test connection
grpcurl -plaintext -v localhost:50052 list
```

### View Traces in Jaeger

1. Open http://localhost:8080/jaeger/
2. Select service from dropdown (e.g., "game-coordinator-service")
3. Click "Find Traces"
4. Click on trace to see details

**Useful filters:**
- Service: `game-coordinator-service`
- Operation: `joustmania.GameCoordinatorService/StreamGameEvents`
- Min Duration: `100ms` (find slow requests)
- Tags: `error=true` (find failures)

### Debug OpenTelemetry

```bash
# Check OTel Collector health
curl http://localhost:13133/

# View collector logs
docker compose logs -f otel-collector

# Test OTLP endpoint
grpcurl -plaintext localhost:4317 list
```

### Debug Network Issues

```bash
# Test service connectivity
docker compose exec game-coordinator nc -zv controller-manager 50052

# View network
docker network ls
docker network inspect joustmania_default

# DNS resolution
docker compose exec game-coordinator nslookup game-coordinator
```

### Performance Profiling

```bash
# Python profiling
docker compose exec game-coordinator python -m cProfile -s cumtime server.py

# Memory profiling
pip install memory_profiler
python -m memory_profiler services/game_coordinator/server.py
```

---

## Code Organization

### Project Structure

```
JoustMania/
├── lib/                     # Shared libraries
│   ├── colors.py           # Color constants
│   ├── types.py            # Game enums, types
│   ├── telemetry.py        # OpenTelemetry setup
│   ├── otel_metrics.py     # Metrics helpers
│   ├── grpc_tracing.py     # gRPC interceptors
│   └── feature_flags.py    # Feature flag client
├── proto/                   # Protocol buffer definitions
│   ├── controller_manager.proto
│   ├── game_coordinator.proto
│   ├── menu.proto
│   └── audio.proto
├── services/                # Microservices
│   ├── controller_manager/
│   ├── game_coordinator/
│   ├── menu/
│   ├── audio/
│   ├── dashboard/          # Web UI + reverse proxy
│   └── grafana/            # Dashboards
├── tests/                   # Integration tests
│   └── integration/
├── scripts/                 # Helper scripts
│   ├── ci/                 # CI/CD scripts
│   ├── setup/              # Installation scripts
│   └── pairing-daemon/     # PS Move pairing daemon
├── tools/                   # Development tools
│   ├── manualpair.py       # Manual controller pairing
│   └── live_dashboard.py   # Performance monitoring
├── docs/                    # Documentation
├── audio/                   # Audio files
├── docker-compose.yml
└── README.md
```

### Service Structure

Each service follows this pattern:

```
services/<service-name>/
├── server.py               # Entry point, gRPC server setup
├── servicer.py             # gRPC servicer implementation
├── metrics.py              # Prometheus/OTEL metrics
├── Dockerfile              # Multi-stage build
├── pyproject.toml          # Python dependencies (uv managed)
├── tests/                  # Unit tests
│   ├── conftest.py
│   └── test_*.py
└── README.md               # Service documentation
```

Proto files are centralized in the `proto/` directory.

### Protobuf Code Generation

Proto files are in the `proto/` directory. After modifying any `.proto` file:

```bash
# Regenerate all Python bindings
make protos

# Verify changes
git diff proto/
```

Generated files (`*_pb2.py`, `*_pb2_grpc.py`) are committed to the repo.

### Import Conventions

```python
# Shared libraries
from lib import colors
from lib.types import Games
from lib.telemetry import setup_tracing
from lib.otel_metrics import Counter, Gauge

# Proto definitions
from proto import controller_manager_pb2
from proto import game_coordinator_pb2

# gRPC
import grpc

# OpenTelemetry
from opentelemetry import trace
```

---

## Adding New Features

### Adding a New Service

1. **Create Service Directory**

```bash
mkdir -p services/newservice
cd services/newservice
```

2. **Define Protobuf Schema**

```protobuf
// newservice.proto
syntax = "proto3";
package joustmania;

service NewService {
  rpc DoSomething(DoSomethingRequest) returns (DoSomethingResponse);
}

message DoSomethingRequest {
  string param = 1;
}

message DoSomethingResponse {
  bool success = 1;
  string message = 2;
}
```

3. **Generate Python Code**

```bash
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. newservice.proto
```

4. **Implement Server**

```python
# server.py
import asyncio
import logging
import grpc.aio
from proto import newservice_pb2, newservice_pb2_grpc

class NewServiceServicer(newservice_pb2_grpc.NewServiceServicer):
    async def DoSomething(self, request, context):
        # Implementation
        return newservice_pb2.DoSomethingResponse(
            success=True,
            message=f"Processed: {request.param}"
        )

async def serve():
    server = grpc.aio.server()
    newservice_pb2_grpc.add_NewServiceServicer_to_server(
        NewServiceServicer(), server
    )
    server.add_insecure_port('[::]:50057')
    await server.start()
    await server.wait_for_termination()

if __name__ == '__main__':
    asyncio.run(serve())
```

5. **Create Dockerfile**

```dockerfile
FROM python:3.11-slim AS builder
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml .
RUN uv sync --frozen

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY . .
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
```

6. **Add to docker-compose.yml**

```yaml
newservice:
  build: ./services/newservice
  container_name: joustmania-newservice
  ports:
    - "50057:50057"
  environment:
    - OTEL_SERVICE_NAME=joustmania-newservice
    - OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
  depends_on:
    - otel-collector
  restart: unless-stopped
```

7. **Test the Service**

```bash
# Build
docker compose build newservice

# Start
docker compose up -d newservice

# Test with grpcurl
grpcurl -plaintext localhost:50057 list
grpcurl -plaintext -d '{"param":"test"}' \
    localhost:50057 joustmania.NewService/DoSomething
```

### Adding a New RPC to Existing Service

1. Update `.proto` file
2. Regenerate Python code
3. Implement new method in Servicer class
4. Rebuild service
5. Test with grpcurl

### Adding a New Game Mode

See `services/game_coordinator/games/README.md` for the game mode implementation guide.

---

## Best Practices

### Code Style

- **Python:** Follow PEP 8
- **Docstrings:** Use Google style
- **Type hints:** Use where appropriate
- **Linting:** Use `ruff` (run `make lint`)
- **Formatting:** Use `ruff format` (run `make format`)

### gRPC Best Practices

1. **Use streaming for real-time data**
   ```python
   def StreamData(self, request, context):
       while True:
           yield DataResponse(...)
   ```

2. **Handle errors properly**
   ```python
   try:
       # ...
   except Exception as e:
       context.set_code(grpc.StatusCode.INTERNAL)
       context.set_details(str(e))
       return ErrorResponse()
   ```

3. **Add deadlines/timeouts**
   ```python
   with grpc.insecure_channel('localhost:50053') as channel:
       stub = GameCoordinatorStub(channel)
       response = stub.GetGameState(request, timeout=5.0)
   ```

### OpenTelemetry Best Practices

1. **Add spans for critical operations**
   ```python
   from opentelemetry import trace

   tracer = trace.get_tracer(__name__)

   with tracer.start_as_current_span("operation_name") as span:
       span.set_attribute("key", "value")
       # Do work
   ```

2. **Propagate context**
   ```python
   # Context automatically propagated in gRPC
   ```

3. **Add meaningful attributes**
   ```python
   span.set_attribute("setting.key", key)
   span.set_attribute("setting.value", value)
   span.set_attribute("validation.result", "success")
   ```

### Docker Best Practices

1. **Use multi-stage builds**
2. **Don't run as root** (add USER in Dockerfile)
3. **Use `.dockerignore`**
4. **Pin base image versions**
5. **Minimize layers**

### Testing Best Practices

1. **Write unit tests for business logic**
2. **Write integration tests for gRPC APIs**
3. **Mock external dependencies**
4. **Test error cases**
5. **Use fixtures for common setup**

---

## Troubleshooting

### Common Issues

#### Services won't start

```bash
# Check logs
docker compose logs <service>

# Check if ports are in use
sudo netstat -tulpn | grep 5005

# Rebuild from scratch
docker compose down
docker compose build --no-cache
docker compose up -d
```

#### gRPC connection refused

```bash
# Verify service is running
docker compose ps

# Test with grpcurl
grpcurl -plaintext localhost:50053 list

# Check service logs for errors
docker compose logs game-coordinator
```

#### Permission denied (hardware access)

```bash
# Ensure containers are privileged
# Check docker-compose.yml for:
privileged: true
devices:
  - /dev/bus/usb:/dev/bus/usb
```

#### Traces not appearing in Jaeger

```bash
# Check OTel Collector
docker compose logs otel-collector

# Verify OTLP endpoint
docker compose exec game-coordinator env | grep OTEL

# Test trace export
grpcurl -plaintext localhost:4317 list
```

---

## Resources

- [Architecture Documentation](ARCHITECTURE.md)
- [gRPC Python Guide](https://grpc.io/docs/languages/python/)
- [OpenTelemetry Python](https://opentelemetry.io/docs/instrumentation/python/)
- [Docker Compose Reference](https://docs.docker.com/compose/compose-file/)
- [grpcurl Documentation](https://github.com/fullstorydev/grpcurl)

---

## Getting Help

- **Issues:** https://github.com/WatchMeJoustMyFlags/JoustMania/issues
- **Discussions:** https://github.com/WatchMeJoustMyFlags/JoustMania/discussions
- **Documentation:** Browse `docs/` directory

---

Happy coding! 🎮
