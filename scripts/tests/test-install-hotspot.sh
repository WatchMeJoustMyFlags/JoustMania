#!/usr/bin/env bash
# The stage hotspot setup (UC-061 FR-186, Pre-2, Pre-3, A8, BR-001, BR-004;
# #2099). Runs scripts/setup/install_hotspot.sh under a fake root prefix against
# stub nmcli, iw, rfkill, ss, ufw, firewall-cmd, systemctl, systemd-run, sysctl,
# apt-get and id; no real network device, unit, /etc or /boot is touched:
#  - every pre-check refuses with its fix and changes nothing: NetworkManager,
#    wlan0, Wi-Fi country (set and persisted), 5 GHz channel present and not
#    disabled / no IR, hard block, dnsmasq-base (installed or refused), the full
#    dnsmasq service, other :53 / :67 listeners, ufw and firewalld (with the
#    exact allow rules; --firewall-rules-added proceeds), root
#  - the password: stdin, a 0600 caller-owned --psk-file (not a symlink),
#    charset limits, never in argv, output or any file but the 0600 keyfile;
#    `bash -x` refused; the keyfile via a hidden temp name, an atomic move and
#    `nmcli connection load`; a stored password that differs is refused
#  - the forced self-test runs with the uplink connected and its failure leaves
#    the fallback disarmed; the radio is switched on; the ip_forward drop-in
#  - A8: over the hotspot the self-test is skipped and the reactivation is
#    detached, with the "you will be disconnected" line
#  - --remove uses the installed remover; the upgrade's stale-files hint
# Runs as part of `make lint`, or directly: ./scripts/tests/test-install-hotspot.sh
# UC: UC-061 Pre-1 Pre-2 Pre-3 A8 BR-001 BR-004
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALL="${ROOT}/scripts/setup/install_hotspot.sh"
UUID=e0602321-f72b-408b-983e-56ee57d519ee
export UUID
# A test that reaches for the real docker fails instead of using it
export PATH="${ROOT}/scripts/tests/lib/fail-docker:${PATH}"
unset SUDO_USER SSH_CONNECTION || true
fails=0
fail() {
  echo "FAIL $*"
  fails=$((fails + 1))
}

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
STUBS="${WORK}/stubs"
mkdir -p "${STUBS}"

# --- stubs (state in $CASE/s/, argv logs in $CASE/*.argv) -------------------
cat >"${STUBS}/nmcli" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${CASE}/nmcli.argv"
S="${CASE}/s"
get() { cat "${S}/$1" 2>/dev/null || true; }
case "$*" in
  "radio wifi") get radio ;;
  "radio wifi on") echo enabled >"${S}/radio" ;;
  "connection load "*)
    [[ -f "${S}/load-ignored" ]] || { touch "${S}/profile"; echo "$3" >"${S}/keyfile-path"; } ;;
  "-g connection.uuid connection show uuid $UUID")
    [[ -f "${S}/profile" ]] && echo "$UUID" || exit 10 ;;
  "-e no -s -g 802-11-wireless-security.psk connection show uuid $UUID")
    if [[ -f "${S}/psk-override" ]]; then cat "${S}/psk-override"; else sed -n 's/^psk=//p' "$(get keyfile-path)"; fi ;;
  "-e no -g 802-11-wireless.ssid,802-11-wireless.channel connection show uuid $UUID")
    if [[ -f "${S}/ssid-override" ]]; then cat "${S}/ssid-override"; else
      sed -n 's/^ssid=//p' "$(get keyfile-path)"; sed -n 's/^channel=//p' "$(get keyfile-path)"; fi ;;
  "-g GENERAL.STATE device show "* | "-g GENERAL.STATE,"*" device show "* | "-g IP4.ADDRESS device show "*)
    dev="${*: -1}"
    [[ -f "${S}/${dev}-state" ]] || exit 10
    IFS=, read -ra fields <<<"$2"
    for f in "${fields[@]}"; do
      case "$f" in
        GENERAL.STATE) get "${dev}-state" ;;
        GENERAL.CON-UUID) get "${dev}-uuid"; echo ;;
        IP4.ADDRESS) get "${dev}-ip4"; echo ;;
      esac
    done ;;
  "--wait 30 connection up uuid $UUID")
    echo "100 (connected)" >"${S}/wlan0-state"; echo "$UUID" >"${S}/wlan0-uuid"; printf '10.42.0.1/24' >"${S}/wlan0-ip4" ;;
  "connection down uuid $UUID")
    [[ "$(get wlan0-uuid)" == "$UUID" ]] || exit 10
    echo "30 (disconnected)" >"${S}/wlan0-state"; : >"${S}/wlan0-uuid"; : >"${S}/wlan0-ip4" ;;
  "connection delete uuid $UUID") rm -f "${S}/profile" ;;
  *) echo "nmcli stub: unexpected: $*" >&2; exit 99 ;;
esac
EOF
cat >"${STUBS}/iw" <<'EOF'
#!/usr/bin/env bash
S="${CASE}/s"
case "$*" in
  "reg get") cat "${S}/reg" ;;
  "dev wlan0 info")
    if [[ "$(cat "${S}/wlan0-uuid" 2>/dev/null)" == "$UUID" ]]; then
      printf 'Interface wlan0\n\twiphy 0\n\ttype AP\n\tchannel %s (5180 MHz)\n' "$(cat "${S}/ap-channel")"
    else
      printf 'Interface wlan0\n\twiphy 0\n\ttype managed\n'
    fi ;;
  "phy phy0 info") cat "${S}/phy" ;;
  "dev wlan0 station dump") ;;
  *) exit 1 ;;
