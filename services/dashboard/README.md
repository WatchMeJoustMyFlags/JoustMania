# Dashboard

Real-time controller visualization and game control web UI.

## Overview

The Dashboard is a single-page application that displays live controller state, game events, and embedded Grafana/Jaeger views. It connects to backend gRPC services through the Connect Proxy using the Connect protocol over HTTP/1.1.

Built with TypeScript and Vite, served in production by [static-web-server](https://github.com/static-web-server/static-web-server) (a lightweight Rust-based HTTP server).

## Features

- **Controller grid** -- live LED color, battery level, and accelerometer dot visualization for each connected controller
- **Acceleration waveform** -- canvas-based 10-second scrolling waveform with warning/danger threshold lines
- **Game controls** -- mode selector dropdown, start/stop buttons
- **Game event log** -- recent events (game start, player deaths, winner) in the status bar
- **Embedded observability** -- tabbed Grafana dashboard and Jaeger trace views via iframes

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
    ├── main.ts           # Entry point, streaming loops, event handling
    ├── style.css
    ├── client.ts         # Connect protocol client (fetch-based)
    ├── components/
    │   ├── AccelWaveform.ts   # Canvas waveform with ring buffer
    │   ├── ControllerCard.ts  # Single controller card (LED, battery, accel dot)
    │   ├── ControllerGrid.ts  # Grid layout managing controller cards
    │   ├── Controls.ts        # Game mode selector and start/stop buttons
    │   └── GameStatus.ts      # Status bar (state, counts, event log)
    └── gen/                   # Generated proto TypeScript types
```

## See Also

- [Connect Proxy](../connect-proxy/) -- gRPC-to-Connect bridge the dashboard talks through
- [Architecture](../../docs/ARCHITECTURE.md)
