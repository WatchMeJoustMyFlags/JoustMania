"""Pairing agent registration, also after a bluetoothd restart (UC-001 BR-005, #632, #1466)."""

import asyncio
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
    proc.stdout.read = AsyncMock(return_value=b"Agent registered\nDefault agent request successful\n")
    return proc


@pytest.fixture(autouse=True)
def _no_previous_agent():
    register_pairing_agent._proc = None
    yield
    register_pairing_agent._proc = None


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
