#!/bin/bash
# Docker's default log limit on the Pi (UC-060 A6; #2027, audit finding 8).
#
#   sudo ./scripts/setup/docker-log-defaults.sh
#
# Every JoustMania service already sets its own log limit in the compose files;
# this is the host default for any other container (json-file, 10m x 3), so a
# chatty container never fills the SD card. Run by setup_runtime.sh.
#
# - No daemon.json: it is written with the defaults.
# - daemon.json exists: the defaults are merged in with jq and every key already
#   there wins (the operator's choice is kept). A log driver other than
#   json-file, invalid JSON or no jq: the file is left alone with a WARN; a
#   json-file option set for another driver would stop docker from starting.
# - Docker is restarted only when the file changed (the defaults apply to
#   containers created after that). The file is never overwritten in place:
#   written to a temporary file next to it, checked, then renamed.
#
# Test overrides: DOCKER_DAEMON_JSON (the file), JOUSTMANIA_DOCKER_RESTART=0
# (do not restart docker).
set -euo pipefail

DAEMON_JSON="${DOCKER_DAEMON_JSON:-/etc/docker/daemon.json}"
DEFAULTS='{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}'

say() { echo "  → $*"; }

restart_docker() {
    if [[ "${JOUSTMANIA_DOCKER_RESTART:-1}" == 0 ]]; then
        return 0
    fi
    say "restarting docker so the log defaults apply to new containers"
    systemctl restart docker || say "WARN: docker restart failed; restart it by hand: sudo systemctl restart docker"
}

if [[ ! -e "$DAEMON_JSON" ]]; then
    mkdir -p "$(dirname "$DAEMON_JSON")"
    tmp="$(mktemp "$DAEMON_JSON.XXXXXX")"
    printf '%s\n' "$DEFAULTS" >"$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$DAEMON_JSON"
    say "wrote $DAEMON_JSON: json-file logs, 10m x 3"
    restart_docker
    exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
    say "WARN: $DAEMON_JSON exists and jq is missing; log defaults not merged (add json-file, max-size 10m, max-file 3 by hand)"
    exit 0
fi
if ! jq -e 'type == "object"' "$DAEMON_JSON" >/dev/null 2>&1; then
    say "WARN: $DAEMON_JSON is not a JSON object; left alone, log defaults not set"
    exit 0
fi
driver="$(jq -r '."log-driver" // "json-file"' "$DAEMON_JSON")"
if [[ "$driver" != json-file ]]; then
    say "$DAEMON_JSON uses log driver '$driver'; left alone"
    exit 0
fi

tmp="$(mktemp "$DAEMON_JSON.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
# defaults * existing: a key already in the file wins, nested log-opts included
jq --argjson d "$DEFAULTS" '$d * .' "$DAEMON_JSON" >"$tmp"
if jq -e --slurpfile old "$DAEMON_JSON" '. == $old[0]' "$tmp" >/dev/null; then
    say "$DAEMON_JSON already sets the log defaults; unchanged"
    exit 0
fi
chmod --reference="$DAEMON_JSON" "$tmp" 2>/dev/null || chmod 0644 "$tmp"
mv -f "$tmp" "$DAEMON_JSON"
trap - EXIT
say "merged json-file log defaults (10m x 3) into $DAEMON_JSON; existing keys kept"
restart_docker
