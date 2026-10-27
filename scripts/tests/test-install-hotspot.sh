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
# UC: UC-061 Pre-2 Pre-3 A8 BR-001 BR-004
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALL="${ROOT}/scripts/setup/install_hotspot.sh"
UUID=e0602321-f72b-408b-983e-56ee57d519ee
export UUID
# A test that reaches for the real docker fails instead of using it
export PATH="${ROOT}/scripts/tests/lib/fail-docker:${PATH}"
unset SUDO_USER || true
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
for tool in systemd-run sysctl; do
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"${CASE}/%s.argv"\n' "$tool" >"${STUBS}/${tool}"
done
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

# --- the upgrade's stale-files hint (lib-joustmania.sh) --------------------------------
HREPO="${WORK}/hint-repo"
g() { git -c init.defaultBranch=main -c commit.gpgsign=false -c user.name=t -c user.email=t@example.invalid -C "${HREPO}" "$@"; }
mkdir -p "${HREPO}/scripts/setup"
g init -q
echo a >"${HREPO}/scripts/setup/joustmania-hotspot.sh"; echo a >"${HREPO}/README"
g add -A; g commit -q -m one; H1="$(g rev-parse HEAD)"
echo b >"${HREPO}/README"; g commit -q -am two; H2="$(g rev-parse HEAD)"
echo b >"${HREPO}/scripts/setup/joustmania-hotspot.sh"; g commit -q -am three; H3="$(g rev-parse HEAD)"
# shellcheck source=../setup/lib-joustmania.sh
source "${ROOT}/scripts/setup/lib-joustmania.sh"
touch "${WORK}/installed-helper"
export JOUSTMANIA_HOTSPOT_INSTALLED="${WORK}/installed-helper"
[[ -z "$(joustmania_hotspot_hint "${HREPO}" "$H1" "$H2")" ]] || fail "hint: printed without a hotspot change"
joustmania_hotspot_hint "${HREPO}" "$H2" "$H3" | grep -q "hotspot files changed: run sudo ${HREPO}/scripts/setup/install_hotspot.sh" ||
  fail "hint: no line for a changed helper"
joustmania_hotspot_hint "${HREPO}" "$H3" "$H1" | grep -q "hotspot files changed" || fail "hint: no line on a rollback"
export JOUSTMANIA_HOTSPOT_INSTALLED="${WORK}/not-installed"
[[ -z "$(joustmania_hotspot_hint "${HREPO}" "$H2" "$H3")" ]] || fail "hint: printed although the hotspot is not installed"
grep -q 'joustmania_hotspot_hint "$JOUSTMANIA_DIR" "$prev_ref" "$commit"' "${ROOT}/scripts/setup/joustmania-upgrade.sh" &&
  grep -q 'joustmania_hotspot_hint "$JOUSTMANIA_DIR" "$running" "$T_REF"' "${ROOT}/scripts/setup/joustmania-upgrade.sh" ||
  fail "hint: joustmania-upgrade does not call it on upgrade and rollback"

if [[ "$fails" -gt 0 ]]; then
  echo "test-install-hotspot: ${fails} failure(s)"
  exit 1
fi
echo "test-install-hotspot: all checks passed"
