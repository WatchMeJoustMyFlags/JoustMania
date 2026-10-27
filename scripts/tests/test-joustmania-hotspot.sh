#!/usr/bin/env bash
# The stage hotspot fallback (UC-061, FR-184, FR-186, FR-187; #2099). Runs the
# helper and the NM dispatcher hook against stub nmcli / iw / systemctl and a
# fake clock; no real network device, unit or /run is touched:
#  - evaluate: up only after the uplink was not stably connected for the delay
#    (20 s; 60 s while connecting or with no IPv4 address, D1), never in the
#    boot hold, never over a Wi-Fi client that is up or connecting (A2); down
#    only after 15 s connected without a break (A1); the state-based debounce
#    brings it up under a flapping link and never toggles (A6); a hotspot that
#    dropped comes back (A7); failures are recorded and exit 0 (A3, BR-006);
#    not set up / enabled=no does nothing but take it down (A5)
#  - the conf is parsed against an allow-list, never sourced (BR-010)
#  - the status file is format 1, 0644, with no secret (FR-187)
#  - render writes the password verbatim (bash 5.2 `&` safe) into a 0600 file,
#    refuses bad passwords, SSIDs and channels, and never prints it (BR-004)
#  - the helper never asks NM for secrets and refuses `bash -x` (BR-004)
#  - the hook maps NM events to `systemctl start --no-block`, never a restart
#  - selftest only with the uplink connected and wlan0 free (BR-001), always
#    takes the hotspot down again; remove deletes every installed file
# Runs as part of `make lint`, or directly: ./scripts/tests/test-joustmania-hotspot.sh
# UC: UC-061 main A1 A2 A3 A4 A5 A6 A7 BR-001 BR-004 BR-006 BR-008 BR-010
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HELPER="${ROOT}/scripts/setup/joustmania-hotspot.sh"
HOOK="${ROOT}/scripts/setup/90-joustmania-hotspot"
TEMPLATE="${ROOT}/scripts/setup/joustmania-hotspot.nmconnection.template"
UUID=e0602321-f72b-408b-983e-56ee57d519ee
# A test that reaches for the real docker fails instead of using it
export PATH="${ROOT}/scripts/tests/lib/fail-docker:${PATH}"
unset CONNECTION_UUID || true
fails=0
fail() {
  echo "FAIL $*"
  fails=$((fails + 1))
}

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# --- stubs ----------------------------------------------------------------------
STUBS="${WORK}/stubs"
mkdir -p "${STUBS}"
# nmcli: state in $NM/<file>; every argv line is logged to $CASE/nmcli.argv
cat >"${STUBS}/nmcli" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${CASE}/nmcli.argv"
for a in "$@"; do
  case "$a" in -s | --show-secrets) touch "${CASE}/secrets-requested"; exit 1 ;; esac
done
NM="${CASE}/nm"
get() { cat "${NM}/$1" 2>/dev/null || true; }
dev_show() { # $1 = fields, $2 = device
  [[ -f "${NM}/$2-state" ]] || { echo "Error: Device '$2' not found." >&2; exit 10; }
  local f
  IFS=, read -ra fields <<<"$1"
  for f in "${fields[@]}"; do
    case "$f" in
      GENERAL.STATE) get "$2-state" ;;
      GENERAL.CON-UUID) get "$2-uuid"; echo ;;
      IP4.ADDRESS) get "$2-ip4"; echo ;;
    esac
  done
}
case "$*" in
  "-g connection.uuid connection show uuid $UUID")
    [[ -f "${NM}/profile" ]] && echo "$UUID" || { echo "Error: unknown connection" >&2; exit 10; } ;;
  "-g "*" device show "*) dev_show "$2" "$5" ;;
  "--wait 30 connection up uuid $UUID")
    if [[ -f "${NM}/up-fails" ]]; then
      echo "Error: Connection activation failed: No suitable device found for this connection." >&2
      exit 4
    fi
    echo "100 (connected)" >"${NM}/wlan0-state"; echo "$UUID" >"${NM}/wlan0-uuid"
    printf '%s' "10.42.0.1/24" >"${NM}/wlan0-ip4" ;;
  "connection down uuid $UUID")
    [[ "$(get wlan0-uuid)" == "$UUID" ]] || exit 10
    echo "30 (disconnected)" >"${NM}/wlan0-state"; : >"${NM}/wlan0-uuid"; : >"${NM}/wlan0-ip4" ;;
  "connection delete uuid $UUID") rm -f "${NM}/profile" ;;
  *) echo "nmcli stub: unexpected: $*" >&2; exit 99 ;;