esac
EOF
cat >"${STUBS}/systemctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${CASE}/systemctl.argv"
case "$*" in
  "is-active --quiet NetworkManager") [[ ! -f "${CASE}/s/nm-down" ]] ;;
  "is-active --quiet dnsmasq") [[ -f "${CASE}/s/dnsmasq-active" ]] ;;
  *) exit 0 ;;
esac
EOF
cat >"${STUBS}/ss" <<'EOF'
#!/usr/bin/env bash
case "$1" in -Hlnu) cat "${CASE}/s/ss-udp" 2>/dev/null ;; -Hlnt) cat "${CASE}/s/ss-tcp" 2>/dev/null ;; esac
exit 0
EOF
cat >"${STUBS}/rfkill" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${CASE}/rfkill.argv"
[[ "$1" == list ]] && cat "${CASE}/s/rfkill" 2>/dev/null
exit 0
EOF
cat >"${STUBS}/ufw" <<'EOF'
#!/usr/bin/env bash
cat "${CASE}/s/ufw" 2>/dev/null || echo "Status: inactive"
EOF
cat >"${STUBS}/firewall-cmd" <<'EOF'
#!/usr/bin/env bash
[[ -f "${CASE}/s/firewalld" ]] && echo running && exit 0
echo "not running"; exit 252
EOF
cat >"${STUBS}/apt-get" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${CASE}/apt.argv"
[[ -f "${CASE}/s/apt-ok" ]] || exit 100
touch "${CASE}/dnsmasq" && chmod +x "${CASE}/dnsmasq"
EOF
# sysctl runs after the conf and before the keyfile: the hook for the A8 cases
# (a timer pass during the run, a session that hangs there and gets a signal)
cat >"${STUBS}/sysctl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${CASE}/sysctl.argv"
if [[ -f "${CASE}/s/sysctl-evaluate" ]]; then
  JOUSTMANIA_HOTSPOT_CONF="${CASE}/root/etc/joustmania/hotspot.conf" \
    "${CASE}/root/usr/local/lib/joustmania/joustmania-hotspot" evaluate --once >>"${CASE}/evaluate.out" 2>&1
fi
if [[ -f "${CASE}/s/sysctl-hang" ]]; then touch "${CASE}/s/hanging"; sleep 30; fi
exit 0
STUB
# cp: a copy to the A8 backup fails halfway when the case asks for it
REAL_CP="$(command -v cp)"
cat >"${STUBS}/cp" <<EOF
#!/usr/bin/env bash
if [[ -f "\${CASE}/s/cp-fails" && "\${*: -1}" == *.joustmania-hotspot.backup ]]; then
  printf 'psk=trunc' >"\${*: -1}"
  exit 1
fi
exec "${REAL_CP}" "\$@"
EOF
cat >"${STUBS}/systemd-run" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${CASE}/systemd-run.argv"
EOF
cat >"${STUBS}/id" <<'EOF'
#!/usr/bin/env bash
[[ "$1" == -u ]] && { cat "${CASE}/s/uid" 2>/dev/null || echo 0; exit 0; }
exec /usr/bin/id "$@"
EOF
chmod +x "${STUBS}"/*

PSK='Joust&Mania1'
# make_case <name>: a Pi that passes every check; eth0 connected, wlan0 free
make_case() {
  CASE="${WORK}/$1"
  S="${CASE}/s"
  R="${CASE}/root"
  mkdir -p "${S}" "${R}/boot/firmware" "${CASE}/run"
  export CASE
  echo "100 (connected)" >"${S}/eth0-state"; printf '192.168.1.50/24' >"${S}/eth0-ip4"
  echo "30 (disconnected)" >"${S}/wlan0-state"; : >"${S}/wlan0-uuid"
  echo enabled >"${S}/radio"
  printf 'global\ncountry DE: DFS-ETSI\n\t(2400 - 2483 @ 40), (N/A, 20), (N/A)\n' >"${S}/reg"
  echo 'console=tty1 root=PARTUUID=1-02 rootwait cfg80211.ieee80211_regdom=DE' >"${R}/boot/firmware/cmdline.txt"
  printf '\t\t\t* 5180.0 MHz [36] (23.0 dBm)\n\t\t\t* 5200.0 MHz [40] (23.0 dBm)\n\t\t\t* 5220.0 MHz [44] (23.0 dBm)\n\t\t\t* 5240.0 MHz [48] (23.0 dBm) (no IR)\n\t\t\t* 5260.0 MHz [52] (20.0 dBm) (no IR, radar detection)\n' >"${S}/phy"
  echo 36 >"${S}/ap-channel"
  printf '0: phy0: Wireless LAN\n\tSoft blocked: no\n\tHard blocked: no\n' >"${S}/rfkill"
  printf 'UNCONN 0 0 127.0.0.53%%lo:53 0.0.0.0:*\nUNCONN 0 0 0.0.0.0:5353 0.0.0.0:*\n' >"${S}/ss-udp"
  printf 'LISTEN 0 4096 0.0.0.0:80 0.0.0.0:*\nLISTEN 0 4096 127.0.0.53%%lo:53 0.0.0.0:*\n' >"${S}/ss-tcp"
  touch "${CASE}/dnsmasq" && chmod +x "${CASE}/dnsmasq"
  echo $$ >"${CASE}/dnsmasq.pid"
  echo 10000 >"${CASE}/now"
  echo "5000.0 9000.0" >"${CASE}/uptime"
  : >"${CASE}/nmcli.argv"; : >"${CASE}/systemctl.argv"
  export PATH="${STUBS}:${PATH#"${STUBS}:"}" \
    JOUSTMANIA_HOTSPOT_ROOT="${R}" \
    JOUSTMANIA_HOTSPOT_DNSMASQ="${CASE}/dnsmasq" \
    JOUSTMANIA_HOTSPOT_DNSMASQ_PID="${CASE}/dnsmasq.pid" \
    JOUSTMANIA_HOTSPOT_STATE_DIR="${CASE}/run/joustmania-hotspot" \
    JOUSTMANIA_HOTSPOT_NOW_FILE="${CASE}/now" \
    JOUSTMANIA_HOTSPOT_PROC_UPTIME="${CASE}/uptime"
}
# run_install [args...]: the password on stdin (--psk-stdin) unless --psk-file is given
run_install() {
  local rc=0
  local mode=--psk-stdin
  [[ " $* " == *" --psk-file "* ]] && mode=""
  printf '%s\n' "${INPUT:-$PSK}" | "$INSTALL" $mode "$@" >"${CASE}/out" 2>&1 || rc=$?
  return "$rc"
}
installed() { [[ -e "${R}/usr/local/lib/joustmania/joustmania-hotspot" ]]; }
no_psk_leak() { # the password only in the keyfile
  local hits
  hits="$(grep -rlF -- "$PSK" "${CASE}" 2>/dev/null | grep -v "/etc/NetworkManager/system-connections/joustmania-hotspot.nmconnection$" | grep -v "/s/psk-override$" || true)"
  [[ -z "$hits" ]] || fail "${CASE##*/}: the password leaked into: ${hits//$'\n'/ }"
}
refused() { # <case> <substring>
  if run_install "${@:3}"; then fail "$1: not refused"; fi
  grep -qF -- "$2" "${CASE}/out" || fail "$1: no '$2' in: $(tail -n 3 "${CASE}/out")"
  ! installed || fail "$1: installed files although refused"
  [[ ! -e "${R}/etc/joustmania/hotspot.conf" ]] || fail "$1: wrote the conf although refused"
}

