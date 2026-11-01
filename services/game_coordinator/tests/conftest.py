"""
Shared fixtures and mocks for game coordinator tests.

Provides MockControllerManagerService and EventCollector
for testing game modes without real hardware or gRPC services.
"""

import asyncio
import dataclasses
import sys
import time
from pathlib import Path

import pytest
from openfeature.evaluation_context import EvaluationContext

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent

# Add paths for imports
sys.path.insert(0, str(project_root))

# Disable OpenTelemetry for tests - must be done before importing service modules
from lib.otel_logging import disable_logging_for_tests
from lib.otel_metrics import disable_metrics_for_tests
from lib.telemetry import disable_telemetry_for_tests

disable_telemetry_for_tests()
disable_metrics_for_tests()
disable_logging_for_tests()

# Import protobufs from proto package (must be after path setup)
from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games.base import SLOW_MUSIC_SPEED, Player
from services.game_coordinator.games.flag_snapshot import resolve_music_windows


async def async_noop(*_args, **_kwargs):
    """Async no-op function for use as a dummy event_publisher."""
    pass


class MockGameplayStream:
    """Gameplay stream double for game tests: accepts writes and records them in ``messages``."""

    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


class FakeInterventionGame:
    """The one game double for intervention handler tests (#2361).

    Implements the surface of ``intervention_target.InterventionTarget`` with the
    same method names and parameter names as ``BaseGameMode``; the Protocol is
    deliberately not a base class (its ``...`` bodies would return None silently).
    ``tests/test_intervention_target.py`` fails when this fake drifts from the
    Protocol. Players are the real ``games.base.Player`` dataclass. Every effect
    is recorded in the lists below.
    """

    def __init__(
        self,
        name="Nonstop Joust",
        *,
        serials=("p1", "p2"),
        players=None,
        audio_client=None,
        gameplay_stream=None,
        flag_eval_context=None,
        game_kind="primary",
        experiment_id="",
        arm="",
        revive_result=True,
        kill_result=True,
    ):
        self._name = name
        self.players = dict(players) if players is not None else {s: Player(serial=s) for s in serials}
        self.audio_client = audio_client
        self.gameplay_stream = gameplay_stream
        self._flag_eval_context = flag_eval_context if flag_eval_context is not None else EvaluationContext()
        self.game_kind = game_kind
        self.experiment_id = experiment_id
        self.arm = arm
        self.revive_result = revive_result
        self.kill_result = kill_result
        self.music_speed = SLOW_MUSIC_SPEED
        self.music_windows = resolve_music_windows({})
        self.init_music_windows = self.music_windows
        self.tempo_override = None
        self.tempo_ramp = None
        self.global_difficulty_factor = 1.0
        self.killed: list[str] = []
        self.revived: list[str] = []
        self.shields: list[tuple[str, float]] = []
        self.group_shields: list[tuple[list[str], float, frozenset[str]]] = []
        self.toughness_boosts: list[tuple[str, float, float | None]] = []
        self.warned: list[str] = []
        self.tightened: list[str] = []
        self.rubberbands: list[tuple[str, float | None]] = []
        self.sounds: list[tuple[object, int]] = []
        self.sensitivity_overrides: list[object] = []

    @property
    def flag_eval_context(self):
        return self._flag_eval_context

    def get_game_name(self):
        return self._name

    def apply_sensitivity_override(self, value):
        self.sensitivity_overrides.append(value)

    def clear_sensitivity_override(self):
        self.sensitivity_overrides.append(None)

    async def _play_sound(self, sound, priority=2, parent_context=None, volume=1.0):  # noqa: ARG002
        self.sounds.append((sound, priority))

    async def _kill_player(self, serial, accel_mag, reason="motion"):  # noqa: ARG002
        self.killed.append(serial)
        self.players[serial].alive = False
        return self.kill_result

    async def revive_player(self, serial, spawn_grace=None):  # noqa: ARG002
        self.revived.append(serial)
        if self.revive_result:
            self.players[serial].alive = True
        return self.revive_result

    async def grant_shield(self, serial, seconds):
        self.shields.append((serial, seconds))
        return True

    async def grant_group_shield(self, serials, seconds, *, low_battery=frozenset()):
        self.group_shields.append((list(serials), seconds, frozenset(low_battery)))
        return True

    async def grant_toughness_boost(self, serial, seconds, boost=None):
        self.toughness_boosts.append((serial, seconds, boost))
        return True

    async def warn_player(self, serial):
        self.warned.append(serial)
        return True

    async def apply_soft_penalty(self, serial, seconds, factor=None):  # noqa: ARG002
        self.tightened.append(serial)
        return True

    async def apply_auto_rubberband(self, strength, seconds=None):
        self.rubberbands.append((strength, seconds))
        return 0

    def grants(self, flag_key) -> list[str]:
        """Serials granted by ``flag_key``, in order."""
        if flag_key == "shield_seconds":
            return [s for s, _ in self.shields]
        if flag_key == "toughness_boost_seconds":
            return [s for s, _, _ in self.toughness_boosts]
        return self.warned + self.tightened


