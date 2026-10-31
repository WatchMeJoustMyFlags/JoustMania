"""UC-021 Play Team Joust (Teams, Random Teams): main scenario, A2 and the mode-level rules (#1361).

Drives the real ``SimpleTeamsGame`` / ``RandomTeamsGame`` against the conftest mock controller
manager. Only the leaves are faked: a recording gameplay stream, a recording audio stub (reused
from the UC-020 tests) and the runtime config (short countdown and rainbow, so a full ``run()``
takes about three seconds). All random assertions use explicit non-zero seeds.

Units covered here:
- main step 1: serial-sorted round-robin assignment, optional seeded shuffle
- main step 2: the team countdown (team colour, white, green; ``countdown_phase_duration_ms`` 0 skips it)
- main steps 3 to 5: a full ``run()`` to ``team_winner``, rainbow, voice line and ``game_ended``
- A2: every team eliminated before the next win check publishes ``game_tie`` and ``game_ended``
- BR-001: Teams uses the first N team colours, Random Teams N distinct ones
- BR-002: a natural team-mode end latches ``completed`` through the base ``run()``
- Post-F-1: a ``run()`` that cannot start publishes ``game_error`` and raises
"""

import asyncio
import random
import time
from unittest.mock import create_autospec, patch

import pytest
from opentelemetry import trace

from lib.types import GameEvent, Sound
from proto import controller_manager_pb2, game_coordinator_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games.base import GameState
from services.game_coordinator.games.random_teams import RandomTeamsGame
from services.game_coordinator.games.teams import SimpleTeamsGame
from services.game_coordinator.games.teams_base import TEAM_COLORS, TEAM_WIN_SOUNDS
from services.game_coordinator.interventions import INTERVENTION_SPECS, InterventionContext
from services.game_coordinator.lifecycle_handlers import handle_eliminate_player
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager
from services.game_coordinator.tests.conftest import EventCollector, MockControllerManagerService
from services.game_coordinator.tests.test_ffa_uc020 import (
    RecordingAudio,
    RecordingControllerManager,
    RecordingPlayerStream,
    RecordingStream,
)


@pytest.fixture
def fast_config():
    """Short countdown and rainbow."""
    config = GamePerformanceConfig(countdown_phase_duration_ms=50, winner_rainbow_duration_ms=200)
    with patch.object(get_config_manager(), "get_config", return_value=config):
        yield config


class ScriptedTieStream(RecordingStream):
    """After 2 s: eliminate controller 0 like the agent does, then send controller 1's lethal sample alone."""

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner
        self.fired = False

    async def __anext__(self):
        frame = await super().__anext__()
        game = self.owner.game
        if (
            self.fired
            or self.start_time is None
            or game_clock.now() - self.start_time < 2.0
            or game.state != GameState.RUNNING
        ):
            return frame
        self.fired = True
        ctx = InterventionContext(
            spec=next(s for s in INTERVENTION_SPECS if s.flag_key == "eliminate_player"),
            value="n1:mock_controller_0",
            payload="mock_controller_0",
            target_serial="mock_controller_0",
            game=game,
            objective="balanced",
        )
        await handle_eliminate_player(ctx)
        game.players["mock_controller_1"].smoothed_accel = 10.0  # already shaking hard (as in UC-020 A1)
        lethal = controller_manager_pb2.Vector3(x=6.0, y=6.0, z=6.0)
        return controller_manager_pb2.GameplayDataUpdate(
            controllers=[controller_manager_pb2.GameplayData(serial="mock_controller_1", accel=lethal)],
            timestamp=int(time.time() * 1000),
        )


class ScriptedTieControllerManager(RecordingControllerManager):
    game = None

    def StreamGameplayData(self):  # noqa: N802 - gRPC stub name
        self.stream = ScriptedTieStream(self, self.death_schedule, self.infinite, self.max_duration, owner=self)
        return self.stream


def _controllers(*serials: str) -> list:
    return [controller_manager_pb2.ControllerState(serial=s) for s in serials]


def _initial_players(count: int) -> list:
    return [game_coordinator_pb2.Player(serial=f"mock_controller_{i}") for i in range(count)]


