"""BlueZ pairing agent for PS Move controllers.

Runs `bluetoothctl` as a background subprocess with a pairing agent
that auto-accepts connections and handles PIN requests for ZCM2
(PS4-era) controllers. ZCM1 controllers use unauthenticated HID
and don't trigger the agent.
"""

import asyncio
import logging

logger = logging.getLogger("psmove-pairing")


async def register_pairing_agent() -> bool:
    """Start a bluetoothctl process with a default agent.

    The agent auto-accepts pairing and handles PIN "0000" for ZCM2 controllers.
    Runs as a background subprocess for the lifetime of the daemon.

    Returns True if agent registration succeeded.
    """
    try:
        # stdout/stderr go to /dev/null: bluetoothctl prints device and
        # property events for as long as it runs, and an undrained pipe fills
        # after ~64 KB, blocking bluetoothctl and with it every ZCM2 PIN
        # request (#1477). Nothing here depends on its output.
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Register as default agent with KeyboardOnly capability
        # This makes bluetoothctl respond to PIN requests automatically
        proc.stdin.write(b"agent KeyboardOnly\n")
        proc.stdin.write(b"default-agent\n")
        await proc.stdin.drain()

        # Give bluetoothctl time to register; it exits early only on failure
        await asyncio.sleep(1)
        if proc.returncode is not None:
            logger.error(f"bluetoothctl exited with code {proc.returncode} - ZCM2 controller pairing will not work")
            return False
        logger.info("BlueZ pairing agent started (bluetoothctl running)")

        # Keep the process reference so it doesn't get garbage collected
        register_pairing_agent._proc = proc
        return True

    except FileNotFoundError:
        logger.error("bluetoothctl not found - ZCM2 controller pairing will not work")
        return False
    except Exception as e:
        logger.error(f"Failed to register pairing agent: {e}")
        return False