esac
EOF
cat >"${STUBS}/systemctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${CASE}/systemctl.argv"
[[ "$1" == is-system-running ]] && cat "${CASE}/sysstate" 2>/dev/null
exit 0
EOF
cat >"${STUBS}/iw" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  "dev wlan0 station dump")
    n="$(cat "${CASE}/nm/stations" 2>/dev/null || echo 0)"
    for ((i = 0; i < n; i++)); do printf 'Station 02:00:00:00:00:%02d (on wlan0)\n\tinactive time:\t10 ms\n' "$i"; done ;;
  "dev wlan0 info") cat "${CASE}/nm/iw-info" 2>/dev/null ;;
  *) exit 1 ;;
esac
EOF
chmod +x "${STUBS}"/*
export UUID

# make_case <name>: set up, profile loaded, eth0 down, wlan0 free, uptime 1000 s,
# clock at 10000
make_case() {
  CASE="${WORK}/$1"
  NM="${CASE}/nm"
  mkdir -p "${NM}" "${CASE}/etc" "${CASE}/run"
  export CASE
  printf 'enabled=yes\nssid=JoustMania\nchannel=36\n' >"${CASE}/etc/hotspot.conf"
  touch "${NM}/profile"
  set_dev eth0 "20 (unavailable)" ""
  set_dev wlan0 "30 (disconnected)" ""
  echo "1000.00 2000.00" >"${CASE}/uptime"
  echo 10000 >"${CASE}/now"
  touch "${CASE}/dnsmasq" && chmod +x "${CASE}/dnsmasq"
  : >"${CASE}/nmcli.argv"
  : >"${CASE}/systemctl.argv"
  export PATH="${STUBS}:${PATH#"${STUBS}:"}" \
    JOUSTMANIA_HOTSPOT_CONF="${CASE}/etc/hotspot.conf" \
    JOUSTMANIA_HOTSPOT_STATE_DIR="${CASE}/run/joustmania-hotspot" \
    JOUSTMANIA_HOTSPOT_NOW_FILE="${CASE}/now" \
    JOUSTMANIA_HOTSPOT_PROC_UPTIME="${CASE}/uptime" \
    JOUSTMANIA_HOTSPOT_DNSMASQ="${CASE}/dnsmasq" \
    JOUSTMANIA_HOTSPOT_DNSMASQ_PID="${CASE}/dnsmasq.pid" \
    JOUSTMANIA_HOTSPOT_ROOT="${CASE}/root"
}
# set_dev <iface> <state line> <ip4 or uuid>
set_dev() {
  echo "$2" >"${NM}/$1-state"
  if [[ "$1" == wlan0 ]]; then printf '%s' "$3" >"${NM}/wlan0-uuid"; else printf '%s' "$3" >"${NM}/$1-ip4"; fi
}
eth0_connected() { set_dev eth0 "100 (connected)" "192.168.1.50/24"; }
eth0_down() { set_dev eth0 "20 (unavailable)" ""; }
at() { echo "$1" >"${CASE}/now"; }
once() { "$HELPER" evaluate --once >>"${CASE}/out" 2>&1 || fail "${CASE##*/}: evaluate --once exit $?"; }
field() { sed -n "s/^$1=//p" "${CASE}/run/joustmania-hotspot/status" 2>/dev/null; }
ups() { grep -c "connection up uuid ${UUID}" "${CASE}/nmcli.argv" || true; }
downs() { grep -c "connection down uuid ${UUID}" "${CASE}/nmcli.argv" || true; }
expect_state() { # <state> [reason substring]
  local got
  got="$(field state)"
  [[ "$got" == "$1" ]] || fail "${CASE##*/}: state '$got', want '$1' (reason: $(field reason))"
  [[ -z "${2:-}" ]] || [[ "$(field reason)" == *"$2"* ]] || fail "${CASE##*/}: reason '$(field reason)' lacks '$2'"
}

