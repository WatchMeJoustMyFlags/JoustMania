# JoustMania Scripts

Scripts for JoustMania deployment and hardware configuration.

**Note:** Most development tasks (linting, testing, building) are handled via Makefile targets.
See `make help` for available commands.

---

## Directory Structure

```
scripts/
├── ci/          # CI validation scripts (complex multi-step)
├── docker/      # Docker helper scripts
├── hardware/    # Hardware configuration scripts (Pi-specific)
├── planning/    # Phase workflow utilities
├── setup/       # Host system setup scripts (Pi-specific)
└── testing/     # Manual testing utilities
```

---

## Quick Reference

| Task | Command |
|------|---------|
| Run tests | `make test` |
| Lint code | `make lint` |
| Format code | `make format` |
| Build services | `make build-all-services` |
| Start Docker stack | `make docker-start` |
| Stop Docker stack | `make docker-stop` |
| All CI checks | `make ci-all` |

---

## Runtime flags (flag service, epic #1407)

Runtime flags change only through the flag service's HTTP API (envoy route
`http://localhost/flags/`, `docs/specs/services.md` §7.2); the files in
`services/flagd/` are the read-only baseline. No script writes a flag file.

| Script | Purpose |
|------|---------|
| `flags.sh` | Generic client: `state`, `get`, `variant`, `set`, `set-value`, `clear`, `reset`, `restart` (flag service + flagd together) |
| `flagservice.py` | The Python client the stage scripts share (standard library only) |
| `agent-backend.sh` | `decision.*` flags (backend, model, thresholds, capture, effort) |
| `agent-killswitch.sh` | The agent master switch `enabled` (on = overlay, off = baseline) |
| `agent-dryrun-enable.sh` | `enabled` + `experiments_enabled` for a `make dry-run` |
| `talk-stage.sh` | The talk's stage baseline, permissions beat and Bluetooth fallback (`writer=stage` overlays) |
| `talk-preflight.sh` | Read-only pre-flight; reads the flag service (`GET /state`, `GET /agent`) |

Every write is an in-memory overlay (never persisted; a flag-service restart
returns to the baseline). The script tests (`scripts/tests/`) run against
`scripts/tests/fake_flag_service.py`, which enforces the real flag registry.

---

## Hardware Scripts

Located in `scripts/hardware/` - Raspberry Pi specific.

### reset_bluetooth_connections.sh

Removes PS Move Bluetooth registrations only (live BlueZ devices and saved-only
registrations of adapters BlueZ does not present); other Bluetooth devices stay
paired. Runs `psmove_pairing.registrations` inside the pairing-daemon container,
then restarts `pairing-daemon` (it would otherwise skip re-pairing controllers it
already verified). No reboot. Non-zero exit if any removal failed.

```bash
scripts/hardware/reset_bluetooth_connections.sh --dry-run   # list, change nothing
scripts/hardware/reset_bluetooth_connections.sh             # remove, then re-pair over USB
```

**Use when:** PS Move controllers won't pair or have connection issues.

### update_asound.sh

Configures ALSA audio settings for Pi 4/5 audio output.

```bash
scripts/hardware/update_asound.sh
```

**Use when:** Audio output issues or after changing audio hardware.

### update_permissions.sh

Sets ownership of JoustMania directory to current user.

```bash
scripts/hardware/update_permissions.sh
```

**Use when:** Permission errors after sudo operations.

---

## Setup Scripts

Located in `scripts/setup/` - For initial Raspberry Pi setup.

### setup_host.sh

Installs system dependencies and configures the host environment.

**What it does:**
- Updates system packages
- Installs Python, Docker, build tools
- Creates Python virtual environment
- Configures audio (ALSA) and Bluetooth (disables the Pi's internal radio; saved Bluetooth pairings are kept, #1443)
- Installs supervisor configuration

```bash
scripts/setup/setup_host.sh
```

**Duration:** ~10-15 minutes

### install_autostart.sh / uninstall_autostart.sh

Install/remove systemd service for autostart on boot.

```bash
sudo scripts/setup/install_autostart.sh
sudo scripts/setup/uninstall_autostart.sh
```

---

## Planning Scripts

Located in `scripts/planning/` - Phase workflow utilities.

```bash
./scripts/planning/phase-status.sh           # Show phase status
./scripts/planning/phase-start.sh <number>   # Start a planned phase
./scripts/planning/phase-complete.sh <number> # Mark phase complete
```

---

## CI Scripts

Located in `scripts/ci/` - Complex validation scripts.

Most CI operations are inlined in the Makefile. These scripts handle
operations too complex for Make syntax:

- `validate-protos.sh` - Proto generation + git diff + bytecode verification
- `validate-packages.sh` - Multi-step uv package validation

```bash
make validate-protos    # Recommended way to run
make validate-packages
```

---

## Testing Utilities

Located in `scripts/testing/` - Manual testing tools.

### color_tests/

Python utilities for manual PS Move controller color testing:

```bash
cd scripts/testing/color_tests/
python interactive_colortest.py
```

### Other utilities

- `simulate_game.py` - Game simulation for testing
- `test-mock.py` / `test-mock-with-pause.py` - Mock environment testing

**For automated tests, use:** `make test`

---

## First-Time Setup (Raspberry Pi)

```bash
# 1. Run host setup
scripts/setup/setup_host.sh

# 2. (Optional) Enable autostart
sudo scripts/setup/install_autostart.sh
```

---

## Notes

- Setup and hardware scripts are Raspberry Pi / Linux specific
- Most development tasks should use Makefile targets
- Docker scripts require Docker installed and user in docker group
- All scripts should be run from repository root
