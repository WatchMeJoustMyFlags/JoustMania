#!/bin/bash
# joustmania-led: show the stage Pi's start state on the board's ACT LED
# (UC-060 BR-005, C-042, FR-183; #2027 PR 4).
#
#   joustmania-led starting|ok|degraded|failed   apply that state
#   joustmania-led restore                       the board's own trigger again
#   joustmania-led from-request                  the state joustmania-status --led left
#   joustmania-led stop-post                     ExecStopPost: restore on a clean stop,
#                                                failed otherwise ($SERVICE_RESULT)
#   joustmania-led hotspot-failed|hotspot-clear  set or clear the UC-061 hotspot cue
#                                                (the hotspot helper, #2137)
#
# starting = trigger heartbeat; degraded = timer 500/500 ms (1 Hz); failed =
# timer 100/100 ms (5 Hz); ok = default-on (solid on). `trigger` is written
# first, then delay_on and delay_off. The LED is /sys/class/leds/ACT, else led0.
#
# This is the only writer of the LED (UC-061 BR-011). It keeps the UC-060 state
# (STATE_DIR/state) and the UC-061 hotspot cue (STATE_DIR/hotspot, timer
# 200/1800 ms: one short flash every 2 s) as two records and shows the winner,
# highest first: failed, starting, the hotspot cue, degraded, ok, the board's
# own trigger. Every change runs under STATE_DIR/lock, because the game unit's
# hooks and the hotspot timer can call it at the same moment.
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

# Serialise every change: the game unit's hooks and the hotspot helper's
# timer both run this as root and may overlap. Without flock or a state dir
# (a dev box) it goes on unlocked.
lock() {
    command -v flock >/dev/null 2>&1 || return 0
    mkdir -p "$STATE_DIR" 2>/dev/null || return 0
    exec 8>"$STATE_DIR/lock" 2>/dev/null || return 0
    flock -w 10 8 || log "another joustmania-led holds $STATE_DIR/lock; going on"
}

# Sets TRIGGER ON OFF for a pattern word; 1 for an unknown word
pattern() {
    ON="" OFF=""
    case "$1" in
        starting) TRIGGER=heartbeat ;;
        ok) TRIGGER=default-on ;;
        degraded) TRIGGER=timer ON=500 OFF=500 ;;
        failed) TRIGGER=timer ON=100 OFF=100 ;;
        hotspot) TRIGGER=timer ON=200 OFF=1800 ;;
        *) return 1 ;;
    esac
}

cue_set() { [[ -e "$STATE_DIR/hotspot" ]]; }

# The word to show for a UC-060 state (UC-061 BR-011): the hotspot cue covers
# ok, degraded, the board's trigger (default) and nothing set since boot
effective() { # $1 = UC-060 state word or ""
    case "$1" in
        failed | starting) echo "$1" ;;
        *) if cue_set; then echo hotspot; else echo "${1:-default}"; fi ;;
    esac
}

save_default_trigger() { # $1 = LED dir
    # Save the board's own trigger once per boot (/run is cleared at boot), so
    # restore puts back what the board had before we touched it
    if [[ ! -s "$STATE_DIR/default-trigger" ]]; then
        local own
        own="$(current_trigger "$1")"
        mkdir -p "$STATE_DIR" 2>/dev/null && [[ -n "$own" ]] &&
            printf '%s\n' "$own" >"$STATE_DIR/default-trigger" 2>/dev/null
    fi
}

# Write one pattern word to the LED; 1 when the trigger cannot be written
show() { # $1 = LED dir, $2 = pattern word
    pattern "$2" || return 1
    write_sysfs "$1/trigger" "$TRIGGER" || {
        log "cannot write $1/trigger; $2 not shown"
        return 1
    }
    if [[ -n "$ON" ]]; then
        write_sysfs "$1/delay_on" "$ON" || log "cannot write $1/delay_on"
        write_sysfs "$1/delay_off" "$OFF" || log "cannot write $1/delay_off"
    fi
}

describe() { # $1 = pattern word
    pattern "$1" && echo "$TRIGGER${ON:+ $ON/$OFF ms}"
}

