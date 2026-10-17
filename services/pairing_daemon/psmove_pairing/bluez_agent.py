"""BlueZ pairing agent for PS Move controllers.

Runs `bluetoothctl` as a background subprocess and asks it to register a
KeyboardOnly default agent. bluetoothctl does not answer PIN or
confirmation requests by itself: it waits for input on stdin, and the
daemon writes none. ZCM1 controllers use unauthenticated HID and don't
trigger the agent.
"""

import asyncio
import logging

logger = logging.getLogger("psmove-pairing")


async def _stop_previous_agent() -> None:
    """Terminate the bluetoothctl process of an earlier registration, if it still runs."""
    proc = getattr(register_pairing_agent, "_proc", None)
    register_pairing_agent._proc = None
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except ProcessLookupError:
        pass
    except TimeoutError:
        proc.kill()
        await proc.wait()
    finally:
        if proc.stdin is not None:
            proc.stdin.close()


async def register_pairing_agent() -> bool:
    """Start a bluetoothctl process with a default agent.

    Sends `agent KeyboardOnly` and `default-agent`. It does not answer PIN or
    confirmation requests (bluetoothctl prompts on stdin for those).
    Runs as a background subprocess for the lifetime of the daemon.

    Called again after every bluetoothd restart (UC-001 BR-005): the restart
    ends the previous session, whose process is stopped first.

    Returns True if bluetoothctl is still running after the settle time. In
    interactive mode bluetoothctl does not exit when agent registration fails,
    so this catches startup failures only, not a refused registration.
    """
    await _stop_previous_agent()
    try:
        # stdout/stderr go to /dev/null: bluetoothctl prints device and
        # property events for as long as it runs, and an undrained pipe fills
        # after ~64 KB and blocks bluetoothctl, agent included (#1477).
        # Nothing here depends on its output.
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Ask bluetoothctl to register a KeyboardOnly default agent
        proc.stdin.write(b"agent KeyboardOnly\n")
        proc.stdin.write(b"default-agent\n")
        await proc.stdin.drain()

        # Give bluetoothctl time to start. A refused registration does not end
        # it (interactive mode), so an exit here means bluetoothctl itself failed.
        await asyncio.sleep(1)
        if proc.returncode is not None:
            proc.stdin.close()
            logger.error(f"bluetoothctl exited with code {proc.returncode} - no BlueZ pairing agent")
            return False
        logger.info("bluetoothctl running (agent registration not confirmed)")

        # Keep the process reference so it doesn't get garbage collected
        register_pairing_agent._proc = proc
        return True

    except FileNotFoundError:
        logger.error("bluetoothctl not found - ZCM2 controller pairing will not work")
        return False
    except Exception as e:
        logger.error(f"Failed to register pairing agent: {e}")
        return False
