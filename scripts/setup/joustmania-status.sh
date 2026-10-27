#!/bin/bash
# joustmania-status: the state of the stage Pi on one screen (UC-060 BR-005,
# FR-183, A1, A4, A5; #2027). Read-only: it never starts, pulls or changes
# anything, and never prints env values.
#
#   ./scripts/setup/joustmania-status.sh
#
# One line per check, `OK`, `WARN`, `FAIL`, `SKIP` or `INFO`:
#   - version: the pinned version (release tag or sha) and its image tag, or
#     WARN when the checkout is on a branch (A4)
#   - unit: the joustmania unit's state; start-limit-hit (systemd refuses to
#     start it, A5) is FAIL with the recovery command (BR-006, #2102)
#   - last start: what joustmania-start.sh recorded (start.env)
#   - images: images missing on the Pi, with the joustmania-upgrade.sh command
#     that pulls them (boot never pulls, BR-001)
#   - service <name>: running / healthy, per active service; a running but
#     unhealthy one names its recovery, `docker restart <container>` (#2125)
#   - clock: NTP synchronised, or INFO when not (no RTC, accepted: until NTP
#     sync TLS to the registry fails, so an upgrade cannot pull; the start does
#     not need it, so it never changes the state)
#   - docker not reachable (daemon down, user not in the docker group): FAIL,
#     and the image and service checks are skipped
# The last line is `state: ok|starting|degraded|failed`.
# Exit: 0 ok, 1 starting or degraded (any WARN: an observability or agent
# problem, a branch checkout), 2 failed (a game service down or its image
# missing, the unit failed, docker not reachable). While the unit is starting,
# a game service that is not running yet is INFO and the state is `starting`.
# An INFO `led:` line shows the ACT LED state (C-042) joustmania-led recorded.
#
#   ./scripts/setup/joustmania-status.sh --led
#
# For the unit's ExecStartPost only (#2027 PR 4): the start has ended, so a
# unit that is still `activating` counts as started and a service that is not
# up yet is judged (WARN or FAIL), never `starting`; the state is written to
# led.request in the unit's runtime dir, where the root-owned joustmania-led
# (the next ExecStartPost) applies it to the LED. This writes only that file.
set -uo pipefail

LED_MODE=0
case "${1:-}" in
    --led) LED_MODE=1 ;;
    "") ;;
    *)
        echo "usage: joustmania-status.sh [--led]" >&2
        exit 64
        ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOUSTMANIA_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
# shellcheck source=lib-joustmania.sh
source "$SCRIPT_DIR/lib-joustmania.sh"

UNIT="joustmania"
# Where joustmania-start.sh writes it: the unit's RuntimeDirectory, else /tmp
# (an old unit, or a start by hand). The two *_STATE_DIR overrides are for tests.
RUN_STATE_DIR="${JOUSTMANIA_RUN_STATE_DIR:-/run/joustmania}"
TMP_STATE_DIR="${JOUSTMANIA_TMP_STATE_DIR:-/tmp/joustmania}"
if [[ -n "${JOUSTMANIA_STATUS_DIR:-}" ]]; then
    STATE_FILE="$JOUSTMANIA_STATUS_DIR/start.env"
elif [[ -r "$RUN_STATE_DIR/start.env" ]]; then
    STATE_FILE="$RUN_STATE_DIR/start.env"
else
    STATE_FILE="$TMP_STATE_DIR/start.env"
fi
# The LED helper's record and the LED itself (overrides for tests)
LED_STATE_DIR="${JOUSTMANIA_LED_STATE_DIR:-/run/joustmania-led}"
LED_SYSFS="${JOUSTMANIA_LED_SYSFS:-/sys/class/leds}"
LED_HELPER="${JOUSTMANIA_LED_HELPER:-/usr/local/lib/joustmania/joustmania-led}"
RECOVER_CMD="sudo systemctl reset-failed $UNIT && sudo systemctl start $UNIT"
WORST=0 # 0 ok, 1 warn, 2 fail
STARTING=0

