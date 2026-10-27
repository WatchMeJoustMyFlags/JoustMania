#!/bin/bash
# joustmania-hotspot: the stage Pi's own Wi-Fi hotspot as a fallback when its
# uplink has no connection (UC-061, FR-184, FR-186, FR-187, C-043 to C-045; #2099).
#
#   joustmania-hotspot evaluate          decide and act (the unit's ExecStart);
#                                        loops while it waits, at most 180 s
#   joustmania-hotspot evaluate --once   one pass only (tests, debugging)
#   joustmania-hotspot status            the non-secret status file (format 1)
#   joustmania-hotspot render TEMPLATE OUT SSID CHANNEL
#                                        write the NM keyfile; the password is
#                                        one line on stdin, never an argument
#   joustmania-hotspot selftest [HOLD_S] bring the hotspot up for HOLD_S s (<= 30)
#                                        while the uplink is connected, check it,
#                                        take it down (setup only, BR-001)
#   joustmania-hotspot remove            take it down and remove every installed file
#
# It runs as root: the setup installs a root-owned copy as
# /usr/local/lib/joustmania/joustmania-hotspot (the #2130 pattern), and the unit,
# timer and dispatcher hook run only that copy. Nothing in the pi-writable
# checkout runs as root. It never reads, prints or passes the password, except
# `render`, which reads it from stdin and writes it only into the 0600 keyfile.
# It never calls `nmcli -s` / `--show-secrets` and refuses to run under `bash -x`.
#
# Decisions (UC-061 BR-008): the uplink is connected when its NM device state is
# 100 and it has an IPv4 address outside 169.254/16; states 40 to 90, or 100 with
# no such address, mean connecting; anything else means not connected. The
# recorded state in STATE_DIR (cleared at boot: /run is tmpfs) says since when the
# uplink has not been stably connected and since when it is connected, so new
# events never restart a wait (A6). Up after up_delay_s not connected
# (connecting_wait_s while connecting or briefly connected), never in the first
# boot_hold_s after boot; down after stable_s connected without a break. A Wi-Fi
# client that is up or connecting on wlan0 always wins (A2). Every decision the
# helper cannot carry out is logged and recorded, and it still exits 0 (BR-006).
#
# ACT LED (A10, BR-011; #2137): on a change into or out of state=failed it asks
# the root-owned UC-060 LED helper (hotspot-failed / hotspot-clear), the only
# writer of the LED, which decides what wins; it never writes the LED itself.
#
# Test overrides: JOUSTMANIA_HOTSPOT_CONF, JOUSTMANIA_HOTSPOT_STATE_DIR,
# JOUSTMANIA_HOTSPOT_ROOT (prefix for `remove`), JOUSTMANIA_HOTSPOT_NOW_FILE
# (fake clock: epoch seconds in a file; a pause adds to it instead of sleeping),
# JOUSTMANIA_HOTSPOT_PROC_UPTIME (also the debounce clock), JOUSTMANIA_HOTSPOT_DNSMASQ,
# JOUSTMANIA_HOTSPOT_DNSMASQ_PID, JOUSTMANIA_HOTSPOT_MAX_RUN_S,
# JOUSTMANIA_HOTSPOT_LED_HELPER, and PATH stubs.

# Shell tracing would print the password in `render` (BR-004)
case $- in
    *x*)
        echo "joustmania-hotspot: refusing to run with shell tracing on (UC-061 BR-004)" >&2
        exit 2
        ;;
esac
set -uo pipefail
export LC_ALL=C
# bash 5.2: `&` in a ${var//pat/rep} replacement means the match. Nothing here
# substitutes a secret, but keep the old meaning should anyone add one.
shopt -u patsub_replacement 2>/dev/null || true

