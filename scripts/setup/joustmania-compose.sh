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
# - Pin (UC-060 BR-003, C-040): when the checkout is pinned (detached HEAD at a
#   release tag or a commit), IMAGE_TAG is derived from HEAD (X.Y.Z of the
#   joustmania-vX.Y.Z tag, else the 40-character sha) and exported, so it wins
#   over .env, .env.local and the shell. On a branch nothing is derived.
# Never prints the env files or their values. Never run `config` without
# `--services`/`--images`/`-q` where output is logged; it prints interpolated
# secrets.
set -euo pipefail

JOUSTMANIA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$JOUSTMANIA_DIR"

if [[ -f scripts/setup/lib-joustmania.sh ]]; then
    # shellcheck source=lib-joustmania.sh
    source scripts/setup/lib-joustmania.sh
    if PIN_IMAGE_TAG="$(joustmania_pin_image_tag "$JOUSTMANIA_DIR")"; then
        export IMAGE_TAG="$PIN_IMAGE_TAG"
    fi
fi

COMPOSE_FILES=(-f docker-compose.yml)
[[ -f .env ]] && COMPOSE_FILES+=(--env-file .env)
[[ -f .env.local ]] && COMPOSE_FILES+=(--env-file .env.local)

exec docker compose "${COMPOSE_FILES[@]}" "$@"
