#!/bin/bash
# Set up the stage hotspot fallback (UC-061, FR-186; #2099). Run as root, at
# home with the cable in: it checks the Pi first and changes nothing when a
# check fails, installs root-owned copies of the helper, unit, timer and hook,
# writes the password into the 0600 NM keyfile, proves the access point with a
# forced self-test and only then arms the fallback.
#
#   sudo scripts/setup/install_hotspot.sh [--ssid NAME] [--channel 36|40|44|48]
#        [--uplink IFACE] [--psk-file FILE | --psk-stdin] [--hold SECONDS]
#        [--firewall-rules-added] [--pause-wifi-client]
#   sudo scripts/setup/install_hotspot.sh --remove
#
# The password (UC-061 BR-004): prompted twice on the terminal (`IFS= read -rs`),
# or the first line of --psk-file (a regular 0600 file owned by the caller; delete
# it afterwards), or one line on stdin with --psk-stdin (automation; never
# `echo pw | sudo ...`, which lands in the shell history). It is never an
# argument, never logged, never printed; the script refuses to run under `bash -x`.
#
# A Wi-Fi client up on wlan0 blocks the self-test (the hotspot needs the radio).
# --pause-wifi-client (or answering the prompt on a terminal) disconnects it for
# the self-test and reconnects the same connection afterwards, also on failure
# (A11). It refuses when an SSH session runs over wlan0, or when it cannot tell;
# a reconnect that fails exits non-zero.
#
# Re-running it changes the password, SSID or channel (A8): all file work is done
# first, the reactivation last and detached, so a session over the hotspot that
# drops cannot leave a half-done setup.
#
# Test overrides: JOUSTMANIA_HOTSPOT_ROOT (prefix for every installed path),
# JOUSTMANIA_HOTSPOT_DNSMASQ, JOUSTMANIA_HOTSPOT_PROC and _SELF_PID (a fake /proc
# for the A11 session walk), and PATH stubs for nmcli, iw, ip, rfkill, ss, ufw,
# firewall-cmd, systemctl, systemd-run, sysctl, apt-get and id. The helper's own
# overrides (JOUSTMANIA_HOTSPOT_STATE_DIR, ...) pass through to its selftest.
case $- in
    *x*)
        echo "install_hotspot: refusing to run with shell tracing on (UC-061 BR-004)" >&2
        exit 2
        ;;
esac
set -uo pipefail
export LC_ALL=C
shopt -u patsub_replacement 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${JOUSTMANIA_HOTSPOT_ROOT:-}"
readonly UUID=e0602321-f72b-408b-983e-56ee57d519ee
LIB_DIR="$ROOT/usr/local/lib/joustmania"
HELPER="$LIB_DIR/joustmania-hotspot"
UNIT_DIR="$ROOT/etc/systemd/system"
HOOK="$ROOT/etc/NetworkManager/dispatcher.d/90-joustmania-hotspot"
KEYFILE_DIR="$ROOT/etc/NetworkManager/system-connections"
KEYFILE="$KEYFILE_DIR/joustmania-hotspot.nmconnection"
CONF_DIR="$ROOT/etc/joustmania"
CONF="$CONF_DIR/hotspot.conf"
SYSCTL_FILE="$ROOT/etc/sysctl.d/90-joustmania-ip-forward.conf"
DNSMASQ="${JOUSTMANIA_HOTSPOT_DNSMASQ:-/usr/sbin/dnsmasq}"
WLAN=wlan0

say() { echo "install_hotspot: $*"; }
die() {
    echo "install_hotspot: $*" >&2
    exit 1
}

SSID=JoustMania CHANNEL=36 UPLINK=eth0 PSK_FILE="" PSK_STDIN=0 HOLD=20 FIREWALL_OK=0 REMOVE=0 PAUSE=0
while (($#)); do
    case "$1" in
        --ssid) SSID="${2:-}" && shift ;;
        --channel) CHANNEL="${2:-}" && shift ;;
        --uplink) UPLINK="${2:-}" && shift ;;
        --psk-file) PSK_FILE="${2:-}" && shift ;;
        --psk-stdin) PSK_STDIN=1 ;;
        --hold) HOLD="${2:-}" && shift ;;
        --firewall-rules-added) FIREWALL_OK=1 ;;
        --pause-wifi-client) PAUSE=1 ;;
        --remove) REMOVE=1 ;;
        -h | --help)
            sed -n '2,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "unknown argument '${1:0:40}' (see --help)" ;;
    esac
    shift