readonly UUID=e0602321-f72b-408b-983e-56ee57d519ee
readonly WLAN=wlan0
readonly AP_ADDR=10.42.0.1
readonly STATUS_FORMAT=1
ROOT="${JOUSTMANIA_HOTSPOT_ROOT:-}"
CONF="${JOUSTMANIA_HOTSPOT_CONF:-${ROOT}/etc/joustmania/hotspot.conf}"
STATE_DIR="${JOUSTMANIA_HOTSPOT_STATE_DIR:-/run/joustmania-hotspot}"
NOW_FILE="${JOUSTMANIA_HOTSPOT_NOW_FILE:-}"
PROC_UPTIME="${JOUSTMANIA_HOTSPOT_PROC_UPTIME:-/proc/uptime}"
DNSMASQ="${JOUSTMANIA_HOTSPOT_DNSMASQ:-/usr/sbin/dnsmasq}"
DNSMASQ_PID="${JOUSTMANIA_HOTSPOT_DNSMASQ_PID:-/run/nm-dnsmasq-${WLAN}.pid}"
MAX_RUN_S="${JOUSTMANIA_HOTSPOT_MAX_RUN_S:-180}"
LED_HELPER="${JOUSTMANIA_HOTSPOT_LED_HELPER:-${ROOT}/usr/local/lib/joustmania/joustmania-led}"
readonly PASS_INTERVAL_S=5
readonly UP_WAIT_S=30

log() { echo "joustmania-hotspot: $*"; }

# The debounce clock: seconds since boot (/proc/uptime), monotonic, so an NTP
# step of the wall clock never stretches or skips a wait; the state it is
# compared with lives in /run, which is cleared at boot too
now() {
    if [[ -n "$NOW_FILE" ]]; then
        head -n 1 "$NOW_FILE"
    else
        uptime_s
    fi
}

pause() { # $1 = seconds
    if [[ -n "$NOW_FILE" ]]; then
        printf '%s\n' "$(($(now) + $1))" >"$NOW_FILE"
    else
        sleep "$1"
    fi
}

uptime_s() {
    local up
    read -r up _ <"$PROC_UPTIME" 2>/dev/null || up=0
    echo "${up%%.*}"
}

# --- configuration (BR-010: parsed against an allow-list, never sourced) -----

C_ENABLED=yes C_SSID=JoustMania C_CHANNEL=36 C_UPLINK=eth0
C_UP_DELAY=20 C_CONNECTING_WAIT=60 C_STABLE=15 C_BOOT_HOLD=45

valid_ssid() { [[ "$1" =~ ^[A-Za-z0-9_-]([A-Za-z0-9\ _-]{0,30}[A-Za-z0-9_-])?$ ]]; }
valid_channel() { [[ "$1" =~ ^(36|40|44|48)$ ]]; }
valid_iface() { [[ "$1" =~ ^[A-Za-z0-9_.-]{1,15}$ && "$1" != "$WLAN" ]]; }
in_range() { [[ "$1" =~ ^[0-9]{1,4}$ ]] && (($1 >= $2 && $1 <= $3)); }
# BR-004: 8 to 63 characters, printable ASCII 0x21 to 0x7E, no backslash, so the
# NM keyfile (GKeyFile) stores it verbatim
valid_psk() { ((${#1} >= 8 && ${#1} <= 63)) && [[ "$1" =~ ^[!-~]+$ && "$1" != *\\* ]]; }

read_conf() {
    local line key val
    [[ -f "$CONF" ]] || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        [[ -z "${line//[[:space:]]/}" || "$line" == \#* ]] && continue
        if [[ "$line" != *=* ]]; then
            log "ignoring a line without '=' in $CONF"
            continue
        fi
        key="${line%%=*}" val="${line#*=}"
        case "$key" in
            enabled) [[ "$val" == yes || "$val" == no ]] && C_ENABLED="$val" || key="!$key" ;;
            ssid) valid_ssid "$val" && C_SSID="$val" || key="!$key" ;;
            channel) valid_channel "$val" && C_CHANNEL="$val" || key="!$key" ;;
            uplink_iface) valid_iface "$val" && C_UPLINK="$val" || key="!$key" ;;
            up_delay_s) in_range "$val" 5 120 && C_UP_DELAY="$val" || key="!$key" ;;
            connecting_wait_s) in_range "$val" 20 300 && C_CONNECTING_WAIT="$val" || key="!$key" ;;
            stable_s) in_range "$val" 5 120 && C_STABLE="$val" || key="!$key" ;;
            boot_hold_s) in_range "$val" 0 180 && C_BOOT_HOLD="$val" || key="!$key" ;;
            *) log "ignoring unknown key '${key:0:32}' in $CONF" ;;
        esac
        # values are never logged: only the key whose value was refused
        [[ "$key" == !* ]] && log "ignoring an invalid value for '${key#!}' in $CONF (default kept)"
    done <"$CONF"
    ((C_CONNECTING_WAIT >= C_UP_DELAY)) || C_CONNECTING_WAIT="$C_UP_DELAY"
    return 0
}

