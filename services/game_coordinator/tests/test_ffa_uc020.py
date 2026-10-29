"""UC-020 Play Joust Free-for-All: flows not covered elsewhere (#1360).

Drives the real ``FFAGame`` (and, for the force-end, the real ``GameSession``)
against the mock controller manager from conftest. Only the leaves are faked: a
recording gameplay stream, a recording audio stub and the runtime config (short
countdown and rainbow so a full ``run()`` takes about two seconds).

Units covered here:
- main (steps 1, 2, 5, 6, 7): a full ``run()`` from ``game_starting`` to ``game_ended``
- A1: tie through an agent elimination plus a motion death between two win checks
- A2: a disconnect eliminates through the normal death path and continues the game
- A4: a force-end stops the game with no winner and publishes ``game_force_ended``
- BR-001: the win check runs after every controller sample of a frame
- BR-002: ``death_grace_period_seconds`` has no effect in FFA
- step 4: the music alternates 1.0x / 1.3x windows and the death threshold follows the window (#1498)
- step 6: the winner rainbow lasts ``winner_rainbow_duration_ms`` (#1498)
- step 7: with ``analytics.enabled`` off, no ``player_analytics`` but still ``game_ended`` (#1498)
- Pre-1: ``run()`` with fewer than two players publishes ``game_error`` and never starts (#1498)
"""

import asyncio
import threading
import time
from unittest.mock import create_autospec, patch

import pytest

from lib.types import GameEvent, Sound
from proto import audio_pb2, controller_manager_pb2, game_coordinator_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.event_bus import EventBus
from services.game_coordinator.game_session import GAME_KIND_PRIMARY, GameSession
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import FAST_MUSIC_SPEED, SLOW_MUSIC_SPEED, GameState, Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.interventions import INTERVENTION_SPECS, InterventionContext
from services.game_coordinator.lifecycle_handlers import ELIMINATE_REASON, handle_eliminate_player
from services.game_coordinator.runtime_config import AnalyticsConfig, GamePerformanceConfig, get_config_manager
from services.game_coordinator.tests.conftest import (
    EventCollector,
    MockBidirectionalStream,
    MockControllerManagerService,
)

LETHAL = controller_manager_pb2.Vector3(x=6.0, y=6.0, z=6.0)  # ~10.4 g, far above any death threshold
STILL = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0)


# --------------------------------------------------------------------------- #
# Fakes (leaves only)
# --------------------------------------------------------------------------- #
class RecordingStream(MockBidirectionalStream):
    """The conftest gameplay stream, also recording every control message written."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.writes: list = []

    async def write(self, message):
        self.writes.append(message)
        await super().write(message)

    def effects(self, effect) -> list[str]:
        """Serials that received ``effect``."""
        return [
            m.game_effect.serial for m in self.writes if m.HasField("game_effect") and m.game_effect.effect == effect
        ]


class RecordingControllerManager(MockControllerManagerService):
    """Mock controller manager whose gameplay stream records writes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stream: RecordingStream | None = None

    def StreamGameplayData(self):  # noqa: N802 - gRPC stub name
        self.stream = RecordingStream(self, self.death_schedule, self.infinite, self.max_duration)
        return self.stream


