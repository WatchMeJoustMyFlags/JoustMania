#!/bin/bash
#
# JoustMania Setup: first-time install of a Pi (UC-060 A6, #2027)
#
#   1) Runtime only: scripts/setup/setup_runtime.sh (Docker, compose, Bluetooth,
#      Docker log defaults)
#   2) Full development setup: scripts/setup/setup_host.sh, then a reboot
#
# A failed setup script ends here with a non-zero exit: nothing else is offered
# and nothing reboots. After a successful one, the stage steps (C-040, C-041):
#   - .env.local from .env.example (mode 600; never IMAGE_TAG, BR-003)
#   - the autostart unit (sudo scripts/setup/install_autostart.sh)
#   - the pin: scripts/setup/joustmania-upgrade.sh <release tag | sha> pulls the
#     version's images while online, switches the checkout and starts the unit,
#     so the first boot needs no network (BR-001). Without the unit, a checkout
#     that is already pinned gets its images pulled.
# The autostart refuses to start a checkout on a branch (A4), so pin it.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_DIR="$SCRIPT_DIR/scripts/setup"
# shellcheck source=scripts/setup/lib-joustmania.sh
source "$SETUP_DIR/lib-joustmania.sh"

STEP_FAILED=0
LATER=() # commands the operator still has to run

# ask <question>: true on y/Y (one key, no Enter)
ask() {
    local reply=""
    read -r -p "$1 (y/n) " -n 1 reply || reply=""
    echo
    [[ "$reply" =~ ^[Yy]$ ]]
}

# run_logged <script> <log>: runs the script, keeps a log, returns the
# script's exit code (not tee's)
run_logged() {
    bash "$1" 2>&1 | tee "$2"
    return "${PIPESTATUS[0]}"
}

# pin_target: the version to pin. A checkout that is pinned keeps its own; else
# the newest release that contains the pinnable floor of the upgrade command,
# else this checkout's commit (an exact sha is a valid pin, C-040).
pin_target() {
    local floor tag
    if joustmania_pin_image_tag "$SCRIPT_DIR" >/dev/null; then
        joustmania_pin_ref "$SCRIPT_DIR"
        return 0
    fi
    floor="$(sed -n 's/^MIN_PINNABLE_COMMIT="\([0-9a-f]*\)"$/\1/p' "$SETUP_DIR/joustmania-upgrade.sh")"
    while IFS= read -r tag; do
        [[ -n "$tag" && -n "$floor" ]] || continue
        if git -C "$SCRIPT_DIR" merge-base --is-ancestor "$floor" "$tag" 2>/dev/null; then
            echo "$tag"
            return 0
        fi
    done < <(git -C "$SCRIPT_DIR" tag -l 'joustmania-v*' | grep -E '^joustmania-v[0-9]+\.[0-9]+\.[0-9]+$' | sort -rV)
    git -C "$SCRIPT_DIR" rev-parse HEAD
}

stage_steps() {
    local autostart=0 target

    echo ""
    echo "=========================================="
    echo "Stage Pi: env file, autostart, pinned version"
    echo "=========================================="

    # .env.local: decision-backend API keys and opt-in profiles, private
    if [[ -e "$SCRIPT_DIR/.env.local" ]]; then
        echo ".env.local exists; left as it is"
    elif ask "Create .env.local (API keys, opt-in profiles; mode 600) from .env.example?"; then
        if install -m 600 "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env.local"; then
            echo "  → created; edit it with: nano $SCRIPT_DIR/.env.local (no IMAGE_TAG there: the pin decides it)"
        else
            echo "ERROR: could not create .env.local" >&2
            STEP_FAILED=1
        fi
    fi

    if ask "Enable autostart on boot (the full stack, joustmania.service)?"; then
        if sudo "$SETUP_DIR/install_autostart.sh"; then
            autostart=1
        else
            echo "ERROR: installing the autostart unit failed" >&2
            STEP_FAILED=1
        fi
    fi

    target="$(pin_target)"
    if [[ -z "$target" ]]; then
        echo "ERROR: git cannot read the checkout; no version to pin" >&2
        STEP_FAILED=1
        return 0
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "Docker is not usable by $(id -un) yet (log out and back in for the docker group); images not pulled."
        defer_pin "$autostart" "$target"
        return 0
    fi
    if ((autostart)); then
        if ask "Pin to $target, pull its images and start it now (needs the network)?"; then
            "$SETUP_DIR/joustmania-upgrade.sh" "$target" || {
                echo "ERROR: joustmania-upgrade.sh $target failed (see above); the first boot is not ready" >&2
                STEP_FAILED=1
            }
        else
            defer_pin "$autostart" "$target"
        fi
    elif joustmania_pin_image_tag "$SCRIPT_DIR" >/dev/null; then
        if ask "Pull the images of $target now, so the first start needs no network?"; then
            "$SETUP_DIR/joustmania-compose.sh" pull || {
                echo "ERROR: pulling the images failed" >&2
                STEP_FAILED=1
            }
        else
            defer_pin "$autostart" "$target"
        fi
    else
        defer_pin "$autostart" "$target"
    fi
}