# --- NetworkManager reads (never with secrets) --------------------------------

# Three-way: 0 present, 1 absent (nmcli exit 10: no such connection), 2 unknown
# (a D-Bus error, a timeout, NetworkManager not running: say nothing is wrong)
PROFILE_RC=0
profile_state() {
    local out
    PROFILE_RC=0
    out="$(nmcli -g connection.uuid connection show uuid "$UUID" 2>/dev/null)" || PROFILE_RC=$?
    if ((PROFILE_RC == 0)) && [[ "$out" == "$UUID" ]]; then return 0; fi
    ((PROFILE_RC == 10)) && return 1
    return 2
}
profile_loaded() { profile_state; }

# The leading number of "100 (connected)"
state_number() {
    local word="${1%% *}"
    [[ "$word" =~ ^[0-9]+$ ]] && echo "$word" || echo 0
}

has_ipv4() { # $@ = NM IP4.ADDRESS values ("a.b.c.d/nn | ...")
    local addr
    for addr in $(printf '%s\n' "$@" | tr '|' '\n'); do
        [[ "$addr" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] || continue
        [[ "$addr" == 169.254.* ]] && continue
        return 0
    done
    return 1
}

# connected | connecting | down (BR-008)
uplink_class() {
    local out state
    if ! out="$(nmcli -g GENERAL.STATE,IP4.ADDRESS device show "$C_UPLINK" 2>/dev/null)" || [[ -z "$out" ]]; then
        echo down
        return
    fi
    state="$(state_number "$(head -n 1 <<<"$out")")"
    if ((state == 100)) && has_ipv4 "$(tail -n +2 <<<"$out")"; then
        echo connected
    elif ((state >= 40 && state <= 100)); then
        echo connecting
    else
        echo down
    fi
}

# ap-on | ap-starting | client-up | client-connecting | free | unavailable | missing
wlan_class() {
    local out state uuid
    if ! out="$(nmcli -g GENERAL.STATE,GENERAL.CON-UUID device show "$WLAN" 2>/dev/null)" || [[ -z "$out" ]]; then
        echo missing
        return
    fi
    state="$(state_number "$(head -n 1 <<<"$out")")"
    uuid="$(sed -n 2p <<<"$out")"
    if ((state <= 20)); then
        echo unavailable
    elif ((state >= 40 && state <= 100)); then
        if [[ "$uuid" == "$UUID" ]]; then
            ((state == 100)) && echo ap-on || echo ap-starting
        else
            ((state == 100)) && echo client-up || echo client-connecting
        fi
    else
        echo free
    fi
}

client_count() {
    local dump
    command -v iw >/dev/null 2>&1 || {
        echo unknown
        return
    }
    dump="$(iw dev "$WLAN" station dump 2>/dev/null)" || {
        echo unknown
        return
    }
    grep -c '^Station' <<<"$dump" || true
}

# One printable line of at most 160 characters from a tool's error output
one_line() { head -n 1 | tr -cd '[:print:]' | cut -c1-160; }

hotspot_down() {
    nmcli connection down uuid "$UUID" >/dev/null 2>&1 || true
}

hotspot_up() { # sets UP_ERROR on failure
    local err
    if [[ ! -x "$DNSMASQ" ]]; then
        UP_ERROR="dnsmasq-base is not installed ($DNSMASQ missing)"
        return 1
    fi
    if err="$(nmcli --wait "$UP_WAIT_S" connection up uuid "$UUID" 2>&1 >/dev/null)"; then
        return 0
    fi
    UP_ERROR="nmcli: $(one_line <<<"${err:-connection up failed}")"
    return 1
}

# --- state files ---------------------------------------------------------------

state_get() { head -n 1 "$STATE_DIR/$1" 2>/dev/null; }
state_set() { printf '%s\n' "$2" >"$STATE_DIR/$1"; }
state_clear() { rm -f "$STATE_DIR/$1"; }

write_status() { # $1 = state, $2 = reason
    local tmp="$STATE_DIR/.status.$$" clients=""
    [[ "$1" == on ]] && clients="$(client_count)"
    {
        echo "format=$STATUS_FORMAT"
        echo "state=$1"
        echo "reason=$2"
        echo "ssid=$C_SSID"
        echo "band=5GHz"
        echo "channel=$C_CHANNEL"
        echo "uplink=$C_UPLINK"
        echo "clients=$clients"
        echo "url=http://$AP_ADDR/"
        echo "updated=$(date +%s)"
    } >"$tmp" && chmod 0644 "$tmp" && mv -f "$tmp" "$STATE_DIR/status"
}

# --- ACT LED cue (A10, BR-011) ---------------------------------------------------

# Ask the UC-060 LED helper for the cue only when it changes (led_cue in
# STATE_DIR, cleared at boot like the helper's own record), so a check every
# 60 s does not rewrite the LED or fill the journal. led_cue is recorded only
# when the helper answers 0, so a missing, old or stuck helper is asked again
# by the next check (an install_autostart.sh run later is picked up); its
# failure is logged once per kind (led_error). The hotspot goes on as before
# (BR-006).
led_cue() { # $1 = the recorded hotspot state
    local want=clear last rc=0 err=""
    [[ "$1" == failed ]] && want=failed
    last="$(state_get led_cue)"
    # nothing was shown since boot: there is no cue to clear
    [[ "$last" == "$want" || (-z "$last" && "$want" == clear) ]] && return 0
    if [[ ! -x "$LED_HELPER" ]]; then
        err="no LED helper at $LED_HELPER (the UC-060 autostart is not installed); hotspot LED cue $want not shown"
    else
        # longer than the helper's own lock wait (flock -w 10); an older helper
        # without the hotspot commands prints its usage on stderr: drop it
        timeout 15 "$LED_HELPER" "hotspot-$want" </dev/null 2>/dev/null || rc=$?
        ((rc == 0)) || err="the LED helper failed (rc $rc) on hotspot-$want; re-run install_autostart.sh if it is older than this helper"
    fi
    if [[ -z "$err" ]]; then
        state_set led_cue "$want" 2>/dev/null || true
        state_clear led_error
        return 0
    fi
    if [[ "$(state_get led_error)" != "$want $rc" ]]; then
        log "$err"
        state_set led_error "$want $rc" 2>/dev/null || true
    fi
    return 0
}

# --- evaluate --------------------------------------------------------------------

# One pass: sets STATE, REASON and DONE (1 = no reason to look again soon)
evaluate_pass() {
    local now uplink wlan since conn_for=-1 unstable_for need up rc
    STATE="" REASON="" DONE=1
    if ! read_conf; then
        hotspot_down
        STATE=not-set-up REASON="no $CONF (UC-061 A5)"
        return
    fi
    if [[ "$C_ENABLED" == no ]]; then
        hotspot_down
        STATE=disabled REASON="enabled=no in $CONF (UC-061 A5)"
        return
    fi
    profile_state && rc=0 || rc=$?
    if ((rc == 1)); then
        hotspot_down
        STATE=not-set-up REASON="the joustmania-hotspot profile is not loaded in NetworkManager"
        return
    elif ((rc == 2)); then
        # a failed query changes nothing: a running hotspot is the Host's only access
        STATE=waiting REASON="cannot ask NetworkManager (rc ${PROFILE_RC})"
        return
    fi
    now="$(now)"
    uplink="$(uplink_class)"
    wlan="$(wlan_class)"

    # BR-008 bookkeeping: since when connected, since when not stably connected
    if [[ "$uplink" == connected ]]; then
        since="$(state_get connected_since)"
        [[ "$since" =~ ^[0-9]+$ ]] || {
            since="$now"
            state_set connected_since "$now"
        }
        conn_for=$((now - since))
    else
        state_clear connected_since
    fi
    if ((conn_for >= C_STABLE)); then
        state_clear unstable_since
        state_clear client_seen
        if [[ "$wlan" == ap-on || "$wlan" == ap-starting ]]; then
            hotspot_down
            log "hotspot down: $C_UPLINK connected for ${conn_for}s (UC-061 A1)"
        fi
        STATE=off REASON="$C_UPLINK connected"
        return
    fi
    since="$(state_get unstable_since)"
    [[ "$since" =~ ^[0-9]+$ ]] || {
        since="$now"
        state_set unstable_since "$now"
    }
    unstable_for=$((now - since))
    # A client was up at the last pass and is gone now: the drop happened at
    # some point since then, so the wait starts now and NM gets the full delay
    # to reconnect it before the hotspot takes the radio (A2)
    if [[ -e "$STATE_DIR/client_seen" && ("$wlan" == free || "$wlan" == client-connecting) ]]; then
        state_clear client_seen
        state_set unstable_since "$now"
        unstable_for=0
    fi

    case "$wlan" in
        ap-on)
            STATE=on REASON="$C_UPLINK $uplink"
            # a fresh connection must hold for stable_s before the hotspot goes down
            [[ "$uplink" == connected ]] && DONE=0
            return
            ;;
        ap-starting)
            STATE=waiting REASON="hotspot starting" DONE=0
            return
            ;;
        client-up)
            # a client connection counts as connectivity: mark it, so the
            # first pass after it drops restarts the wait (A2)
            state_set unstable_since "$now"
            state_set client_seen 1
            STATE=off REASON="Wi-Fi client up on $WLAN (UC-061 A2)"
            return
            ;;
        client-connecting)
            STATE=waiting REASON="Wi-Fi client connecting on $WLAN (UC-061 A2)" DONE=0
            return
            ;;
        missing)
            STATE=failed REASON="no $WLAN device (UC-061 A3)"
            return
            ;;
        unavailable)
            STATE=failed REASON="$WLAN unavailable: radio off, rfkill or no Wi-Fi country (UC-061 A3)"
            return
            ;;
    esac

    # wlan0 is free and the uplink is not stably connected
    if [[ "$uplink" == down ]]; then need="$C_UP_DELAY"; else need="$C_CONNECTING_WAIT"; fi
    up="$(uptime_s)"
    if ((up < C_BOOT_HOLD)); then
        STATE=waiting REASON="boot hold, ${up}s of ${C_BOOT_HOLD}s after boot (a known Wi-Fi may connect first)" DONE=0
        return
    fi
    if ((unstable_for < need)); then
        STATE=waiting REASON="$C_UPLINK $uplink for ${unstable_for}s of ${need}s" DONE=0
        return
    fi
    if [[ "$uplink" == connected ]]; then
        # never next to a connected uplink (BR-002): wait until it is stable
        # (A1) or a later pass sees it drop
        STATE=waiting REASON="$C_UPLINK connected for ${conn_for}s, not yet ${C_STABLE}s" DONE=0
        return
    fi
    if hotspot_up; then
        STATE=on REASON="$C_UPLINK $uplink for ${unstable_for}s"
    else
        STATE=failed REASON="$UP_ERROR (UC-061 A3)"
    fi
}