# --- happy path --------------------------------------------------------------------
make_case happy
run_install --hold 3 || fail "happy: exit $? ($(tail -n 5 "${CASE}/out"))"
mode() { stat -c %a "${R}$1" 2>/dev/null || echo missing; }
[[ "$(mode /usr/local/lib/joustmania/joustmania-hotspot)" == 755 ]] || fail "happy: helper mode"
[[ "$(mode /etc/systemd/system/joustmania-hotspot.service)" == 644 ]] || fail "happy: unit mode"
[[ "$(mode /etc/systemd/system/joustmania-hotspot.timer)" == 644 ]] || fail "happy: timer mode"
[[ "$(mode /etc/NetworkManager/dispatcher.d/90-joustmania-hotspot)" == 755 ]] || fail "happy: hook mode"
[[ "$(mode /etc/joustmania/hotspot.conf)" == 644 ]] || fail "happy: conf mode"
[[ "$(mode /etc/NetworkManager/system-connections/joustmania-hotspot.nmconnection)" == 600 ]] || fail "happy: keyfile mode"
grep -qxF "psk=${PSK}" "${R}/etc/NetworkManager/system-connections/joustmania-hotspot.nmconnection" || fail "happy: password not verbatim in the keyfile"
[[ "$(grep -v '^#' "${R}/etc/joustmania/hotspot.conf")" == $'enabled=yes\nssid=JoustMania\nchannel=36\nuplink_iface=eth0' ]] ||
  fail "happy: conf: $(cat "${R}/etc/joustmania/hotspot.conf")"
grep -qx 'net.ipv4.ip_forward=1' "${R}/etc/sysctl.d/90-joustmania-ip-forward.conf" || fail "happy: no ip_forward drop-in"
[[ -z "$(find "${R}/etc/NetworkManager/system-connections" -name '.*' -type f)" ]] || fail "happy: hidden temp keyfile left"
grep -q "^connection load ${R}/etc/NetworkManager/system-connections/joustmania-hotspot.nmconnection$" "${CASE}/nmcli.argv" || fail "happy: keyfile not loaded"
[[ "$(grep -c '^PASS' "${CASE}/out")" == 4 ]] || fail "happy: self-test did not pass 4 checks: $(cat "${CASE}/out")"
grep -q "connection down uuid ${UUID}" "${CASE}/nmcli.argv" || fail "happy: self-test left the hotspot up"
grep -qx "enable --now joustmania-hotspot.timer" "${CASE}/systemctl.argv" || fail "happy: timer not armed"
grep -qx "start --no-block joustmania-hotspot.service" "${CASE}/systemctl.argv" || fail "happy: no first check"
[[ ! -s "${CASE}/systemd-run.argv" ]] || fail "happy: scheduled a reactivation without an active hotspot"
no_psk_leak

# custom values; a 0600 caller-owned password file
make_case pskfile
echo 44 >"${S}/ap-channel"
echo "100 (connected)" >"${S}/enp1s0-state"; printf '192.168.1.51/24' >"${S}/enp1s0-ip4"
printf '%s\n' "$PSK" >"${CASE}/pw" && chmod 600 "${CASE}/pw"
SUDO_USER="$(id -un)" INPUT=unused run_install --psk-file "${CASE}/pw" --ssid "Stage Pi" --channel 44 --uplink enp1s0 --hold 0 ||
  fail "pskfile: exit $? ($(tail -n 3 "${CASE}/out"))"
