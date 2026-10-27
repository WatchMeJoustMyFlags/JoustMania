"""
Operator pad service for JoustMania (UC-015, #2050).

Reads a USB macro pad through evdev (exclusive grab, hotplug) and turns key gestures into menu
commands and force-end. It serves no gRPC port: it is a client of the menu, the game coordinator
and the audio service, and writes no flag itself. Optional: a stack without it is unchanged.
"""

import asyncio
import logging
import os
import signal

# Bootstrap level only: the log_level flag takes over once synced (lib/log_level.py)
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

from lib.feature_flags import get_flag_client, init_flag_domain, wait_for_provider_ready
from lib.grpc_utils import create_channel
from lib.otel_logging import init_logging
from lib.otel_metrics import init_metrics
from lib.system_metrics import start_system_metrics_collector
from proto import audio_pb2_grpc, game_coordinator_pb2_grpc, menu_pb2_grpc
from services.operator_pad import metrics
from services.operator_pad.actions import Actions
from services.operator_pad.config import FLAG_DOMAIN, PadConfig
from services.operator_pad.controller import PadController
from services.operator_pad.cues import CuePlayer
from services.operator_pad.device import DeviceManager
from services.operator_pad.preview import Previewer
from services.operator_pad.state import MenuStatusReader

logger = logging.getLogger(__name__)


async def serve() -> None:
    init_metrics()
    init_logging()
    metrics_task = start_system_metrics_collector(
        cpu_counter=metrics.process_cpu_seconds_total,
        memory_gauge=metrics.process_resident_memory_bytes,
        threads_gauge=metrics.process_threads,
    )

    init_flag_domain(FLAG_DOMAIN)
    if not await wait_for_provider_ready(FLAG_DOMAIN):
        logger.warning("operator flag set not ready after 5s; using code defaults")
    config = PadConfig(get_flag_client(FLAG_DOMAIN))

    menu_channel = create_channel(f"{os.getenv('MENU_HOST', 'menu')}:{os.getenv('MENU_PORT', '50054')}")
    coordinator_channel = create_channel(
        f"{os.getenv('GAME_COORDINATOR_HOST', 'game-coordinator')}:{os.getenv('GAME_COORDINATOR_PORT', '50053')}"
    )
    audio_channel = create_channel(f"{os.getenv('AUDIO_HOST', 'audio')}:{os.getenv('AUDIO_PORT', '50056')}")
    menu_stub = menu_pb2_grpc.MenuServiceStub(menu_channel)

    cue_player = CuePlayer(audio_pb2_grpc.AudioServiceStub(audio_channel))
    previewer = Previewer(cue_player, config, menu_stub=menu_stub)
    devices: DeviceManager | None = None

    def set_lock_led(on: bool) -> None:
        if devices is not None:
            devices.set_lock_led(on)

    actions = Actions(
        menu_stub,
        game_coordinator_pb2_grpc.GameCoordinatorServiceStub(coordinator_channel),
        cue_player,
        set_lock_led=set_lock_led,
        previewer=previewer,
    )
    controller = PadController(config, MenuStatusReader(menu_stub), actions, cue_player)
    devices = DeviceManager(config, controller)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    logger.info("Operator pad service started")
    try:
        await devices.run(stop)
    finally:
        logger.info("Shutting down operator pad service")
        metrics_task.cancel()
        await actions.drain()
        previewer.cancel()
        cue_player.cancel_all()
        await menu_channel.close()
        await coordinator_channel.close()
        await audio_channel.close()


if __name__ == "__main__":
    asyncio.run(serve())