class MockBidirectionalStream:
    """Mock bidirectional gRPC stream for StreamGameplayData."""

    def __init__(self, controller_manager, death_schedule=None, infinite=False, max_duration=5.0):
        """
        Initialize mock bidirectional stream.

        Args:
            controller_manager: Parent MockControllerManagerService
            death_schedule: Dict mapping time offset to controller index for deaths
            infinite: If True, stream indefinitely (for force_end tests)
            max_duration: Maximum stream duration in seconds (default 5.0)
        """
        self.controller_manager = controller_manager
        self.death_schedule = death_schedule or {}
        self.start_time = None
        self.running = True
        self.filter_serials = None
        self.infinite = infinite
        self.max_duration = max_duration

    async def write(self, message):
        """Handle client messages (config, filter updates, effects)."""
        if message.HasField("config"):
            self.start_time = game_clock.now()
        elif message.HasField("filter_update"):
            self.filter_serials = set(message.filter_update.serials) if message.filter_update.serials else None
        elif message.HasField("game_effect"):
            # Game effects (countdown, death, etc.) - just log in mock
            pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        """Yield next gameplay data update."""
        if not self.running:
            raise StopAsyncIteration

        if self.start_time is None:
            await asyncio.sleep(0.01)
            return controller_manager_pb2.GameplayDataUpdate(controllers=[], timestamp=int(time.time() * 1000))

        elapsed = game_clock.now() - self.start_time

        if not self.infinite and elapsed > self.max_duration:
            self.running = False
            raise StopAsyncIteration

        gameplay_data_list = []
        for i, controller in enumerate(self.controller_manager.controllers):
            died = False
            for death_time, death_index in self.death_schedule.items():
                if elapsed > death_time and i == death_index:
                    controller.accel.x = 5.0 + i
                    controller.accel.y = 3.0 + i
                    controller.accel.z = 4.0
                    died = True
                    break

            if not died:
                controller.accel.x = 0.1
                controller.accel.y = 0.0
                controller.accel.z = 1.0

            if self.filter_serials is not None and controller.serial not in self.filter_serials:
                continue

            gd = controller_manager_pb2.GameplayData(
                serial=controller.serial,
                move_num=i,
                battery=controller.battery,
                team=controller.team,
                color=controller.color,
                accel=controller.accel,
                gyro=controller.gyro,
            )
            gameplay_data_list.append(gd)

        await game_clock.sleep(1.0 / 60.0)
        return controller_manager_pb2.GameplayDataUpdate(
            controllers=gameplay_data_list, timestamp=int(time.time() * 1000)
        )


class MockControllerManagerService:
    """Mock ControllerManager gRPC service for testing."""

    def __init__(
        self,
        num_controllers: int = 4,
        death_schedule: dict | None = None,
        infinite: bool = False,
        max_duration: float = 5.0,
    ):
        """
        Initialize mock controller manager.

        Args:
            num_controllers: Number of mock controllers to simulate
            death_schedule: Dict mapping time offset to controller index for deaths
            infinite: If True, stream indefinitely (for force_end tests)
            max_duration: Maximum stream duration in seconds
        """
        self.num_controllers = num_controllers
        self.controllers = []
        self.death_schedule = death_schedule or {}
        self.infinite = infinite
        self.max_duration = max_duration
        self._initialize_controllers()

    def _initialize_controllers(self):
        """Create mock controller objects."""
        for i in range(self.num_controllers):
            controller = controller_manager_pb2.ControllerState(
                serial=f"mock_controller_{i}",
                move_num=i,
                battery=80,
                trigger_pressed=False,
                move_pressed=False,
                team=0,
                color=controller_manager_pb2.RGB(r=255, g=255, b=255),
                accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0),
                gyro=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=0.0),
            )
            self.controllers.append(controller)

    def StreamGameplayData(self):
        """Return a mock bidirectional stream for gameplay data."""
        return MockBidirectionalStream(self, self.death_schedule, self.infinite, self.max_duration)

    async def SetControllerColor(self, request):
        """Mock SetControllerColor RPC."""
        return controller_manager_pb2.SetControllerColorResponse(success=True)


class EventCollector:
    """Collects events published by the game."""

    def __init__(self):
        self.events: list[tuple] = []

    async def publish(self, event_type: str, data: dict):
        """Collect published event."""
        self.events.append((event_type, data))

    def get_events_of_type(self, event_type: str) -> list[dict]:
        """Get all events of a specific type."""
        return [data for et, data in self.events if et == event_type]

    def count_events_of_type(self, event_type: str) -> int:
        """Count events of a specific type."""
        return len(self.get_events_of_type(event_type))

    def clear(self):
        """Clear all collected events."""
        self.events.clear()


