"""
gRPC servicer for the Python HID service.

Implements ControllerIOServiceServicer from psmove_hid.proto.
Unary RPCs delegate to DeviceManager via asyncio.to_thread().
Bidirectional StreamIO registers a stream queue and forwards data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from proto import psmove_hid_pb2, psmove_hid_pb2_grpc
from services.python_hid.device_manager import Command, CommandType, DeviceManager

logger = logging.getLogger(__name__)


class ControllerIOServicer(psmove_hid_pb2_grpc.ControllerIOServiceServicer):
    """gRPC servicer backed by DeviceManager thread."""

    def __init__(self, device_manager: DeviceManager):
        self._dm = device_manager

    async def Discover(self, request, context):
        result = await asyncio.to_thread(
            self._dm.send_command,
            Command(
                type=CommandType.DISCOVER,
                args={
                    "force": request.force,
                    "verify_only": request.verify_only,
                    "exclude_serials": list(request.exclude_serials),
                },
            ),
        )

        controllers = []
        if result:
            for info in result:
                controllers.append(
                    psmove_hid_pb2.DiscoveredController(
                        serial=info["serial"],
                        adapter_name=info.get("adapter_name", ""),
                        product_id=info.get("product_id", 0),
                    )
                )

        return psmove_hid_pb2.DiscoverResponse(controllers=controllers)

    async def Open(self, request, context):
        success = await asyncio.to_thread(
            self._dm.send_command,
            Command(type=CommandType.OPEN, args={"serial": request.serial}),
        )
        return psmove_hid_pb2.OpenResponse(success=bool(success))

    async def Close(self, request, context):
        await asyncio.to_thread(
            self._dm.send_command,
            Command(type=CommandType.CLOSE, args={"serial": request.serial}),
        )
        return psmove_hid_pb2.CloseResponse()

    async def CloseAll(self, request, context):
        await asyncio.to_thread(
            self._dm.send_command,
            Command(type=CommandType.CLOSE_ALL),
        )
        return psmove_hid_pb2.CloseAllResponse()

    async def StreamIO(self, request_iterator, context):
        """Bidirectional stream: receive IOCommands, yield SensorData."""
        stream_id = str(uuid.uuid4())
        stream_queue = self._dm.register_stream(stream_id)
        logger.info(f"StreamIO started: {stream_id}")

        # Background task to process incoming IOCommands
        async def process_commands():
            try:
                async for io_command in request_iterator:
                    await asyncio.to_thread(
                        self._dm.send_command,
                        Command(
                            type=CommandType.SET_OUTPUT,
                            args={
                                "serial": io_command.serial,
                                "r": io_command.r,
                                "g": io_command.g,
                                "b": io_command.b,
                                "rumble": io_command.rumble,
                            },
                        ),
                    )
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"StreamIO command processing ended: {e}")

        command_task = asyncio.create_task(process_commands())

        try:
            while not context.cancelled():
                try:
                    sensor_data = await asyncio.to_thread(stream_queue.get, timeout=0.1)
                    yield psmove_hid_pb2.SensorData(
                        serial=sensor_data.serial,
                        battery=sensor_data.battery,
                        trigger=sensor_data.trigger,
                        temperature=sensor_data.temperature,
                        button_move=sensor_data.button_move,
                        button_trigger=sensor_data.button_trigger,
                        button_ps=sensor_data.button_ps,
                        button_cross=sensor_data.button_cross,
                        button_circle=sensor_data.button_circle,
                        button_square=sensor_data.button_square,
                        button_triangle=sensor_data.button_triangle,
                        button_select=sensor_data.button_select,
                        button_start=sensor_data.button_start,
                        accel_x=sensor_data.accel_x,
                        accel_y=sensor_data.accel_y,
                        accel_z=sensor_data.accel_z,
                        gyro_x=sensor_data.gyro_x,
                        gyro_y=sensor_data.gyro_y,
                        gyro_z=sensor_data.gyro_z,
                    )
                except Exception:
                    # Timeout or queue empty — just loop
                    continue
        finally:
            command_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await command_task
            self._dm.unregister_stream(stream_id)
            logger.info(f"StreamIO ended: {stream_id}")