# --- main scenario: up only after the up delay -----------------------------------
make_case main
at 10000; once; expect_state waiting "eth0 down for 0s of 20s"
at 10019; once; expect_state waiting
[[ "$(ups)" == 0 ]] || fail "main: brought the hotspot up before the 20 s delay"
at 10020; once; expect_state on "eth0 down for 20s"
[[ "$(ups)" == 1 ]] || fail "main: want one 'connection up', got $(ups)"
grep -qx -- "--wait 30 connection up uuid ${UUID}" "${CASE}/nmcli.argv" || fail "main: up not by uuid with --wait 30"
[[ "$(head -n 1 "${CASE}/run/joustmania-hotspot/status")" == format=1 ]] || fail "main: status file is not format 1"
[[ "$(stat -c %a "${CASE}/run/joustmania-hotspot/status")" == 644 ]] || fail "main: status file mode is not 0644"
[[ "$(field url)" == "http://10.42.0.1/" && "$(field channel)" == 36 && "$(field ssid)" == JoustMania ]] ||
  fail "main: status fields: $(cat "${CASE}/run/joustmania-hotspot/status")"
"$HELPER" status | grep -qx "state=on" || fail "main: status action does not print the state"

# the unit's own loop: waits on the fake clock until it can act, then ends
make_case loop
"$HELPER" evaluate >"${CASE}/out" 2>&1 || fail "loop: exit $?"
expect_state on
[[ "$(cat "${CASE}/now")" == 10020 ]] || fail "loop: acted at $(cat "${CASE}/now"), want 10020"
[[ "$(grep -c 'waiting' "${CASE}/out")" -le 2 ]] || fail "loop: logs every waiting pass: $(cat "${CASE}/out")"

# a check that cannot act ends after MAX_RUN_S and leaves the rest to the timer
make_case loop-bounded
set_dev wlan0 "70 (connecting (getting IP configuration))" "11111111-2222-3333-4444-555555555555"
JOUSTMANIA_HOTSPOT_MAX_RUN_S=30 "$HELPER" evaluate >"${CASE}/out" 2>&1 || fail "loop-bounded: exit $?"
grep -q "the timer checks again" "${CASE}/out" || fail "loop-bounded: no end line"
(($(cat "${CASE}/now") <= 10035)) || fail "loop-bounded: ran past MAX_RUN_S"

# clients from iw when on
make_case clients
echo 3 >"${NM}/stations"
at 10020; once; at 10040; once
[[ "$(field clients)" == 3 ]] || fail "clients: want 3, got '$(field clients)'"

# --- D1 / BR-008: what counts as connected --------------------------------------
make_case stable
eth0_connected
at 10000; once; at 10015; once; at 10100; once
expect_state off "eth0 connected"
[[ "$(ups)" == 0 ]] || fail "stable: brought the hotspot up next to a connected eth0"

for row in "linklocal|100 (connected)|169.254.10.2/16" "noaddr|100 (connected)|" "ipconfig|70 (connecting (getting IP configuration))|"; do
  IFS='|' read -r name state ip <<<"$row"
  make_case "a4-${name}"
  set_dev eth0 "$state" "$ip"
  at 10000; once; at 10020; once; at 10059; once
  [[ "$(ups)" == 0 ]] || fail "a4-${name}: up before the 60 s connecting wait"
  expect_state waiting "eth0 connecting"
  at 10060; once
  expect_state on
done