def _teams_game(*, num_teams=2, random_assignment=False, rng_seed=0, players=4, cm=None, audio=None, game_id="uc021"):
    events = EventCollector()
    game = SimpleTeamsGame(
        controller_manager_client=cm or MockControllerManagerService(num_controllers=players, infinite=True),
        event_publisher=events.publish,
        audio_client=audio,
        game_id=game_id,
        num_teams=num_teams,
        initial_players=_initial_players(players),
        random_assignment=random_assignment,
        rng_seed=rng_seed,
    )
    return game, events


def _rgb(message) -> tuple:
    return (message.color.r, message.color.g, message.color.b)


# --------------------------------------------------------------------------- #
# Main step 1
# --------------------------------------------------------------------------- #
class TestMainStep1Assignment:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", flow="main", steps=[1])
    async def test_unsorted_controllers_are_sorted_then_assigned_round_robin(self):
        """UC-021 step 1: controllers arrive unsorted, teams follow the serial order 0, 1, 0, 1."""
        game, events = _teams_game(random_assignment=False)

        await game._initialize_players_impl(_controllers("c3", "c1", "c0", "c2"))

        assert {s: p.team for s, p in game.players.items()} == {"c0": 0, "c1": 1, "c2": 0, "c3": 1}
        assert list(game.players) == ["c0", "c1", "c2", "c3"]
        assert events.get_events_of_type("players_initialized")[0]["serials"] == ["c0", "c1", "c2", "c3"]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", flow="main", steps=[1])
    async def test_random_assignment_shuffles_the_sorted_order_with_the_seed(self):
        """UC-021 step 1: with random_assignment a fixed seed gives the order random.Random(seed) gives."""
        serials = [f"c{i}" for i in range(6)]
        seed = 7
        expected_order = sorted(serials)
        random.Random(seed).shuffle(expected_order)
        assert expected_order != sorted(serials)  # the seed really shuffles

        game, _ = _teams_game(random_assignment=True, rng_seed=seed, players=6)
        await game._initialize_players_impl(_controllers(*reversed(serials)))

        assert list(game.players) == expected_order  # insertion order is the shuffled order
        assert {s: game.players[s].team for s in serials} == {s: i % 2 for i, s in enumerate(expected_order)}


# --------------------------------------------------------------------------- #
# Main step 2
# --------------------------------------------------------------------------- #
class TestMainStep2Countdown:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", flow="main", steps=[2])
    async def test_countdown_shows_team_colour_then_white_then_green(self):
        """UC-021 step 2: beep + team colours, beep + white, beep + green, start sound, countdown_end."""
        game, events = _teams_game()
        await game._initialize_players_impl(_controllers("c0", "c1", "c2", "c3"))
        events.clear()
        sequence: list = []

        async def record_sound(sound, **_kwargs):
            sequence.append(("sound", sound))

        stream = RecordingPlayerStream()
        original_write = stream.write

        async def record_write(message):
            await original_write(message)
            if message.HasField("base_color"):
                sequence.append(("color", message.base_color.serial, _rgb(message.base_color)))

        stream.write = record_write
        game.gameplay_stream = stream
        game.running = True

        config = GamePerformanceConfig(countdown_phase_duration_ms=50)
        with (
            patch.object(get_config_manager(), "get_config", return_value=config),
            patch.object(game, "_play_sound", autospec=True, side_effect=record_sound),
        ):
            await game._countdown()

        team_rgb = {s: tuple(game.team_colors[p.team]["rgb"]) for s, p in game.players.items()}
        beep = ("sound", Sound.SFX_BEEP_LOUD)
        expected = [beep] + [("color", s, team_rgb[s]) for s in game.players]
        expected += [beep] + [("color", s, (255, 255, 255)) for s in game.players]
        expected += [beep] + [("color", s, (0, 255, 0)) for s in game.players]
        expected += [("sound", Sound.SFX_START3)]
        assert sequence == expected
        assert team_rgb["c0"] != team_rgb["c1"]
        types = [t for t, _ in events.events]
        assert types == [GameEvent.COUNTDOWN_START, GameEvent.COUNTDOWN_END]
        assert events.get_events_of_type(GameEvent.COUNTDOWN_START) == [{"phase_duration_ms": 50}]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", flow="main", steps=[2])
    async def test_zero_phase_duration_skips_the_countdown(self):
        """UC-021 step 2: countdown_phase_duration_ms 0 goes straight from countdown_start to countdown_end."""
        game, events = _teams_game()
        await game._initialize_players_impl(_controllers("c0", "c1"))
        events.clear()
        game.gameplay_stream = RecordingPlayerStream()
        game.running = True

        config = GamePerformanceConfig(countdown_phase_duration_ms=0)
        with (
            patch.object(get_config_manager(), "get_config", return_value=config),
            patch.object(game, "_play_sound", autospec=True) as play_sound,
        ):
            await game._countdown()

        play_sound.assert_not_called()
        assert game.gameplay_stream.writes == []
        assert events.events == [
            (GameEvent.COUNTDOWN_START, {"phase_duration_ms": 0}),
            (GameEvent.COUNTDOWN_END, {}),
        ]