# Fixtures


@pytest.fixture
def mock_controller_manager():
    """Fixture providing mock ControllerManager service with default death schedule."""
    return MockControllerManagerService(
        num_controllers=3,
        death_schedule={2.0: 1, 3.0: 2},  # Controllers 1 and 2 die
    )


@pytest.fixture
def event_collector():
    """Fixture providing event collector."""
    return EventCollector()


@pytest.fixture(autouse=True)
def _flag_providers_ready_instantly(monkeypatch):
    """Skip the game-admission wait for flag providers (#1329, UC-010 A5).

    Unit tests have no flagd, so every provider stays NOT_READY and each session
    start would wait the full bound (5 s). The wait itself and its wiring are
    tested in test_flag_readiness.py, which patches this attribute itself.
    """

    async def _ready(_span, *_args, **_kwargs):
        return []

    monkeypatch.setattr("services.game_coordinator.game_session.await_flag_providers_ready", _ready)


GAME_TIME_SCALE = 10.0


class DilatedGameClock:
    """Game time that runs ``scale`` times faster than the wall clock (#2361).

    ``now()`` advances ``scale`` seconds per real second and ``sleep(s)`` waits ``s / scale``
    real seconds, so every game-time wait and deadline stays consistent with the others
    while the suite does not sit through them. Production keeps the monotonic clock at 1x.
    """

    def __init__(self, scale: float = GAME_TIME_SCALE):
        self.scale = scale
        self._origin_real = time.monotonic()
        self._origin_game = game_clock.now()

    def now(self) -> float:
        return self._origin_game + (time.monotonic() - self._origin_real) * self.scale

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self.scale)


@pytest.fixture(autouse=True)
def _fast_game_time(request, monkeypatch):
    """Run game time 10x faster than real time (#2361).

    A test that compares game time with the wall clock opts out with
    ``@pytest.mark.real_game_time``; a test that patches ``game_clock.now`` itself patches after this fixture and wins.
    """
    if request.node.get_closest_marker("real_game_time") or request.node.get_closest_marker("real_durations"):
        return
    clock = DilatedGameClock()
    monkeypatch.setattr(game_clock, "now", clock.now)
    monkeypatch.setattr(game_clock, "sleep", clock.sleep)


@pytest.fixture(autouse=True)
def _short_real_time_durations(request, monkeypatch):
    """Shorten the real-time intros, pauses, holds, beeps, instruction waits, reconnect backoff
    and the default countdown / rainbow (#2361).

    Production keeps its durations: this only replaces the module constants and the
    class attribute the modes read at call time. A test that asserts the real values
    or the real timing opts out with ``@pytest.mark.real_durations``; a test that
    sets its own value patches it after this fixture and wins.
    """
    if request.node.get_closest_marker("real_durations"):
        return
    from services.game_coordinator import runtime_config
    from services.game_coordinator.games import fight_club, tournament, traitor, werewolf, zombie
    from services.game_coordinator.games.base import BaseGameMode

    for module, name, value in (
        (tournament, "TOURNAMENT_INTRO_DURATION_S", 0.1),
        (tournament, "ROUND_PAUSE_S", 0.0),
        (werewolf, "WEREWOLF_INTRO_DURATION_S", 0.1),
        (zombie, "ZOMBIE_INTRO_DURATION_S", 0.1),
        (fight_club, "FC_INTRO_DURATION_S", 0.1),
        (fight_club, "ROUND_PAUSE_SECONDS", 0.0),
        (traitor, "TRAITOR_SIGNAL_DURATION", 1),
    ):
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(BaseGameMode, "_WIN_HOLD_S", 0.0)
    monkeypatch.setattr(BaseGameMode, "_COUNTDOWN_BEEP_POLL_S", 0.0)
    monkeypatch.setattr(BaseGameMode, "_INSTRUCTIONS_WAIT_SCALE", 0.1)
    monkeypatch.setattr(BaseGameMode, "_RECONNECT_BACKOFF_S", 0.01)

    # The singleton's code defaults (750 ms countdown phases, winner rainbow): only
    # when the config still holds them, so a test that sets its own keeps it.
    manager = runtime_config.get_config_manager()
    defaults = runtime_config.GamePerformanceConfig()
    original = manager.config
    if (original.countdown_phase_duration_ms, original.winner_rainbow_duration_ms) == (
        defaults.countdown_phase_duration_ms,
        defaults.winner_rainbow_duration_ms,
    ):
        monkeypatch.setattr(
            manager,
            "config",
            dataclasses.replace(original, countdown_phase_duration_ms=50, winner_rainbow_duration_ms=100),
        )
