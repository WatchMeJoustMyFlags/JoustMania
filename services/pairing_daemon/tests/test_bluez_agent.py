"""BlueZ pairing agent: registration and answers (UC-001 BR-005, BR-009, #632, #1492).

The agent is an org.bluez.Agent1 object on the system bus. The tests call its
methods directly (as BlueZ would over D-Bus) and register it on a fake bus.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError as DBusReplyError

from psmove_pairing import bluez_agent
from psmove_pairing.bluez_agent import (
    AGENT_PATH,
    REJECTED,
    PairingAgent,
    address_from_device_path,
    register_pairing_agent,
)

KNOWN = "00:06:F7:AA:BB:CC"
UNKNOWN = "00:06:F7:11:22:33"


def _device(address: str, hci: str = "hci0") -> str:
    return f"/org/bluez/{hci}/dev_{address.replace(':', '_')}"


def _reply(error_name: str | None = None) -> Message:
    if error_name is None:
        return Message(message_type=MessageType.METHOD_RETURN, reply_serial=1)
    return Message(message_type=MessageType.ERROR, error_name=error_name, reply_serial=1, body=["boom"], signature="s")


def _bluez_calls(agent: PairingAgent, member: str, *args):
    """Run an Agent1 method the way dbus-fast does for a BlueZ call (the decorated wrapper drops the result)."""
    return getattr(PairingAgent, member).__wrapped__(agent, *args)


@pytest.fixture
def agent() -> PairingAgent:
    agent = PairingAgent()
    agent.is_known = lambda address: address == KNOWN
    return agent


class TestAgentAnswersOnlyUsbPairedControllers:
    @pytest.mark.uc("UC-001", br=["BR-009"])
    def test_known_controller_gets_pin_and_passkey(self, agent):
        assert _bluez_calls(agent, "RequestPinCode", _device(KNOWN)) == "0000"
        assert _bluez_calls(agent, "RequestPasskey", _device(KNOWN)) == 0

    @pytest.mark.uc("UC-001", br=["BR-009"])
    def test_known_controller_is_confirmed_and_authorized(self, agent):
        device = _device(KNOWN, "hci1")
        # No error reply means BlueZ takes it as yes
        _bluez_calls(agent, "RequestConfirmation", device, 123456)
        _bluez_calls(agent, "RequestAuthorization", device)
        _bluez_calls(agent, "AuthorizeService", device, "00001124-0000-1000-8000-00805f9b34fb")

    @pytest.mark.parametrize(
        "request_",
        [
            lambda a, d: _bluez_calls(a, "RequestPinCode", d),
            lambda a, d: _bluez_calls(a, "RequestPasskey", d),
            lambda a, d: _bluez_calls(a, "RequestConfirmation", d, 123456),
            lambda a, d: _bluez_calls(a, "RequestAuthorization", d),
            lambda a, d: _bluez_calls(a, "AuthorizeService", d, "00001124-0000-1000-8000-00805f9b34fb"),
            lambda a, d: _bluez_calls(a, "DisplayPinCode", d, "0000"),
        ],
        ids=["pin", "passkey", "confirmation", "authorization", "service", "display-pin"],
    )
    @pytest.mark.uc("UC-001", br=["BR-009"])
    def test_unknown_device_is_rejected_and_logged(self, agent, request_, caplog):
        with caplog.at_level(logging.INFO, logger="psmove-pairing"), pytest.raises(DBusReplyError) as rejected:
            request_(agent, _device(UNKNOWN))

        assert rejected.value.type == REJECTED
        assert any(r.levelno == logging.INFO and UNKNOWN in r.getMessage() for r in caplog.records)

    @pytest.mark.uc("UC-001", br=["BR-009"])
    def test_agent_rejects_everything_before_the_daemon_wires_it(self):
        with pytest.raises(DBusReplyError) as rejected:
            _bluez_calls(PairingAgent(), "RequestPinCode", _device(KNOWN))
        assert rejected.value.type == REJECTED

    def test_methods_match_the_bluez_agent1_api(self):
        """BlueZ calls these by name and signature (doc/org.bluez.Agent.rst)."""
        methods = {
            m.attrib["name"]: "".join(f"{x.attrib['direction'][0]}{x.attrib['type']}" for x in m)
            for m in PairingAgent().introspect().to_xml()
        }
        assert methods == {
            "Release": "",
            "RequestPinCode": "ioos",
            "DisplayPinCode": "iois",
            "RequestPasskey": "ioou",
            "DisplayPasskey": "ioiuiq",
            "RequestConfirmation": "ioiu",
            "RequestAuthorization": "io",
            "AuthorizeService": "iois",
            "Cancel": "",
        }

    def test_device_path_gives_the_address(self):
        assert address_from_device_path("/org/bluez/hci2/dev_00_06_f7_aa_bb_cc") == KNOWN


@pytest.fixture
def bus():
    """A fresh fake system bus; the agent is exported again on every new connection."""
    bus = create_autospec(MessageBus, instance=True)
    bus.call.return_value = _reply()
    with (
        patch.object(bluez_agent.bluez_dbus, "get_system_bus", AsyncMock(return_value=bus)),
        patch.object(bluez_agent, "_exported_on", None),
        patch.object(bluez_agent, "_registered_on", None),
        patch.object(bluez_agent, "_watched_bus", None),
        patch.object(bluez_agent, "_last_failure", None),
    ):
        yield bus


def _members(bus) -> list[tuple[str, list]]:
    """AgentManager1 calls (the agent registration), in order."""
    return [
        (c.args[0].member, list(c.args[0].body))
        for c in bus.call.await_args_list
        if c.args[0].interface == "org.bluez.AgentManager1"
    ]


def _owner_changed(new_owner: str, name: str = "org.bluez") -> Message:
    return Message.new_signal(
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameOwnerChanged",
        "sss",
        [name, ":1.1" if not new_owner else "", new_owner],
    )


async def _drain_background() -> None:
    for _ in range(10):
        await asyncio.sleep(0)
    if bluez_agent._background_tasks:
        await asyncio.gather(*bluez_agent._background_tasks)


class TestRegisterPairingAgent:
    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005", "BR-009"])
    async def test_registers_then_requests_default_agent(self, bus):
        assert await register_pairing_agent() is True

        bus.export.assert_called_once_with(AGENT_PATH, bluez_agent.agent)
        assert _members(bus) == [("RegisterAgent", [AGENT_PATH, "KeyboardOnly"]), ("RequestDefaultAgent", [AGENT_PATH])]
        assert {c.args[0].interface for c in bus.call.await_args_list} == {
            "org.bluez.AgentManager1",
            "org.freedesktop.DBus",  # AddMatch for bluetoothd restarts
        }

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_default_agent_is_requested_only_after_the_registration_reply(self, bus):
        """#1492: RequestDefaultAgent before RegisterAgent replied finds no agent."""
        register_reply = asyncio.get_running_loop().create_future()

        async def call(message):
            return await register_reply if message.member == "RegisterAgent" else _reply()

        bus.call.side_effect = call

        registration = asyncio.create_task(register_pairing_agent())
        for _ in range(5):
            await asyncio.sleep(0)
        assert [m for m, _ in _members(bus)] == ["RegisterAgent"]

        register_reply.set_result(_reply())
        assert await registration is True
        assert [m for m, _ in _members(bus)] == ["RegisterAgent", "RequestDefaultAgent"]

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005", "BR-009"])
    async def test_registering_again_reuses_the_exported_agent(self, bus):
        """After a bluetoothd restart the bus connection is the same: register again, export once."""
        assert await register_pairing_agent() is True
        assert await register_pairing_agent() is True

        bus.export.assert_called_once()
        assert [m for m, _ in _members(bus)] == ["RegisterAgent", "RequestDefaultAgent"] * 2

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_agent_bluez_still_knows_is_made_default(self, bus):
        bus.call.side_effect = lambda m: _reply(
            "org.bluez.Error.AlreadyExists" if m.member == "RegisterAgent" else None
        )

        assert await register_pairing_agent() is True
        assert [m for m, _ in _members(bus)] == ["RegisterAgent", "RequestDefaultAgent"]

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_refused_registration_is_reported(self, bus, caplog):
        bus.call.side_effect = lambda _m: _reply("org.bluez.Error.InvalidArguments")

        assert await register_pairing_agent() is False
        assert [m for m, _ in _members(bus)] == ["RegisterAgent"]
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_refused_default_agent_is_reported(self, bus):
        bus.call.side_effect = lambda m: _reply("org.bluez.Error.DoesNotExist" if m.member != "RegisterAgent" else None)

        assert await register_pairing_agent() is False

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_unconfirmed_registration_times_out(self, bus, caplog):
        async def never_replies(_message):
            await asyncio.Event().wait()

        bus.call.side_effect = never_replies

        with patch.object(bluez_agent, "REGISTRATION_TIMEOUT_SECONDS", 0.01):
            assert await register_pairing_agent() is False
        assert any("did not confirm" in r.getMessage() for r in caplog.records)

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_unreachable_bus_is_reported(self):
        with patch.object(bluez_agent.bluez_dbus, "get_system_bus", AsyncMock(side_effect=OSError("no bus"))):
            assert await register_pairing_agent() is False


