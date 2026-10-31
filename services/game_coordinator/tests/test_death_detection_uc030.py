"""UC-030 Detect a Player's Death: units without a test before #1362.

Drives the real ``BaseGameMode`` detection path through ``FFAGame`` (which
inherits it unchanged). Player spans are autospec'd OTel spans so the span
events (names and attributes) can be asserted; the gameplay stream records the
effect commands sent to the controller manager.

Units covered here:
- main (steps 3-10): lethal samples → explosion, death effect, ``player_death``
  span event, win check
- steps 6-7: the warning flash (``GAME_EFFECT_PLAYER_WARNING``) and the
  ``death_warning`` span event with its attributes, at most once per window
- A1: during grace or invincibility the EMA is reset and no warning or death fires
- A4: below the warning threshold nothing happens
- BR-003: ``_warmup_ema`` primes the EMA without any death check, and
  ``_game_loop`` itself sets the 0.3 s start grace
"""

import time
from unittest.mock import create_autospec

import pytest
from opentelemetry.trace import INVALID_SPAN_CONTEXT, Span

from lib.types import GameEvent, Sound
from proto import audio_pb2, controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games.base import WARNING_DURATION, Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.tournament import TournamentPlayer
from services.game_coordinator.tests.conftest import EventCollector, MockControllerManagerService

LETHAL = controller_manager_pb2.Vector3(x=6.0, y=6.0, z=6.0)  # ~10.4 g
STILL = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0)  # 1 g: gravity only
STARTUP_GRACE_S = 0.3  # UC-030 BR-003


class RecordingStream:
    """Gameplay stream: records writes, and optionally yields scripted frames."""

    def __init__(self, frames=(), frame_interval_s: float = 0.0):
        self.writes: list = []
        self._frames = list(frames)
        self._frame_interval_s = frame_interval_s

    async def write(self, message):
        self.writes.append(message)

    def effects(self, effect) -> list[str]:
        return [
            m.game_effect.serial for m in self.writes if m.HasField("game_effect") and m.game_effect.effect == effect
        ]

    def __aiter__(self):
        return self

    async def __anext__(self):
        import asyncio

        if not self._frames:
            raise StopAsyncIteration
        if self._frame_interval_s:
            await asyncio.sleep(self._frame_interval_s)
        return self._frames.pop(0)


class RecordingAudio:
    """Stands in for ``audio_pb2_grpc.AudioServiceStub`` (its RPCs are instance attributes, so no autospec)."""

    def __init__(self):
        self.sounds: list[str] = []

    async def PlaySound(self, request):  # noqa: N802 - gRPC stub name
        self.sounds.append(request.file_path)
        return audio_pb2.PlaySoundResponse(success=True)


def _frame(*samples):
    return controller_manager_pb2.GameplayDataUpdate(
        controllers=[controller_manager_pb2.GameplayData(serial=s, accel=a) for s, a in samples],
        timestamp=int(time.time() * 1000),
    )


def _game(count: int = 3, *, audio=None) -> tuple[FFAGame, EventCollector]:
    """An FFA game mid-play with ``count`` alive players, each with an autospec'd span."""
    events = EventCollector()
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=count),
        event_publisher=events.publish,
        audio_client=audio,
        game_id="test_uc030",
    )
    game.gameplay_stream = RecordingStream()
    game.running = True
    game.start_time = game_clock.now()
    for i in range(count):
        serial = f"p{i}"
        game.players[serial] = Player(serial=serial, team=0, alive=True, color=(255, 0, 0))
        span = create_autospec(Span, instance=True)
        span.get_span_context.return_value = INVALID_SPAN_CONTEXT  # trace-context injection formats its ids
        game.players[serial].span = span
    return game, events


def _span_events(span, name: str) -> list[dict]:
    """Attributes of every ``add_event(name, attributes=...)`` call on an autospec'd span."""
    return [c.kwargs.get("attributes", {}) for c in span.add_event.call_args_list if c.args and c.args[0] == name]


