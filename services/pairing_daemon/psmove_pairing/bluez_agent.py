"""BlueZ pairing agent for PS Move controllers.

Runs `bluetoothctl` as a background subprocess with a pairing agent
that auto-accepts connections and handles PIN requests for ZCM2
(PS4-era) controllers. ZCM1 controllers use unauthenticated HID
and don't trigger the agent.
"""

import asyncio
import logging

logger = logging.getLogger("psmove-pairing")


# Bytes per read of bluetoothctl's output
_READ_CHUNK = 4096


async def _drain_output(stream: asyncio.StreamReader) -> None:
    """Read bluetoothctl's output (stdout and stderr) until it exits (#1477).

    bluetoothctl prints every device and property change for as long as it
    runs. An unread pipe fills up (about 64 KB on Linux), bluetoothctl then
    blocks on its write and stops answering, PIN requests of ZCM2 included.
    """
    while chunk := await stream.read(_READ_CHUNK):
        logger.debug(f"bluetoothctl: {chunk.decode(errors='replace').rstrip()}")


async def _stop_previous_agent() -> None:
    """Stop the bluetoothctl process of an earlier registration, if it still runs, and its output reader."""
    proc = getattr(register_pairing_agent, "_proc", None)
    reader = getattr(register_pairing_agent, "_drain_task", None)
    register_pairing_agent._proc = None
    register_pairing_agent._drain_task = None
    try:
        if proc is not None and proc.returncode is None:
            await _terminate(proc)
    finally:
        if reader is not None:
            reader.cancel()
            # asyncio.wait does not raise: a cancellation of the caller itself still propagates
            await asyncio.wait({reader})


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except ProcessLookupError:
        pass
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def register_pairing_agent() -> bool:
    """Start a bluetoothctl process with a default agent.

    The agent auto-accepts pairing and handles PIN "0000" for ZCM2 controllers.
    Runs as a background subprocess for the lifetime of the daemon; a
    background task reads its output for as long as it runs (#1477).

    Called again after every bluetoothd restart (UC-001 BR-005): the restart
    ends the previous session, whose process and output reader are stopped first.

    Returns True if agent registration succeeded.
    """
    await _stop_previous_agent()
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        # Keep the references: the process must not be garbage collected, and both are stopped on re-registration
        register_pairing_agent._proc = proc
        register_pairing_agent._drain_task = asyncio.create_task(_drain_output(proc.stdout))

        # Register as default agent with KeyboardOnly capability
        # This makes bluetoothctl respond to PIN requests automatically
        proc.stdin.write(b"agent KeyboardOnly\n")
        proc.stdin.write(b"default-agent\n")
        await proc.stdin.drain()

        # Give bluetoothctl time to register (its answer is logged at debug level)
        await asyncio.sleep(1)
        logger.info("BlueZ pairing agent started (bluetoothctl running)")
        return True

    except FileNotFoundError:
        logger.error("bluetoothctl not found - ZCM2 controller pairing will not work")
        return False
    except Exception as e:
        logger.error(f"Failed to register pairing agent: {e}")
        await _stop_previous_agent()
        return False
