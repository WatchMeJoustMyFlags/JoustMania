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
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Register as default agent with KeyboardOnly capability
        # This makes bluetoothctl respond to PIN requests automatically
        proc.stdin.write(b"agent KeyboardOnly\n")
        proc.stdin.write(b"default-agent\n")
        await proc.stdin.drain()

        # Give bluetoothctl time to register
        await asyncio.sleep(1)

        # Read any output to check for errors
        # Use a short timeout - bluetoothctl stays running
        try:
            output = await asyncio.wait_for(proc.stdout.read(4096), timeout=1.0)
            output_str = output.decode(errors="replace")
            logger.debug(f"bluetoothctl agent output: {output_str}")

            if "Agent registered" in output_str or "agent" in output_str.lower():
                logger.info("BlueZ pairing agent registered via bluetoothctl")
            else:
                logger.info("BlueZ pairing agent started (bluetoothctl running)")
        except TimeoutError:
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