class TestUC030DeathDetection:
    """UC-030 Detect a Player's Death."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", steps=[3, 4, 5, 8, 9, 10])
    async def test_lethal_motion_eliminates_through_the_death_path(self):
        """UC-030 main: sustained lethal motion → explosion, death effect, player_death event, win check."""
        audio = RecordingAudio()
        game, events = _game(2, audio=audio)
        span = game.players["p0"].span

        for _ in range(10):
            if await game._process_gameplay_update(_frame(("p0", LETHAL), ("p1", STILL))):
                break

        assert game.players["p0"].alive is False
        assert audio.sounds == [Sound.SFX_EXPLOSION.value]
        assert game.gameplay_stream.effects(controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH) == ["p0"]
        deaths = _span_events(span, "player_death")
        assert deaths
        assert deaths[0]["death.reason"] == "motion"
        assert deaths[0]["accel_magnitude"] > game._compute_effective_thresholds(game.players["p0"])[1]
        # Step 10: the win check ran and found the last player standing.
        assert events.get_events_of_type(GameEvent.GAME_WINNER) == [{"serial": "p1"}]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", steps=[6, 7])
    async def test_warning_flashes_and_records_death_warning_once_per_window(self):
        """UC-030 steps 6-7: above the warning threshold the player is warned once per 0.5 s window."""
        game, _ = _game(2)
        player = game.players["p0"]
        span = player.span
        warn, death = game._compute_effective_thresholds(player)
        player.smoothed_accel = (warn + death) / 2  # between warning and death
        sample = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=(warn + death) / 2)

        before = game_clock.now()
        await game._process_controller_state(controller_manager_pb2.GameplayData(serial="p0", accel=sample))
        await game._process_controller_state(controller_manager_pb2.GameplayData(serial="p0", accel=sample))

        assert player.alive is True
        assert game.gameplay_stream.effects(controller_manager_pb2.GAME_EFFECT_PLAYER_WARNING) == ["p0"]
        warnings = _span_events(span, "death_warning")
        assert len(warnings) == 1
        assert set(warnings[0]) == {"accel_magnitude", "threshold", "sensitivity", "sensitivity_factor", "music_speed"}
        assert warnings[0]["threshold"] == pytest.approx(warn)
        assert warn < warnings[0]["accel_magnitude"] < death
        assert player.warning_until >= before + WARNING_DURATION
        assert WARNING_DURATION == 0.5

    async def _assert_window_suppresses_detection(self, game, events, player):
        """Drive warning-zone / lethal / warning-zone frames; the EMA resets and nothing fires."""
        warn, death = game._compute_effective_thresholds(player)
        warning_zone = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=(warn + death) / 2)
        player.smoothed_accel = death + 1.0  # a spike left in the filter

        for sample in (warning_zone, LETHAL, warning_zone):
            await game._process_controller_state(controller_manager_pb2.GameplayData(serial="p0", accel=sample))
            assert player.smoothed_accel == 0.0  # reset every frame inside the window

        assert player.alive is True
        assert player.warning_until == 0.0
        assert game.gameplay_stream.effects(controller_manager_pb2.GAME_EFFECT_PLAYER_WARNING) == []
        assert game.gameplay_stream.effects(controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH) == []
        assert _span_events(player.span, "death_warning") == []
        assert _span_events(player.span, "player_death") == []
        assert events.get_events_of_type(GameEvent.PLAYER_DEATH) == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A1")
    async def test_grace_resets_ema_and_fires_no_warning_or_death(self):
        """UC-030 A1: inside ``grace_until`` the EMA is reset; no warning, death_warning or death fires."""
        game, events = _game(2)
        player = game.players["p0"]
        player.grace_until = game_clock.now() + 60.0
        await self._assert_window_suppresses_detection(game, events, player)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A1")
    async def test_agent_shield_on_plain_ffa_player_suppresses_detection(self):
        """UC-030 A1: ``grant_shield`` on a plain FFA player writes ``grace_until`` and detection is suppressed."""
        game, events = _game(2)
        player = game.players["p0"]
        assert await game.grant_shield("p0", 60.0) is True
        assert player.grace_until > game_clock.now()
        await self._assert_window_suppresses_detection(game, events, player)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-025", br=["BR-001"])
    async def test_match_invincibility_resets_ema_and_fires_no_warning_or_death(self):
        """UC-025 BR-001: per-match ``invincible_until`` (Tournament/Fight Club only) also blocks detection."""
        game, events = _game(2)
        player = TournamentPlayer(serial="p0", team=0, alive=True, color=(255, 0, 0))
        player.span = game.players["p0"].span
        game.players["p0"] = player
        player.invincible_until = game_clock.now() + 60.0
        await self._assert_window_suppresses_detection(game, events, player)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A4")
    async def test_below_warning_threshold_nothing_happens(self):
        """UC-030 A4: a still controller gets no warning, no death and no effect."""
        game, events = _game(2)
        player = game.players["p0"]

        for _ in range(10):
            await game._process_gameplay_update(_frame(("p0", STILL), ("p1", STILL)))

        assert player.alive is True
        assert player.warning_until == 0.0
        assert game.gameplay_stream.writes == []
        assert player.span.add_event.call_args_list == []
        assert events.events == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", br=["BR-003"])
    async def test_warmup_ema_primes_the_filter_without_killing(self):
        """UC-030 BR-003: ~100 ms of buffered frames prime the EMA; no death check runs, even on a spike."""
        frames = [_frame(("p0", LETHAL), ("p1", STILL)) for _ in range(30)]
        game, events = _game(2)
        game.gameplay_stream = RecordingStream(frames, frame_interval_s=1 / 60)

        started = time.time()
        await game._warmup_ema()
        elapsed = time.time() - started

        consumed = 30 - len(game.gameplay_stream._frames)
        _, death = game._compute_effective_thresholds(game.players["p0"])
        assert game.players["p0"].smoothed_accel > death  # primed with the real (spiky) data
        assert game.players["p1"].smoothed_accel == pytest.approx(1.0)
        assert all(p.alive for p in game.players.values())  # ... but nobody died
        assert events.get_events_of_type(GameEvent.PLAYER_DEATH) == []
        assert game.gameplay_stream.effects(controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH) == []
        assert elapsed >= 0.1  # drains for ~100 ms (no upper wall-clock bound: CI load)
        assert 1 <= consumed < 30

    @pytest.mark.real_game_time
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A1", br=["BR-003"])
    async def test_game_loop_sets_the_startup_grace(self):
        """UC-030 BR-003: _game_loop grants every alive player 0.3 s of grace; a spike in that window cannot kill."""
        game, events = _game(3)
        game.players["p2"].alive = False
        game.players["p0"].smoothed_accel = 10.0  # a spike left in the filter
        game.gameplay_stream = RecordingStream([_frame(("p0", LETHAL), ("p1", STILL))])

        before = game_clock.now()
        await game._game_loop()
        after = time.time()

        for serial in ("p0", "p1"):
            assert before + STARTUP_GRACE_S <= game.players[serial].grace_until <= after + STARTUP_GRACE_S
        assert game.players["p2"].grace_until == 0.0  # dead players get none
        assert game.players["p0"].alive is True
        assert game.players["p0"].smoothed_accel == 0.0  # A1: the EMA is reset during grace
        assert events.get_events_of_type(GameEvent.PLAYER_DEATH) == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A1", br=["BR-003"])
    async def test_game_loop_startup_grace_never_shortens_a_longer_grace(self):
        """UC-030 BR-003: a shield granted before the loop (longer than 0.3 s) is kept."""
        game, _events = _game(2)
        shield_until = game_clock.now() + 60
        game.players["p0"].grace_until = shield_until
        game.gameplay_stream = RecordingStream([_frame(("p0", STILL), ("p1", STILL))])

        await game._game_loop()

        assert game.players["p0"].grace_until == shield_until