lock_state() { # $1 = wait seconds
    mkdir -p "$STATE_DIR" && chmod 0755 "$STATE_DIR" 2>/dev/null
    exec 9>"$STATE_DIR/lock" || return 1
    flock -w "$1" 9
}

evaluate() {
    local once="${1:-}" start last=""
    if ! lock_state 60; then
        log "another check holds $STATE_DIR/lock; nothing done"
        return 0
    fi
    start="$(now)"
    while :; do
        evaluate_pass
        write_status "$STATE" "$REASON" || log "cannot write $STATE_DIR/status"
        led_cue "$STATE"
        # one line per kind of decision, not per waiting second
        if [[ "$STATE: ${REASON//[0-9]/}" != "$last" ]]; then
            log "$STATE: $REASON"
            last="$STATE: ${REASON//[0-9]/}"
        fi
        [[ "$once" == --once || "$DONE" == 1 ]] && break
        if (($(now) - start >= MAX_RUN_S)); then
            log "still $STATE after ${MAX_RUN_S}s; the timer checks again"
            break
        fi
        pause "$PASS_INTERVAL_S"
    done
    return 0
}

# --- status ----------------------------------------------------------------------

status() {
    if [[ -f "$STATE_DIR/status" ]]; then
        cat "$STATE_DIR/status"
    else
        printf 'format=%s\nstate=unknown\nreason=no check ran since boot\n' "$STATUS_FORMAT"
    fi
}

