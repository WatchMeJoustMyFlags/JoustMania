"""Remove PS Move Bluetooth registrations from this host (other Bluetooth devices stay).

Thin wrapper around ``psmove_pairing.registrations`` in the pairing daemon.
Run it where the system D-Bus and /var/lib/bluetooth are reachable, as root:

    sudo uv run --project services/pairing_daemon python tools/clear_devices.py --dry-run
    sudo uv run --project services/pairing_daemon python tools/clear_devices.py

After a real run, restart the pairing daemon (``docker compose restart
pairing-daemon``): it remembers controllers it already verified this session and
would not re-pair them over USB otherwise. Exit code 1 if any removal failed.

Prefer ``scripts/hardware/reset_bluetooth_connections.sh``, which runs the same
module inside the pairing-daemon container and restarts the daemon for you.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (REPO_ROOT, REPO_ROOT / "services" / "pairing_daemon"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from psmove_pairing.registrations import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