grep -q "delete the password file now" "${CASE}/out" || fail "pskfile: no hint to delete the file"
grep -qx "ssid=Stage Pi" "${R}/etc/joustmania/hotspot.conf" && grep -qx "channel=44" "${R}/etc/joustmania/hotspot.conf" &&
  grep -qx "uplink_iface=enp1s0" "${R}/etc/joustmania/hotspot.conf" || fail "pskfile: conf values"
chmod 644 "${CASE}/pw"
make_case pskfile-mode
printf '%s\n' "$PSK" >"${CASE}/pw" && chmod 644 "${CASE}/pw"
SUDO_USER="$(id -un)" refused pskfile-mode "mode 0600" --psk-file "${CASE}/pw"
make_case pskfile-owner
printf '%s\n' "$PSK" >"${CASE}/pw" && chmod 600 "${CASE}/pw"
SUDO_USER=nobody-else refused pskfile-owner "owned by nobody-else" --psk-file "${CASE}/pw"
make_case pskfile-link
printf '%s\n' "$PSK" >"${CASE}/pw" && chmod 600 "${CASE}/pw" && ln -s "${CASE}/pw" "${CASE}/pw-link"
SUDO_USER="$(id -un)" refused pskfile-link "not a symlink" --psk-file "${CASE}/pw-link"

# --- password rules, tracing, root, arguments -------------------------------------
for bad in 'short7!' 'with space1' 'back\slash1' "$(printf 'z%.0s' {1..64})"; do
  make_case "badpsk-${#bad}"
  INPUT="$bad" refused "badpsk '${bad:0:8}'" "invalid password"
  ! grep -qF -- "$bad" "${CASE}/out" || fail "badpsk: password echoed"
done
make_case trace
if bash -x "$INSTALL" --remove >"${CASE}/out" 2>&1; then fail "trace: ran under bash -x"; fi
grep -q "refusing to run with shell tracing on" "${CASE}/out" || fail "trace: no refusal"
make_case notroot
echo 1000 >"${S}/uid"
refused notroot "run as root"
make_case badchannel
refused badchannel "invalid --channel" --channel 52
make_case badssid
refused badssid "invalid --ssid" --ssid "bad;ssid"

# --- pre-checks: refused with the fix, nothing changed -----------------------------
make_case nm-down; touch "${S}/nm-down"; refused nm-down "enable --now NetworkManager"
make_case no-wlan; rm -f "${S}/wlan0-state"; echo 'dtoverlay=disable-wifi' >"${R}/boot/firmware/config.txt"
refused no-wlan "remove dtoverlay=disable-wifi"
make_case country00; printf 'global\ncountry 00: DFS-UNSET\n' >"${S}/reg"; refused country00 "raspi-config nonint do_wifi_country"
make_case country-not-persisted; echo 'console=tty1 rootwait' >"${R}/boot/firmware/cmdline.txt"
refused country-not-persisted "not persisted for the next boot"
make_case noir; refused noir "not allowed for an access point" --channel 48
make_case noir2; printf '\t\t\t* 5180.0 MHz [36] (20.0 dBm) (no IR)\n' >"${S}/phy"; refused noir2 "not allowed for an access point"
make_case no5ghz; printf '\t\t\t* 2412.0 MHz [1] (20.0 dBm)\n' >"${S}/phy"; refused no5ghz "has no 5 GHz channel 36"
make_case hardblock; printf '0: phy0: Wireless LAN\n\tSoft blocked: no\n\tHard blocked: yes\n' >"${S}/rfkill"
refused hardblock "hard-blocked"
make_case nodnsmasq; rm -f "${CASE}/dnsmasq"; refused nodnsmasq "apt-get install dnsmasq-base"
make_case dnsmasq-apt; rm -f "${CASE}/dnsmasq"; touch "${S}/apt-ok"
run_install --hold 0 || fail "dnsmasq-apt: exit $? ($(tail -n 3 "${CASE}/out"))"
grep -q "install -y -q --no-install-recommends dnsmasq-base" "${CASE}/apt.argv" || fail "dnsmasq-apt: not installed"
make_case dnsmasq-service; touch "${S}/dnsmasq-active"; refused dnsmasq-service "systemctl disable --now dnsmasq"
make_case listener67; printf 'UNCONN 0 0 0.0.0.0:67 0.0.0.0:*\n' >>"${S}/ss-udp"; refused listener67 "0.0.0.0:67"
make_case listener53; printf 'LISTEN 0 32 *:53 *:*\n' >>"${S}/ss-tcp"; refused listener53 "*:53"
make_case ufw; echo "Status: active" >"${S}/ufw"
refused ufw "sudo ufw allow in on wlan0 to any port 67 proto udp"
grep -qF "sudo ufw allow in on wlan0 to any port 80 proto tcp" "${CASE}/out" || fail "ufw: no port 80 rule"
run_install --firewall-rules-added --hold 0 || fail "ufw: --firewall-rules-added refused"
make_case firewalld; touch "${S}/firewalld"; refused firewalld "--zone=nm-shared --add-interface=wlan0"
# every problem at once, not one per run
make_case many; touch "${S}/nm-down"; echo "Status: active" >"${S}/ufw"
refused many "2 problem(s)"

# --- radio on, self-test failure, keyfile checks ----------------------------------
make_case radio-off; echo disabled >"${S}/radio"
run_install --hold 0 || fail "radio-off: exit $?"
grep -qx "radio wifi on" "${CASE}/nmcli.argv" || fail "radio-off: radio not switched on"
grep -qx "unblock wifi" "${CASE}/rfkill.argv" || fail "radio-off: no rfkill unblock"

