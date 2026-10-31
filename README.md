<p align="center">
  <img src="logo/joustmania2.png" alt="JoustMania" width="200">
</p>

<h1 align="center">JoustMania</h1>

<p align="center">
  <a href="https://github.com/WatchMeJoustMyFlags/JoustMania/actions/workflows/ci.yml"><img src="https://github.com/WatchMeJoustMyFlags/JoustMania/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://sonarcloud.io/summary/new_code?id=WatchMeJoustMyFlags_JoustMania"><img src="https://sonarcloud.io/api/project_badges/measure?project=WatchMeJoustMyFlags_JoustMania&metric=alert_status" alt="Quality Gate Status"></a>
  <a href="https://sonarcloud.io/summary/new_code?id=WatchMeJoustMyFlags_JoustMania"><img src="https://sonarcloud.io/api/project_badges/measure?project=WatchMeJoustMyFlags_JoustMania&metric=coverage" alt="Coverage"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.14-blue.svg" alt="Python 3.14"></a>
</p>

**Microservices-based motion gaming platform for PlayStation Move controllers.**

A cloud-native refactor of the [original JoustMania](https://github.com/adangert/JoustMania) party game system, rebuilt with modern observability practices. Ideal for learning distributed systems, gRPC, and OpenTelemetry.

![JoustMania at Magfest](logo/magfest.jpg)

## Quick Start

### With PS Move Controllers (Raspberry Pi / Linux)

```bash
git clone https://github.com/WatchMeJoustMyFlags/JoustMania.git
cd JoustMania
./setup.sh    # Docker, Bluetooth config; then .env.local, autostart and the pinned version
```

The setup script guides you through the installation, then offers a private `.env.local`, autostart on boot and pins the checkout to a release (or an exact commit) with its images pulled, so the Pi also starts without a network ([UC-060](docs/use_cases/UC-060.md) A6). The autostart runs the full stack; the lite stack is for manual tests only.

### Demo Mode (No Hardware)

```bash
git clone https://github.com/WatchMeJoustMyFlags/JoustMania.git
cd JoustMania
make up-mock
```

**Open the dashboard:** http://localhost

| Interface | URL | Purpose |
|-----------|-----|---------|
| Dashboard | http://localhost | Main UI, controller visualization |
| Jaeger | http://localhost/jaeger/ | Distributed tracing |
| VictoriaMetrics | http://localhost/victoria-metrics/vmui/ | Metrics (Prometheus query API) |
| Grafana | http://localhost/grafana/ | Dashboards (anonymous access enabled; no login configured) |

## Game Modes

- **Joust FFA** - Free-for-all elimination, last player standing wins
- **Joust Teams** - Team-based combat, last team standing wins
- **Joust Random Teams** - Randomized team assignment with formation phase
- **Swapper** - Killed players switch teams; ends when all on one team
- **Tournament** - Single elimination bracket with 1v1 matches
- **Fight Club** - 1v1 arena with winner-stays queue system
- **Werewolf** - Hidden role: secret werewolves revealed after countdown
- **Traitor** - Team game with secret traitors working for the enemy
- **Zombies** - Infection survival; killed humans become zombies
- **Non-Stop Joust** - Respawn-enabled combat with kill/death scoring

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Envoy (host port :80)                       │
│   Unified entry point; routes to the dashboard (:8080, internal)│
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
      ┌───────────────┐ ┌───────────┐ ┌───────────────┐
      │     Menu      │ │   Game    │ │  Controller   │
      │    :50054     │ │Coordinator│ │   Manager     │
      └───────────────┘ │  :50053   │ │ :50052        │
                        └───────────┘ │ -> rust-hid   │
                              │       │    :50058     │
                              │       └───────────────┘
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
      ┌───────────────┐ ┌───────────┐ ┌───────────────┐
      │     Audio     │ │   flagd   │ │ OTel Collector│
      │    :50056     │ │  :8015    │ │ -> Jaeger     │
      └───────────────┘ └───────────┘ │ VictoriaMetr. │
                              ▲       │ Grafana       │
                              │       └───────┬───────┘
                      ┌───────┴───────┐       │ OTLP
                      │ flag-service  │       ▼
                      │ :50064/:8070  │ ┌───────────────┐
                      └───────────────┘ │ Agent (Go)    │
                              ▲         │ AI game master│
                              └─────────┤ :4317 / :13134│
                      (interventions    └───────────────┘
                       via flags)
```

**For detailed architecture:** See [docs/specs/services.md](docs/specs/services.md)

## Development

```bash
make lint          # Lint code
make format        # Format code
make test          # Run integration tests
```

**Dev Container:** Open in VS Code and click "Reopen in Container" for a pre-configured environment.

See [CONTRIBUTING.md](docs/CONTRIBUTING.md) for the full development guide.

## Mock Environment

For development without PS Move controllers, use mock mode (see Quick Start above).

See [Mock Environment Guide](services/controller_manager/MOCK_ENVIRONMENT.md) for simulating controllers and running integration tests.

## Documentation

| Document | Description |
|----------|-------------|
| [Services spec](docs/specs/services.md) | Services, ports, RPCs and service interactions |
| [Docs index](docs/README.md) | Entry point to the specs: requirements, use cases, technical specs |
| [Northstar](docs/NORTHSTAR.md) | The "dark factory" goal and its hard constraints |
| [Operator pad](docs/use_cases/UC-015.md) | USB macro pad as a menu remote (UC-015) |
| [Development](docs/DEVELOPMENT.md) | Building, running, debugging |
| [Contributing](docs/CONTRIBUTING.md) | Code style, PR workflow |
| [Ready up and admin mode](docs/use_cases/UC-010.md) | Button map (UC-010 BR-005, UC-012 BR-005), admin mode |
| [LED states](docs/specs/services.md#led-states-controller-feedback) | Lobby, game and admin LED colours and effects |
| [Observability](docs/observability-quickstart.md) | Tracing, metrics, dashboards |

## Services

| Service | Port | Purpose |
|---------|------|---------|
| Controller Manager | 50052 | PS Move I/O and pairing |
| Game Coordinator | 50053 | Game lifecycle management |
| Menu | 50054 | Menu navigation |
| Audio | 50056 | Audio playback and mixing |
| Dashboard | 8080 (container-internal) | Web UI; reached via Envoy on host port 80 |
| Envoy | 80 (host) | Reverse proxy and single entry point |
| rust-hid | 50058 (loopback) | HID access and pairing; the default Bluetooth backend |
| Pairing Daemon | 8002 (host network) | USB pairing loop, Bluetooth monitor, unpair |
| Agent | 4317 (OTLP), 13134 (HTTP) | Go AI game master: observes via OTLP, acts only through flags |
| Operator Pad | - | Optional (`operator-pad` compose profile): USB macro pad as a menu remote ([UC-015](docs/use_cases/UC-015.md)) |
| Connect Proxy | 8080 (container-internal) | gRPC-web bridge |
| flagd | 8015 | Feature flags (OpenFeature) |
| Flag Service | 50064 (flagd sync), 8070 (HTTP API) | Runtime flag state; the only writer path for runtime flags |
| OTel Collector | 4317 / 4318 | Receives OTLP (logs end in a debug sink); exports to Jaeger, VictoriaMetrics and the agent |
| VictoriaMetrics | 8428 | Metric store (Prometheus query API) |

## Technology Stack

- **Languages:** Python 3.14 (game services), Go 1.26 (agent, flag-service, connect-proxy), Rust (rust-hid, edition 2024), TypeScript (dashboard)
- **Communication:** gRPC with Protocol Buffers
- **Observability:** OpenTelemetry, Jaeger, VictoriaMetrics, Grafana
- **Infrastructure:** Docker, Docker Compose

## Releases

The stack is released as versioned images on GHCR (`joustmania-vX.Y.Z` tags).

- On a Pi, `joustmania-upgrade joustmania-vX.Y.Z` switches to a release and rolls back if it is not healthy ([UC-060](docs/use_cases/UC-060.md)).
- Without a network at the venue, the Pi opens its own hotspot ([UC-061](docs/use_cases/UC-061.md)).

## Credits

- **[Adam Engert](https://github.com/adangert)** - Original JoustMania creator
- **[Original JoustMania](https://github.com/adangert/JoustMania)** - The game this fork is based on
- **[Steam Release](https://store.steampowered.com/app/1093850/JoustMania/)** - Original game on Steam
- **[psmoveapi](https://github.com/thp/psmoveapi)** by Thomas Perl - the PS Move support was originally built on it; its protocol documentation and reference implementation guided the direct HID implementation

## License

MIT License (code) + CC BY-SA 4.0 (audio assets). See [LICENSE](LICENSE) for details.

## Links

- [Issues](https://github.com/WatchMeJoustMyFlags/JoustMania/issues)
- [Discussions](https://github.com/WatchMeJoustMyFlags/JoustMania/discussions)