# --- A1: the cable returns; down only after 15 s without a break ---------------
make_case a1
at 10000; once; at 10020; once; expect_state on
eth0_connected
at 10030; once; at 10044; once
[[ "$(downs)" == 0 ]] || fail "a1: hotspot down before 15 s of connection"
expect_state on
at 10045; once
[[ "$(downs)" == 1 ]] || fail "a1: want one 'connection down', got $(downs)"
expect_state off
# a connection that breaks before 15 s does not count
make_case a1-break
at 10000; once; at 10020; once
eth0_connected; at 10030; once
eth0_down; at 10040; once
eth0_connected; at 10050; once; at 10060; once
[[ "$(downs)" == 0 ]] || fail "a1-break: a broken connection counted as stable"
at 10065; once
[[ "$(downs)" == 1 ]] || fail "a1-break: not down after 15 s of unbroken connection"
# the unit loop keeps waiting for stable_s instead of ending while on
make_case a1-loop
at 10000; once; at 10020; once
eth0_connected
"$HELPER" evaluate >"${CASE}/out" 2>&1 || fail "a1-loop: exit $?"
expect_state off
(($(cat "${CASE}/now") - 10020 <= 20)) || fail "a1-loop: down only at $(cat "${CASE}/now")"

# --- A6: a flapping link; recorded state, not time since the last event --------
# Up within the 60 s connecting wait however many events arrive, then never
# toggled: eth0 alternates 10 s connected / 5 s down, a check on every event.
make_case a6
first_up=""
for ((t = 10000; t <= 10180; t += 5)); do
  if (((t - 10000) % 15 < 10)); then eth0_connected; else eth0_down; fi
  at "$t"; once; once  # two events in the same second
  if [[ -z "$first_up" && "$(ups)" -ge 1 ]]; then first_up="$t"; fi
done
[[ -n "$first_up" ]] || fail "a6: the hotspot never came up under a flapping link (starved)"
[[ -z "$first_up" ]] || ((first_up - 10000 <= 60)) || fail "a6: up only after $((first_up - 10000)) s"
[[ "$(ups)" == 1 && "$(downs)" == 0 ]] || fail "a6: toggled: $(ups) up, $(downs) down"

# --- A2: a known Wi-Fi network wins ------------------------------------------------
make_case a2-up
set_dev wlan0 "100 (connected)" "11111111-2222-3333-4444-555555555555"
at 10000; once; at 10100; once
expect_state off "Wi-Fi client up"
[[ "$(ups)" == 0 ]] || fail "a2-up: pre-empted a Wi-Fi client"
make_case a2-connecting
set_dev wlan0 "50 (connecting (configuring))" "11111111-2222-3333-4444-555555555555"
at 10000; once; at 10100; once
expect_state waiting "Wi-Fi client connecting"
[[ "$(ups)" == 0 ]] || fail "a2-connecting: pre-empted a connecting Wi-Fi client"
set_dev wlan0 "30 (disconnected)" ""
at 10105; once
expect_state on

# a client that drops after a long time restarts the wait (review F1): NM gets
# up_delay_s to reconnect it before the hotspot takes the radio
make_case a2-drop
set_dev wlan0 "100 (connected)" "11111111-2222-3333-4444-555555555555"
at 10000; once; at 10300; once
expect_state off "Wi-Fi client up"
set_dev wlan0 "30 (disconnected)" ""
at 10301; once; at 10319; once
expect_state waiting "eth0 down"
[[ "$(ups)" == 0 ]] || fail "a2-drop: hotspot up right after a long-lived client dropped"
set_dev wlan0 "60 (connecting (need authentication))" "11111111-2222-3333-4444-555555555555"
at 10322; once; expect_state waiting "Wi-Fi client connecting"
set_dev wlan0 "100 (connected)" "11111111-2222-3333-4444-555555555555"
at 10330; once; expect_state off "Wi-Fi client up"
[[ "$(ups)" == 0 ]] || fail "a2-drop: pre-empted the reconnecting client"
set_dev wlan0 "30 (disconnected)" ""
at 10350; once; at 10370; once
expect_state on

