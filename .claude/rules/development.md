# Development Gotchas

## Testing
- Disable OTEL in `conftest.py`: call `disable_telemetry_for_tests()` and `disable_metrics_for_tests()` from `lib.telemetry` / `lib.otel_metrics`
- Verify metrics: `@patch("services.<svc>.metrics.<metric_name>")`
- After `.proto` changes: `make protos` then `make test`

## Metrics
- Import from `lib.otel_metrics` (NOT `prometheus_client` directly)
- Naming: prefix with service (`game_`, `controller_`), counters end `_total`, histograms end `_seconds`/`_bytes`
- Standard process metrics: `process_cpu_seconds_total`, `process_resident_memory_bytes`, `process_threads`
- Tracing spans: dot notation (`game.process_death`, `controller.poll_batch`)

## Adding a Game Mode
1. Create `services/game_coordinator/games/<mode>.py`, extend `BaseGameMode` or `TeamsGameBase`
2. Register in `games/__init__.py` + add to `lib/types.py` `Games` enum
3. Override: `get_game_name()`, `_kill_player_impl()`, `_check_win_condition()`, `_get_death_thresholds()`
4. Optional: add config to `game_coordinator.proto`, update `GameFactory._extract_mode_config()` + `MenuServicer._build_game_config()`

## Service Layout
Each service: `server.py` (entry), `servicer.py` (gRPC impl), `metrics.py`, `tests/`, `pyproject.toml`

## Observability
- Stack at `http://localhost:8080/`: `/jaeger/`, `/grafana/`, `/prometheus/`
- Dashboards: `services/grafana/dashboards/`
- Alerts: `services/prometheus/alerts.yml`
