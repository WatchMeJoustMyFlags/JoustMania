"""
Shared utilities for performance tests.

Provides mock controller data generators, timing utilities,
and game instance factories for load/stress testing the motion
processing pipeline.
"""

import random
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

# Setup paths for imports
test_dir = Path(__file__).parent
tests_dir = test_dir.parent
service_dir = tests_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(tests_dir))

from proto import controller_manager_pb2
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame


@dataclass
class TimingResult:
    """Stores timing measurements from a performance test run."""

    frame_times_ms: list[float] = field(default_factory=list)
    total_time_s: float = 0.0
    frame_count: int = 0

    @property
    def mean_ms(self) -> float:
        if not self.frame_times_ms:
            return 0.0
        return sum(self.frame_times_ms) / len(self.frame_times_ms)

    @property
    def p95_ms(self) -> float:
        if not self.frame_times_ms:
            return 0.0
        sorted_times = sorted(self.frame_times_ms)
        idx = int(len(sorted_times) * 0.95)
        return sorted_times[min(idx, len(sorted_times) - 1)]

    @property
    def p99_ms(self) -> float:
        if not self.frame_times_ms:
            return 0.0
        sorted_times = sorted(self.frame_times_ms)
        idx = int(len(sorted_times) * 0.99)
        return sorted_times[min(idx, len(sorted_times) - 1)]

    @property
    def max_ms(self) -> float:
        if not self.frame_times_ms:
            return 0.0
        return max(self.frame_times_ms)

    @property
    def min_ms(self) -> float:
        if not self.frame_times_ms:
            return 0.0
        return min(self.frame_times_ms)


@contextmanager
def measure_time():
    """Context manager that measures elapsed wall-clock time.

    Usage:
        with measure_time() as timer:
            do_work()
        print(f"Elapsed: {timer.elapsed_ms:.2f}ms")
    """

    class Timer:
        def __init__(self):
            self.start = 0.0
            self.end = 0.0

        @property
        def elapsed_s(self) -> float:
            return self.end - self.start

        @property
        def elapsed_ms(self) -> float:
            return (self.end - self.start) * 1000.0

    timer = Timer()
    timer.start = time.monotonic()
    try:
        yield timer
    finally:
        timer.end = time.monotonic()


def generate_mock_accel(
    num_controllers: int,
    movement: str = "idle",
) -> list[controller_manager_pb2.GameplayData]:
    """Generate mock accelerometer data for N controllers.

    Args:
        num_controllers: Number of controllers to generate data for.
        movement: Type of movement to simulate:
            - "idle": Resting state (~1.0g on Z axis)
            - "gentle": Small movements (~1.2-1.4g)
            - "active": Moderate movements (~1.5-2.0g)
            - "violent": Large movements (~3.0-5.0g, should trigger death)
            - "random": Random mix of all movement types

    Returns:
        List of GameplayData protobuf messages.
    """
    controllers = []
    for i in range(num_controllers):
        if movement == "idle":
            ax, ay, az = 0.05, 0.02, 1.0
        elif movement == "gentle":
            ax = random.uniform(-0.3, 0.3)
            ay = random.uniform(-0.3, 0.3)
            az = random.uniform(0.8, 1.2)
        elif movement == "active":
            ax = random.uniform(-0.8, 0.8)
            ay = random.uniform(-0.8, 0.8)
            az = random.uniform(0.5, 1.5)
        elif movement == "violent":
            ax = random.uniform(2.0, 4.0)
            ay = random.uniform(1.5, 3.0)
            az = random.uniform(1.0, 3.0)
        elif movement == "random":
            choice = random.choice(["idle", "gentle", "active"])
            ax = random.uniform(-1.0, 1.0) if choice != "idle" else 0.05
            ay = random.uniform(-1.0, 1.0) if choice != "idle" else 0.02
            az = random.uniform(0.5, 1.5) if choice != "idle" else 1.0
        else:
            ax, ay, az = 0.0, 0.0, 1.0

        gd = controller_manager_pb2.GameplayData(
            serial=f"perf_controller_{i}",
            move_num=i,
            battery=80,
            team=0,
            color=controller_manager_pb2.RGB(r=255, g=255, b=255),
            accel=controller_manager_pb2.Vector3(x=ax, y=ay, z=az),
            gyro=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=0.0),
        )
        controllers.append(gd)
    return controllers


async def async_noop(*_args, **_kwargs):
    """Async no-op function for use as a dummy event_publisher."""
    pass


class MockGameplayStream:
    """Minimal mock for gameplay stream writes during performance tests."""

    async def write(self, message):
        """Accept writes silently."""
        pass


def create_ffa_game(num_controllers: int) -> tuple[FFAGame, list[str]]:
    """Create an FFA game instance with mock players pre-initialized.

    Args:
        num_controllers: Number of players to create.

    Returns:
        Tuple of (game_instance, list_of_serial_numbers).
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("test_conftest", tests_dir / "conftest.py")
    test_conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(test_conftest)
    MockControllerManagerService = test_conftest.MockControllerManagerService

    mock_cm = MockControllerManagerService(num_controllers=num_controllers)

    game = FFAGame(
        controller_manager_client=mock_cm,
        event_publisher=async_noop,
        audio_client=None,
        game_id=f"perf_test_{int(time.time())}",
    )

    # Pre-initialize players directly (bypass async _initialize_players)
    serials = []
    for i in range(num_controllers):
        serial = f"perf_controller_{i}"
        game.players[serial] = Player(
            serial=serial,
            team=0,
            alive=True,
            color=(255, 255, 255),
        )
        serials.append(serial)

    # Set game to running state for processing
    game.running = True
    game.start_time = time.time()
    game.gameplay_stream = MockGameplayStream()

    return game, serials
