"""bluetoothctl output never backs up into the pairing agent (UC-001, #1477).

bluetoothctl prints device and property events for as long as it runs. An
undrained pipe fills after ~64 KB and blocks it, its agent included.
"""

import asyncio
import sys
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from psmove_pairing import bluez_agent
from psmove_pairing.bluez_agent import register_pairing_agent

_REAL_SPAWN = asyncio.create_subprocess_exec
_REAL_SLEEP = asyncio.sleep

# Writes far more than a pipe buffer (64 KB on Linux) to stdout and stderr, then exits.
_CHATTY_BLUETOOTHCTL = (
    "import sys\n"
    "chunk = b'[CHG] Device 00:06:F7:00:00:00 RSSI: -60\\n' * 1024\n"
    "for _ in range(16):\n"
    "    sys.stdout.buffer.write(chunk)\n"
    "    sys.stdout.buffer.flush()\n"
    "    sys.stderr.buffer.write(chunk)\n"
    "    sys.stderr.buffer.flush()\n"
)


def _fake_bluetoothctl() -> MagicMock:
    proc = create_autospec(asyncio.subprocess.Process, instance=True)
    proc.returncode = None
    proc.stdin = create_autospec(asyncio.StreamWriter, instance=True)
    # With DEVNULL asyncio exposes no stream objects, mirror that.
    proc.stdout = None
    proc.stderr = None
    return proc


@pytest.fixture(autouse=True)
def _no_previous_agent():
    register_pairing_agent._proc = None
    yield
    register_pairing_agent._proc = None


@pytest.fixture
def no_wait():
    with patch.object(bluez_agent.asyncio, "sleep", create_autospec(asyncio.sleep)) as sleep:
        yield sleep


class TestAgentOutputIsNotPiped:
    @pytest.mark.uc("UC-001")
    async def test_bluetoothctl_output_goes_to_devnull(self, no_wait):
        spawn = create_autospec(asyncio.create_subprocess_exec, return_value=_fake_bluetoothctl())
        with patch.object(bluez_agent.asyncio, "create_subprocess_exec", spawn):
            assert await register_pairing_agent() is True

        kwargs = spawn.await_args.kwargs
        assert kwargs["stdout"] == asyncio.subprocess.DEVNULL
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        # stdin stays a pipe: the agent commands are written to it and it must stay open
        assert kwargs["stdin"] == asyncio.subprocess.PIPE

    @pytest.mark.uc("UC-001")
    async def test_every_registration_avoids_undrained_pipes(self, no_wait):
        """Re-registration after a bluetoothd restart (#1476) spawns the same way, no reader task left behind."""
        spawn = create_autospec(
            asyncio.create_subprocess_exec, side_effect=[_fake_bluetoothctl(), _fake_bluetoothctl()]
        )
        tasks_before = asyncio.all_tasks()
        with patch.object(bluez_agent.asyncio, "create_subprocess_exec", spawn):
            assert await register_pairing_agent() is True
            assert await register_pairing_agent() is True

        assert spawn.await_count == 2
        for call in spawn.await_args_list:
            assert call.kwargs["stdout"] == asyncio.subprocess.DEVNULL
            assert call.kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert asyncio.all_tasks() == tasks_before

    @pytest.mark.uc("UC-001")
    async def test_chatty_bluetoothctl_never_blocks(self):
        """A real child writing about 1.3 MB runs to completion; with an undrained pipe it would block at ~64 KB."""

        spawned = []

        async def spawn_chatty(*_args, **kwargs):
            proc = await _REAL_SPAWN(sys.executable, "-c", _CHATTY_BLUETOOTHCTL, **kwargs)
            spawned.append(proc)
            return proc

        async def short_sleep(_delay):
            await _REAL_SLEEP(0)

        with (
            patch.object(bluez_agent.asyncio, "create_subprocess_exec", side_effect=spawn_chatty),
            patch.object(bluez_agent.asyncio, "sleep", side_effect=short_sleep),
        ):
            await register_pairing_agent()

        # The child may already have exited by the registration check; only its exit matters here.
        [proc] = spawned
        try:
            assert await asyncio.wait_for(proc.wait(), timeout=5.0) == 0
        finally:
            if proc.returncode is None:
                proc.kill()
                # communicate() also drains any pipe, so cleanup cannot hang on the unfixed code
                await asyncio.wait_for(proc.communicate(), timeout=5.0)


class TestAgentRegistrationCheck:
    @pytest.mark.uc("UC-001")
    async def test_bluetoothctl_that_exits_at_once_is_a_failed_registration(self, no_wait):
        proc = _fake_bluetoothctl()
        proc.returncode = 1
        spawn = create_autospec(asyncio.create_subprocess_exec, return_value=proc)
        with patch.object(bluez_agent.asyncio, "create_subprocess_exec", spawn):
            assert await register_pairing_agent() is False

        proc.stdin.close.assert_called_once()
        assert getattr(register_pairing_agent, "_proc", None) is None

    @pytest.mark.uc("UC-001")
    async def test_running_bluetoothctl_is_kept_as_the_agent(self, no_wait):
        proc = _fake_bluetoothctl()
        spawn = create_autospec(asyncio.create_subprocess_exec, return_value=proc)
        with patch.object(bluez_agent.asyncio, "create_subprocess_exec", spawn):
            assert await register_pairing_agent() is True

        assert [c.args[0] for c in proc.stdin.write.call_args_list] == [b"agent KeyboardOnly\n", b"default-agent\n"]
        assert register_pairing_agent._proc is proc