class TestAgentIsRegisteredAgain:
    """BlueZ forgets the agent whenever bluetoothd restarts; the daemon registers it again (UC-001 BR-009)."""

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_owner_changes_of_org_bluez_are_watched(self, bus):
        assert await register_pairing_agent() is True

        bus.add_message_handler.assert_called_once_with(bluez_agent._on_bus_message)
        [match] = [c.args[0] for c in bus.call.await_args_list if c.args[0].member == "AddMatch"]
        assert "NameOwnerChanged" in match.body[0] and "arg0='org.bluez'" in match.body[0]

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_external_bluetoothd_restart_registers_again(self, bus):
        """(b) bluetoothd restarted or crashed outside the daemon: it rejoins the bus with a new owner."""
        assert await register_pairing_agent() is True

        bluez_agent._on_bus_message(_owner_changed(""))  # bluetoothd gone
        assert bluez_agent._registered_on is None
        assert len(_members(bus)) == 2  # nothing to register with while it is gone

        bluez_agent._on_bus_message(_owner_changed(":1.42"))  # bluetoothd back
        await _drain_background()

        assert [m for m, _ in _members(bus)] == ["RegisterAgent", "RequestDefaultAgent"] * 2
        assert bluez_agent._registered_on is bus

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_other_names_are_ignored(self, bus):
        assert await register_pairing_agent() is True

        bluez_agent._on_bus_message(_owner_changed(":1.42", name="org.freedesktop.NetworkManager"))
        await _drain_background()

        assert len(_members(bus)) == 2
        assert bluez_agent._registered_on is bus

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_bluetoothd_down_at_start_is_retried_until_it_answers(self, bus, caplog):
        """(a) No bluetoothd at daemon start: each monitor cycle tries again; the error is logged once."""
        bus.call.side_effect = lambda m: _reply(
            "org.freedesktop.DBus.Error.ServiceUnknown" if m.interface == "org.bluez.AgentManager1" else None
        )
        assert await register_pairing_agent() is False
        assert await bluez_agent.ensure_pairing_agent() is False

        bus.call.side_effect = None
        assert await bluez_agent.ensure_pairing_agent() is True
        assert await bluez_agent.ensure_pairing_agent() is True  # confirmed: nothing sent

        assert [m for m, _ in _members(bus)] == ["RegisterAgent"] * 2 + ["RegisterAgent", "RequestDefaultAgent"]
        assert sum(r.levelno == logging.ERROR for r in caplog.records) == 1

    @pytest.mark.uc("UC-001", br=["BR-009"])
    async def test_new_bus_connection_exports_and_registers_again(self, bus):
        """(c) lib.bluez_dbus reconnected: the agent is not exported on the new connection yet."""
        assert await register_pairing_agent() is True

        new_bus = create_autospec(MessageBus, instance=True)
        new_bus.call.return_value = _reply()
        with patch.object(bluez_agent.bluez_dbus, "get_system_bus", AsyncMock(return_value=new_bus)):
            assert await bluez_agent.ensure_pairing_agent() is True

        new_bus.export.assert_called_once_with(AGENT_PATH, bluez_agent.agent)
        new_bus.add_message_handler.assert_called_once_with(bluez_agent._on_bus_message)
        assert [m for m, _ in _members(new_bus)] == ["RegisterAgent", "RequestDefaultAgent"]
        assert bluez_agent._registered_on is new_bus
