# Architecture

Source of truth: `docs/specs/services.md` (topology, every service and port, all RPCs, dependencies),
`docs/entity_model.md` (states), `docs/specs/flags.md`, `docs/specs/telemetry.md`. Read the relevant
section there instead of relying on memory; this file only holds the orientation.

- Core gRPC chain: audio and controller-manager (no app deps) → game-coordinator → menu. The Go agent
  observes via OTLP and acts only through flagd flags (plus shadow games); flagd serves flags to all services.
- Streams (bidi): `StreamButtonEvents` (buttons + LEDs), `StreamGameplayData` (motion + effects).
  Server-stream: `StreamGameEvents`, `StreamMenuEvents`.
- Menu controller states: CONNECTED (dim LED) → READY (bright LED), plus ADMIN and IDLE; disconnected
  controllers are removed. Game: IDLE → STARTING → RUNNING → ENDING → ENDED.
- Motion: HID ≈ 1 kHz → controller-manager state loop fixed at 100 Hz → gameplay stream 60 Hz by flag
  (`game_loop.update_frequency_hz`, fallback 30 Hz); no idle stream. EMA filtering and death thresholds
  (sensitivity 0-4) live in the game coordinator.
