"""
Mobile Controller Service for phone-based controllers.

Provides gRPC interface between mobile-gateway and controller-manager.
Runs on a separate port (50063) alongside the main gRPC server.
"""

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from proto import mobile_controller_pb2, mobile_controller_pb2_grpc
from services.controller_manager import metrics

if TYPE_CHECKING:
    from services.controller_manager.multiplexer.mobile_adapter import MobileAdapter

logger = logging.getLogger(__name__)


class MobileControllerService(mobile_controller_pb2_grpc.MobileControllerServiceServicer):
    """gRPC service for phone controller registration and data streaming."""

    def __init__(self, adapter: "MobileAdapter"):
        self.adapter = adapter
        logger.info("MobileControllerService initialized")

    def RegisterPhone(self, request, _context):
        """Register a phone as a controller."""
        try:
            phone = self.adapter.register_phone(name=request.name)
            metrics.controller_mobile_phones_active.set(self.adapter.phone_count)
            logger.info(f"Mobile: Registered phone {phone.serial} (name={request.name!r})")
            return mobile_controller_pb2.RegisterPhoneResponse(serial=phone.serial, success=True)
        except Exception as e:
            logger.error(f"RegisterPhone error: {e}")
            return mobile_controller_pb2.RegisterPhoneResponse(serial="", success=False)

    def UnregisterPhone(self, request, _context):
        """Unregister a phone controller."""
        try:
            success = self.adapter.unregister_phone(request.serial)
            if success:
                metrics.controller_mobile_phones_active.set(self.adapter.phone_count)
                logger.info(f"Mobile: Unregistered phone {request.serial}")
            return mobile_controller_pb2.UnregisterPhoneResponse(success=success)
        except Exception as e:
            logger.error(f"UnregisterPhone error: {e}")
            return mobile_controller_pb2.UnregisterPhoneResponse(success=False)

    async def StreamPhoneData(self, request_iterator, context):
        """Bidirectional stream: receive sensor data, send LED/rumble commands.

        Incoming: PhoneDataUpdate messages at ~60Hz
        Outgoing: PhoneCommand messages when LED/rumble changes (polled at 20Hz)
        """
        serial = None
        phone = None

        try:
            # Process incoming sensor data in a background task
            async def process_incoming():
                nonlocal serial, phone
                async for update in request_iterator:
                    if serial is None:
                        serial = update.serial
                        phone = self.adapter.get_phone(serial)
                        if phone is None:
                            logger.warning(f"Mobile: StreamPhoneData for unknown serial {serial}")
                            return

                    if phone is not None:
                        phone.update_motion(
                            accel_x=update.accel.x,
                            accel_y=update.accel.y,
                            accel_z=update.accel.z,
                            gyro_x=update.gyro.x,
                            gyro_y=update.gyro.y,
                            gyro_z=update.gyro.z,
                            buttons=update.buttons,
                            battery=update.battery,
                            seq=update.seq,
                        )
                        metrics.controller_mobile_data_received_total.inc()

            incoming_task = asyncio.create_task(process_incoming())

            try:
                # Poll for output changes at 20Hz and yield commands
                while not context.cancelled():
                    if phone is not None:
                        output = phone.get_pending_output()
                        if output is not None:
                            led, rumble = output
                            # Send LED command
                            yield mobile_controller_pb2.PhoneCommand(
                                serial=serial,
                                led=mobile_controller_pb2.LedColor(
                                    r=led["r"],
                                    g=led["g"],
                                    b=led["b"],
                                ),
                            )
                            # Send rumble command if non-zero
                            if rumble > 0:
                                yield mobile_controller_pb2.PhoneCommand(
                                    serial=serial,
                                    rumble=mobile_controller_pb2.RumbleCommand(intensity=rumble),
                                )
                    await asyncio.sleep(0.05)  # 20Hz output polling

                    # Check if incoming stream ended
                    if incoming_task.done():
                        break
            finally:
                incoming_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await incoming_task

        except Exception as e:
            logger.error(f"StreamPhoneData error: {e}")

        logger.info(f"Mobile: StreamPhoneData ended for {serial}")
