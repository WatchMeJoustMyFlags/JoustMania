#!/bin/bash
# `docker compose` exactly as the Pi runs it. Used by joustmania-start.sh, the
# systemd unit's ExecStop and the Pi helper scripts; run it by hand the same way:
#   ./scripts/setup/joustmania-compose.sh up -d agent
#
# - Compose file: docker-compose.yml only, named with -f. A bare
#   `docker compose` also auto-loads docker-compose.override.yml, the dev/test
#   file that publishes the mock control port (50062) and the gRPC ports on all
#   interfaces, i.e. to the venue network (#1319).
# - Env files: the tracked .env (release IMAGE_TAG), then the git-ignored
#   .env.local when it exists (stage IMAGE_TAG, decision-backend API keys,
#   agent overrides; later files win). Passing --env-file switches off compose's
#   implicit .env, so both are named. A reboot through systemd therefore comes
#   back exactly like the runbook's manual bring-up (#1488).
# Never prints the env files or their values. Never run `config` without
# `--services`/`--images`/`-q` where output is logged; it prints interpolated
# secrets.
set -euo pipefail

JOUSTMANIA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$JOUSTMANIA_DIR"

COMPOSE_FILES=(-f docker-compose.yml)
[[ -f .env ]] && COMPOSE_FILES+=(--env-file .env)
[[ -f .env.local ]] && COMPOSE_FILES+=(--env-file .env.local)

exec docker compose "${COMPOSE_FILES[@]}" "$@"