# --- render (BR-004) --------------------------------------------------------------

render() { # $1 = template, $2 = out (must not exist), $3 = ssid, $4 = channel; stdin = password
    local template="${1:-}" out="${2:-}" ssid="${3:-}" channel="${4:-}" psk="" line n_psk=0 n_ssid=0 n_channel=0
    if [[ ! -f "$template" || -z "$out" ]]; then
        log "usage: render TEMPLATE OUT SSID CHANNEL (password on stdin)"
        return 2
    fi
    valid_ssid "$ssid" || {
        log "invalid SSID: 1 to 32 characters of A-Z a-z 0-9 _ - and space, no leading or trailing space"
        return 2
    }
    valid_channel "$channel" || {
        log "invalid channel: 36, 40, 44 or 48 (UC-061 BR-003)"
        return 2
    }
    IFS= read -r psk || [[ -n "$psk" ]] || true
    if ! valid_psk "$psk"; then
        psk=""
        log "invalid password: 8 to 63 printable ASCII characters, no space and no backslash (UC-061 BR-004)"
        return 2
    fi
    umask 077
    # noclobber: OUT must be a new file (the setup passes a hidden temp name)
    if ! (set -o noclobber && : >"$out") 2>/dev/null; then
        psk=""
        log "refusing to write $out: it exists or cannot be created"
        return 2
    fi
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            \#* | '') printf '%s\n' "$line" ;;
            'psk=@PSK@')
                printf 'psk=%s\n' "$psk"
                n_psk=$((n_psk + 1))
                ;;
            'ssid=@SSID@')
                printf 'ssid=%s\n' "$ssid"
                n_ssid=$((n_ssid + 1))
                ;;
            'channel=@CHANNEL@')
                printf 'channel=%s\n' "$channel"
                n_channel=$((n_channel + 1))
                ;;
            *@PSK@* | *@SSID@* | *@CHANNEL@*) printf '%s\n' "${line%%=*}=@UNEXPECTED@" ;;
            *) printf '%s\n' "$line" ;;
        esac
    done <"$template" >"$out"
    psk=""
    chmod 0600 "$out"
    if ((n_psk != 1 || n_ssid != 1 || n_channel != 1)) || grep -q '@UNEXPECTED@' "$out"; then
        rm -f "$out"
        log "template $template does not have exactly one psk=@PSK@, ssid=@SSID@ and channel=@CHANNEL@ line"
        return 2
    fi
    return 0
}