report() { # $1 = OK|WARN|FAIL|SKIP|INFO, $2 = text
    printf '%-4s %s\n' "$1" "$2"
    case "$1" in
        WARN) ((WORST < 1)) && WORST=1 ;;
        FAIL) WORST=2 ;;
    esac
    return 0
}
compose() { "$SCRIPT_DIR/joustmania-compose.sh" "$@"; }

# --- version (BR-003, A4) ---------------------------------------------------
if PIN_TAG="$(joustmania_pin_image_tag "$JOUSTMANIA_DIR")"; then
    report OK "version: $(joustmania_pin_ref "$JOUSTMANIA_DIR") (image tag $PIN_TAG)"
elif BRANCH="$(git -C "$JOUSTMANIA_DIR" symbolic-ref -q --short HEAD 2>/dev/null)"; then
    report WARN "version: not pinned (branch $BRANCH at $(git -C "$JOUSTMANIA_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)); pin with scripts/setup/joustmania-upgrade.sh <joustmania-vX.Y.Z | sha> (UC-060 A4)"
else
    # Not a proven branch: git cannot read the checkout (A4 fails open)
    report WARN "version: git cannot read the checkout (check: git -C $JOUSTMANIA_DIR status)"
fi

# --- unit (A5, BR-006) ------------------------------------------------------
if command -v systemctl >/dev/null 2>&1; then
    ACTIVE="$(systemctl show -p ActiveState --value "$UNIT" 2>/dev/null || true)"
    SUB="$(systemctl show -p SubState --value "$UNIT" 2>/dev/null || true)"
    RESULT="$(systemctl show -p Result --value "$UNIT" 2>/dev/null || true)"
    if [[ "$RESULT" == start-limit-hit ]]; then
        report FAIL "unit: start-limit-hit (systemd refuses to start it after 3 failed starts in 15 min); recover with: $RECOVER_CMD"
    elif [[ "$ACTIVE" == failed ]]; then
        report FAIL "unit: failed (result ${RESULT:-unknown}); see: journalctl -u $UNIT -b"
    elif [[ "$ACTIVE" == activating && $LED_MODE -eq 1 ]]; then
        # ExecStartPost: the start script has ended
        report OK "unit: start ended (ExecStartPost)"
    elif [[ "$ACTIVE" == activating ]]; then
        STARTING=1
        report INFO "unit: starting"
    elif [[ "$ACTIVE" == active ]]; then
        report OK "unit: active (${SUB:-unknown})"
    else
        report WARN "unit: ${ACTIVE:-unknown} (not started; autostart installed? sudo scripts/setup/install_autostart.sh)"
    fi
else
    report SKIP "unit: no systemctl"
fi

# --- last start (start.env) -------------------------------------------------
if [[ -r "$STATE_FILE" ]]; then
    LAST="$(grep -E '^(result|reason|missing|down|ts)=' "$STATE_FILE" | grep -vE '^[a-z]+=$' | tr '\n' ' ')"
    report INFO "last start: ${LAST% }"
else
    report INFO "last start: no start.env (not started since boot)"
fi

# --- images and services ----------------------------------------------------
# The full config holds interpolated secrets: it goes straight into the parser.
# compose config works without the daemon: check docker first, or every image
# would look missing.
if ! docker info >/dev/null 2>&1; then
    report FAIL "docker: not reachable (daemon not running, or $(id -un 2>/dev/null || echo this user) not in the docker group); images and services not checked"
elif ! SERVICE_IMAGES="$(compose config 2>/dev/null | joustmania_service_graph)" || [[ -z "$SERVICE_IMAGES" ]]; then
    report FAIL "docker: compose cannot read the project (compose config failed)"