# the drop happens long after the last pass saw the client (the timer runs every
# 60 s): the wait still starts at the first pass after the drop (review F1 delta)
make_case a2-late-drop
set_dev wlan0 "100 (connected)" "11111111-2222-3333-4444-555555555555"
at 10000; once; at 10060; once
set_dev wlan0 "30 (disconnected)" ""
at 10110; once
[[ "$(ups)" == 0 ]] || fail "a2-late-drop: hotspot up at the first pass after the drop"
at 10129; once
[[ "$(ups)" == 0 ]] || fail "a2-late-drop: hotspot up before 20 s after the drop was seen"
at 10130; once
expect_state on

# --- never up next to a connected uplink (review F2, BR-002) -----------------------
make_case f2-connected
set_dev eth0 "70 (connecting (getting IP configuration))" ""
at 10000; once; at 10055; once
eth0_connected
at 10060
"$HELPER" evaluate >"${CASE}/out" 2>&1 || fail "f2-connected: exit $?"
[[ "$(ups)" == 0 ]] || fail "f2-connected: hotspot up next to a connected eth0"
expect_state off "eth0 connected"
(($(cat "${CASE}/now") <= 10075)) || fail "f2-connected: ended at $(cat "${CASE}/now")"

# --- the debounce clock is /proc/uptime, not the wall clock (review F3) ----------
make_case f3-uptime
unset JOUSTMANIA_HOTSPOT_NOW_FILE
echo "1000.10 0" >"${CASE}/uptime"; once
echo "1019.90 0" >"${CASE}/uptime"; once
[[ "$(ups)" == 0 ]] || fail "f3-uptime: up before 20 s of uptime"
echo "1020.00 0" >"${CASE}/uptime"; once
expect_state on "eth0 down for 20s"
[[ "$(cat "${CASE}/run/joustmania-hotspot/unstable_since")" == 1000 ]] || fail "f3-uptime: state not on the uptime clock"

# --- boot hold ---------------------------------------------------------------------
make_case boot-hold
echo "12.30 20.00" >"${CASE}/uptime"
at 10000; once; at 10030; once
expect_state waiting "boot hold"
[[ "$(ups)" == 0 ]] || fail "boot-hold: up during the boot hold"
echo "45.00 90.00" >"${CASE}/uptime"
at 10033; once
expect_state on

# --- A3: the hotspot cannot start; recorded, exit 0, no 2.4 GHz retry ---------
make_case a3-nm
touch "${NM}/up-fails"
at 10000; once; at 10020; once
expect_state failed "No suitable device"
make_case a3-dnsmasq
rm -f "${CASE}/dnsmasq"
at 10000; once; at 10020; once
expect_state failed "dnsmasq-base"
[[ "$(ups)" == 0 ]] || fail "a3-dnsmasq: tried to start without dnsmasq-base"
make_case a3-nowlan
rm -f "${NM}/wlan0-state"
at 10000; once; expect_state failed "no wlan0"
make_case a3-radio
set_dev wlan0 "20 (unavailable)" ""
at 10000; once; expect_state failed "radio off"
# a late wlan0 is picked up by the next check (timer)
set_dev wlan0 "30 (disconnected)" ""
at 10060; once; expect_state on

# --- A7: the hotspot dropped while in use ----------------------------------------
make_case a7
at 10000; once; at 10020; once; expect_state on
set_dev wlan0 "30 (disconnected)" ""
at 10200; once
expect_state on
[[ "$(ups)" == 2 ]] || fail "a7: a dropped hotspot did not come back"

# --- A5: not set up, switched off ---------------------------------------------------
make_case a5-noconf
rm -f "${CASE}/etc/hotspot.conf"
at 10100; once; expect_state not-set-up
[[ "$(ups)" == 0 ]] || fail "a5-noconf: up without a configuration"
make_case a5-disabled
at 10000; once; at 10020; once; expect_state on
printf 'enabled=no\n' >"${CASE}/etc/hotspot.conf"
at 10030; once; expect_state disabled
[[ "$(downs)" == 1 ]] || fail "a5-disabled: a running hotspot was not taken down"
make_case a5-noprofile
rm -f "${NM}/profile"
at 10100; once; expect_state not-set-up "profile"
[[ "$(ups)" == 0 ]] || fail "a5-noprofile: up without a profile"