done

[[ "$(id -u)" == 0 ]] || die "run as root: sudo $0"

if ((REMOVE)); then
    # The installed remover, so an old checkout without this script can still undo it
    if [[ -x "$HELPER" ]]; then
        JOUSTMANIA_HOTSPOT_ROOT="$ROOT" "$HELPER" remove
    else
        JOUSTMANIA_HOTSPOT_ROOT="$ROOT" "$SCRIPT_DIR/joustmania-hotspot.sh" remove
    fi
    say "the ip_forward drop-in $SYSCTL_FILE stays (Docker needs forwarding anyway)"
    exit 0
fi

# --- arguments (the helper's rules, UC-061 BR-003, BR-004, BR-010) ------------
[[ "$SSID" =~ ^[A-Za-z0-9_-]([A-Za-z0-9\ _-]{0,30}[A-Za-z0-9_-])?$ ]] ||
    die "invalid --ssid: 1 to 32 characters of A-Z a-z 0-9 _ - and space, no leading or trailing space"
[[ "$CHANNEL" =~ ^(36|40|44|48)$ ]] || die "invalid --channel: 36, 40, 44 or 48 (non-DFS 5 GHz, UC-061 BR-003)"
[[ "$UPLINK" =~ ^[A-Za-z0-9_.-]{1,15}$ && "$UPLINK" != "$WLAN" ]] || die "invalid --uplink '${UPLINK:0:20}'"
[[ "$HOLD" =~ ^[0-9]{1,2}$ ]] && ((HOLD <= 30)) || die "invalid --hold: 0 to 30 seconds"
[[ -z "$PSK_FILE" || "$PSK_STDIN" == 0 ]] || die "use either --psk-file or --psk-stdin"

# --- pre-checks: collect every problem, change nothing ---------------------------
problems=()
problem() { problems+=("$1"); }

# The phy of wlan0 and the line of the channel in `iw phy <phy> info`
channel_line() {
    local phy
    phy="$(iw dev "$WLAN" info 2>/dev/null | sed -n 's/^[[:space:]]*wiphy \([0-9]\+\).*/phy\1/p' | head -n 1)"
    [[ -n "$phy" ]] || return 1
    iw phy "$phy" info 2>/dev/null | grep -E "^[[:space:]]*\* [0-9.]+ MHz \[$CHANNEL\]" | head -n 1
}

# Listeners on port $2 of protocol $1 (udp/tcp) that are not on a loopback address
foreign_listeners() {
    local flag=-Hlnu
    [[ "$1" == tcp ]] && flag=-Hlnt
    ss "$flag" 2>/dev/null | awk -v port=":$2" '{ a = $4; if (substr(a, length(a) - length(port) + 1) == port) print a }' |
        grep -vE '^(127\.|\[::1\]|::1)' || true
}

# A8: is the hotspot itself active right now (a change over the hotspot)?
hotspot_active() {
    [[ "$(nmcli -g GENERAL.STATE,GENERAL.CON-UUID device show "$WLAN" 2>/dev/null | sed -n 2p)" == "$UUID" ]]
}
# Decided once, before any change: a later probe could see the hotspot drop
# mid-run and take the wrong branch (a cable-less Pi would lose its only access)
# The uplink connected: NM state 100 and an IPv4 address outside 169.254/16 (BR-008)
uplink_connected() {
    local out
    out="$(nmcli -g GENERAL.STATE,IP4.ADDRESS device show "$UPLINK" 2>/dev/null)" || return 1
    [[ "$(head -n 1 <<<"$out")" == 100* ]] || return 1
    tail -n +2 <<<"$out" | tr '|' '\n' | sed 's/^ *//' | grep -E '^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]+' | grep -qv '^169\.254\.'
}
# A8 only without a connected uplink: with the cable in (the 15 s overlap of A1)
# the normal cable path applies, with the self-test
HS_UP=0 A8=0
hotspot_active && HS_UP=1
((HS_UP)) && ! uplink_connected && A8=1