# defer_pin <autostart 0|1> <target>: the pin step left for later, the same
# whichever step deferred it. With the autostart on a branch checkout, the
# next boot refuses to start (A4) until the pin has run.
defer_pin() {
    if (($1)); then
        LATER+=("$SETUP_DIR/joustmania-upgrade.sh $2   # pin, pull the images, start")
        if ! joustmania_pin_image_tag "$SCRIPT_DIR" >/dev/null; then
            echo "Not pinned yet: the autostart refuses to start a branch checkout (UC-060 A4); the next boot starts nothing until you run the pin listed below."
        fi
    elif joustmania_pin_image_tag "$SCRIPT_DIR" >/dev/null; then
        LATER+=("$SETUP_DIR/joustmania-compose.sh pull   # the images of $2, while online")
    else
        LATER+=("sudo $SETUP_DIR/install_autostart.sh && $SETUP_DIR/joustmania-upgrade.sh $2   # autostart, pin, pull")
    fi
}

finish() {
    local cmd
    echo ""
    echo "=========================================="
    if ((STEP_FAILED)); then
        echo "Setup finished with errors (see above)"
    else
        echo "Setup Complete!"
    fi
    echo "=========================================="
    echo ""
    if ((${#LATER[@]})); then
        echo "Still to do:"
        for cmd in "${LATER[@]}"; do echo "  $cmd"; done
        echo ""
    fi
    echo "Start or check the stack (the full stack, as the autostart runs it):"
    echo "  sudo systemctl start joustmania     # with the autostart"
    echo "  $SETUP_DIR/joustmania-compose.sh up -d"
    echo "  $SETUP_DIR/joustmania-status.sh"
    echo "  (docker-compose.lite.yml is for manual tests only)"
    echo ""
}

if [[ "$EUID" -eq 0 ]]; then
    echo "Run ./setup.sh as the user that owns the checkout, without sudo; it asks for sudo when needed." >&2
    exit 1
fi
if [[ ! -O "$SCRIPT_DIR" ]]; then
    echo "The checkout $SCRIPT_DIR is not owned by $(id -un) (cloned with sudo?). Fix it first:" >&2
    echo "  cd $SCRIPT_DIR && ./scripts/hardware/update_permissions.sh" >&2
    exit 1
fi

echo "=========================================="
echo "JoustMania Setup"
echo "=========================================="
echo ""
echo "Choose setup type:"
echo ""
echo "  1) Runtime Only (5 min)"
echo "     - Docker, Bluetooth config"
echo "     - For running JoustMania on a Pi"
echo ""
echo "  2) Full Development Setup (15-30 min)"
echo "     - Everything in Runtime, plus:"
echo "     - Python/uv for development and testing outside Docker"
echo ""
echo "  3) Cancel"
echo ""

REPLY=""
read -r -p "Select option [1-3]: " -n 1 REPLY || REPLY=""
echo
echo ""

case "$REPLY" in
    1)
        echo "=========================================="
        echo "Runtime Setup"
        echo "=========================================="
        if ! run_logged "$SETUP_DIR/setup_runtime.sh" setup_runtime.log; then
            echo "ERROR: Runtime setup failed. Check setup_runtime.log for details." >&2
            exit 1
        fi
        stage_steps
        finish
        exit "$STEP_FAILED"
        ;;

    2)
        echo "=========================================="
        echo "Full Development Setup"
        echo "=========================================="
        echo ""
        echo "This will:"
        echo "  1. Install system dependencies and configure host"
        echo ""
        echo "This process requires a reboot when complete."
        echo ""
        if ! ask "Continue?"; then
            echo "Setup cancelled."
            exit 0
        fi

        echo ""
        echo "=========================================="
        echo "Host System Setup"
        echo "=========================================="
        if ! run_logged "$SETUP_DIR/setup_host.sh" setup_host.log; then
            echo "ERROR: Host setup failed. Check setup_host.log for details." >&2
            exit 1
        fi
        stage_steps
        finish
        if ((STEP_FAILED)); then
            echo "Not rebooting: fix the errors above first."
            exit 1
        fi
        echo "System will reboot in 5 seconds..."
        (sleep 5; sudo reboot) &
        exit 0
        ;;

    *)
        echo "Setup cancelled."
        exit 0
        ;;
esac
