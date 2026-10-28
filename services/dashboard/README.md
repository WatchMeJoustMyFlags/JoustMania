# Dashboard

Real-time controller visualization and game control web UI.

## Overview

The Dashboard is a single-page application that displays live controller state, game events, and embedded Grafana/Jaeger views. It connects to backend gRPC services through the Connect Proxy using the Connect protocol over HTTP/1.1.

Built with TypeScript and Vite, served in production by [static-web-server](https://github.com/static-web-server/static-web-server) (a lightweight Rust-based HTTP server).

## Features

- **Controller grid** -- live LED color, battery level, and accelerometer dot visualization for each connected controller
- **Game controls** -- mode selector dropdown, start/stop buttons
- **Game event log** -- recent events (game start, player deaths, winner) in the status bar
- **Embedded observability** -- tabbed Grafana dashboard and Jaeger trace views via iframes
- **Agent decisions** -- the Agent tab polls Jaeger (`/jaeger/api/traces?service=agent&operation=agent.decide`) every 1.5s and shows one row per `agent.decide` span: backend (requested vs used), model, answer, confidence against the act threshold, outcome, rationale and a trace link, plus a current-backend strip

## Configuration

The dashboard itself has no runtime configuration. Vite's dev server proxies `/joustmania` requests to `http://localhost:8080` (the connect-proxy). In production, Envoy handles routing.

| Property | Value |
|----------|-------|
| **Container port** | 8080 |
| **Container** | `joustmania-dashboard` |
| **Served by** | static-web-server (alpine) |
| **Memory limit** | 64 MB |

## Development

```bash
cd services/dashboard
npm install
npm run dev       # Vite dev server on http://localhost:5173
npm run build     # Production build to dist/
```

## Files

```
services/dashboard/
├── Dockerfile            # Multi-stage: node build + static-web-server
├── package.json
├── tsconfig.json
├── vite.config.ts        # Dev proxy to connect-proxy
└── src/
    ├── index.html        # SPA shell with tabs, controls, status bar
    ├── main.ts           # Entry point: component wiring, streams and pollers
    ├── style.css
    ├── client.ts         # Connect protocol client (fetch-based)
    ├── connect.ts        # Connect streaming framing
    ├── controllerStream.ts / stream.ts   # Reconnecting controller / game-event streams
    ├── poller.ts / pageGate.ts           # Single-chain pollers (backoff) and tab / page-visibility gate
    ├── jaeger.ts / vm.ts                 # Jaeger and VictoriaMetrics readers (no browser clock)
    ├── agent.ts          # agent.decide / flag_state / rollout spans and intervention counts
    ├── experiments.ts    # experiment.progress snapshots (ExperimentStore)
    ├── follow.ts         # Followed game (real, else live shadow)
    ├── degrade.ts        # Per-source degraded-state messages
    ├── adapters.ts / stageSettings.ts    # Adapter badges and stage settings strip (VictoriaMetrics)
    ├── shield.ts / gameEvents.ts / identify.ts / unpair.ts / modes.ts / motion.ts / quality.ts
    ├── components/
    │   ├── ControllerCard.ts  # Single controller card (LED, battery, accel dot)
    │   ├── ControllerGrid.ts  # Grid layout managing controller cards
    │   ├── Controls.ts        # Game mode selector and start/stop buttons
    │   ├── GameStatus.ts      # Status bar (state, counts, event log)
    │   ├── StageSettings.ts   # Stage settings strip
    │   ├── AgentView.ts       # Agent tab: decide feed, flag panel, overlay
    │   └── ExperimentsView.ts # Experiments tab cards
    └── gen/                   # Hand-written TS mirrors of the proto messages the client uses
```

## See Also

- [Connect Proxy](../connect-proxy/) -- gRPC-to-Connect bridge the dashboard talks through
- [Services spec](../../docs/specs/services.md)
