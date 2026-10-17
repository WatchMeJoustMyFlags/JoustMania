"""Pairing agent registration, also after a bluetoothd restart (UC-001 BR-005, #632, #1466)."""

import asyncio
import os
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from psmove_pairing import bluez_agent
from psmove_pairing.bluez_agent import register_pairing_agent


def _fake_bluetoothctl() -> MagicMock:
    proc = create_autospec(asyncio.subprocess.Process, instance=True)
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdout = MagicMock()
    # bluetoothctl's output, then end of file once the process is stopped
    proc.stdout.read = AsyncMock(side_effect=[b"Agent registered\nDefault agent request successful\n", b""])
    return proc


@pytest.fixture(autouse=True)
async def _no_previous_agent():
    register_pairing_agent._proc = None
    yield
    await bluez_agent._stop_previous_agent()


class TestRegisterPairingAgent:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_registers_keyboard_only_default_agent(self):
        proc = _fake_bluetoothctl()
        with (
            patch.object(bluez_agent.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)) as spawn,
            patch.object(bluez_agent.asyncio, "sleep", AsyncMock()),
        ):
            assert await register_pairing_agent() is True

        assert spawn.await_args.args == ("bluetoothctl",)
        assert [c.args[0] for c in proc.stdin.write.call_args_list] == [b"agent KeyboardOnly\n", b"default-agent\n"]
        assert register_pairing_agent._proc is proc

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_registering_again_stops_the_previous_session(self):
        """After a bluetoothd restart the old bluetoothctl session is dead weight: stop it, start a new one."""
        old, new = _fake_bluetoothctl(), _fake_bluetoothctl()
        with (
            patch.object(bluez_agent.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[old, new])),
            patch.object(bluez_agent.asyncio, "sleep", AsyncMock()),
        ):
            await register_pairing_agent()
            await register_pairing_agent()

        old.terminate.assert_called_once()
        old.wait.assert_awaited()
        new.terminate.assert_not_called()
        assert register_pairing_agent._proc is new

    @pytest.mark.asyncio
    async def test_exited_previous_session_is_not_signalled(self):
        old, new = _fake_bluetoothctl(), _fake_bluetoothctl()
        old.returncode = 0
        with (
            patch.object(bluez_agent.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[old, new])),
            patch.object(bluez_agent.asyncio, "sleep", AsyncMock()),
        ):
            await register_pairing_agent()
            await register_pairing_agent()

        old.terminate.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_bluetoothctl_is_reported(self):
        with patch.object(bluez_agent.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError)):
            assert await register_pairing_agent() is False


# A stand-in bluetoothctl that writes far more than a pipe buffer (~64 KB on Linux) to
# stdout and stderr, as a long-running session does with device and property events,
# then answers a PIN request; with an undrained pipe it blocks before the answer.
_CHATTY_BLUETOOTHCTL = textwrap.dedent(
    """\
    #!{python}
    import sys, time
    for _ in range(64):
        sys.stdout.write("[CHG] Device 00:06:F7:AA:BB:CC RSSI: -60" + " " * 4000 + "\\n")
        sys.stderr.write("event" + " " * 4000 + "\\n")
        sys.stdout.flush()
        sys.stderr.flush()
    open({answered!r}, "w").close()
    time.sleep(60)
    """
)


@pytest.fixture
def chatty_bluetoothctl(tmp_path, monkeypatch) -> Path:
    """Put a fake ``bluetoothctl`` first on PATH; returns the file it creates once all output is written."""
    answered = tmp_path / "answered"
    script = tmp_path / "bin" / "bluetoothctl"
    script.parent.mkdir()
    script.write_text(_CHATTY_BLUETOOTHCTL.format(python=sys.executable, answered=str(answered)))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")
    return answered


async def _wait_for(path: Path) -> bool:
    """Whether ``path`` appears within 10 s."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if await asyncio.to_thread(path.exists):
            return True
        await asyncio.sleep(0.05)
    return False


class TestAgentOutputIsDrained:
    """#1477: bluetoothctl must never block on a full stdout/stderr pipe (UC-001 BR-005)."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_long_running_session_never_blocks_on_its_output(self, chatty_bluetoothctl):
        assert await register_pairing_agent() is True

        assert await _wait_for(chatty_bluetoothctl), "bluetoothctl blocked on an undrained output pipe"
        assert register_pairing_agent._proc.returncode is None

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_registering_again_stops_the_old_process_and_its_reader(self, chatty_bluetoothctl):
        """After a bluetoothd restart the old session is terminated and its output reader ends (no leak)."""
        await register_pairing_agent()
        old_proc, old_reader = register_pairing_agent._proc, register_pairing_agent._drain_task
        assert await _wait_for(chatty_bluetoothctl)

        await register_pairing_agent()

        assert old_proc.returncode is not None
        assert old_reader.done()
        assert register_pairing_agent._proc is not old_proc
        assert register_pairing_agent._proc.returncode is None