# --- selftest (setup only, BR-001 exception) ----------------------------------

SELFTEST_FAILS=0
check() { # $1 = description, rest = command
    local what="$1"
    shift
    if "$@"; then
        echo "PASS $what"
    else
        echo "FAIL $what"
        SELFTEST_FAILS=$((SELFTEST_FAILS + 1))
    fi
}

iw_is_ap_on_channel() {
    local info
    info="$(iw dev "$WLAN" info 2>/dev/null)" || return 1
    grep -q 'type AP' <<<"$info" && grep -qE "channel $C_CHANNEL( |$)" <<<"$info"
}

wlan_has_ap_addr() {
    nmcli -g IP4.ADDRESS device show "$WLAN" 2>/dev/null | tr '|' '\n' | sed 's/^ *//; s/ *$//' | grep -qx "$AP_ADDR/24"
}

dnsmasq_running() {
    local pid
    pid="$(head -n 1 "$DNSMASQ_PID" 2>/dev/null)"
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

selftest() { # $1 = hold seconds (default 20, at most 30)
    local hold="${1:-20}" wlan
    in_range "$hold" 0 30 || hold=20
    if ! read_conf; then
        log "selftest: no $CONF; run the setup first"
        return 1
    fi
    if ! profile_loaded; then
        log "selftest: the joustmania-hotspot profile is not loaded in NetworkManager"
        return 1
    fi
    if ! lock_state 90; then
        log "selftest: a check is running; try again"
        return 1
    fi
    wlan="$(wlan_class)"
    case "$wlan" in
        ap-on | ap-starting)
            log "selftest: the hotspot is in use; the self-test would drop its clients"
            return 1
            ;;
        client-up | client-connecting)
            log "selftest: a Wi-Fi client is up on $WLAN; the self-test would drop it"
            return 1
            ;;
        missing | unavailable)
            log "selftest: $WLAN is $wlan (radio off, rfkill or no Wi-Fi country)"
            return 1
            ;;
    esac
    if [[ "$(uplink_class)" != connected ]]; then
        log "selftest: $C_UPLINK is not connected; the self-test runs only with the uplink connected (UC-061 BR-001)"
        return 1
    fi
    trap hotspot_down EXIT
    if ! hotspot_up; then
        echo "FAIL bring up the hotspot: $UP_ERROR"
        hotspot_down
        return 1
    fi
    check "$WLAN is an active access point of the joustmania-hotspot profile" \
        test "$(wlan_class)" = ap-on
    check "iw: $WLAN is type AP on channel $C_CHANNEL" iw_is_ap_on_channel
    check "$WLAN has $AP_ADDR/24" wlan_has_ap_addr
    check "NetworkManager's dnsmasq serves DHCP on $WLAN" dnsmasq_running
    if ((SELFTEST_FAILS == 0 && hold > 0)); then
        log "selftest: hotspot '$C_SSID' up for ${hold}s; join it from a phone now to check the password"
        pause "$hold"
    fi
    hotspot_down
    trap - EXIT
    if ((SELFTEST_FAILS > 0)); then
        log "selftest: $SELFTEST_FAILS check(s) failed; the hotspot is down again"
        return 1
    fi
    log "selftest: passed; the hotspot is down again"
    return 0
}

