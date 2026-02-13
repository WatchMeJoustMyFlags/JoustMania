"""Shared BlueZ D-Bus helpers using dbus-fast.

Provides async functions for interacting with BlueZ and systemd over D-Bus
using dbus-fast's low-level Message API (same pattern Bleak uses).

All functions take ``bus`` as first argument for testability.
Use :func:`get_system_bus` for the shared singleton connection.
"""

import asyncio
import logging

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

logger = logging.getLogger(__name__)

_bus: MessageBus | None = None
_bus_lock = asyncio.Lock()

ORG_BLUEZ = "org.bluez"
ORG_BLUEZ_PATH = "/org/bluez"
DBUS_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"


class DBusError(Exception):
    """Raised when a D-Bus method call returns an error reply."""

    def __init__(self, error_name: str, error_body: list | None = None):
        self.error_name = error_name
        self.error_body = error_body or []
        body_text = str(self.error_body[0]) if self.error_body else error_name
        super().__init__(body_text)


def assert_reply(reply: Message) -> Message:
    """Raise :class:`DBusError` if *reply* is an error, otherwise return it."""
    if reply.message_type == MessageType.ERROR:
        raise DBusError(reply.error_name, reply.body)
    return reply


async def get_system_bus() -> MessageBus:
    """Return a shared system bus connection (lazy singleton with reconnect)."""
    global _bus
    if _bus is not None and _bus.connected:
        return _bus
    async with _bus_lock:
        # Double-checked locking
        if _bus is not None and _bus.connected:
            return _bus
        _bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return _bus


async def close_system_bus() -> None:
    """Disconnect the shared system bus if connected."""
    global _bus
    async with _bus_lock:
        if _bus is not None:
            _bus.disconnect()
            _bus = None


async def get_managed_objects(bus: MessageBus) -> dict:
    """Call org.freedesktop.DBus.ObjectManager.GetManagedObjects on BlueZ.

    Returns the full object tree: ``{object_path: {interface: {prop: value}}}``.
    This is far more efficient than N+1 introspection walks.
    """
    reply = assert_reply(
        await bus.call(
            Message(
                destination=ORG_BLUEZ,
                path="/",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects",
            )
        )
    )
    return reply.body[0]


async def get_adapter_property(bus: MessageBus, adapter_path: str, prop: str):
    """Get a single property from org.bluez.Adapter1."""
    reply = assert_reply(
        await bus.call(
            Message(
                destination=ORG_BLUEZ,
                path=adapter_path,
                interface=DBUS_PROPERTIES_INTERFACE,
                member="Get",
                signature="ss",
                body=["org.bluez.Adapter1", prop],
            )
        )
    )
    # Properties.Get returns Variant; unwrap to plain value
    val = reply.body[0]
    return val.value if isinstance(val, Variant) else val


async def set_adapter_property(bus: MessageBus, adapter_path: str, prop: str, value: Variant) -> None:
    """Set a single property on org.bluez.Adapter1.

    *value* must be a :class:`dbus_fast.Variant`, e.g. ``Variant("b", True)``.
    """
    assert_reply(
        await bus.call(
            Message(
                destination=ORG_BLUEZ,
                path=adapter_path,
                interface=DBUS_PROPERTIES_INTERFACE,
                member="Set",
                signature="ssv",
                body=["org.bluez.Adapter1", prop, value],
            )
        )
    )


async def get_device_property(bus: MessageBus, device_path: str, prop: str):
    """Get a single property from org.bluez.Device1."""
    reply = assert_reply(
        await bus.call(
            Message(
                destination=ORG_BLUEZ,
                path=device_path,
                interface=DBUS_PROPERTIES_INTERFACE,
                member="Get",
                signature="ss",
                body=["org.bluez.Device1", prop],
            )
        )
    )
    val = reply.body[0]
    return val.value if isinstance(val, Variant) else val


async def restart_systemd_unit(unit: str, mode: str = "replace") -> None:
    """Restart a systemd unit via the Manager D-Bus interface."""
    bus = await get_system_bus()
    assert_reply(
        await bus.call(
            Message(
                destination="org.freedesktop.systemd1",
                path="/org/freedesktop/systemd1",
                interface="org.freedesktop.systemd1.Manager",
                member="RestartUnit",
                signature="ss",
                body=[unit, mode],
            )
        )
    )