apply() { # $1 = starting|ok|degraded|failed
    local dir word
    if ! pattern "$1"; then
        log "unknown state '$1'"
        return 0
    fi
    if ! dir="$(led_dir)"; then
        log "no writable ACT/led0 LED under $SYSFS (no LED or not root); state $1 not shown"
        return 0
    fi
    save_default_trigger "$dir"
    word="$(effective "$1")"
    show "$dir" "$word" || return 0
    record "$1"
    if [[ "$word" == "$1" ]]; then
        log "state $1 ($(describe "$1")) on $dir"
    else
        log "state $1 recorded; the hotspot cue ($(describe hotspot)) wins on $dir (UC-061 BR-011)"
    fi
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
    if cue_set; then
        show "$dir" hotspot || return 0
        record default
        log "the board trigger $own is due, but the hotspot cue ($(describe hotspot)) wins on $dir (UC-061 BR-011)"
    elif write_sysfs "$dir/trigger" "$own"; then
        record default
        log "restored the board trigger $own on $dir"
    else
        log "cannot write $dir/trigger; trigger $own not restored"
    fi
}

# UC-061 A10: the hotspot helper's cue, kept apart from the UC-060 state
hotspot_cue() { # $1 = failed|clear
    local dir base own
    base="$(head -n 1 "$STATE_DIR/state" 2>/dev/null || true)"
    if [[ "$1" == clear ]]; then
        if ! cue_set; then
            log "no hotspot cue to clear"
            return 0
        fi
        rm -f "$STATE_DIR/hotspot"
        case "$base" in
            failed | starting)
                log "hotspot cue cleared; state $base stays on the LED"
                return 0
                ;;
        esac
        if ! dir="$(led_dir)"; then
            log "no writable ACT/led0 LED under $SYSFS; hotspot cue cleared"
            return 0
        fi
        case "$base" in
            ok | degraded)
                show "$dir" "$base" && log "hotspot cue cleared; state $base ($(describe "$base")) on $dir"
                ;;
            *)
                own="$(head -n 1 "$STATE_DIR/default-trigger" 2>/dev/null || true)"
                if [[ -n "$own" ]] && write_sysfs "$dir/trigger" "$own"; then
                    log "hotspot cue cleared; restored the board trigger $own on $dir"
                else
                    log "hotspot cue cleared; cannot restore the board trigger on $dir"
                fi
                ;;
        esac
        return 0
    fi
    if ! dir="$(led_dir)"; then
        log "no writable ACT/led0 LED under $SYSFS (no LED or not root); hotspot cue not shown"
        return 0
    fi
    case "$base" in
        failed | starting)
            mkdir -p "$STATE_DIR" 2>/dev/null && : >"$STATE_DIR/hotspot" 2>/dev/null
            log "hotspot cue recorded; state $base wins on $dir (UC-061 BR-011)"
            return 0
            ;;
    esac
    save_default_trigger "$dir"
    show "$dir" hotspot || return 0
    mkdir -p "$STATE_DIR" 2>/dev/null && : >"$STATE_DIR/hotspot" 2>/dev/null
    log "hotspot cue ($(describe hotspot)) on $dir: the hotspot should be on but failed (UC-061 A10)"
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
    starting | ok | degraded | failed | restore | from-request | stop-post | hotspot-failed | hotspot-clear) lock ;;
esac

case "${1:-}" in
    starting)
        # A request left by an earlier start must not be applied to this one
        rm -f "$REQUEST" 2>/dev/null
        apply starting
        ;;
    ok | degraded | failed) apply "$1" ;;
    restore) restore ;;
    from-request) from_request ;;
    hotspot-failed) hotspot_cue failed ;;
    hotspot-clear) hotspot_cue clear ;;
    stop-post)
        if [[ "${SERVICE_RESULT:-}" == success ]]; then
            restore
        else
            log "unit result ${SERVICE_RESULT:-unknown} (exit ${EXIT_CODE:-?} ${EXIT_STATUS:-?})"
            apply failed
        fi
        ;;
    *)
        echo "usage: joustmania-led starting|ok|degraded|failed|restore|from-request|stop-post|hotspot-failed|hotspot-clear" >&2
        exit 64
        ;;
esac
exit 0
