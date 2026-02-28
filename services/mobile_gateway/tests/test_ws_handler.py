"""
Unit tests for ws_handler — WebSocket phone controller handler.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from proto import mobile_controller_pb2
from services.mobile_gateway.ws_handler import PhoneSession


@pytest.fixture
def mock_ws():
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


@pytest.fixture
def mock_stub():
    stub = AsyncMock()
    stub.RegisterPhone = AsyncMock(
        return_value=mobile_controller_pb2.RegisterPhoneResponse(serial="PHONE0000", success=True)
    )
    stub.UnregisterPhone = AsyncMock(return_value=mobile_controller_pb2.UnregisterPhoneResponse(success=True))
    stub.StreamPhoneData = MagicMock(return_value=AsyncMock())
    return stub


class TestPhoneSessionRegister:
    @pytest.mark.asyncio
    async def test_register_success(self, mock_ws, mock_stub):
        session = PhoneSession(mock_ws, mock_stub)
        result = await session.register("Alice")
        assert result is True
        assert session.serial == "PHONE0000"
        mock_stub.RegisterPhone.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_failure(self, mock_ws, mock_stub):
        mock_stub.RegisterPhone.return_value = mobile_controller_pb2.RegisterPhoneResponse(serial="", success=False)
        session = PhoneSession(mock_ws, mock_stub)
        result = await session.register("Bob")
        assert result is False
        assert session.serial is None

    @pytest.mark.asyncio
    async def test_register_rpc_error(self, mock_ws, mock_stub):
        mock_stub.RegisterPhone.side_effect = Exception("connection refused")
        session = PhoneSession(mock_ws, mock_stub)
        result = await session.register("Charlie")
        assert result is False


class TestPhoneSessionUnregister:
    @pytest.mark.asyncio
    async def test_unregister(self, mock_ws, mock_stub):
        session = PhoneSession(mock_ws, mock_stub)
        session.serial = "PHONE0000"
        await session.unregister()
        mock_stub.UnregisterPhone.assert_called_once()

    @pytest.mark.asyncio
    async def test_unregister_no_serial(self, mock_ws, mock_stub):
        session = PhoneSession(mock_ws, mock_stub)
        await session.unregister()
        mock_stub.UnregisterPhone.assert_not_called()


class TestPhoneSessionSendData:
    @pytest.mark.asyncio
    async def test_send_sensor_data_without_stream(self, mock_ws, mock_stub):
        session = PhoneSession(mock_ws, mock_stub)
        # Should not raise even without stream
        await session.send_sensor_data({"ax": 1.0, "ay": 0, "az": 0})

    @pytest.mark.asyncio
    async def test_send_button(self, mock_ws, mock_stub):
        session = PhoneSession(mock_ws, mock_stub)
        # Should not raise without stream
        await session.send_button(0x01)

    @pytest.mark.asyncio
    async def test_send_sensor_data_with_stream(self, mock_ws, mock_stub):
        session = PhoneSession(mock_ws, mock_stub)
        session.serial = "PHONE0000"
        mock_stream = AsyncMock()
        mock_stream.write = AsyncMock()
        mock_stream.__aiter__ = AsyncMock(return_value=AsyncMock(__anext__=AsyncMock(side_effect=StopAsyncIteration)))
        session._stream = mock_stream

        with patch("services.mobile_gateway.ws_handler.metrics"):
            await session.send_sensor_data(
                {
                    "ax": 1.5,
                    "ay": -0.3,
                    "az": 0.8,
                    "gx": 0.1,
                    "gy": 0.2,
                    "gz": -0.1,
                    "buttons": 1,
                    "battery": 85,
                }
            )

        mock_stream.write.assert_called_once()
        call_args = mock_stream.write.call_args[0][0]
        assert call_args.serial == "PHONE0000"
        assert call_args.accel.x == 1.5
        assert call_args.buttons == 1
