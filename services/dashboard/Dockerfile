# Dashboard Dockerfile
# Multi-stage build: Node for build, static-web-server for serving
#
# static-web-server is a lightweight (~6MB) Rust-based HTTP server
# with built-in SPA fallback support via environment variables

# Stage 1: Build the TypeScript application
FROM node:24-alpine@sha256:8a32c8cf649a49915a66e81e7a609223c708d0adabf288ee3f4077d765542f85 AS builder

WORKDIR /app

# Copy package files first (layer cache); the lock is required (C-046)
COPY services/dashboard/package.json services/dashboard/package-lock.json ./

# Install exactly the locked dependencies; fails when the lock and package.json differ
RUN npm ci

# Copy source code
COPY services/dashboard/ ./

# Build the application
RUN npm run build

# Stage 2: Serve with static-web-server
FROM joseluisq/static-web-server:2-alpine@sha256:2fe3e5c0a20691f23fb270338578c8d31b5f7a19abb308c4c1ec901a7eefbe2e

# Copy built files to the public directory
COPY --from=builder /app/dist /public

# Configure static-web-server via environment variables
ENV SERVER_PORT=8080
ENV SERVER_ROOT=/public
ENV SERVER_FALLBACK_PAGE=/public/index.html
ENV SERVER_COMPRESSION=true

EXPOSE 8080

# Health check - optimized for fast startup (static files serve immediately)
HEALTHCHECK --interval=2s --timeout=2s --start-period=1s --retries=3 \
    CMD wget -q -O /dev/null http://localhost:8080/ || exit 1
