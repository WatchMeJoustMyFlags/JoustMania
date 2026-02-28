"""Utility functions for PS Move pairing daemon."""

import asyncio
import logging
import os

logger = logging.getLogger("psmove-pairing")


async def run_command(
    cmd: list[str],
    capture_stderr: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run a subprocess command asynchronously and return exit code and output.

    Args:
        cmd: Command and arguments to run
        capture_stderr: Whether to capture stderr in output
        env: Additional environment variables to set (merged with current env)
    """
    try:
        stderr = asyncio.subprocess.STDOUT if capture_stderr else asyncio.subprocess.DEVNULL

        # Merge additional env vars with current environment
        run_env = None
        if env:
            run_env = os.environ.copy()
            run_env.update(env)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            env=run_env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = stdout.decode("utf-8", errors="replace").strip()
        return proc.returncode or 0, output
    except TimeoutError:
        logger.error(f"Command timed out: {' '.join(cmd)}")
        return -1, "timeout"
    except Exception as e:
        logger.error(f"Command failed: {e}")
        return -1, str(e)
