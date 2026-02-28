"""
WebSocket handler for phone controller connections.

Lifecycle per phone:
1. WS connect -> RegisterPhone() gRPC -> open StreamPhoneData stream
2. WS message (JSON sensor data) -> PhoneDataUpdate on gRPC stream
3. gRPC PhoneCommand (LED/rumble) -> WS JSON message to phone
4. WS disconnect -> cancel stream -> UnregisterPhone() gRPC
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

import grpc.aio
from aiohttp import web

from proto import mobile_controller_pb2, mobile_controller_pb2_grpc
from services.mobile_gateway import metrics

logger = logging.getLogger(__name__)


class PhoneSession:
    """Manages a single phone's WebSocket + gRPC lifecycle."""

    def __init__(self, ws: web.WebSocketResponse, stub: mobile_controller_pb2_grpc.MobileControllerServiceStub):
        self.ws = ws
        self.stub = stub
        self.serial: str | None = None
        self._stream = None
        self._outgoing_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._seq: int = 0

    async def register(self, name: str) -> bool:
        """Register phone with controller-manager."""
        try:
            resp = await self.stub.RegisterPhone(mobile_controller_pb2.RegisterPhoneRequest(name=name))
            if resp.success:
                self.serial = resp.serial
                logger.info(f"Registered phone {self.serial} (name={name!r})")
                return True
            logger.warning(f"Failed to register phone (name={name!r})")
            return False
        except Exception as e:
            logger.error(f"RegisterPhone RPC failed: {e}")
            return False

    async def unregister(self) -> None:
        """Unregister phone from controller-manager."""
        if self.serial:
            try:
                await self.stub.UnregisterPhone(mobile_controller_pb2.UnregisterPhoneRequest(serial=self.serial))
                logger.info(f"Unregistered phone {self.serial}")
            except Exception as e:
                logger.error(f"UnregisterPhone RPC failed: {e}")

    async def start_stream(self) -> None:
        """Open bidirectional gRPC stream and start command relay task."""
        self._stream = self.stub.StreamPhoneData()
        # Start task to relay commands from gRPC back to phone WebSocket
        asyncio.create_task(self._relay_commands())

    async def send_sensor_data(self, data: dict) -> None:
        """Send normalized sensor data to controller-manager via gRPC stream."""
        if self._stream is None or self.serial is None:
            return

        self._seq += 1
        start = time.monotonic()

        update = mobile_controller_pb2.PhoneDataUpdate(
            serial=self.serial,
            accel=mobile_controller_pb2.Vector3(
                x=data.get("ax", 0.0),
                y=data.get("ay", 0.0),
                z=data.get("az", 0.0),
            ),
            gyro=mobile_controller_pb2.Vector3(
                x=data.get("gx", 0.0),
                y=data.get("gy", 0.0),
                z=data.get("gz", 0.0),
            ),
            buttons=data.get("buttons", 0),
            battery=data.get("battery", 100),
            seq=self._seq,
        )
        await self._stream.write(update)

        elapsed = time.monotonic() - start
        metrics.mobile_ws_latency_seconds.observe(elapsed)

    async def send_button(self, buttons: int) -> None:
        """Send button state update."""
        if self._stream is None or self.serial is None:
            return

        self._seq += 1
        update = mobile_controller_pb2.PhoneDataUpdate(
            serial=self.serial,
            accel=mobile_controller_pb2.Vector3(x=0, y=0, z=1),
            buttons=buttons,
            battery=100,
            seq=self._seq,
        )
        await self._stream.write(update)

    async def close_stream(self) -> None:
        """Close the gRPC stream."""
        if self._stream:
            with contextlib.suppress(Exception):
                await self._stream.done_writing()

    async def _relay_commands(self) -> None:
        """Read PhoneCommand messages from gRPC and relay to WebSocket."""
        if self._stream is None:
            return
        try:
            async for cmd in self._stream:
                msg = {}
                if cmd.HasField("led"):
                    msg = {
                        "type": "led",
                        "r": cmd.led.r,
                        "g": cmd.led.g,
                        "b": cmd.led.b,
                    }
                elif cmd.HasField("rumble"):
                    msg = {
                        "type": "rumble",
                        "intensity": cmd.rumble.intensity,
                    }
                if msg:
                    try:
                        await self.ws.send_json(msg)
                    except ConnectionResetError:
                        break
        except grpc.aio.AioRpcError as e:
            if e.code() != grpc.StatusCode.CANCELLED:
                logger.error(f"gRPC stream error for {self.serial}: {e}")
        except Exception as e:
            logger.error(f"Command relay error for {self.serial}: {e}")


async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
    """Handle a phone WebSocket connection."""
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    stub = request.app["grpc_stub"]
    session = PhoneSession(ws, stub)

    metrics.mobile_connections_active.inc()
    metrics.mobile_connections_total.inc()

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")

                if msg_type == "join":
                    # Phone joining — register and start streaming
                    name = data.get("name", "Phone")
                    if await session.register(name):
                        await session.start_stream()
                        await ws.send_json({"type": "joined", "serial": session.serial})
                    else:
                        await ws.send_json({"type": "error", "message": "Registration failed"})

                elif msg_type == "motion":
                    await session.send_sensor_data(data)

                elif msg_type == "button":
                    await session.send_button(data.get("buttons", 0))

            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        await session.close_stream()
        await session.unregister()
        metrics.mobile_connections_active.dec()
        logger.info(f"Phone {session.serial} disconnected")

    return ws
