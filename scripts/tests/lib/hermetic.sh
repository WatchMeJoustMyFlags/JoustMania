#!/usr/bin/env bash
# Hermetic guard for scripts/tests/test-*.sh (#2315). Source it near the top of every test:
#
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/hermetic.sh"
#
# It (1) puts the fail-loud docker stub (lib/fail-docker) first on PATH, so a test that reaches
# the real docker stack exits 97 with REAL-DOCKER-CALLED instead of spawning containers,
# (2) unsets COMPOSE / DOCKER (they would bypass the stub), and (3) points the flag and
# service URLs at a dead address (port 9, discard: nothing listens), so a live GET fails fast.
#
# A test that fakes docker itself prepends its own dir AFTER sourcing this
# (export PATH="${WORK}/bin:${PATH}") and wins; one that needs a live fake flag service
# overrides FLAG_SERVICE_URL after sourcing (lib-fake-flag-service.sh does).
# Opt-out (no expected user): a line `# hermetic: opt-out <reason>` in the test.

_HERMETIC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

unset COMPOSE DOCKER

case ":${PATH}:" in
  ":${_HERMETIC_DIR}/fail-docker:"*) ;;
  *) export PATH="${_HERMETIC_DIR}/fail-docker:${PATH}" ;;
esac

export FLAG_SERVICE_URL="http://127.0.0.1:9/flags"
export TALK_BASE_URL="http://127.0.0.1:9"
export JOUSTMANIA_REGISTRY_URL="http://127.0.0.1:9"
export REGISTRY_URL="http://127.0.0.1:9"
unset _HERMETIC_DIR