# --- BR-010: allow-list, never sourced ----------------------------------------------
make_case conf
cat >"${CASE}/etc/hotspot.conf" <<EOF
enabled=yes
channel=1
ssid=Bad;SSID
uplink_iface=enp1s0
\$(touch ${CASE}/pwned)
psk=SecretValue99
up_delay_s=9999
EOF
at 10000; once
[[ ! -e "${CASE}/pwned" ]] || fail "conf: the configuration was executed"
[[ "$(field channel)" == 36 && "$(field ssid)" == JoustMania ]] || fail "conf: invalid values were not replaced by defaults"
[[ "$(field uplink)" == enp1s0 ]] || fail "conf: uplink_iface not used"
grep -q "device show enp1s0" "${CASE}/nmcli.argv" || fail "conf: did not read the configured uplink"
grep -q "ignoring unknown key 'psk'" "${CASE}/out" || fail "conf: unknown key not reported"
grep -q "invalid value for 'channel'" "${CASE}/out" || fail "conf: invalid value not reported"
! grep -q "SecretValue99\|Bad;SSID" "${CASE}/out" || fail "conf: a value was logged"
at 10020; once; expect_state on # up_delay_s=9999 out of range: the 20 s default

# --- BR-004: never secrets, never under bash -x ------------------------------------
for c in "${WORK}"/*/; do
  [[ ! -e "${c}secrets-requested" ]] || fail "$(basename "$c"): the helper asked nmcli for secrets"
done
if grep -hv '^[[:space:]]*#' "$HELPER" "$HOOK" | grep -qE -- 'nmcli.* (-s|--show-secrets)( |$)|set -x'; then
  fail "helper or hook calls nmcli -s/--show-secrets or set -x"
fi
make_case trace
if bash -x "$HELPER" status >"${CASE}/out" 2>&1; then fail "trace: ran under bash -x"; fi
grep -q "refusing to run with shell tracing on" "${CASE}/out" || fail "trace: no refusal line"

# --- render (BR-004) ----------------------------------------------------------------
make_case render
n=0
# shellcheck disable=SC2016 # literal hostile passwords, never expanded
for psk in 'Joust&Mania1' '&&&&&&&&' 'p@PSK@ss&w0rd' "a#b;c\$d\"e'f!" '`id`$(id)%s&1|<>' '~!@#$%^&*()_+{}[]:;?/.,=-' \
  "$(printf 'x%.0s' {1..63})" '12345678'; do
  n=$((n + 1))
  out="${CASE}/r${n}.nmconnection"
  printf '%s\n' "$psk" | "$HELPER" render "$TEMPLATE" "$out" "Joust Mania" 44 >"${CASE}/rout" 2>&1 ||
    fail "render #$n: exit $? ($(cat "${CASE}/rout"))"
  [[ "$(grep -c '^psk=' "$out")" == 1 ]] || fail "render #$n: not exactly one psk line"
  grep -qxF -- "psk=${psk}" "$out" || fail "render #$n: password not stored verbatim (bash patsub?)"
  grep -qxF "ssid=Joust Mania" "$out" || fail "render #$n: ssid line"
  grep -qxF "channel=44" "$out" || fail "render #$n: channel line"
  ! grep -q '@PSK@\|@SSID@\|@CHANNEL@' <(grep -v '^#\|^psk=' "$out") || fail "render #$n: placeholder left"
  [[ "$(stat -c %a "$out")" == 600 ]] || fail "render #$n: mode $(stat -c %a "$out"), want 600"
  ! grep -qF -- "$psk" "${CASE}/rout" || fail "render #$n: password printed"
done
for psk in 'short7!' "$(printf 'y%.0s' {1..64})" 'with space1' 'back\slash1' $'tab\there1' 'pässwort12' ''; do
  n=$((n + 1))
  out="${CASE}/r${n}.nmconnection"
  if printf '%s\n' "$psk" | "$HELPER" render "$TEMPLATE" "$out" JoustMania 36 >"${CASE}/rout" 2>&1; then
    fail "render #$n: accepted an invalid password"
  fi
  [[ ! -e "$out" ]] || fail "render #$n: wrote a file for an invalid password"
  [[ -z "$psk" ]] || ! grep -qF -- "$psk" "${CASE}/rout" || fail "render #$n: invalid password printed"
done
for args in "JoustMania 6" "JoustMania 52" " lead 36" "trail  36" "$(printf 'S%.0s' {1..33}) 36" "semi;colon 36"; do
  n=$((n + 1))
  ssid="${args% *}" ch="${args##* }"
  if printf 'GoodPass99\n' | "$HELPER" render "$TEMPLATE" "${CASE}/r${n}" "$ssid" "$ch" >/dev/null 2>&1; then
    fail "render: accepted ssid '$ssid' channel '$ch'"
  fi
done
touch "${CASE}/exists"
if printf 'GoodPass99\n' | "$HELPER" render "$TEMPLATE" "${CASE}/exists" JoustMania 36 >/dev/null 2>&1; then
  fail "render: overwrote an existing file"
fi

# --- dispatcher hook ----------------------------------------------------------------
make_case hook
hook() { CONNECTION_UUID="${3:-}" "$HOOK" "$1" "$2"; }
starts() { grep -c '^start --no-block joustmania-hotspot.service$' "${CASE}/systemctl.argv" || true; }
for ev in "eth0 up" "eth0 down" "eth0 dhcp4-change" "wlan0 down"; do
  : >"${CASE}/systemctl.argv"
  # shellcheck disable=SC2086
  hook $ev "$UUID"
  [[ "$(starts)" == 1 ]] || fail "hook: '$ev' did not start the check"
done
for ev in "eth0 connectivity-change" "eth0 pre-up" "wlan0 up" "docker0 down" "enp1s0 down" " hostname"; do
  : >"${CASE}/systemctl.argv"
  # shellcheck disable=SC2086
  hook $ev "$UUID"
  [[ "$(starts)" == 0 ]] || fail "hook: '$ev' started the check"
done
: >"${CASE}/systemctl.argv"
for ((i = 0; i < 10; i++)); do hook eth0 down; hook eth0 up; done
[[ "$(starts)" == 20 ]] || fail "hook: a burst of 20 events gave $(starts) starts"
! grep -q restart "${CASE}/systemctl.argv" || fail "hook: used a restart"
printf 'uplink_iface=enp1s0\n' >>"${CASE}/etc/hotspot.conf"
: >"${CASE}/systemctl.argv"
hook enp1s0 down; hook eth0 down
[[ "$(starts)" == 1 ]] || fail "hook: uplink_iface not honoured"
printf 'uplink_iface=enp2s0\r\n' >"${CASE}/etc/hotspot.conf"
: >"${CASE}/systemctl.argv"
hook enp2s0 down
[[ "$(starts)" == 1 ]] || fail "hook: a CRLF conf line was not read"
printf 'uplink_iface=enp1s0\n' >"${CASE}/etc/hotspot.conf"
echo stopping >"${CASE}/sysstate"
: >"${CASE}/systemctl.argv"
hook enp1s0 down
[[ "$(starts)" == 0 ]] || fail "hook: started the check during shutdown"
rm -f "${CASE}/sysstate" "${CASE}/etc/hotspot.conf"
: >"${CASE}/systemctl.argv"
hook eth0 down
[[ "$(starts)" == 0 ]] || fail "hook: acted without a configuration"

# --- selftest (BR-001 exception) ------------------------------------------------------
selftest_case() {
  make_case "$1"
  eth0_connected
  printf 'Interface wlan0\n\ttype AP\n\tchannel 36 (5180 MHz), width: 20 MHz\n' >"${NM}/iw-info"
  echo $$ >"${CASE}/dnsmasq.pid"
}
selftest_case selftest
"$HELPER" selftest 3 >"${CASE}/out" 2>&1 || fail "selftest: exit $? ($(cat "${CASE}/out"))"
[[ "$(grep -c '^PASS' "${CASE}/out")" == 4 ]] || fail "selftest: want 4 PASS lines: $(cat "${CASE}/out")"
[[ "$(ups)" == 1 && "$(downs)" == 1 ]] || fail "selftest: not brought up and down once"
[[ "$(cat "${CASE}/now")" == 10003 ]] || fail "selftest: did not hold for 3 s"
selftest_case selftest-badchannel
printf 'Interface wlan0\n\ttype AP\n\tchannel 40 (5200 MHz)\n' >"${NM}/iw-info"
if "$HELPER" selftest 0 >"${CASE}/out" 2>&1; then fail "selftest-badchannel: passed on the wrong channel"; fi
[[ "$(downs)" == 1 ]] || fail "selftest-badchannel: hotspot left up"
selftest_case selftest-noeth
eth0_down
if "$HELPER" selftest 0 >"${CASE}/out" 2>&1; then fail "selftest-noeth: ran without a connected uplink"; fi
[[ "$(ups)" == 0 ]] || fail "selftest-noeth: brought the hotspot up"
selftest_case selftest-client
set_dev wlan0 "100 (connected)" "11111111-2222-3333-4444-555555555555"
if "$HELPER" selftest 0 >"${CASE}/out" 2>&1; then fail "selftest-client: would drop a Wi-Fi client"; fi
[[ "$(ups)" == 0 ]] || fail "selftest-client: brought the hotspot up"
selftest_case selftest-upfails
touch "${NM}/up-fails"
if "$HELPER" selftest 0 >"${CASE}/out" 2>&1; then fail "selftest-upfails: passed"; fi
grep -q "^FAIL bring up the hotspot" "${CASE}/out" || fail "selftest-upfails: no FAIL line"

# --- remove -------------------------------------------------------------------------
make_case remove
R="${CASE}/root"
mkdir -p "$R/etc/NetworkManager/dispatcher.d" "$R/etc/NetworkManager/system-connections" \
  "$R/etc/systemd/system" "$R/usr/local/lib/joustmania" "${CASE}/run/joustmania-hotspot"
touch "$R/etc/NetworkManager/dispatcher.d/90-joustmania-hotspot" \
  "$R/etc/NetworkManager/system-connections/joustmania-hotspot.nmconnection" \
  "$R/etc/systemd/system/joustmania-hotspot.service" "$R/etc/systemd/system/joustmania-hotspot.timer" \
  "$R/usr/local/lib/joustmania/joustmania-hotspot" "$R/usr/local/lib/joustmania/joustmania-led"
at 10000; once; at 10020; once
"$HELPER" remove >"${CASE}/out" 2>&1 || fail "remove: exit $?"
left="$(cd "$R" && find . -type f | sort | tr '\n' ' ')"
[[ "$left" == "./usr/local/lib/joustmania/joustmania-led " ]] || fail "remove: left behind: $left"
[[ ! -e "${CASE}/etc/hotspot.conf" && ! -e "${CASE}/run/joustmania-hotspot" ]] || fail "remove: conf or state left"
grep -q "disable --now joustmania-hotspot.timer joustmania-hotspot.service" "${CASE}/systemctl.argv" || fail "remove: units not disabled"
[[ "$(downs)" == 1 ]] || fail "remove: hotspot not taken down"
grep -q "connection delete uuid ${UUID}" "${CASE}/nmcli.argv" || fail "remove: profile not deleted"

if [[ "$fails" -gt 0 ]]; then
  echo "test-joustmania-hotspot: ${fails} failure(s)"
  exit 1
fi
echo "test-joustmania-hotspot: all checks passed"
