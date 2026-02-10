"""Tests for lib.bluez_dbus shared module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lib.bluez_dbus import (
    DBusError,
    assert_reply,
    get_adapter_property,
    get_managed_objects,
    get_system_bus,
    restart_systemd_unit,
    set_adapter_property,
)


class TestDBusError:
    def test_error_with_body(self):
        err = DBusError("org.freedesktop.DBus.Error.Failed", ["Something went wrong"])
        assert err.error_name == "org.freedesktop.DBus.Error.Failed"
        assert str(err) == "Something went wrong"

    def test_error_without_body(self):
        err = DBusError("org.freedesktop.DBus.Error.Failed")
        assert str(err) == "org.freedesktop.DBus.Error.Failed"


class TestAssertReply:
    def test_returns_reply_on_success(self):
        from dbus_fast import MessageType

        reply = MagicMock()
        reply.message_type = MessageType.METHOD_RETURN
        result = assert_reply(reply)
        assert result is reply

    def test_raises_on_error(self):
        from dbus_fast import MessageType

        reply = MagicMock()
        reply.message_type = MessageType.ERROR
        reply.error_name = "org.freedesktop.DBus.Error.Failed"
        reply.body = ["test error"]

        with pytest.raises(DBusError) as exc_info:
            assert_reply(reply)
        assert exc_info.value.error_name == "org.freedesktop.DBus.Error.Failed"


class TestGetSystemBus:
    @pytest.mark.asyncio
    async def test_lazy_connect(self):
        """Verify singleton behavior — connect is called once."""
        import lib.bluez_dbus as mod

        # Reset global state
        mod._bus = None

        mock_bus_instance = MagicMock()
        mock_bus_instance.connected = True

        mock_bus_cls = MagicMock()
        mock_bus_cls.return_value.connect = AsyncMock(return_value=mock_bus_instance)

        with patch.object(mod, "_bus", None), patch("lib.bluez_dbus.MessageBus", mock_bus_cls):
            bus1 = await get_system_bus()
            assert bus1 is mock_bus_instance

    @pytest.mark.asyncio
    async def test_reconnect_on_disconnect(self):
        """Verify new bus created when previous is disconnected."""
        import lib.bluez_dbus as mod

        stale_bus = MagicMock()
        stale_bus.connected = False

        new_bus = MagicMock()
        new_bus.connected = True

        mock_bus_cls = MagicMock()
        mock_bus_cls.return_value.connect = AsyncMock(return_value=new_bus)

        with patch.object(mod, "_bus", stale_bus), patch("lib.bluez_dbus.MessageBus", mock_bus_cls):
            bus = await get_system_bus()
            assert bus is new_bus


class TestGetManagedObjects:
    @pytest.mark.asyncio
    async def test_parses_response(self):
        from dbus_fast import MessageType

        mock_bus = AsyncMock()
        reply = MagicMock()
        reply.message_type = MessageType.METHOD_RETURN
        reply.body = [
            {
                "/org/bluez/hci0": {
                    "org.bluez.Adapter1": {"Address": "AA:BB:CC:DD:EE:FF"},
                },
                "/org/bluez/hci0/dev_11_22_33_44_55_66": {
                    "org.bluez.Device1": {"Address": "11:22:33:44:55:66"},
                },
            }
        ]
        mock_bus.call = AsyncMock(return_value=reply)

        result = await get_managed_objects(mock_bus)
        assert "/org/bluez/hci0" in result
        assert result["/org/bluez/hci0"]["org.bluez.Adapter1"]["Address"] == "AA:BB:CC:DD:EE:FF"


class TestGetAdapterProperty:
    @pytest.mark.asyncio
    async def test_returns_unwrapped_value(self):
        from dbus_fast import MessageType, Variant

        mock_bus = AsyncMock()
        reply = MagicMock()
        reply.message_type = MessageType.METHOD_RETURN
        reply.body = [Variant("b", True)]
        mock_bus.call = AsyncMock(return_value=reply)

        result = await get_adapter_property(mock_bus, "/org/bluez/hci0", "Powered")
        assert result is True

        # Verify the message was constructed correctly
        msg = mock_bus.call.call_args[0][0]
        assert msg.member == "Get"
        assert msg.body == ["org.bluez.Adapter1", "Powered"]


class TestSetAdapterProperty:
    @pytest.mark.asyncio
    async def test_sends_variant(self):
        from dbus_fast import MessageType, Variant

        mock_bus = AsyncMock()
        reply = MagicMock()
        reply.message_type = MessageType.METHOD_RETURN
        reply.body = []
        mock_bus.call = AsyncMock(return_value=reply)

        await set_adapter_property(mock_bus, "/org/bluez/hci0", "Powered", Variant("b", True))

        msg = mock_bus.call.call_args[0][0]
        assert msg.member == "Set"
        assert msg.body[0] == "org.bluez.Adapter1"
        assert msg.body[1] == "Powered"


class TestRestartSystemdUnit:
    @pytest.mark.asyncio
    async def test_calls_restart_unit(self):
        from dbus_fast import MessageType

        mock_bus = AsyncMock()
        reply = MagicMock()
        reply.message_type = MessageType.METHOD_RETURN
        reply.body = ["/org/freedesktop/systemd1/job/123"]
        mock_bus.call = AsyncMock(return_value=reply)

        with patch("lib.bluez_dbus.get_system_bus", AsyncMock(return_value=mock_bus)):
            await restart_systemd_unit("bluetooth.service")

        msg = mock_bus.call.call_args[0][0]
        assert msg.destination == "org.freedesktop.systemd1"
        assert msg.member == "RestartUnit"
        assert msg.body == ["bluetooth.service", "replace"]

    @pytest.mark.asyncio
    async def test_raises_on_error(self):
        from dbus_fast import MessageType

        mock_bus = AsyncMock()
        reply = MagicMock()
        reply.message_type = MessageType.ERROR
        reply.error_name = "org.freedesktop.systemd1.NoSuchUnit"
        reply.body = ["Unit not found"]
        mock_bus.call = AsyncMock(return_value=reply)

        with (
            patch("lib.bluez_dbus.get_system_bus", AsyncMock(return_value=mock_bus)),
            pytest.raises(DBusError, match="Unit not found"),
        ):
            await restart_systemd_unit("nonexistent.service")