# Is this session itself carried by the hotspot? SSH_CONNECTION's local address
# (when sudo kept it) or the login host `who -m` shows, in 10.42.0.0/24
session_over_hotspot() {
    local server_ip from
    server_ip="$(awk '{ print $3 }' <<<"${SSH_CONNECTION:-}")"
    [[ "$server_ip" == 10.42.0.* ]] && return 0
    from="$(who -m 2>/dev/null | sed -n 's/.*(\([^)]*\)).*/\1/p' | head -n 1)"
    [[ "$from" == 10.42.0.* ]]
}

# A11: the uuid of a Wi-Fi client connection up or connecting on wlan0 (empty
# when none), decided once before any change
wifi_client_uuid() {
    local out state uuid
    out="$(nmcli -g GENERAL.STATE,GENERAL.CON-UUID device show "$WLAN" 2>/dev/null)" || return 0
    state="${out%%$'\n'*}"
    state="${state%% *}"
    uuid="$(sed -n 2p <<<"$out")"
    [[ "$state" =~ ^[0-9]+$ ]] && ((state >= 40 && state <= 100)) || return 0
    [[ -n "$uuid" && "$uuid" != "$UUID" ]] && echo "$uuid"
    return 0
}
CLIENT_UUID="$(wifi_client_uuid)"
CLIENT_PAUSED=0
RESUME_FAILED=0

