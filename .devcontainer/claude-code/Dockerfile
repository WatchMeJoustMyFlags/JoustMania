FROM python:3.14-slim

ARG GO_VERSION=1.25.1
ARG RUST_VERSION=1.88.0
ARG NODE_MAJOR=22

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# ── System packages ──────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    g++ \
    gcc \
    git \
    gnupg \
    libasound2-dev \
    libbluetooth-dev \
    libdbus-1-dev \
    libglib2.0-dev \
    libhidapi-dev \
    libhidapi-hidraw0 \
    libudev-dev \
    make \
    pkg-config \
    protobuf-compiler \
    sudo \
    unzip \
    wget \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js 22 ───────────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Go ───────────────────────────────────────────────────────────────
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-$(dpkg --print-architecture).tar.gz" \
    | tar -C /usr/local -xzf - \
    && ln -s /usr/local/go/bin/go /usr/local/bin/go \
    && ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt

# ── Rust (via rustup, non-root) ──────────────────────────────────────
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH="/usr/local/cargo/bin:$PATH"
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain ${RUST_VERSION} --component clippy \
    && chmod -R a+rwx $RUSTUP_HOME $CARGO_HOME

# ── uv (Python package manager) ─────────────────────────────────────
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

# ── buf CLI (protobuf) ──────────────────────────────────────────────
RUN BUF_ARCH=$(dpkg --print-architecture | sed 's/amd64/x86_64/' | sed 's/arm64/aarch64/') \
    && curl -fsSL "https://github.com/bufbuild/buf/releases/latest/download/buf-Linux-${BUF_ARCH}" \
       -o /usr/local/bin/buf \
    && chmod +x /usr/local/bin/buf

# ── GitHub CLI ───────────────────────────────────────────────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# ── Claude Code CLI ─────────────────────────────────────────────────
RUN npm install -g @anthropic-ai/claude-code

# ── Non-root user ───────────────────────────────────────────────────
RUN groupadd --gid 1000 claude \
    && useradd --uid 1000 --gid claude --shell /bin/bash --create-home claude \
    && echo "claude ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/claude

# ── Global git config ───────────────────────────────────────────────
RUN git config --system --add safe.directory '*'

USER claude
WORKDIR /workspace
