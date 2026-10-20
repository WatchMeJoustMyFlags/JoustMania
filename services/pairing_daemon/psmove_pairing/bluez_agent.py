"""BlueZ pairing agent for PS Move controllers (UC-001 BR-009).

The daemon exports an ``org.bluez.Agent1`` object on the system bus and
registers it as BlueZ's default agent (capability KeyboardOnly). It answers
PIN, passkey, confirmation and authorization requests only for controllers
paired over USB in this daemon session and not unpaired since; every other
device gets ``org.bluez.Error.Rejected`` (#1492).

It replaces a ``bluetoothctl`` session that never answered its prompts and
raced ``default-agent`` against the registration. No bluetoothctl process is
spawned any more, so its output can no longer back up and stall (#1477).
"""

import asyncio
import logging
from collections.abc import Callable

from dbus_fast import Message
from dbus_fast.annotations import DBusObjectPath, DBusStr, DBusUInt16, DBusUInt32
from dbus_fast.errors import DBusError as DBusReplyError
from dbus_fast.service import ServiceInterface, dbus_method

from lib import bluez_dbus

logger = logging.getLogger("psmove-pairing")

AGENT_PATH = "/org/joustmania/pairing_agent"
AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
# PS Move ZCM2 pairs with a legacy PIN: BlueZ asks a KeyboardOnly agent for it (RequestPinCode).
CAPABILITY = "KeyboardOnly"
PIN_CODE = "0000"
PASSKEY = 0
REGISTRATION_TIMEOUT_SECONDS = 5.0
REJECTED = "org.bluez.Error.Rejected"


def address_from_device_path(device: str) -> str:
    """``/org/bluez/hci0/dev_00_06_F7_AA_BB_CC`` -> ``00:06:F7:AA:BB:CC``."""
    return device.rsplit("/", 1)[-1].removeprefix("dev_").replace("_", ":").upper()


def _no_controller_known(_address: str) -> bool:
    return False


class PairingAgent(ServiceInterface):
    """org.bluez.Agent1: answers for controllers this daemon paired over USB, rejects every other device."""

    def __init__(self) -> None:
        super().__init__(AGENT_INTERFACE)
        # Set by the daemon to USBPairing.paired_this_session; until then every request is rejected.
        self.is_known: Callable[[str], bool] = _no_controller_known

    def _require_known(self, device: str, request: str) -> str:
        address = address_from_device_path(device)
        if not self.is_known(address):
            logger.info(f"Pairing agent: rejected {request} from {address}: not paired over USB this session")
            raise DBusReplyError(REJECTED, f"{address} was not paired over USB")
        logger.info(f"Pairing agent: answering {request} from {address}")
        return address

    @dbus_method()
    def Release(self) -> None:
        logger.info("Pairing agent released by BlueZ")

    @dbus_method()
    def RequestPinCode(self, device: DBusObjectPath) -> DBusStr:
        self._require_known(device, "PIN code request")
        return PIN_CODE

    @dbus_method()
    def DisplayPinCode(self, device: DBusObjectPath, pincode: DBusStr) -> None:
        self._require_known(device, "PIN code display")

    @dbus_method()
    def RequestPasskey(self, device: DBusObjectPath) -> DBusUInt32:
        self._require_known(device, "passkey request")
        return PASSKEY

    @dbus_method()
    def DisplayPasskey(self, device: DBusObjectPath, passkey: DBusUInt32, entered: DBusUInt16) -> None:
        # Informational only: nothing to answer, so nothing to reject
        logger.debug(f"Pairing agent: passkey shown for {address_from_device_path(device)}")

    @dbus_method()
    def RequestConfirmation(self, device: DBusObjectPath, passkey: DBusUInt32) -> None:
        self._require_known(device, "passkey confirmation")

    @dbus_method()
    def RequestAuthorization(self, device: DBusObjectPath) -> None:
        self._require_known(device, "pairing authorization")

    @dbus_method()
    def AuthorizeService(self, device: DBusObjectPath, uuid: DBusStr) -> None:
        self._require_known(device, f"service authorization {uuid}")

    @dbus_method()
    def Cancel(self) -> None:
        logger.info("Pairing agent: BlueZ cancelled the request")


agent = PairingAgent()
_exported_on = None


def set_known_controller_check(is_known: Callable[[str], bool]) -> None:
    """Which addresses the agent answers for (UC-001 BR-009): the daemon's USB pairings of this session."""
    agent.is_known = is_known


async def _call_agent_manager(bus, member: str, signature: str, body: list) -> None:
    reply = await bus.call(
        Message(
            destination=bluez_dbus.ORG_BLUEZ,
            path=bluez_dbus.ORG_BLUEZ_PATH,
            interface=AGENT_MANAGER_INTERFACE,
            member=member,
            signature=signature,
            body=body,
        )
    )
    bluez_dbus.assert_reply(reply)


async def _register(bus) -> None:
    try:
        await _call_agent_manager(bus, "RegisterAgent", "os", [AGENT_PATH, CAPABILITY])
    except bluez_dbus.DBusError as e:
        # bluetoothd still knows the agent from an earlier registration (no restart in between)
        if e.error_name != "org.bluez.Error.AlreadyExists":
            raise
    # Only after RegisterAgent replied: earlier, BlueZ has no agent to make default (#1492)
    await _call_agent_manager(bus, "RequestDefaultAgent", "o", [AGENT_PATH])


async def register_pairing_agent() -> bool:
    """Register the agent with BlueZ as the default agent (UC-001 BR-009).

    Called at daemon start and again after every bluetoothd restart (UC-001
    BR-005): the restart drops BlueZ's agent registrations. The agent object
    stays exported on the daemon's bus connection; it is exported again only on
    a new connection.

    Returns True once BlueZ confirmed both RegisterAgent and RequestDefaultAgent.
    A refusal, a missing reply within REGISTRATION_TIMEOUT_SECONDS or an
    unreachable bus is logged as an error and returns False; the daemon keeps running.
    """
    global _exported_on
    try:
        bus = await bluez_dbus.get_system_bus()
        if _exported_on is not bus:
            bus.export(AGENT_PATH, agent)
            _exported_on = bus
        await asyncio.wait_for(_register(bus), timeout=REGISTRATION_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.error(f"BlueZ did not confirm the pairing agent within {REGISTRATION_TIMEOUT_SECONDS:g}s")
        return False
    except bluez_dbus.DBusError as e:
        logger.error(f"BlueZ refused the pairing agent: {e.error_name}: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to register pairing agent: {e}")
        return False
    logger.info(f"BlueZ pairing agent registered as default agent ({CAPABILITY})")
    return True
