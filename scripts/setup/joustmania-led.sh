#!/bin/bash
# joustmania-led: show the stage Pi's start state on the board's ACT LED
# (UC-060 BR-005, C-042, FR-183; #2027 PR 4).
#
#   joustmania-led starting|ok|degraded|failed   apply that state
#   joustmania-led restore                       the board's own trigger again
#   joustmania-led from-request                  the state joustmania-status --led left
#   joustmania-led stop-post                     ExecStopPost: restore on a clean stop,
#                                                failed otherwise ($SERVICE_RESULT)
#
# starting = trigger heartbeat; degraded = timer 500/500 ms (1 Hz); failed =
# timer 100/100 ms (5 Hz); ok = default-on (solid on). `trigger` is written
# first, then delay_on and delay_off. The LED is /sys/class/leds/ACT, else led0.
#
# It runs as root: install_autostart.sh installs a root-owned copy as
# /usr/local/lib/joustmania/joustmania-led and the unit runs that copy with the
# `+` prefix, so no sudoers rule is needed and the code that runs as root is not
# writable by the unit's user. It reads only one validated word from the
# unit's runtime dir and writes only to the LED and to its own state dir.
# No LED (dev box) or no permission: one line, exit 0. It never fails the unit.
#
# Test overrides: JOUSTMANIA_LED_SYSFS (default /sys/class/leds),
# JOUSTMANIA_LED_STATE_DIR (default /run/joustmania-led), JOUSTMANIA_RUN_STATE_DIR
# (default /run/joustmania, where joustmania-status --led writes led.request).
set -uo pipefail

SYSFS="${JOUSTMANIA_LED_SYSFS:-/sys/class/leds}"
STATE_DIR="${JOUSTMANIA_LED_STATE_DIR:-/run/joustmania-led}"
RUN_DIR="${JOUSTMANIA_RUN_STATE_DIR:-/run/joustmania}"
REQUEST="$RUN_DIR/led.request"
START_ENV="$RUN_DIR/start.env"

log() { echo "joustmania-led: $*"; }

led_dir() {
    local name
    for name in ACT led0; do
        if [[ -w "$SYSFS/$name/trigger" ]]; then
            echo "$SYSFS/$name"
            return 0
        fi
    done
    return 1
}

# The selected trigger is the [bracketed] word of the trigger file
current_trigger() {
    tr ' ' '\n' <"$1/trigger" 2>/dev/null | sed -n 's/^\[\(.*\)\]$/\1/p' | head -n 1
}

write_sysfs() { # $1 = file, $2 = value
    printf '%s' "$2" 2>/dev/null >"$1"
}

record() { # $1 = state word for joustmania-status
    mkdir -p "$STATE_DIR" 2>/dev/null && printf '%s\n' "$1" >"$STATE_DIR/state" 2>/dev/null
    return 0
}

apply() { # $1 = starting|ok|degraded|failed
    local dir trigger on="" off=""
    case "$1" in
        starting) trigger=heartbeat ;;
        ok) trigger=default-on ;;
        degraded) trigger=timer on=500 off=500 ;;
        failed) trigger=timer on=100 off=100 ;;
        *)
            log "unknown state '$1'"
            return 0
            ;;
    esac
    if ! dir="$(led_dir)"; then
        log "no writable ACT/led0 LED under $SYSFS (no LED or not root); state $1 not shown"
        return 0
    fi
    # Save the board's own trigger once per boot (/run is cleared at boot), so
    # restore puts back what the board had before we touched it
    if [[ ! -s "$STATE_DIR/default-trigger" ]]; then
        local own
        own="$(current_trigger "$dir")"
        mkdir -p "$STATE_DIR" 2>/dev/null && [[ -n "$own" ]] &&
            printf '%s\n' "$own" >"$STATE_DIR/default-trigger" 2>/dev/null
    fi
    if ! write_sysfs "$dir/trigger" "$trigger"; then
        log "cannot write $dir/trigger; state $1 not shown"
        return 0
    fi
    if [[ -n "$on" ]]; then
        write_sysfs "$dir/delay_on" "$on" || log "cannot write $dir/delay_on"
        write_sysfs "$dir/delay_off" "$off" || log "cannot write $dir/delay_off"
    fi
    record "$1"
    log "state $1 ($trigger${on:+ $on/$off ms}) on $dir"
}

restore() {
    local dir own
    own="$(head -n 1 "$STATE_DIR/default-trigger" 2>/dev/null || true)"
    if [[ -z "$own" ]]; then
        log "nothing to restore (the LED was not changed since boot)"
        return 0
    fi
    if ! dir="$(led_dir)"; then
        log "no writable ACT/led0 LED under $SYSFS; nothing restored"
        return 0
    fi
    if write_sysfs "$dir/trigger" "$own"; then
        record default
        log "restored the board trigger $own on $dir"
    else
        log "cannot write $dir/trigger; trigger $own not restored"
    fi
}

# The first line of a file the unit's user owns: a regular file, not a
# symlink, read with a bound (a FIFO swapped in must not hang the unit). The
# content is only compared with the state words, never logged.
read_word() { # $1 = file
    [[ -f "$1" && ! -L "$1" ]] || return 1
    timeout 2 head -c 64 -- "$1" </dev/null 2>/dev/null | head -n 1
}

from_request() {
    local word
    if [[ -e "$REQUEST" || -L "$REQUEST" ]]; then
        word="$(read_word "$REQUEST" || true)"
        case "$word" in
            ok | degraded | failed | starting) apply "$word" ;;
            *)
                log "led.request in $RUN_DIR holds no valid state; showing degraded"
                apply degraded
                ;;
        esac
        return 0
    fi
    # No request: joustmania-status has no --led (a rollback to a version
    # without it) or it stopped first. The start succeeded (ExecStartPost), so
    # take what joustmania-start.sh recorded, never a guessed degraded.
    word="$(read_word "$START_ENV" >/dev/null && timeout 2 grep -m 1 '^result=' -- "$START_ENV" </dev/null 2>/dev/null)"
    case "${word#result=}" in
        ok | degraded)
            log "no led.request from joustmania-status --led; using start.env result ${word#result=}"
            apply "${word#result=}"
            ;;
        *)
            log "no led.request and no start.env result; restoring the board trigger"
            restore
            ;;
    esac
}

case "${1:-}" in
    starting)
        # A request left by an earlier start must not be applied to this one
        rm -f "$REQUEST" 2>/dev/null
        apply starting
        ;;
    ok | degraded | failed) apply "$1" ;;
    restore) restore ;;
    from-request) from_request ;;
    stop-post)
        if [[ "${SERVICE_RESULT:-}" == success ]]; then
            restore
        else
            log "unit result ${SERVICE_RESULT:-unknown} (exit ${EXIT_CODE:-?} ${EXIT_STATUS:-?})"
            apply failed
        fi
        ;;
    *)
        echo "usage: joustmania-led starting|ok|degraded|failed|restore|from-request|stop-post" >&2
        exit 64
        ;;
esac
exit 0