# --- remove ------------------------------------------------------------------------

remove() {
    local f
    # Disarm first (#2196): without the hook and the configuration nothing can
    # start a check that brings the hotspot or the ACT LED cue back; a check
    # already started finds no configuration and only takes the hotspot down
    rm -f "$ROOT/etc/NetworkManager/dispatcher.d/90-joustmania-hotspot" "$CONF"
    systemctl disable --now joustmania-hotspot.timer joustmania-hotspot.service >/dev/null 2>&1 || true
    # a check that still runs ends on its next pass (no configuration); wait for it
    lock_state 60 || log "a check still holds $STATE_DIR/lock; going on"
    hotspot_down
    # the cue lives in the LED helper's record, which outlives STATE_DIR
    led_cue removed
    nmcli connection delete uuid "$UUID" >/dev/null 2>&1 || true
    for f in \
        "$ROOT/etc/NetworkManager/dispatcher.d/90-joustmania-hotspot" \
        "$ROOT/etc/NetworkManager/system-connections/joustmania-hotspot.nmconnection" \
        "$ROOT/etc/systemd/system/joustmania-hotspot.service" \
        "$ROOT/etc/systemd/system/joustmania-hotspot.timer"; do
        rm -f "$f"
    done
    # hidden temp keyfiles a crashed setup left behind (they hold the password)
    rm -f "$ROOT/etc/NetworkManager/system-connections"/.joustmania-hotspot.*
    rm -rf "$STATE_DIR"
    systemctl daemon-reload >/dev/null 2>&1 || true
    log "removed the hotspot profile, unit, timer, hook and configuration"
    # last: the installed copy of this script
    rm -f "$ROOT/usr/local/lib/joustmania/joustmania-hotspot"
    return 0
}

case "${1:-}" in
    evaluate) evaluate "${2:-}" ;;
    status) status ;;
    render) render "${2:-}" "${3:-}" "${4:-}" "${5:-}" ;;
    selftest) selftest "${2:-}" ;;
    remove) remove ;;
    *)
        echo "usage: joustmania-hotspot evaluate [--once] | status | render TEMPLATE OUT SSID CHANNEL | selftest [HOLD_S] | remove" >&2
        exit 2
        ;;
esac
