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
      // Proxy VictoriaMetrics queries (agent applied-intervention count, #792)
      // through envoy, which routes /victoria-metrics/* to the VM HTTP API. This
      // only matters for `vite dev`; in production the dashboard is served behind
      // the same envoy.
      '/victoria-metrics': {
        target: 'http://localhost:80',
        changeOrigin: true,
      },
      // Operator unpair (UC-005) through envoy -> connect-proxy. Dev-only, as above.
      // changeOrigin stays false: the proxy's same-origin check compares the
      // browser's Origin with the Host header.
      '/pairing': {
        target: 'http://localhost:80',
        changeOrigin: false,
      },
      // Proxy Jaeger HTTP API queries (agent.decide feed, #1233) through envoy,
      // which routes /jaeger/* to the Jaeger query service. Dev-only, as above.
      '/jaeger': {
        target: 'http://localhost:80',
        changeOrigin: true,
      },
    },
  },
})