# A11: is this session carried by wlan0 (pausing the client would cut the
# Operator off)? Fails closed. Prints the reason and returns 0 when an SSH peer
# routes over wlan0, or when it cannot tell (an SSH, mosh, tmux or screen
# ancestor but no peer it can route, `ss` failing, an unroutable peer). Returns
# 1 when every SSH peer routes elsewhere, or when there is no SSH at all (no
# SSH_CONNECTION in this process's or an ancestor's environment, no sshd, mosh,
# tmux or screen ancestor, no SSH connection: a local console). Peers: the
# SSH_CONNECTION of this process and every ancestor (sudo strips it from its
# child, not from itself) and every established sshd connection from `ss`.
PROC="${JOUSTMANIA_HOTSPOT_PROC:-/proc}"
SELF_PID="${JOUSTMANIA_HOTSPOT_SELF_PID:-$$}"
# the address of an `ss` endpoint: no port, no brackets, no ::ffff: prefix
endpoint_addr() {
    local a="${1%:*}"
    a="${a#[}" && a="${a%]}"
    echo "${a#::ffff:}"
}
# the device traffic to <addr> leaves through (empty when unknown)
route_dev() {
    if [[ "$1" == *%* ]]; then
        echo "${1##*%}" # link-local with its zone
        return
    fi
    ip -o route get "$1" 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -n 1
}
session_over_wlan() {
    local pid="$SELF_PID" depth=0 comm conn ss_out loc peer users addr dev wlan_addrs
    local ssh_seen=0 mosh_seen=0 peers=() locals=()
    while [[ "$pid" =~ ^[0-9]+$ ]] && ((pid > 1 && depth < 64)); do
        comm="$(cat "$PROC/$pid/comm" 2>/dev/null)"
        case "$comm" in mosh*) mosh_seen=1 ;; esac
        case "$comm" in sshd* | dropbear* | mosh* | tmux* | screen*) ssh_seen=1 ;; esac
        conn="$(tr '\0' '\n' <"$PROC/$pid/environ" 2>/dev/null | sed -n 's/^SSH_CONNECTION=//p' | head -n 1)"
        if [[ -n "$conn" ]]; then
            ssh_seen=1
            read -r peer _ loc _ <<<"$conn"
            peers+=("$peer") && locals+=("$loc")
        fi
        pid="$(sed -n 's/^PPid:[[:space:]]*//p' "$PROC/$pid/status" 2>/dev/null)"
        depth=$((depth + 1))
    done
    if ((mosh_seen)); then # roams: its UDP session may run over wlan0, invisible to ss -t
        echo "a mosh session cannot be checked (it may run over $WLAN)"
        return 0
    fi
    if [[ -n "${SSH_CONNECTION:-}" ]]; then
        ssh_seen=1
        read -r peer _ loc _ <<<"$SSH_CONNECTION"
        peers+=("$peer") && locals+=("$loc")
    fi
    if ss_out="$(ss -Htnp state established 2>/dev/null)"; then
        while read -r _ _ loc peer users; do
            [[ -n "$peer" ]] || continue
            [[ "$loc" == *:22 || "$users" == *'"sshd'* || "$users" == *'"dropbear'* ]] || continue
            peers+=("$(endpoint_addr "$peer")") && locals+=("$(endpoint_addr "$loc")")
        done <<<"$ss_out"
    elif ((ssh_seen)); then
        echo "cannot tell which interface this session uses (ss failed)"
        return 0
    fi
    if ((${#peers[@]} == 0)); then
        ((ssh_seen)) || return 1 # a local console
        echo "cannot tell which interface this session uses (no SSH peer found)"
        return 0
    fi
    wlan_addrs="$(ip -o addr show dev "$WLAN" 2>/dev/null | awk '{ print $4 }' | sed 's|/.*||')"
    for addr in "${locals[@]}"; do
        if [[ "$addr" == *%"$WLAN" ]] || { [[ -n "$addr" ]] && grep -qxF -- "${addr%%\%*}" <<<"$wlan_addrs"; }; then
            echo "an SSH session runs over $WLAN (local address $addr)"
            return 0
        fi
    done
    for addr in "${peers[@]}"; do
        dev="$(route_dev "$addr")"
        if [[ -z "$dev" ]]; then
            echo "cannot tell which interface carries the SSH peer '${addr:0:60}'"
            return 0
        fi
        if [[ "$dev" == "$WLAN" ]]; then
            echo "an SSH session runs over $WLAN (peer $addr)"
            return 0
        fi
    done
    return 1
}

country=""
precheck() {
    local reg line
    systemctl is-active --quiet NetworkManager ||
        problem "NetworkManager is not running: sudo systemctl enable --now NetworkManager"
    if ! nmcli -g GENERAL.STATE device show "$WLAN" >/dev/null 2>&1; then
        if grep -qs '^[[:space:]]*dtoverlay=disable-wifi' "$ROOT/boot/firmware/config.txt" "$ROOT/boot/config.txt"; then
            problem "no $WLAN: remove dtoverlay=disable-wifi from config.txt and reboot"
        else
            problem "no $WLAN device managed by NetworkManager"
        fi
    fi

    # Wi-Fi country, set now and persisted for the next boot (UC-061 Pre-2)
    reg="$(iw reg get 2>/dev/null)"
    country="$(sed -n 's/^country \([A-Z0-9][A-Z0-9]\):.*/\1/p' <<<"$reg" | head -n 1)"
    if [[ -z "$country" || "$country" == 00 ]]; then
        problem "no Wi-Fi country set: sudo raspi-config nonint do_wifi_country <XX> (for example DE), then reboot"
        country=""
    elif ! grep -qs "cfg80211.ieee80211_regdom=$country" "$ROOT/boot/firmware/cmdline.txt" "$ROOT/boot/cmdline.txt"; then
        problem "the Wi-Fi country $country is not persisted for the next boot: sudo raspi-config nonint do_wifi_country $country"
    fi

    # 5 GHz on this chip and the channel allowed for an access point
    if [[ -n "$country" ]]; then
        if ! line="$(channel_line)" || [[ -z "$line" ]]; then
            problem "the Wi-Fi chip has no 5 GHz channel $CHANNEL (a Pi 3B or Zero 2 W has no 5 GHz)"
        elif grep -qiE 'disabled|no IR' <<<"$line"; then
            problem "channel $CHANNEL is not allowed for an access point in $country ($(sed 's/^[[:space:]]*\* //' <<<"$line")): pick another of 36, 40, 44, 48 with --channel"
        fi
    fi
    if rfkill list wifi 2>/dev/null | grep -qi 'hard blocked: yes'; then
        problem "Wi-Fi is hard-blocked (a hardware switch); unblock it first"
    fi

    # NM's shared mode runs dnsmasq-base; no other DNS or DHCP server in the way
    if [[ ! -x "$DNSMASQ" ]]; then
        say "dnsmasq-base is missing; installing it (NM's shared mode needs it, and it cannot be installed at the venue)"
        apt-get install -y -q --no-install-recommends dnsmasq-base >/dev/null 2>&1 ||
            problem "dnsmasq-base is not installed and apt-get failed: sudo apt-get install dnsmasq-base"
        [[ -x "$DNSMASQ" ]] || problem "dnsmasq-base is still missing ($DNSMASQ)"
    fi
    if systemctl is-active --quiet dnsmasq; then
        problem "the full dnsmasq service runs and blocks NM's DHCP on $WLAN: sudo systemctl disable --now dnsmasq"
    fi
    local who=""
    # with the hotspot up, NM's own dnsmasq holds :53 and :67 on wlan0 (A8)
    ((HS_UP)) || who="$(
        foreign_listeners udp 53
        foreign_listeners tcp 53
        foreign_listeners udp 67
    )"
    [[ -z "$who" ]] || problem "another DNS or DHCP server listens on $(tr '\n' ' ' <<<"$who")(ss -lnup): stop it first"

    # A host firewall with default deny blocks DHCP, DNS, HTTP and SSH on wlan0
    if ((!FIREWALL_OK)); then
        if ufw status 2>/dev/null | grep -q '^Status: active'; then
            problem "ufw is active; add these rules, then re-run with --firewall-rules-added:
    sudo ufw allow in on $WLAN to any port 67 proto udp
    sudo ufw allow in on $WLAN to any port 53
    sudo ufw allow in on $WLAN to any port 80 proto tcp
    sudo ufw allow in on $WLAN to any port 22 proto tcp"
        fi
        if [[ "$(firewall-cmd --state 2>/dev/null)" == running ]]; then
            problem "firewalld is running; add these rules, then re-run with --firewall-rules-added:
    sudo firewall-cmd --permanent --zone=nm-shared --add-interface=$WLAN
    sudo firewall-cmd --permanent --zone=nm-shared --add-service=dhcp --add-service=dns --add-service=http --add-service=ssh
    sudo firewall-cmd --reload"
        fi
    fi
}
# A8: a channel change cannot be proven over the hotspot itself (no uplink for
# the self-test), and a channel that fails to start would lock a cable-less Pi out
OLD_CHANNEL="$(sed -n 's/^channel=\([0-9]\{2\}\)$/\1/p' "$CONF" 2>/dev/null | tail -n 1)"
# The A1 overlap seen from the hotspot: the cable path would take the hotspot
# down under this session, and the SIGHUP would leave the setup disarmed
if ((HS_UP && !A8)) && session_over_hotspot; then
    problem "you are connected over the hotspot while a cable is in: re-run over the cable LAN, or unplug the cable to re-run over the hotspot (A8)"
fi
if ((A8)) && [[ -n "$OLD_CHANNEL" && "$OLD_CHANNEL" != "$CHANNEL" ]]; then
    problem "changing the channel ($OLD_CHANNEL to $CHANNEL) over the hotspot cannot be tested: plug the cable in and re-run (UC-061 A8)"
fi
precheck
if ((${#problems[@]})); then
    for p in "${problems[@]}"; do echo "install_hotspot: REFUSED: $p" >&2; done
    die "${#problems[@]} problem(s); nothing changed"
fi

# A11: offer the pause on a terminal; refuse when this session runs over wlan0
if [[ -n "$CLIENT_UUID" && "$PAUSE" == 0 && -z "$PSK_FILE" && "$PSK_STDIN" == 0 && -t 0 && -r /dev/tty ]]; then
    reply=""
    read -r -p "A Wi-Fi client is up on $WLAN; the self-test needs the radio. Disconnect it for the self-test and reconnect it afterwards? [y/N] " reply </dev/tty || true
    [[ "$reply" =~ ^[Yy] ]] && PAUSE=1
fi
if ((PAUSE)) && [[ -n "$CLIENT_UUID" ]]; then
    if why="$(session_over_wlan)"; then
        die "REFUSED --pause-wifi-client: $why; pausing the Wi-Fi client could cut you off. Re-run over the cable LAN, or, when you are sure this session does not use $WLAN, disconnect the client by hand (sudo nmcli device disconnect $WLAN), re-run without the option and reconnect it afterwards (sudo nmcli connection up uuid $CLIENT_UUID); nothing changed"
    fi
elif ((PAUSE)); then
    say "--pause-wifi-client: no Wi-Fi client is up on $WLAN; nothing to pause"
fi

# The radio on (after the country check: unblocking without a country is not allowed)
rfkill unblock wifi >/dev/null 2>&1 || true
if [[ "$(nmcli radio wifi 2>/dev/null)" != enabled ]]; then
    say "switching the Wi-Fi radio on (nmcli radio wifi on)"
    nmcli radio wifi on >/dev/null 2>&1 || die "cannot switch the Wi-Fi radio on"
fi

# --- the password (BR-004) ---------------------------------------------------------
PSK=""
TMP_KEYFILE=""
CONF_WRITTEN=0
ARMED=0
KEYFILE_BACKUP=""
# write_conf yes|no: the settings, atomically. `enabled=yes` only once the
# self-test passed (or A8): until then neither the hook nor the timer may bring
# the hotspot up (a refused setup must never leave a half-armed fallback)
write_conf() {
    local tmp="$CONF_DIR/.hotspot.conf.$$"
    {
        echo "# Stage hotspot settings (UC-061 BR-010), written by install_hotspot.sh; no password here"
        echo "enabled=$1"
        echo "ssid=$SSID"
        echo "channel=$CHANNEL"
        echo "uplink_iface=$UPLINK"
    } >"$tmp" && chmod 0644 "$tmp" && mv -f "$tmp" "$CONF"
}
# A11: reconnect the paused Wi-Fi client (the same connection); safe to repeat
resume_wifi_client() {
    ((CLIENT_PAUSED)) || return 0
    CLIENT_PAUSED=0
    if nmcli --wait 30 connection up uuid "$CLIENT_UUID" >/dev/null 2>&1; then
        echo "install_hotspot: the Wi-Fi client on $WLAN is reconnected" >&2
    else
        RESUME_FAILED=1
        echo "install_hotspot: FAILED to reconnect the Wi-Fi client on $WLAN: run sudo nmcli connection up uuid $CLIENT_UUID" >&2
    fi
}
cleanup() {
    local rc=$?
    PSK=""
    resume_wifi_client
    [[ -n "$TMP_KEYFILE" ]] && rm -f "$TMP_KEYFILE"
    # A8: put the previous keyfile back when this run did not finish, so the next
    # activation never uses a half-checked profile
    if [[ -n "$KEYFILE_BACKUP" && -f "$KEYFILE_BACKUP" ]]; then
        mv -fT "$KEYFILE_BACKUP" "$KEYFILE" && nmcli connection load "$KEYFILE" >/dev/null 2>&1
        echo "install_hotspot: the previous hotspot profile is restored; nothing changed for its clients" >&2
    fi
    if ((CONF_WRITTEN && !ARMED)); then
        write_conf no && echo "install_hotspot: the fallback is NOT armed (enabled=no in $CONF)" >&2
    fi
    # A11: a client left down is never a success
    ((RESUME_FAILED && rc == 0)) && exit 1
    return 0
}
trap cleanup EXIT

valid_psk() { ((${#1} >= 8 && ${#1} <= 63)) && [[ "$1" =~ ^[!-~]+$ && "$1" != *\\* ]]; }
readonly PSK_RULE="8 to 63 printable ASCII characters, no space and no backslash"

if [[ -n "$PSK_FILE" ]]; then
    caller="${SUDO_USER:-root}"
    [[ -f "$PSK_FILE" && ! -L "$PSK_FILE" ]] || die "--psk-file must be a regular file, not a symlink"
    [[ "$(stat -c %a "$PSK_FILE")" == 600 ]] || die "--psk-file must have mode 0600 (chmod 600 $PSK_FILE)"
    [[ "$(stat -c %U "$PSK_FILE")" == "$caller" ]] || die "--psk-file must be owned by $caller"
    IFS= read -r PSK <"$PSK_FILE" || [[ -n "$PSK" ]] || die "--psk-file is empty"
elif ((PSK_STDIN)); then
    [[ ! -t 0 ]] || die "--psk-stdin reads a pipe, not a terminal (it would echo the password); run without it to be prompted"
    IFS= read -r PSK || [[ -n "$PSK" ]] || die "no password on stdin"
else
    [[ -r /dev/tty ]] || die "no terminal for the password prompt: use --psk-file or --psk-stdin"
    again=""
    IFS= read -rs -p "Hotspot password ($PSK_RULE): " PSK </dev/tty || die "no password entered"
    echo >/dev/tty
    IFS= read -rs -p "Same password again: " again </dev/tty || die "no password entered"
    echo >/dev/tty
    if [[ "$PSK" != "$again" ]]; then
        again=""
        die "the two passwords differ; nothing changed"
    fi
    again=""
fi
valid_psk "$PSK" || die "invalid password: $PSK_RULE (UC-061 BR-004); nothing changed"

# --- file work first (A8) -----------------------------------------------------------
own=()
((EUID == 0)) && own=(-o root -g root)
say "installing the helper, unit, timer and dispatcher hook"
install -d "${own[@]}" -m 0755 "$LIB_DIR" "$UNIT_DIR" "$(dirname "$HOOK")" "$CONF_DIR" "$(dirname "$SYSCTL_FILE")" ||
    die "cannot create the target directories"
install -d "${own[@]}" -m 0700 "$KEYFILE_DIR" || die "cannot create $KEYFILE_DIR"
install "${own[@]}" -m 0755 "$SCRIPT_DIR/joustmania-hotspot.sh" "$HELPER" &&
    install "${own[@]}" -m 0644 "$SCRIPT_DIR/joustmania-hotspot.service" "$UNIT_DIR/joustmania-hotspot.service" &&
    install "${own[@]}" -m 0644 "$SCRIPT_DIR/joustmania-hotspot.timer" "$UNIT_DIR/joustmania-hotspot.timer" &&
    install "${own[@]}" -m 0755 "$SCRIPT_DIR/90-joustmania-hotspot" "$HOOK" || die "cannot install the hotspot files"

CONF_WRITTEN=1
if ((A8)); then
    # Over the hotspot the fallback stays armed throughout: a SIGHUP of this
    # session, a timer pass or a refusal must never take the Pi's only access down
    ARMED=1
    write_conf yes || die "cannot write $CONF"
else
    write_conf no || die "cannot write $CONF"
fi
# A crash of an earlier run may have left a hidden temp keyfile with a password
rm -f "$KEYFILE_DIR"/.joustmania-hotspot.*
if ((A8)) && [[ -f "$KEYFILE" ]]; then
    # Named for the cleanup only once the copy is complete: a partial copy must
    # never be restored over the working profile
    if ! cp -p "$KEYFILE" "$KEYFILE_DIR/.joustmania-hotspot.backup"; then
        rm -f "$KEYFILE_DIR/.joustmania-hotspot.backup"
        die "cannot keep a copy of the current profile; nothing else changed for the hotspot"
    fi
    KEYFILE_BACKUP="$KEYFILE_DIR/.joustmania-hotspot.backup"
fi

# Docker needs forwarding; NM's shared mode must not leave it off when the
# hotspot goes down again (UC-061 Notes)
printf '# JoustMania (UC-061): Docker and the stage hotspot need IPv4 forwarding\nnet.ipv4.ip_forward=1\n' >"$SYSCTL_FILE" &&
    chmod 0644 "$SYSCTL_FILE" || die "cannot write $SYSCTL_FILE"
sysctl -q -p "$SYSCTL_FILE" >/dev/null 2>&1 || say "could not apply $SYSCTL_FILE now; it applies at the next boot"

# The keyfile: a hidden temp name (NM's keyfile plugin ignores hidden files, so a
# crash never leaves a loadable duplicate), the password through a pipe from a
# builtin, 0600, then an atomic move and a load
TMP_KEYFILE="$KEYFILE_DIR/.joustmania-hotspot.$$.$RANDOM"
printf '%s\n' "$PSK" | "$HELPER" render "$SCRIPT_DIR/joustmania-hotspot.nmconnection.template" "$TMP_KEYFILE" "$SSID" "$CHANNEL" ||
    die "could not render the profile"
chmod 0600 "$TMP_KEYFILE" && mv -fT "$TMP_KEYFILE" "$KEYFILE" || die "cannot move the keyfile into place"
TMP_KEYFILE=""
((EUID == 0)) && chown root:root "$KEYFILE"
nmcli connection load "$KEYFILE" >/dev/null 2>&1 || die "nmcli connection load $KEYFILE failed"
[[ "$(nmcli -g connection.uuid connection show uuid "$UUID" 2>/dev/null)" == "$UUID" ]] ||
    die "NetworkManager did not load $KEYFILE (it ignores a keyfile that is not root-owned 0600)"
# The stored password must be the typed one: compared through pipes, never printed
if ! nmcli -e no -s -g 802-11-wireless-security.psk connection show uuid "$UUID" 2>/dev/null |
    cmp -s - <(printf '%s\n' "$PSK"); then
    if ((A8)); then
        die "NetworkManager stored a different password than the one entered"
    fi
    nmcli connection delete uuid "$UUID" >/dev/null 2>&1 || true
    rm -f "$KEYFILE"
    die "NetworkManager stored a different password than the one entered; profile removed"
fi
# and the SSID and channel are the new ones (a re-install finds the old profile too)
if [[ "$(nmcli -e no -g 802-11-wireless.ssid,802-11-wireless.channel connection show uuid "$UUID" 2>/dev/null)" != "$SSID"$'\n'"$CHANNEL" ]]; then
    die "NetworkManager did not take the new SSID or channel from $KEYFILE"
fi
say "profile joustmania-hotspot loaded (SSID '$SSID', 5 GHz channel $CHANNEL)"

systemctl daemon-reload || die "systemctl daemon-reload failed"

# --- prove it, then arm it ------------------------------------------------------------
if ((A8)); then
    # A8: changed over the hotspot itself; no uplink to run the self-test on, the
    # detached reactivation below applies and proves the new settings
    say "the hotspot is in use: self-test skipped; it restarts with the new settings"
    HOTSPOT_ACTIVE=1
else
    HOTSPOT_ACTIVE=0
    if hotspot_active; then
        say "the uplink is connected: taking the hotspot down for the self-test (UC-061 A1)"
        nmcli connection down uuid "$UUID" >/dev/null 2>&1 || true
    fi
    if ((PAUSE)) && [[ -n "$CLIENT_UUID" ]]; then
        say "disconnecting the Wi-Fi client on $WLAN for the self-test; it is reconnected afterwards (UC-061 A11)"
        # device disconnect also blocks NM's autoconnect from taking the radio back
        CLIENT_PAUSED=1
        nmcli device disconnect "$WLAN" >/dev/null 2>&1 || true
    fi
    say "self-test: the hotspot comes up for ${HOLD}s while $UPLINK stays connected (UC-061 BR-001)"
    selftest_ok=1
    JOUSTMANIA_HOTSPOT_CONF="$CONF" "$HELPER" selftest "$HOLD" || selftest_ok=0
    resume_wifi_client
    if ((!selftest_ok)); then
        systemctl disable --now joustmania-hotspot.timer >/dev/null 2>&1 || true
        [[ -z "$CLIENT_UUID" || "$PAUSE" == 1 ]] ||
            say "a Wi-Fi client is up on $WLAN: re-run with --pause-wifi-client to disconnect it for the self-test"
        die "the self-test failed: the fallback is NOT armed. Fix the FAIL lines above and re-run this script"
    fi
fi

write_conf yes || die "cannot write $CONF"
ARMED=1
systemctl enable --now joustmania-hotspot.timer >/dev/null || {
    ((A8)) || ARMED=0
    die "cannot enable joustmania-hotspot.timer"
}
# the new profile is in place and checked: drop the A8 backup
[[ -z "$KEYFILE_BACKUP" ]] || rm -f "$KEYFILE_BACKUP"
KEYFILE_BACKUP=""
if ((HOTSPOT_ACTIVE)); then
    say "you will be disconnected in a few seconds; rejoin '$SSID' with the new password"
    systemd-run --no-block --collect --unit=joustmania-hotspot-reapply --on-active=5 \
        nmcli --wait 30 connection up uuid "$UUID" >/dev/null 2>&1 ||
        say "could not schedule the reactivation; the new settings apply at the next activation"
else
    systemctl start --no-block joustmania-hotspot.service || true
fi

PSK=""
echo ""
say "done: the hotspot '$SSID' comes up when $UPLINK has no connection (UC-061)"
say "join it and open http://10.42.0.1/ ; phones: turn mobile data off (BR-009)"
say "undo: sudo $SCRIPT_DIR/install_hotspot.sh --remove (or sudo /usr/local/lib/joustmania/joustmania-hotspot remove)"
[[ -z "$PSK_FILE" ]] || say "delete the password file now: shred -u $PSK_FILE"
if ((RESUME_FAILED)); then
    echo "install_hotspot: FAILED: the fallback is armed, but the Wi-Fi client on $WLAN is NOT reconnected: run sudo nmcli connection up uuid $CLIENT_UUID" >&2
    exit 1
fi
exit 0