make_case selftest-fails; echo 40 >"${S}/ap-channel"
if run_install --hold 0; then fail "selftest-fails: finished"; fi
grep -q "the fallback is NOT armed" "${CASE}/out" || fail "selftest-fails: no NOT armed line"
grep -qx "disable --now joustmania-hotspot.timer" "${CASE}/systemctl.argv" || fail "selftest-fails: timer not disarmed"
! grep -qx "enable --now joustmania-hotspot.timer" "${CASE}/systemctl.argv" || fail "selftest-fails: timer armed"
make_case selftest-no-uplink; echo "20 (unavailable)" >"${S}/eth0-state"
if run_install --hold 0; then fail "selftest-no-uplink: finished without a connected uplink"; fi
! grep -qx "enable --now joustmania-hotspot.timer" "${CASE}/systemctl.argv" || fail "selftest-no-uplink: timer armed"

make_case psk-mismatch; echo 'Joust@PSK@Mania1' >"${S}/psk-override"
if run_install --hold 0; then fail "psk-mismatch: finished"; fi
grep -q "stored a different password" "${CASE}/out" || fail "psk-mismatch: no refusal line"
[[ ! -e "${R}/etc/NetworkManager/system-connections/joustmania-hotspot.nmconnection" ]] || fail "psk-mismatch: keyfile kept"
grep -q "connection delete uuid ${UUID}" "${CASE}/nmcli.argv" || fail "psk-mismatch: profile not deleted"
make_case load-ignored; touch "${S}/load-ignored"
if run_install --hold 0; then fail "load-ignored: finished"; fi
grep -q "did not load" "${CASE}/out" || fail "load-ignored: no refusal line"