class RecordingAudio:
    """Stands in for ``audio_pb2_grpc.AudioServiceStub`` (its RPCs are instance attributes, so no autospec)."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    async def PlaySound(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append(("PlaySound", request))
        return audio_pb2.PlaySoundResponse(success=True)

    async def PlayMusic(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append(("PlayMusic", request))
        return audio_pb2.PlayMusicResponse(track_id="track-1", success=True)

    async def StopSound(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append(("StopSound", request))
        return audio_pb2.StopSoundResponse(success=True)

    async def StopMusic(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append(("StopMusic", request))
        return audio_pb2.StopMusicResponse(success=True)

    async def SetVolume(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append(("SetVolume", request))
        return audio_pb2.SetVolumeResponse(success=True)

    async def ChangeTempo(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append(("ChangeTempo", request))
        return audio_pb2.ChangeTempoResponse(success=True)

    def sounds(self) -> list[str]:
        return [req.file_path for name, req in self.calls if name == "PlaySound"]


class RecordingPlayerStream:
    """Gameplay stream for tests that drive frames by hand."""

    def __init__(self):
        self.writes: list = []

    async def write(self, message):
        self.writes.append(message)

    def effects(self, effect) -> list[str]:
        return [
            m.game_effect.serial for m in self.writes if m.HasField("game_effect") and m.game_effect.effect == effect
        ]


@pytest.fixture
def fast_config():
    """Short countdown and rainbow; analytics on (the spec default)."""
    config = GamePerformanceConfig(countdown_phase_duration_ms=50, winner_rainbow_duration_ms=200)
    with patch.object(get_config_manager(), "get_config", return_value=config):
        yield config


class FakeClock:
    """Stands in for the ``time`` module inside games/base.py: ``time()`` is driven by the test."""

    def __init__(self, start: float):
        self.now = start

    def time(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def _initial_players(count: int) -> list:
    return [game_coordinator_pb2.Player(serial=f"mock_controller_{i}") for i in range(count)]


def _ffa_with_players(count: int, *, audio=None) -> tuple[FFAGame, EventCollector]:
    """An FFA game mid-play: ``count`` alive players, a recording stream, loop running."""
    events = EventCollector()
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=count),
        event_publisher=events.publish,
        audio_client=audio,
        game_id="test_uc020",
    )
    game.gameplay_stream = RecordingPlayerStream()
    game.running = True
    game.state = GameState.RUNNING
    game.start_time = time.time() - 5.0
    for i in range(count):
        serial = f"p{i}"
        game.players[serial] = Player(serial=serial, team=0, alive=True, color=(255, 0, 0))
    return game, events


def _frame(*samples: tuple[str, controller_manager_pb2.Vector3], disconnects: tuple[str, ...] = ()):
    return controller_manager_pb2.GameplayDataUpdate(
        controllers=[controller_manager_pb2.GameplayData(serial=s, accel=a) for s, a in samples],
        disconnects=[controller_manager_pb2.DisconnectEvent(serial=s) for s in disconnects],
        timestamp=int(time.time() * 1000),
    )


def _event_types(events: EventCollector) -> list[str]:
    return [event_type for event_type, _ in events.events]


# --------------------------------------------------------------------------- #
# UC-020
# --------------------------------------------------------------------------- #
class TestUC020FreeForAll:
    """UC-020 Play Joust Free-for-All."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="main", steps=[1, 2, 5, 6, 7], br=["BR-001"])
    @pytest.mark.uc("UC-031", flow="main", steps=[2, 4])  # countdown beeps, explosion, congratulations
    async def test_full_game_last_player_standing_wins(self, fast_config):
        """UC-020 main: a full run() ends with the last player standing as the winner."""
        cm = RecordingControllerManager(num_controllers=3, death_schedule={1.0: 0, 1.6: 1}, max_duration=8.0)
        events = EventCollector()
        audio = RecordingAudio()
        game = FFAGame(
            controller_manager_client=cm,
            event_publisher=events.publish,
            audio_client=audio,
            game_id="test_uc020_main",
            initial_players=_initial_players(3),
        )

        await asyncio.wait_for(game.run(), timeout=15.0)

        types = _event_types(events)
        # Step 1: everyone on team 0, players_initialized, unique colours sent with the stream config.
        assert all(p.team == 0 for p in game.players.values())
        initialized = events.get_events_of_type(GameEvent.PLAYERS_INITIALIZED)
        assert initialized == [
            {"player_count": 3, "serials": ["mock_controller_0", "mock_controller_1", "mock_controller_2"]}
        ]
        config_msgs = [m for m in cm.stream.writes if m.HasField("config")]
        colours = {(c.color.r, c.color.g, c.color.b) for c in config_msgs[0].config.colors}
        assert len(colours) == 3
        assert types.index(GameEvent.GAME_STARTING) < types.index(GameEvent.PLAYERS_INITIALIZED)
        assert types.index(GameEvent.PLAYERS_INITIALIZED) < types.index(GameEvent.GAME_STARTED)

        # Step 2: music starts at the countdown volume, the countdown plays, then the game volume.
        audio_sequence = [
            (name, round(req.volume, 3) if name == "SetVolume" else getattr(req, "file_path", ""))
            for name, req in audio.calls
            if name in ("SetVolume", "PlayMusic")
            or (name == "PlaySound" and req.file_path == Sound.SFX_BEEP_LOUD.value)
        ]
        countdown_volume = ("SetVolume", round(game.countdown_music_volume, 3))
        game_volume = ("SetVolume", round(game.game_volume, 3))
        beep = ("PlaySound", Sound.SFX_BEEP_LOUD.value)
        first_beep = audio_sequence.index(beep)
        assert audio_sequence[0] == countdown_volume
        assert audio_sequence.index(("PlayMusic", "")) < first_beep
        assert audio_sequence.index(game_volume) > first_beep
        assert types.index(GameEvent.GAME_STARTED) < types.index(GameEvent.COUNTDOWN_START)

        # Step 5: two eliminations, each a player_death with the serial and the players left.
        deaths = events.get_events_of_type(GameEvent.PLAYER_DEATH)
        assert [(d["serial"], d["alive_remaining"]) for d in deaths] == [
            ("mock_controller_0", "2"),
            ("mock_controller_1", "1"),
        ]
        assert audio.sounds().count(Sound.SFX_EXPLOSION.value) == 2

        # Step 6: game_winner, the rainbow on the winner, "congratulations".
        assert events.get_events_of_type(GameEvent.GAME_WINNER) == [{"serial": "mock_controller_2"}]
        assert cm.stream.effects(controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW) == ["mock_controller_2"]
        assert Sound.VOX_CONGRATULATIONS.value in audio.sounds()
        assert not events.get_events_of_type(GameEvent.GAME_TIE)

        # Step 7: player_analytics for each player, then game_ended with the duration.
        analytics = events.get_events_of_type(GameEvent.PLAYER_ANALYTICS)
        assert sorted(a["serial"] for a in analytics) == sorted(game.players)
        assert [a["serial"] for a in analytics if a["winner"]] == ["mock_controller_2"]
        ended = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert len(ended) == 1
        assert ended[0]["game_id"] == "test_uc020_main"
        assert ended[0]["duration"] > 0
        assert types[-1] == GameEvent.GAME_ENDED
        assert game.state == GameState.ENDED

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A1")
    async def test_tie_when_agent_elimination_and_motion_death_fall_between_win_checks(self):
        """UC-020 A1: an agent elimination plus a motion death before the next win check is a tie."""
        audio = RecordingAudio()
        game, events = _ffa_with_players(2, audio=audio)
        game.players["p1"].smoothed_accel = 10.0  # p1 is already shaking hard

        # The agent eliminates p0 between frames: no win check runs on that path.
        ctx = InterventionContext(
            spec=next(s for s in INTERVENTION_SPECS if s.flag_key == "eliminate_player"),
            value="n1:p0",
            payload="p0",
            target_serial="p0",
            game=game,
            objective="balanced",
        )
        await handle_eliminate_player(ctx)
        assert game.players["p0"].alive is False
        assert not events.get_events_of_type(GameEvent.GAME_WINNER)

        # Next frame: p1's lethal sample kills it, and the win check finds nobody alive.
        game_over = await game._process_gameplay_update(_frame(("p1", LETHAL)))

        assert game_over is True
        assert events.get_events_of_type(GameEvent.GAME_TIE) == [{}]
        assert not events.get_events_of_type(GameEvent.GAME_WINNER)

        # Continues at step 7 with no rainbow and no "congratulations".
        with patch.object(game, "_wait_for_rainbow_effect", autospec=True, return_value=True):
            await game._end_game_impl()
        assert game.gameplay_stream.effects(controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW) == []
        assert Sound.VOX_CONGRATULATIONS.value not in audio.sounds()
        assert len(events.get_events_of_type(GameEvent.GAME_ENDED)) == 1

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A2", br=["BR-001"])
    async def test_disconnect_eliminates_through_death_path_and_game_continues(self):
        """UC-020 A2: a disconnect is a normal elimination; the game goes on while two remain, ends at one."""
        audio = RecordingAudio()
        game, events = _ffa_with_players(3, audio=audio)

        # p0 disconnects: player_death, explosion, death effect; two left, so back to step 3.
        game_over = await game._process_gameplay_update(_frame(("p1", STILL), ("p2", STILL), disconnects=("p0",)))

        assert game_over is False
        assert game.players["p0"].alive is False
        assert events.get_events_of_type(GameEvent.PLAYER_DEATH) == [{"serial": "p0", "alive_remaining": "2"}]
        assert audio.sounds() == [Sound.SFX_EXPLOSION.value]
        assert game.gameplay_stream.effects(controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH) == ["p0"]
        assert not events.get_events_of_type(GameEvent.GAME_WINNER)

        # p1 disconnects: one left, the win check runs at once (step 6).
        game_over = await game._process_gameplay_update(_frame(disconnects=("p1",)))

        assert game_over is True
        assert events.get_events_of_type(GameEvent.GAME_WINNER) == [{"serial": "p2"}]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A3")
    async def test_agent_elimination_uses_the_games_own_kill_path(self):
        """UC-020 A3 (game side only): eliminate_player uses the game's own kill path, reason agent_intervention.

        The applied ``agent_intervention`` publication (``blocked=false``) on a real FFA game is asserted by
        test_intervention_flag_to_effect.py::test_eliminate_event_kills_once_and_next_event_kills_again.
        """
        game, events = _ffa_with_players(3)
        ctx = InterventionContext(
            spec=next(s for s in INTERVENTION_SPECS if s.flag_key == "eliminate_player"),
            value="n1:p1",
            payload="p1",
            target_serial="p1",
            game=game,
            objective="balanced",
        )

        with patch.object(game, "_kill_player", autospec=True, side_effect=game._kill_player) as kill:
            await handle_eliminate_player(ctx)

        kill.assert_awaited_once_with("p1", 0.0, reason=ELIMINATE_REASON)
        assert game.players["p1"].alive is False
        assert events.get_events_of_type(GameEvent.PLAYER_DEATH) == [{"serial": "p1", "alive_remaining": "2"}]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A4")
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    async def test_force_end_stops_ffa_without_a_winner(self, fast_config):
        """UC-020 A4 / UC-027 step 3: a force-end mid-game stops FFA with no winner.

        A4 step 3: ``player_analytics`` for each player and ``game_ended`` (``force_ended``, reason, duration),
        then the session's ``game_force_ended``.
        """
        cm = RecordingControllerManager(num_controllers=3, infinite=True)
        events = EventCollector()
        audio = RecordingAudio()
        game = FFAGame(
            controller_manager_client=cm,
            event_publisher=events.publish,
            audio_client=audio,
            game_id="game_ffa_force",
            initial_players=_initial_players(3),
        )
        session = GameSession(
            game_id="game_ffa_force",
            game_name="FFA",
            players=[],
            game_config=game_coordinator_pb2.StartGameConfig(sensitivity=2),
            event_bus=create_autospec(EventBus, instance=True),
            game_kind=GAME_KIND_PRIMARY,
        )
        # Record the session's bus publications in the same timeline as the game's, to check ordering.
        session.event_bus.publish.side_effect = events.publish
        session.current_game = game
        session.game_running = True
        session.game_state = game_coordinator_pb2.GameState.RUNNING
        # As in production (GameSession._run_game_loop_threaded), the game runs on its own thread and loop,
        # so force_end() joins it before publishing game_force_ended.
        game_thread = threading.Thread(target=lambda: asyncio.run(game.run()), daemon=True)
        session.game_thread = game_thread
        game_thread.start()

        try:
            deadline = time.time() + 10.0
            # start_time is a plain attribute the game loop sets on its first frame; nothing to await on.
            while game.start_time is None and time.time() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            assert game.start_time is not None, "game loop never started"

            ok, error = await session.force_end("operator_stop")
            game_thread.join(timeout=10.0)
            assert not game_thread.is_alive(), "game thread did not stop"
        finally:
            # Never leave the game thread spinning on the infinite mock stream if an assert fired early.
            if game_thread.is_alive():
                game.force_end("teardown")
                game_thread.join(timeout=10.0)

        assert (ok, error) == (True, "")
        session.event_bus.publish.assert_awaited_once_with(
            GameEvent.GAME_FORCE_ENDED, {"reason": "operator_stop", "game_id": "game_ffa_force"}
        )
        assert session.game_state == game_coordinator_pb2.GameState.ENDED
        # Three players are still alive: nobody is declared the winner.
        assert all(p.alive for p in game.players.values())
        assert not events.get_events_of_type(GameEvent.GAME_WINNER)
        assert not events.get_events_of_type(GameEvent.GAME_TIE)
        assert cm.stream.effects(controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW) == []
        assert Sound.VOX_CONGRATULATIONS.value not in audio.sounds()
        assert game.running is False
        # A4 step 3: per-player analytics (none a winner), game_ended force_ended, then the session's event.
        analytics = events.get_events_of_type(GameEvent.PLAYER_ANALYTICS)
        assert sorted(a["serial"] for a in analytics) == sorted(game.players)
        assert not any(a["winner"] for a in analytics)
        (ended,) = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert (ended["outcome"], ended["reason"]) == ("force_ended", "operator_stop")
        assert ended["duration"] > 0
        types = _event_types(events)
        assert types.index(GameEvent.GAME_ENDED) < types.index(GameEvent.GAME_FORCE_ENDED)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="main", steps=[5, 6], br=["BR-001"])
    async def test_win_check_after_every_sample_spares_the_last_player(self):
        """UC-020 BR-001: in a frame where all three move lethally, the third is never evaluated and wins."""
        game, events = _ffa_with_players(3)
        for player in game.players.values():
            player.smoothed_accel = 10.0

        game_over = await game._process_gameplay_update(_frame(("p0", LETHAL), ("p1", LETHAL), ("p2", LETHAL)))

        assert game_over is True
        assert [d["serial"] for d in events.get_events_of_type(GameEvent.PLAYER_DEATH)] == ["p0", "p1"]
        assert game.players["p2"].alive is True
        assert events.get_events_of_type(GameEvent.GAME_WINNER) == [{"serial": "p2"}]
        assert not events.get_events_of_type(GameEvent.GAME_TIE)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="main", steps=[5], br=["BR-002"])
    @pytest.mark.parametrize("grace_seconds", [0.0, 30.0])
    async def test_death_grace_period_has_no_effect_in_ffa(self, grace_seconds):
        """UC-020 BR-002: whatever death_grace_period_seconds is, an FFA death is final and grants no grace."""
        with patch("services.game_coordinator.games.base.read_float_flag", autospec=True) as read_float:
            read_float.side_effect = lambda _domain, key, default, **_kw: (
                grace_seconds if key == "death_grace_period_seconds" else default
            )
            game, _events = _ffa_with_players(3)
        assert game.death_grace_period == grace_seconds
        game.players["p0"].smoothed_accel = 10.0

        await game._process_gameplay_update(_frame(("p0", LETHAL)))
        # Later frames never bring p0 back, however long the grace flag is.
        await game._process_gameplay_update(_frame(("p0", STILL), ("p1", STILL), ("p2", STILL)))

        assert game.players["p0"].alive is False
        assert game.players["p0"].grace_until == 0.0
        assert all(game.players[s].grace_until == 0.0 for s in ("p1", "p2"))

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="main", steps=[4])
    @pytest.mark.uc("UC-031", flow="main", steps=[3])
    async def test_music_alternates_windows_and_death_threshold_follows_the_window(self):
        """UC-020 step 4: the FFA game's tempo intensity alternates 1.0x and 1.3x windows; the death threshold follows.

        Drives the game's own tempo owner (``_check_music_speed``, called every 100 ms by the music loop)
        on a fake clock for two simulated minutes. Since #1833 the intensity ramps between the windows
        (UC-031 BR-005): each change is one glide over the ramp, and the next window counts from the
        ramp end. The death threshold always equals the lerp of the two tables by the music's position.
        """
        audio = RecordingAudio()
        game, _events = _ffa_with_players(3, audio=audio)
        sens = game.sensitivity.value
        slow_death, fast_death = game.slow_max[sens], game.fast_max[sens]
        assert slow_death < fast_death  # the faster window is more forgiving
        windows = game.music_windows
        clock = FakeClock(time.time())
        between_seen = False

        with patch.object(game_clock, "now", clock.time):
            await game._start_game_music()
            assert game.music_speed == SLOW_MUSIC_SPEED
            # (when, new tempo, glide seconds)
            changes: list[tuple[float, float, float]] = [(clock.now, SLOW_MUSIC_SPEED, 0.0)]
            for _tick in range(1200):
                clock.now += 0.1
                await game._check_music_speed()
                tempo_calls = [req for name, req in audio.calls if name == "ChangeTempo"]
                if len(tempo_calls) > len(changes) - 1:
                    changes.append((clock.now, tempo_calls[-1].new_tempo, tempo_calls[-1].transition_duration))
                # The death threshold follows the music's position between 1.0x and 1.3x.
                position = (game.music_speed - SLOW_MUSIC_SPEED) / (FAST_MUSIC_SPEED - SLOW_MUSIC_SPEED)
                expected = slow_death + (fast_death - slow_death) * position
                between_seen = between_seen or 0.05 < position < 0.95
                assert game._compute_effective_thresholds(game.players["p0"])[1] == pytest.approx(expected)

        assert between_seen  # the thresholds ramp, they do not jump
        tempos = [tempo for _when, tempo, _glide in changes]
        assert len(tempos) >= 5
        # ChangeTempoRequest.new_tempo is a proto float, hence approx.
        assert tempos == pytest.approx(
            [SLOW_MUSIC_SPEED if i % 2 == 0 else FAST_MUSIC_SPEED for i in range(len(tempos))], abs=1e-6
        )
        # Each window, counted from the end of the previous ramp, lasts as long as the
        # (no-one-dead-yet) slow / fast windows allow, give or take one tick.
        for (start, tempo, glide), (end, _next, _next_glide) in zip(changes, changes[1:], strict=False):
            is_slow = tempo == pytest.approx(SLOW_MUSIC_SPEED)
            low, high = (windows.slow_min, windows.slow_max) if is_slow else (windows.fast_min, windows.fast_max)
            assert low - 0.11 <= end - start - glide <= high + 0.11

        # A move between the two death thresholds is survived in the 1.3x window and kills in the 1.0x window.
        between = (slow_death + fast_death) / 2
        sample = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=between)
        game.players["p0"].smoothed_accel = between
        game.music_speed = FAST_MUSIC_SPEED
        await game._process_gameplay_update(_frame(("p0", sample)))
        assert game.players["p0"].alive is True
        game.music_speed = SLOW_MUSIC_SPEED
        await game._process_gameplay_update(_frame(("p0", sample)))
        assert game.players["p0"].alive is False

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="main", steps=[6])
    async def test_winner_rainbow_lasts_winner_rainbow_duration_ms(self):
        """UC-020 step 6: the rainbow hold is ``winner_rainbow_duration_ms`` long (default 3 s)."""
        assert GamePerformanceConfig().winner_rainbow_duration_ms == 3000
        game, _events = _ffa_with_players(2)
        fake_asyncio = create_autospec(asyncio)
        config = GamePerformanceConfig(winner_rainbow_duration_ms=1500)

        with (
            patch.object(get_config_manager(), "get_config", return_value=config),
            patch.object(base_module, "asyncio", fake_asyncio),
        ):
            completed = await game._wait_for_rainbow_effect()

        assert completed is True
        waited = sum(call.args[0] for call in fake_asyncio.sleep.await_args_list)
        assert waited == pytest.approx(1.5)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="main", steps=[7])
    async def test_analytics_off_publishes_no_player_analytics_but_game_ended(self):
        """UC-020 step 7: with ``analytics.enabled`` off there is no ``player_analytics``; ``game_ended`` still is."""
        config = GamePerformanceConfig(
            countdown_phase_duration_ms=50,
            winner_rainbow_duration_ms=200,
            analytics=AnalyticsConfig(enabled=False),
        )
        cm = RecordingControllerManager(num_controllers=2, death_schedule={1.0: 0}, max_duration=8.0)
        events = EventCollector()
        game = FFAGame(
            controller_manager_client=cm,
            event_publisher=events.publish,
            audio_client=RecordingAudio(),
            game_id="test_uc020_no_analytics",
            initial_players=_initial_players(2),
        )

        with patch.object(get_config_manager(), "get_config", return_value=config):
            await asyncio.wait_for(game.run(), timeout=15.0)

        assert all(p.analytics is None for p in game.players.values())
        assert events.get_events_of_type(GameEvent.GAME_WINNER) == [{"serial": "mock_controller_1"}]
        assert events.get_events_of_type(GameEvent.PLAYER_ANALYTICS) == []
        ended = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert len(ended) == 1
        assert ended[0]["game_id"] == "test_uc020_no_analytics"
        assert ended[0]["duration"] > 0
        assert _event_types(events)[-1] == GameEvent.GAME_ENDED

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", cond=["Pre-1"])
    async def test_fewer_than_two_players_is_rejected(self, fast_config):
        """UC-020 Pre-1: ``run()`` with one player raises, publishes ``game_error`` and never starts the game."""
        cm = RecordingControllerManager(num_controllers=1, infinite=True)
        events = EventCollector()
        game = FFAGame(
            controller_manager_client=cm,
            event_publisher=events.publish,
            audio_client=RecordingAudio(),
            game_id="test_uc020_one_player",
            initial_players=_initial_players(1),
        )

        with pytest.raises(ValueError, match="Need at least 2 players, got 1"):
            await asyncio.wait_for(game.run(), timeout=15.0)

        assert events.get_events_of_type(GameEvent.GAME_ERROR) == [
            {"game_id": "test_uc020_one_player", "error": "Need at least 2 players, got 1"}
        ]
        types = _event_types(events)
        assert GameEvent.GAME_STARTED not in types
        assert GameEvent.GAME_ENDED not in types
        assert game.state == GameState.ENDED
        assert game.running is False