# --------------------------------------------------------------------------- #
# Main steps 3 to 5, A2, BR-002, Post-F-1 through run()
# --------------------------------------------------------------------------- #
def _winner_case(name, num_teams, players, dead, winner_team, winners, team_name, sound):
    return pytest.param(num_teams, players, dead, winner_team, winners, team_name, sound, id=name)


# Blue is the 7th colour, so only a 7-team game (outside Pre-1: 2..6 teams) reaches its voice line.
BLUE_VOICE_CASE = ("blue-voice", 7, 7, [0, 1, 2, 3, 4, 5], 6, ["mock_controller_6"], "Blue", Sound.VOX_BLUE_TEAM_WIN)

WIN_CASES = [
    # a team-mate of the winners dies first, so the winning team is announced with the alive players only
    _winner_case("pink-falls-back", 2, 6, [2, 1, 3, 5], 0, ["mock_controller_0", "mock_controller_4"], "Pink", None),
    _winner_case(
        "magenta-voice",
        2,
        6,
        [3, 0, 2, 4],
        1,
        ["mock_controller_1", "mock_controller_5"],
        "Magenta",
        Sound.VOX_MAGENTA_TEAM_WIN,
    ),
]


class TestMainSteps3To5:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", flow="main", steps=[3, 4, 5], cond=["Post-S-1"])
    # Deaths are 0.1 game-s apart: dilated 10x that is ~10 ms real, so a loaded runner folds several deaths into one
    # frame and the scripted order (team-mate first) is lost. Real-time spacing keeps one death per frame (#2443).
    @pytest.mark.real_game_time
    @pytest.mark.parametrize(
        ("num_teams", "players", "dead", "winner_team", "winners", "team_name", "sound"), WIN_CASES
    )
    async def test_run_ends_with_team_winner_rainbow_voice_and_game_ended(
        self, fast_config, num_teams, players, dead, winner_team, winners, team_name, sound
    ):
        """UC-021 steps 3 to 5: the last team standing is announced, rainbowed and voiced, then game_ended."""
        await self._assert_team_wins(num_teams, players, dead, winner_team, winners, team_name, sound)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", flow="main", steps=[4])
    async def test_blue_team_win_uses_the_blue_voice_line(self, fast_config):
        """UC-021 step 4: Blue (7th colour, beyond the 2..6 team precondition) maps to the Blue voice line."""
        _, *case = BLUE_VOICE_CASE
        await self._assert_team_wins(*case)

    async def _assert_team_wins(self, num_teams, players, dead, winner_team, winners, team_name, sound):
        schedule = {1.0 + 0.1 * i: idx for i, idx in enumerate(dead)}
        cm = RecordingControllerManager(num_controllers=players, death_schedule=schedule, max_duration=10.0)
        audio = create_autospec(RecordingAudio, instance=True)
        audio.sounds.return_value = []
        game, events = _teams_game(num_teams=num_teams, players=players, cm=cm, audio=audio, game_id="uc021_win")
        played: list = []

        async def record_sound(sound_enum, **_kwargs):
            played.append(sound_enum)

        with patch.object(game, "_play_sound", autospec=True, side_effect=record_sound):
            await asyncio.wait_for(game.run(), timeout=20.0)

        winner_events = events.get_events_of_type("team_winner")
        assert winner_events == [
            {
                "team": winner_team,
                "team_name": team_name,
                "team_color": str(TEAM_COLORS[winner_team]["rgb"]),
                "winning_players": winners,
                "winner_count": len(winners),
            }
        ]
        rainbow = cm.stream.effects(controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW)
        assert sorted(rainbow) == sorted(winners)
        expected_sound = sound or Sound.VOX_CONGRATULATIONS
        assert expected_sound in played
        assert TEAM_WIN_SOUNDS.get(team_name, Sound.VOX_CONGRATULATIONS) == expected_sound
        types = [t for t, _ in events.events]
        assert types.index("team_winner") < types.index(GameEvent.GAME_ENDED)
        assert types[-1] == GameEvent.GAME_ENDED
        assert GameEvent.GAME_FORCE_ENDED not in types
        assert GameEvent.GAME_TIE not in types
        assert game.state == GameState.ENDED

    @pytest.mark.real_game_time
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", flow="A2")
    async def test_run_with_every_team_eliminated_publishes_tie_then_game_ended(self, fast_config):
        """UC-021 A2: the last two teams fall between two win checks: game_tie, then game_ended, no team_winner."""
        # A motion death is checked at once (UC-020 BR-001), so a tie needs one elimination that skips the
        # check (the agent's eliminate_player on controller 0) right before the other team's lethal sample.
        cm = ScriptedTieControllerManager(num_controllers=2, max_duration=12.0)
        audio = create_autospec(RecordingAudio, instance=True)
        game, events = _teams_game(players=2, cm=cm, audio=audio, game_id="uc021_tie")
        cm.game = game
        played: list = []

        async def record_sound(sound_enum, **_kwargs):
            played.append(sound_enum)

        with patch.object(game, "_play_sound", autospec=True, side_effect=record_sound):
            await asyncio.wait_for(game.run(), timeout=20.0)

        types = [t for t, _ in events.events]
        assert events.get_events_of_type(GameEvent.GAME_TIE) == [{}]
        assert "team_winner" not in types
        assert types.index(GameEvent.GAME_TIE) < types.index(GameEvent.GAME_ENDED)
        assert types[-1] == GameEvent.GAME_ENDED
        assert cm.stream.effects(controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW) == []
        assert Sound.VOX_CONGRATULATIONS not in played

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", br=["BR-002"])
    async def test_natural_team_end_through_run_latches_completed(self, fast_config):
        """UC-021 BR-002: a natural end through the base run() records game.outcome completed on the root span."""
        cm = RecordingControllerManager(num_controllers=4, death_schedule={1.0: 1, 1.1: 3}, max_duration=10.0)
        game, _events = _teams_game(cm=cm, audio=create_autospec(RecordingAudio, instance=True), game_id="uc021_br2")
        root = create_autospec(trace.Span, instance=True)
        root.get_span_context.return_value = trace.INVALID_SPAN_CONTEXT

        def capture():
            game._game_root_span = root

        game._capture_game_root_span = capture
        with patch.object(game, "_play_sound", autospec=True):
            await asyncio.wait_for(game.run(), timeout=20.0)

        attrs = {}
        for call in root.set_attributes.call_args_list:
            attrs.update(call.args[0])
        assert attrs == {"game.outcome": "completed"}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", cond=["Post-F-1"])
    @pytest.mark.parametrize("game_cls", [SimpleTeamsGame, RandomTeamsGame], ids=["Teams", "RandomTeams"])
    async def test_one_player_publishes_game_error_and_raises(self, fast_config, game_cls):
        """UC-021 Post-F-1: a run() that cannot start publishes game_error, raises and never starts."""
        events = EventCollector()
        game = game_cls(
            controller_manager_client=RecordingControllerManager(num_controllers=1, infinite=True),
            event_publisher=events.publish,
            audio_client=create_autospec(RecordingAudio, instance=True),
            game_id="uc021_one",
            initial_players=_initial_players(1),
        )

        with pytest.raises(ValueError, match="Need at least 2 players, got 1"):
            await asyncio.wait_for(game.run(), timeout=15.0)

        assert events.get_events_of_type(GameEvent.GAME_ERROR) == [
            {"game_id": "uc021_one", "error": "Need at least 2 players, got 1"}
        ]
        types = [t for t, _ in events.events]
        assert GameEvent.GAME_STARTED not in types
        assert GameEvent.GAME_ENDED not in types
        assert game.state == GameState.ENDED


