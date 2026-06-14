import { defineConfig } from 'vite'

export default defineConfig({
  root: 'src',
  build: {
    outDir: '../dist',
    emptyOutDir: true,
    target: 'es2022',  // Support top-level await and modern JS features
  },
  server: {
    port: 5173,
    proxy: {
      // Proxy Connect-Web requests to the connect-proxy service
      '/joustmania': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
      // Proxy Loki queries (agent experiment telemetry, #981) through envoy,
      // which routes /loki/* to the Loki HTTP API (port 80 entry point). In
      // production the dashboard is served behind the same envoy, so this proxy
      // only matters for `vite dev`.
      '/loki': {
        target: 'http://localhost:80',
        changeOrigin: true,
      },
    },
  },
})
