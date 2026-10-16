# Development Gotchas

Exact contracts live in `docs/specs/` (services, flags, telemetry, decide contract); check there first.

## Testing
- Disable OTEL in `conftest.py`: call `disable_telemetry_for_tests()` and `disable_metrics_for_tests()` from `lib.telemetry` / `lib.otel_metrics`
- Verify metrics: `@patch("services.<svc>.metrics.<metric_name>")`
- Mock collaborators with `create_autospec` / `spec_set`, not a bare `MagicMock` (#1272)
- Tag new tests with the spec unit they cover: `@pytest.mark.uc("UC-040", flow="A2")` / Go `// UC: UC-040 A2` (see `.claude/skills/README.md`)
- After `.proto` changes: `make protos` then `make test`

## Metrics and spans
- Import from `lib.otel_metrics` (NOT `prometheus_client` directly)
- Naming: prefix with service (`game_`, `controller_`, `menu_`, `audio_`, `agent_`), counters end `_total`, histograms end `_seconds`/`_bytes`
- New spans: dot notation (`controller.discover`, `agent.decide`); existing snake_case spans and other known violations are listed in `docs/specs/telemetry.md` §8: do not copy them
- A new metric, span or attribute needs its row in `docs/specs/telemetry.md` first

## Adding a Game Mode
1. Spec first: the mode's use case (UC-020..026 pattern) and requirements
2. Create `services/game_coordinator/games/<mode>.py`, extend `BaseGameMode` or `TeamsGameBase`
3. Add it to the `Games` enum in `lib/types.py` and to `_GAME_CLASSES` in `services/game_coordinator/game_factory.py` (`games/__init__.py` is empty; the menu's mode list derives from the enum)
4. Override `get_game_name()`, `_kill_player_impl()`, `_check_win_condition()`; death thresholds come from `_compute_effective_thresholds()` in `games/base.py`
5. Optional: add config to `game_coordinator.proto`, update `GameFactory._extract_mode_config()` + `MenuServicer._build_game_config()`

## Service Layout
Python services: `server.py` (entry), `servicer.py` (gRPC impl), `metrics.py`, `tests/`, `pyproject.toml`

## Observability
- Envoy on host port 80: `http://localhost/` (dashboard), `/jaeger/`, `/grafana/`, `/prometheus/`
- Dashboards: `services/grafana/dashboards/`
- Alerts: `services/prometheus/alerts.yml`