# --------------------------------------------------------------------------- #
# BR-001
# --------------------------------------------------------------------------- #
class TestBR001TeamColours:
    @pytest.mark.parametrize("num_teams", [2, 3, 4, 5, 6])
    @pytest.mark.uc("UC-021", br=["BR-001"])
    def test_teams_uses_the_first_n_colours(self, num_teams):
        """UC-021 BR-001: Teams uses the first N of the 8 team colours."""
        game, _ = _teams_game(num_teams=num_teams)

        assert game.team_colors == TEAM_COLORS[:num_teams]
        assert [t.name for t in game.teams.values()] == [c["name"] for c in TEAM_COLORS[:num_teams]]

    @pytest.mark.parametrize("num_teams", [2, 3, 4, 5, 6])
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", br=["BR-001"])
    async def test_random_teams_uses_n_distinct_colours_of_the_eight(self, num_teams):
        """UC-021 BR-001: Random Teams draws N distinct colours of the 8, whatever the seed."""
        for seed in range(1, 51):
            game = RandomTeamsGame(
                controller_manager_client=MockControllerManagerService(num_controllers=6),
                event_publisher=EventCollector().publish,
                game_id=f"uc021_colours_{seed}",
                num_teams=num_teams,
                rng_seed=seed,
            )
            await game._initialize_players_impl(_controllers(*(f"c{i}" for i in range(6))))

            names = [c["name"] for c in game.team_colors]
            assert len(names) == num_teams
            assert len(set(names)) == num_teams
            assert all(c in TEAM_COLORS for c in game.team_colors)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", br=["BR-001"])
    async def test_random_teams_seed_one_is_not_the_first_n_colours(self):
        """UC-021 BR-001: the draw is random, not a prefix: seed 1 gives a different pair than Pink, Magenta."""
        game = RandomTeamsGame(
            controller_manager_client=MockControllerManagerService(num_controllers=4),
            event_publisher=EventCollector().publish,
            game_id="uc021_seed1",
            num_teams=2,
            rng_seed=1,
        )
        await game._initialize_players_impl(_controllers("c0", "c1", "c2", "c3"))

        assert game.team_colors != TEAM_COLORS[:2]