else
    MISSING=0
    while IFS='|' read -r svc img _; do
        [[ -n "$svc" ]] || continue
        docker image inspect "$img" >/dev/null 2>&1 && continue
        MISSING=1
        if joustmania_in_list "$svc" "${JOUSTMANIA_GAME_SERVICES[@]}"; then
            report FAIL "image missing: $svc ($img)"
        else
            report WARN "image missing: $svc ($img)"
        fi
    done <<<"$SERVICE_IMAGES"
    if [[ $MISSING -eq 1 ]]; then
        report INFO "images: boot never pulls (UC-060 BR-001); $(joustmania_missing_hint "$JOUSTMANIA_DIR")"
    else
        report OK "images: all present"
    fi
    PS="$(compose ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.Name}}' 2>/dev/null || true)"
    while IFS='|' read -r svc _; do
        [[ -n "$svc" ]] || continue
        line="$(grep -m 1 "^$svc|" <<<"$PS" || true)"
        IFS='|' read -r _ state health name <<<"$line"
        text="service $svc: ${state:-no container}${health:+ ($health)}"
        if [[ "$state" == running && "$health" == unhealthy ]]; then
            # Running but wedged: compose restart policies do not act on health
            # (#2125), so name the restart that recovers it.
            text="$text; recover with: docker restart ${name:-joustmania-$svc}"
        fi
        if [[ "$state" == running && (-z "$health" || "$health" == healthy) ]]; then
            report OK "$text"
        elif [[ $LED_MODE -eq 0 && (("$state" == running && "$health" == starting) || "${ACTIVE:-}" == activating) ]]; then
            # Still coming up (the unit is starting): not a failure yet
            STARTING=1
            report INFO "$text"
        elif joustmania_in_list "$svc" "${JOUSTMANIA_GAME_SERVICES[@]}"; then
            report FAIL "$text"
        else
            report WARN "$text"
        fi
    done <<<"$SERVICE_IMAGES"
fi

# --- clock ------------------------------------------------------------------
if command -v timedatectl >/dev/null 2>&1; then
    if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" == yes ]]; then
        report OK "clock: NTP synchronised"
    else
        report INFO "clock: not NTP synchronised (no RTC, the time may be hours off): TLS to the registry fails until it syncs, so an upgrade cannot pull yet; the start does not need it"
    fi
else
    report SKIP "clock: no timedatectl"
fi

# --- ACT LED (C-042) --------------------------------------------------------
LED_NOW=""
for led in ACT led0; do
    if [[ -r "$LED_SYSFS/$led/trigger" ]]; then
        LED_NOW="$led trigger $(tr ' ' '\n' <"$LED_SYSFS/$led/trigger" | sed -n 's/^\[\(.*\)\]$/\1/p' | head -n 1)"
        break
    fi
done
LED_REC="$(head -n 1 "$LED_STATE_DIR/state" 2>/dev/null || true)"
report INFO "led: ${LED_REC:-not set since boot}${LED_NOW:+ ($LED_NOW)}"
if [[ ! -e "$LED_HELPER" ]]; then
    report INFO "led helper: $LED_HELPER not installed (no LED cue); install it with: sudo $JOUSTMANIA_DIR/scripts/setup/install_autostart.sh"
elif ! cmp -s "$LED_HELPER" "$SCRIPT_DIR/joustmania-led.sh"; then
    report INFO "led helper: $LED_HELPER differs from this checkout; update it with: sudo $JOUSTMANIA_DIR/scripts/setup/install_autostart.sh"
fi

if ((WORST == 2)); then
    STATE=failed RC=2
elif ((STARTING == 1)); then
    STATE=starting RC=1
elif ((WORST == 1)); then
    STATE=degraded RC=1
else
    STATE=ok RC=0
fi
if ((LED_MODE == 1)); then
    # Replace, never follow, a file left in the runtime dir
    rm -f "$RUN_STATE_DIR/led.request" 2>/dev/null
    printf '%s\n' "$STATE" >"$RUN_STATE_DIR/led.request" 2>/dev/null ||
        echo "led: cannot write $RUN_STATE_DIR/led.request; the LED shows degraded"
fi
echo "state: $STATE"
exit "$RC"
