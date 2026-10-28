#!/usr/bin/env bash
# Host setup must never wipe BlueZ registrations (#1443).
#
# setup_host.sh / setup_runtime.sh used to run `rm -rf /var/lib/bluetooth/*`
# when they disabled the Pi's internal radio, deleting every saved pairing on
# the host (keyboards, headsets, ...), not only PS Moves. BlueZ only loads the
# storage of adapters it presents, so the internal adapter's directory is inert
# once the radio is off; PS Move registrations are reset with
# scripts/hardware/reset_bluetooth_connections.sh, which touches PS Moves only.
# Runs as part of `make lint`, or directly:
#   ./scripts/tests/test-setup-keeps-bluetooth.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fails=0

for script in "${ROOT}/setup.sh" "${ROOT}"/scripts/setup/*.sh; do
  [[ -f "${script}" ]] || continue
  # Any rm of /var/lib/bluetooth or everything under it (comments ignored)
  if hits=$(grep -nE '^[^#]*\brm\b[^#]*/var/lib/bluetooth/?(\*|"?\s|"?$|\$)' "${script}"); then
    echo "FAIL ${script#"${ROOT}/"} removes BlueZ registrations:"
    echo "${hits}" | sed 's/^/    /'
    fails=$((fails + 1))
  fi
done

# The check itself must catch the old line and its variants
for bad in 'sudo rm -rf /var/lib/bluetooth/*' '    sudo rm -rf /var/lib/bluetooth/* 2>/dev/null || true' 'rm -rf /var/lib/bluetooth' 'rm -r "/var/lib/bluetooth/"'; do
  if ! grep -qE '^[^#]*\brm\b[^#]*/var/lib/bluetooth/?(\*|"?\s|"?$|\$)' <<<"${bad}"; then
    echo "FAIL self-test: pattern misses: ${bad}"
    fails=$((fails + 1))
  fi
done
if grep -qE '^[^#]*\brm\b[^#]*/var/lib/bluetooth/?(\*|"?\s|"?$|\$)' <<<'# never rm -rf /var/lib/bluetooth/*'; then
  echo "FAIL self-test: a comment is flagged"
  fails=$((fails + 1))
fi

if [[ "${fails}" -ne 0 ]]; then
  exit 1
fi
echo "ok: host setup keeps BlueZ registrations"