# --------------------------------------------------------------------------- #
# BR-003: a force-end declares no winner; the result is latched (#2003)
# --------------------------------------------------------------------------- #
class _EndStream:
    """Gameplay stream stand-in that records the end-of-game effects."""

    def __init__(self):
        self.messages: list = []

    async def write(self, message):
        self.messages.append(message)

    def rainbows(self) -> list[str]:
        return sorted(
            m.game_effect.serial
            for m in self.messages
            if m.HasField("game_effect") and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW
        )


TEAM_MODES = pytest.mark.parametrize("game_cls", [SimpleTeamsGame, RandomTeamsGame], ids=["Teams", "RandomTeams"])


class TestBR003ForceEndNoWinner:
    """UC-021 BR-003 (#2003): only a latched result is announced; a force-end before it declares none."""

    @staticmethod
    async def _running_game(game_cls, events_hook=None):
        events = EventCollector()

        async def publish(event_type, data):
            await events.publish(event_type, data)
            if events_hook:
                events_hook(event_type)

        kwargs = {"random_assignment": False} if game_cls is SimpleTeamsGame else {"rng_seed": 3}
        game = game_cls(
            controller_manager_client=MockControllerManagerService(num_controllers=4, infinite=True),
            event_publisher=publish,
            game_id="uc021_br3",
            num_teams=2,
            initial_players=_initial_players(4),
            **kwargs,
        )
        await game._initialize_players_impl(_controllers(*(f"mock_controller_{i}" for i in range(4))))
        game.gameplay_stream = _EndStream()
        game.start_time = game_clock.now()
        game.state = GameState.RUNNING
        game.running = True
        game.sounds = []

        async def record_sound(sound, *_args, **_kwargs):
            game.sounds.append(sound)

        game._play_sound = record_sound
        return game, events

    @staticmethod
    def _eliminate_team(game, team: int) -> list[str]:
        """Mark team ``team`` dead (no win check); returns the other team's serials."""
        for player in game.players.values():
            if player.team == team:
                player.alive = False
        return sorted(s for s, p in game.players.items() if p.alive)

    @staticmethod
    def _assert_no_winner(game, events, outcome: str, reason: str | None):
        assert game.gameplay_stream.rainbows() == []
        assert game.sounds == []
        assert events.get_events_of_type("team_winner") == []
        ended = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert len(ended) == 1
        assert ended[0]["winning_team"] == "none"
        assert ended[0]["outcome"] == outcome
        if reason is None:
            assert "reason" not in ended[0]
        else:
            assert ended[0]["reason"] == reason

    @staticmethod
    def _assert_winner(game, events, team: int, winners: list[str]):
        assert game.gameplay_stream.rainbows() == winners
        assert len(game.sounds) == 1
        assert events.count_events_of_type("team_winner") == 1
        ended = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert len(ended) == 1
        assert ended[0]["winning_team"] == team
        assert ended[0]["outcome"] == "completed"
        assert "reason" not in ended[0]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", br=["BR-003"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @TEAM_MODES
    async def test_force_end_while_running_declares_no_winner(self, fast_config, game_cls):
        """A force-end before any result: no team_winner, rainbow, voice or wait; game_ended none + reason."""
        game, events = await self._running_game(game_cls)
        self._eliminate_team(game, 1)  # one team left, but no win check ran yet
        game.force_end("operator")

        assert not await game._check_win_condition()  # a force-ended game latches nothing
        await game._end_game_impl()

        self._assert_no_winner(game, events, "force_ended", "operator")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", br=["BR-003"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @TEAM_MODES
    async def test_force_end_after_the_decision_keeps_the_winner(self, fast_config, game_cls):
        """A force-end during the hold after a win keeps the winner, its effects and outcome completed."""
        game, events = await self._running_game(game_cls)
        team = next(p.team for p in game.players.values())
        winners = self._eliminate_team(game, 1 - team)

        assert await game._check_win_condition()
        game.force_end("operator")
        assert await game._check_win_condition()  # latched: not published again
        await game._end_game_impl()

        self._assert_winner(game, events, team, winners)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", br=["BR-003"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @TEAM_MODES
    async def test_same_tick_force_end_during_the_win_publish_keeps_the_winner(self, fast_config, game_cls):
        """Same tick, decision first: a force-end landing while team_winner is published keeps the winner."""
        holder: dict = {}

        def force_end_on_win(event_type):
            if event_type == "team_winner":
                holder["game"].force_end("operator")

        game, events = await self._running_game(game_cls, force_end_on_win)
        holder["game"] = game
        team = next(p.team for p in game.players.values())
        winners = self._eliminate_team(game, 1 - team)

        assert await game._check_win_condition()
        assert game.game_outcome() == "completed"
        await game._end_game_impl()

        self._assert_winner(game, events, team, winners)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", br=["BR-003"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @TEAM_MODES
    async def test_same_tick_force_end_before_the_win_check_declares_no_winner(self, fast_config, game_cls):
        """Same tick, force-end first: the lethal kill's win check then latches and publishes nothing."""
        game, events = await self._running_game(game_cls)
        team = next(p.team for p in game.players.values())
        losers = [s for s, p in game.players.items() if p.team != team]
        game.players[losers[0]].alive = False
        game.force_end("operator")
        game.players[losers[1]].alive = False  # the same tick's last lethal sample

        assert not await game._check_win_condition()
        await game._end_game_impl()

        self._assert_no_winner(game, events, "force_ended", "operator")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", br=["BR-003"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @TEAM_MODES
    async def test_force_end_between_the_force_end_check_and_the_latch_declares_no_winner(self, fast_config, game_cls):
        """A cross-thread force-end lands after the win check's force_ended check, before the latch."""
        game, events = await self._running_game(game_cls)
        self._eliminate_team(game, 1)
        latch = game._latch_game_outcome

        def force_end_then_latch():
            game.force_end("operator")
            latch()

        game._latch_game_outcome = force_end_then_latch

        assert not await game._check_win_condition()  # outcome force_ended: no result latched
        assert game._latched_team is None
        await game._end_game_impl()

        self._assert_no_winner(game, events, "force_ended", "operator")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", br=["BR-003"])
    @TEAM_MODES
    async def test_force_ended_outcome_overrides_a_latched_team(self, fast_config, game_cls):
        """Defence in depth: a force_ended outcome declares no winner even when a team is latched."""
        game, events = await self._running_game(game_cls)
        game._win_latched = True
        game._latched_team = next(p.team for p in game.players.values())
        game.force_end("operator")
        game._latch_game_outcome()

        await game._end_game_impl()

        self._assert_no_winner(game, events, "force_ended", "operator")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-021", br=["BR-003"])
    @TEAM_MODES
    async def test_stream_end_before_a_decision_declares_no_winner(self, fast_config, game_cls):
        """A gameplay stream that ends before any result: winning_team none, outcome completed, no effects."""
        game, events = await self._running_game(game_cls)
        self._eliminate_team(game, 1)  # one team left, but the stream ended before the win check
        game.running = False

        await game._end_game_impl()

        self._assert_no_winner(game, events, "completed", None)
