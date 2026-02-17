<p align="center">
  <img src="logo/joustmania2.png" alt="JoustMania" width="200">
</p>

<h1 align="center">JoustMania</h1>

<p align="center">
  <a href="https://github.com/WatchMeJoustMyFlags/JoustMania/actions/workflows/ci.yml"><img src="https://github.com/WatchMeJoustMyFlags/JoustMania/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://sonarcloud.io/summary/new_code?id=WatchMeJoustMyFlags_JoustMania"><img src="https://sonarcloud.io/api/project_badges/measure?project=WatchMeJoustMyFlags_JoustMania&metric=alert_status" alt="Quality Gate Status"></a>
  <a href="https://sonarcloud.io/summary/new_code?id=WatchMeJoustMyFlags_JoustMania"><img src="https://sonarcloud.io/api/project_badges/measure?project=WatchMeJoustMyFlags_JoustMania&metric=coverage" alt="Coverage"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue.svg" alt="Python 3.11+"></a>
</p>

**Microservices-based motion gaming platform for PlayStation Move controllers.**

A cloud-native refactor of the [original JoustMania](https://github.com/adangert/JoustMania) party game system, rebuilt with modern observability practices. Ideal for learning distributed systems, gRPC, and OpenTelemetry.

![JoustMania at Magfest](logo/magfest.jpg)

## Quick Start

### With PS Move Controllers (Raspberry Pi / Linux)

```bash
git clone https://github.com/WatchMeJoustMyFlags/JoustMania.git
cd JoustMania
./setup.sh    # Install dependencies, Bluetooth config, pairing daemon
```

The setup script will guide you through installation and optionally enable autostart on boot.

### Demo Mode (No Hardware)

```bash
git clone https://github.com/WatchMeJoustMyFlags/JoustMania.git
cd JoustMania
make up-mock
```

**Open the dashboard:** http://localhost:8080

| Interface | URL | Purpose |
|-----------|-----|---------|
| Dashboard | http://localhost:8080 | Main UI, controller visualization |
| Jaeger | http://localhost:8080/jaeger/ | Distributed tracing |
| Prometheus | http://localhost:8080/prometheus/ | Metrics |
| Grafana | http://localhost:8080/grafana/ | Dashboards (admin/joustmania) |

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
│                        Dashboard (:8080)                        │
│         Unified entry point with reverse proxy routing          │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
      ┌───────────────┐ ┌───────────┐ ┌───────────────┐
      │     Menu      │ │   Game    │ │  Controller   │
      │    :50054     │ │Coordinator│ │   Manager     │
      └───────────────┘ │  :50053   │ │    :50052     │
                        └───────────┘ └───────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
      ┌───────────────┐ ┌───────────┐ ┌───────────────┐
      │     Audio     │ │   flagd   │ │ Observability │
      │    :50056     │ │  :8015    │ │ Jaeger/Prom   │
      └───────────────┘ └───────────┘ │ Grafana/Loki  │
                                      └───────────────┘
```

**For detailed architecture:** See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

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
| [Architecture](docs/ARCHITECTURE.md) | System design and service interactions |
| [Development](docs/DEVELOPMENT.md) | Building, running, debugging |
| [Contributing](docs/CONTRIBUTING.md) | Code style, PR workflow |
| [Controller Guide](docs/controller-guide.md) | Button layout, admin mode |
| [LED Feedback](docs/controller-feedback.md) | Controller LED color reference |
| [Observability](docs/observability-quickstart.md) | Tracing, metrics, dashboards |

## Services

| Service | Port | Purpose |
|---------|------|---------|
| Controller Manager | 50052 | PS Move I/O and pairing |
| Game Coordinator | 50053 | Game lifecycle management |
| Menu | 50054 | Menu navigation |
| Audio | 50056 | Audio playback and mixing |
| Dashboard | 8080 | Web UI and reverse proxy |
| Connect Proxy | - | gRPC-web bridge |
| flagd | 8015 | Feature flags (OpenFeature) |

## Technology Stack

- **Language:** Python 3.11+
- **Communication:** gRPC with Protocol Buffers
- **Observability:** OpenTelemetry, Jaeger, Prometheus, Grafana
- **Infrastructure:** Docker, Docker Compose

## Credits

- **[Adam Engert](https://github.com/adangert)** - Original JoustMania creator
- **[Original JoustMania](https://github.com/adangert/JoustMania)** - The game this fork is based on
- **[Steam Release](https://store.steampowered.com/app/1093850/JoustMania/)** - Original game on Steam

## License

MIT License (code) + CC BY-SA 4.0 (audio assets). See [LICENSE](LICENSE) for details.

## Links

- [Issues](https://github.com/WatchMeJoustMyFlags/JoustMania/issues)
- [Discussions](https://github.com/WatchMeJoustMyFlags/JoustMania/discussions)