# the password never on a command line of any stub, never in the output
for c in "${WORK}"/*/; do
  for log in "${c}"*.argv "${c}out"; do
    [[ -f "$log" ]] || continue
    ! grep -qF -- "$PSK" "$log" || fail "$(basename "$c"): password in $(basename "$log")"
  done
done

# --- a refused setup is never armed (review F1): enabled=no, the hook does nothing ---
not_armed() { # <case>
  grep -qx 'enabled=no' "${R}/etc/joustmania/hotspot.conf" || fail "$1: conf not enabled=no: $(cat "${R}/etc/joustmania/hotspot.conf" 2>/dev/null)"
  # a Wi-Fi-only Pi whose router reboots: wlan0 down, the hook starts a check
  echo "20 (unavailable)" >"${S}/eth0-state"; echo "30 (disconnected)" >"${S}/wlan0-state"; : >"${S}/wlan0-uuid"
  : >"${CASE}/nmcli.argv"
  echo 20000 >"${CASE}/now"
  JOUSTMANIA_HOTSPOT_CONF="${R}/etc/joustmania/hotspot.conf" "${R}/usr/local/lib/joustmania/joustmania-hotspot" evaluate --once >/dev/null
  echo 20100 >"${CASE}/now"
  JOUSTMANIA_HOTSPOT_CONF="${R}/etc/joustmania/hotspot.conf" "${R}/usr/local/lib/joustmania/joustmania-hotspot" evaluate --once >/dev/null
  ! grep -q "connection up" "${CASE}/nmcli.argv" || fail "$1: a refused setup brought the hotspot up later"
}
make_case halfarm-client
echo "20 (unavailable)" >"${S}/eth0-state"
echo "100 (connected)" >"${S}/wlan0-state"; echo "11111111-2222-3333-4444-555555555555" >"${S}/wlan0-uuid"
if run_install --hold 0; then fail "halfarm-client: finished over a Wi-Fi client"; fi
not_armed halfarm-client
make_case halfarm-selftest; echo 40 >"${S}/ap-channel"
run_install --hold 0 || true
not_armed halfarm-selftest
make_case halfarm-psk; echo 'other-pass-1' >"${S}/psk-override"
run_install --hold 0 || true
not_armed halfarm-psk
make_case halfarm-ssid; printf 'Other\n36\n' >"${S}/ssid-override"
if run_install --hold 0; then fail "halfarm-ssid: a profile with another SSID was accepted"; fi
grep -q "did not take the new SSID or channel" "${CASE}/out" || fail "halfarm-ssid: no refusal line"
not_armed halfarm-ssid
# a re-install that fails disarms a working setup too
make_case rearm
run_install --hold 0 || fail "rearm: first install exit $?"
grep -qx 'enabled=yes' "${R}/etc/joustmania/hotspot.conf" || fail "rearm: first install not armed"
echo 40 >"${S}/ap-channel"
run_install --hold 0 || true
not_armed rearm

# --- leftover hidden temp keyfiles (review F3) ----------------------------------------
make_case tempfile
mkdir -p "${R}/etc/NetworkManager/system-connections"
printf 'psk=old-secret\n' >"${R}/etc/NetworkManager/system-connections/.joustmania-hotspot.123.456"
run_install --hold 0 || fail "tempfile: exit $?"
[[ ! -e "${R}/etc/NetworkManager/system-connections/.joustmania-hotspot.123.456" ]] || fail "tempfile: a leftover temp keyfile survived the install"
printf 'psk=old-secret\n' >"${R}/etc/NetworkManager/system-connections/.joustmania-hotspot.789.1"
"$INSTALL" --remove >/dev/null 2>&1 || fail "tempfile: remove exit $?"
[[ -z "$(find "${R}/etc/NetworkManager/system-connections" -name '.joustmania-hotspot.*' 2>/dev/null)" ]] || fail "tempfile: remove left a temp keyfile"
# a failure between render and the move leaves no temp keyfile (the EXIT trap)
make_case tempfile-crash
mkdir -p "${R}/etc/NetworkManager/system-connections/joustmania-hotspot.nmconnection/blocker"
if run_install --hold 0; then fail "tempfile-crash: finished"; fi
grep -q "cannot move the keyfile into place" "${CASE}/out" || fail "tempfile-crash: no refusal line: $(tail -n 2 "${CASE}/out")"
[[ -z "$(find "${R}/etc/NetworkManager/system-connections" -name '.joustmania-hotspot.*')" ]] || fail "tempfile-crash: the temp keyfile with the password was left behind"
no_psk_leak

# --- --psk-stdin never reads a terminal (review F4) -----------------------------------
make_case stdin-tty
if command -v script >/dev/null 2>&1; then
  script -qec "\"$INSTALL\" --psk-stdin --hold 0" /dev/null </dev/null >"${CASE}/out" 2>&1 || true
  grep -q -- "--psk-stdin reads a pipe, not a terminal" "${CASE}/out" || fail "stdin-tty: no refusal on a terminal: $(tail -n 2 "${CASE}/out")"
  ! installed || fail "stdin-tty: installed"
fi

# --- A8: no channel change over the hotspot (review F6) ---------------------------------
make_case a8-channel
mkdir -p "${R}/etc/joustmania"
printf 'enabled=yes\nssid=JoustMania\nchannel=36\nuplink_iface=eth0\n' >"${R}/etc/joustmania/hotspot.conf"
echo "20 (unavailable)" >"${S}/eth0-state"
echo "100 (connected)" >"${S}/wlan0-state"; echo "$UUID" >"${S}/wlan0-uuid"
if run_install --channel 40; then fail "a8-channel: changed the channel over the hotspot"; fi
grep -q "cannot be tested: plug the cable in" "${CASE}/out" || fail "a8-channel: no refusal line"
grep -qx 'channel=36' "${R}/etc/joustmania/hotspot.conf" && grep -qx 'enabled=yes' "${R}/etc/joustmania/hotspot.conf" ||
  fail "a8-channel: the working setup was changed"

# --- the self-test takes the hotspot down on Ctrl-C, SIGTERM and SIGHUP (review F2) -----
for sg in SIGINT SIGTERM SIGHUP; do
  make_case "signal-${sg}"
  unset JOUSTMANIA_HOTSPOT_NOW_FILE
  PSK_IN="$PSK" python3 -I - "$sg" "${CASE}/nmcli.argv" "$INSTALL" --psk-stdin --hold 20 >"${CASE}/out" 2>&1 <<'PY' || fail "signal-${sg}: harness"
import os, signal, subprocess, sys, time
sig, argv_log, cmd = getattr(signal, sys.argv[1]), sys.argv[2], sys.argv[3:]
def dfl():
    for s in (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, signal.SIG_DFL)
p = subprocess.Popen(cmd, start_new_session=True, preexec_fn=dfl, stdin=subprocess.PIPE)
p.stdin.write(os.environ["PSK_IN"].encode() + b"\n"); p.stdin.close()
deadline = time.time() + 20
while time.time() < deadline:
    if "connection up" in open(argv_log).read():
        break
    time.sleep(0.1)
time.sleep(0.5)  # into the hold
os.killpg(p.pid, sig)
p.wait(timeout=20)
PY
  grep -q "connection up uuid ${UUID}" "${CASE}/nmcli.argv" || fail "signal-${sg}: the self-test never started"
  grep -q "connection down uuid ${UUID}" "${CASE}/nmcli.argv" || fail "signal-${sg}: the hotspot was left up"
  [[ "$(cat "${S}/wlan0-state")" == "30 (disconnected)" ]] || fail "signal-${sg}: wlan0 is $(cat "${S}/wlan0-state")"
  grep -qx 'enabled=no' "${R}/etc/joustmania/hotspot.conf" || fail "signal-${sg}: armed after an interrupted setup"
done

# --- A8 stays armed and keeps the working profile (review delta) ---------------------
# a8_case <name>: installed with the cable, then the cable is gone and the
# Operator re-runs the setup over the hotspot
a8_case() {
  make_case "$1"
  run_install --hold 0 || fail "$1: first install exit $?"
  cp "${R}/etc/NetworkManager/system-connections/joustmania-hotspot.nmconnection" "${CASE}/old-keyfile"
  echo "20 (unavailable)" >"${S}/eth0-state"
  echo "100 (connected)" >"${S}/wlan0-state"; echo "$UUID" >"${S}/wlan0-uuid"; printf '10.42.0.1/24' >"${S}/wlan0-ip4"
  : >"${CASE}/nmcli.argv"
}
a8_still_up() { # <case>
  grep -qx 'enabled=yes' "${R}/etc/joustmania/hotspot.conf" || fail "$1: A8 disarmed the fallback"
  echo 30000 >"${CASE}/now"
  JOUSTMANIA_HOTSPOT_CONF="${R}/etc/joustmania/hotspot.conf" "${R}/usr/local/lib/joustmania/joustmania-hotspot" evaluate --once >/dev/null
  ! grep -q "connection down\\|connection delete" "${CASE}/nmcli.argv" || fail "$1: the hotspot was taken down: $(grep 'down\|delete' "${CASE}/nmcli.argv")"
  [[ "$(cat "${S}/wlan0-uuid")" == "$UUID" ]] || fail "$1: wlan0 lost the hotspot"
}
a8_case a8-refused
echo 'other-pass-1' >"${S}/psk-override"
if INPUT='NewPass&2027' run_install; then fail "a8-refused: finished with a password mismatch"; fi
cmp -s "${CASE}/old-keyfile" "${R}/etc/NetworkManager/system-connections/joustmania-hotspot.nmconnection" || fail "a8-refused: the previous profile was not restored"
[[ -z "$(find "${R}/etc/NetworkManager/system-connections" -name '.joustmania-hotspot.*')" ]] || fail "a8-refused: a temp or backup keyfile left"
a8_still_up a8-refused
a8_case a8-timer
touch "${S}/sysctl-evaluate"
run_install || fail "a8-timer: exit $? ($(tail -n 3 "${CASE}/out"))"
grep -q '^joustmania-hotspot: on' "${CASE}/evaluate.out" || fail "a8-timer: the check during the run did not see the hotspot on: $(cat "${CASE}/evaluate.out")"
a8_still_up a8-timer
a8_case a8-sighup
touch "${S}/sysctl-hang"
PSK_IN="$PSK" python3 -I - SIGHUP "${S}/hanging" "$INSTALL" --psk-stdin >"${CASE}/out" 2>&1 <<'PY' || fail "a8-sighup: harness"
import os, signal, subprocess, sys, time
sig, marker, cmd = getattr(signal, sys.argv[1]), sys.argv[2], sys.argv[3:]
def dfl():
    for s in (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, signal.SIG_DFL)
p = subprocess.Popen(cmd, start_new_session=True, preexec_fn=dfl, stdin=subprocess.PIPE)
p.stdin.write(os.environ["PSK_IN"].encode() + b"\n"); p.stdin.close()
deadline = time.time() + 20
while time.time() < deadline and not os.path.exists(marker):
    time.sleep(0.1)
os.killpg(p.pid, sig)
p.wait(timeout=20)
PY
[[ -e "${S}/hanging" ]] || fail "a8-sighup: the run never reached the file work"
rm -f "${S}/sysctl-hang"
a8_still_up a8-sighup

# a failed backup copy never replaces the working profile (round 3)
a8_case a8-cpfail
touch "${S}/cp-fails"
if INPUT='NewPass&2027' run_install; then fail "a8-cpfail: finished without a backup"; fi
grep -q "cannot keep a copy of the current profile" "${CASE}/out" || fail "a8-cpfail: no refusal line: $(tail -n 2 "${CASE}/out")"
cmp -s "${CASE}/old-keyfile" "${R}/etc/NetworkManager/system-connections/joustmania-hotspot.nmconnection" ||
  fail "a8-cpfail: the working profile was replaced"
[[ -z "$(find "${R}/etc/NetworkManager/system-connections" -name '.joustmania-hotspot.*')" ]] || fail "a8-cpfail: a partial backup was left"
rm -f "${S}/cp-fails"
a8_still_up a8-cpfail

# cable in while the hotspot is still up (the overlap of A1): the cable path with
# the self-test, not A8 (round 3)
make_case overlap
run_install --hold 0 || fail "overlap: first install exit $?"
echo "100 (connected)" >"${S}/wlan0-state"; echo "$UUID" >"${S}/wlan0-uuid"; printf '10.42.0.1/24' >"${S}/wlan0-ip4"
: >"${CASE}/nmcli.argv"; : >"${CASE}/systemctl.argv"; rm -f "${CASE}/systemd-run.argv"
run_install --hold 0 || fail "overlap: exit $? ($(tail -n 3 "${CASE}/out"))"
! grep -q "self-test skipped" "${CASE}/out" || fail "overlap: treated as A8 with the cable in"
grep -q "taking the hotspot down for the self-test" "${CASE}/out" || fail "overlap: the hotspot was not taken down first"
[[ "$(grep -c '^PASS' "${CASE}/out")" == 4 ]] || fail "overlap: the self-test did not run and pass"
[[ ! -s "${CASE}/systemd-run.argv" ]] || fail "overlap: scheduled the A8 reactivation"
grep -qx 'enabled=yes' "${R}/etc/joustmania/hotspot.conf" || fail "overlap: not armed"

# the overlap seen from the hotspot itself: refused before any change (round 4)
for via in ssh who; do
  make_case "overlap-session-${via}"
  run_install --hold 0 || fail "overlap-session-${via}: first install exit $?"
  echo "100 (connected)" >"${S}/wlan0-state"; echo "$UUID" >"${S}/wlan0-uuid"; printf '10.42.0.1/24' >"${S}/wlan0-ip4"
  cp "${R}/etc/joustmania/hotspot.conf" "${CASE}/conf-before"
  : >"${CASE}/nmcli.argv"
  if [[ "$via" == ssh ]]; then
    SSH_CONNECTION="10.42.0.57 51000 10.42.0.1 22" run_install --hold 0 && fail "overlap-session-ssh: not refused"
  else
    mkdir -p "${CASE}/who-bin"
    printf '#!/usr/bin/env bash\necho "pi       pts/0        2026-10-27 10:00 (10.42.0.57)"\n' >"${CASE}/who-bin/who"
    chmod +x "${CASE}/who-bin/who"
    PATH="${CASE}/who-bin:${PATH}" run_install --hold 0 && fail "overlap-session-who: not refused"
  fi
  grep -q "you are connected over the hotspot while a cable is in" "${CASE}/out" || fail "overlap-session-${via}: no refusal line: $(tail -n 2 "${CASE}/out")"
  ! grep -q "connection down" "${CASE}/nmcli.argv" || fail "overlap-session-${via}: took the hotspot down"
  cmp -s "${CASE}/conf-before" "${R}/etc/joustmania/hotspot.conf" || fail "overlap-session-${via}: the conf changed"
done
# the same overlap from the cable LAN proceeds (the overlap case above)
make_case overlap-lan
run_install --hold 0 || fail "overlap-lan: first install exit $?"
echo "100 (connected)" >"${S}/wlan0-state"; echo "$UUID" >"${S}/wlan0-uuid"; printf '10.42.0.1/24' >"${S}/wlan0-ip4"
SSH_CONNECTION="192.168.1.20 51000 192.168.1.50 22" run_install --hold 0 || fail "overlap-lan: refused over the LAN: $(tail -n 2 "${CASE}/out")"

# --- A8: over the hotspot ------------------------------------------------------------
make_case a8
echo "20 (unavailable)" >"${S}/eth0-state"
echo "100 (connected)" >"${S}/wlan0-state"; echo "$UUID" >"${S}/wlan0-uuid"
printf 'UNCONN 0 0 0.0.0.0:67 0.0.0.0:*\nUNCONN 0 0 10.42.0.1:53 0.0.0.0:*\n' >>"${S}/ss-udp"
run_install || fail "a8: exit $? ($(tail -n 3 "${CASE}/out"))"
grep -q "self-test skipped" "${CASE}/out" || fail "a8: self-test not skipped"
grep -q "you will be disconnected" "${CASE}/out" || fail "a8: no disconnect warning"
grep -q -- "--no-block --collect --unit=joustmania-hotspot-reapply --on-active=5 nmcli --wait 30 connection up uuid ${UUID}" \
  "${CASE}/systemd-run.argv" || fail "a8: no detached reactivation"
grep -qx "enable --now joustmania-hotspot.timer" "${CASE}/systemctl.argv" || fail "a8: timer not armed"
no_psk_leak

# --- --remove uses the installed remover ---------------------------------------------
make_case remove
run_install --hold 0 || fail "remove: install exit $?"
"$INSTALL" --remove >"${CASE}/out" 2>&1 || fail "remove: exit $?"
left="$(cd "${R}" && find . -type f ! -path './boot/*' | sort | tr '\n' ' ')"
[[ "$left" == "./etc/sysctl.d/90-joustmania-ip-forward.conf " ]] || fail "remove: left behind: $left"
grep -q "disable --now joustmania-hotspot.timer joustmania-hotspot.service" "${CASE}/systemctl.argv" || fail "remove: units not disabled"

# --- the upgrade's stale-files hint (lib-joustmania.sh): installed vs checkout -----
# shellcheck source=../setup/lib-joustmania.sh
source "${ROOT}/scripts/setup/lib-joustmania.sh"
make_case hint
run_install --hold 0 || fail "hint: install exit $?"
export JOUSTMANIA_HOTSPOT_INSTALL_ROOT="${R}"
[[ -z "$(joustmania_hotspot_hint "${ROOT}")" ]] || fail "hint: printed although the installed copies match the checkout"
CO="${CASE}/checkout"
mkdir -p "${CO}/scripts/setup"
cp "${ROOT}/scripts/setup/install_hotspot.sh" "${ROOT}/scripts/setup/joustmania-hotspot.sh" "${ROOT}/scripts/setup/joustmania-hotspot.service" \
  "${ROOT}/scripts/setup/joustmania-hotspot.timer" "${ROOT}/scripts/setup/90-joustmania-hotspot" "${CO}/scripts/setup/"
for f in joustmania-hotspot.sh joustmania-hotspot.service joustmania-hotspot.timer 90-joustmania-hotspot; do
  cp "${CO}/scripts/setup/${f}" "${CASE}/keep"
  echo '# newer' >>"${CO}/scripts/setup/${f}"
  joustmania_hotspot_hint "${CO}" | grep -qx "hotspot files changed: run sudo ${CO}/scripts/setup/install_hotspot.sh (UC-061; the password is asked again)" ||
    fail "hint: no line for a changed ${f}"
  cp "${CASE}/keep" "${CO}/scripts/setup/${f}"
done
[[ -z "$(joustmania_hotspot_hint "${CO}")" ]] || fail "hint: printed for an identical checkout"
rm -f "${CO}/scripts/setup/install_hotspot.sh"
joustmania_hotspot_hint "${CO}" | grep -q "remove it with: sudo /usr/local/lib/joustmania/joustmania-hotspot remove" ||
  fail "hint: a version without the setup does not name the installed remover"
export JOUSTMANIA_HOTSPOT_INSTALL_ROOT="${CASE}/nothing-installed"
[[ -z "$(joustmania_hotspot_hint "${CO}")" ]] || fail "hint: printed although the hotspot is not installed"
unset JOUSTMANIA_HOTSPOT_INSTALL_ROOT
[[ "$(grep -c 'joustmania_hotspot_hint "$JOUSTMANIA_DIR"' "${ROOT}/scripts/setup/joustmania-upgrade.sh")" == 2 ]] ||
  fail "hint: joustmania-upgrade does not call it on upgrade and rollback"

if [[ "$fails" -gt 0 ]]; then
  echo "test-install-hotspot: ${fails} failure(s)"
  exit 1
fi
echo "test-install-hotspot: all checks passed"
