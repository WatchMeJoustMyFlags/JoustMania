"""
Controller feedback of the shield hold in the game coordinator (#2096, UC-040 BR-026).

One ordered outbound task per hold game sends the player's hold state; the frame path
only marks the player and never awaits (UC-030 BR-008). The cue values are latched
with the hold values and shipped with every command (BR-027).
"""

import asyncio
import contextlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from proto import controller_manager_pb2 as pb
from services.game_coordinator import game_clock
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import BaseGameMode, Player
from services.game_coordinator.games.ffa import FFAGame

T0 = 1000.0
DT = 1.0 / 60.0
IDLE = (0.0, 0.0, 1.0)


@pytest.fixture(autouse=True)
def _no_shield_limits():
    with patch.object(BaseGameMode, "_shield_limits", return_value=(0, 0)):
        yield


class RecordingStream:
    def __init__(self):
        self.writes = []

    async def write(self, message):
        self.writes.append(message)

    def effects(self, serial=None):
        """(effect name, duration_ms, shield_hold or None) per game effect, optionally for one serial."""
        out = []
        for message in self.writes:
            effect = message.game_effect
            if serial is not None and effect.serial != serial:
                continue
            hold = effect.shield_hold if effect.HasField("shield_hold") else None
            out.append((pb.GameEffect.Name(effect.effect), effect.duration_ms, hold))
        return out


class Clock:
    def __init__(self, t=T0):
        self.t = t


class Flags:
    def __init__(self):
        self.values = {
            "shield.activation": "hold",
            "shield.energy_max_seconds": 5.0,
            "shield.energy_budget_seconds": 15.0,
            "shield.energy_initial_seconds": 0.0,
            "shield.recharge_per_second": 0.0,
            "shield.min_burst_seconds": 0.3,
            "shield.charge_rumble_intensity": 200,
            "shield.charge_rumble_pulse_ms": 120,
            "shield.ending_rumble_seconds": 1.5,
            "shield.hold_rumble_intensity": 90,
            "shield.empty_pulse_ms": 500,
        }
        self.reads: list[str] = []

    def read(self, _set, key, default, **_kw):
        self.reads.append(key)
        return self.values.get(key, default)


@pytest.fixture
def clock():
    c = Clock()
    # The game clock drives the deadlines (UC-030 BR-010); the wall clock the wire timestamps.
    with (
        patch.object(base_module.time, "time", side_effect=lambda: c.t),
        patch.object(game_clock, "now", side_effect=lambda: c.t),
    ):
        yield c


@pytest.fixture
def flags():
    f = Flags()
    with (
        patch.object(base_module, "read_string_flag", side_effect=f.read),
        patch.object(base_module, "read_float_flag", side_effect=f.read),
        patch.object(base_module, "read_int_flag", side_effect=f.read),
    ):
        yield f


def _make_ffa():
    g = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_shield_hold_feedback",
    )
    g.gameplay_stream = RecordingStream()
    for serial in ("p1", "p2"):
        g.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    return g


@pytest_asyncio.fixture
async def game(clock, flags):  # noqa: ARG001 - flags patch base for the test
    g = _make_ffa()
    with patch.object(g, "_play_sound", new_callable=AsyncMock):
        # What the start of the game loop does (UC-040 BR-026, BR-027).
        g._latch_shield_hold()
        g._start_shield_hold_clock(clock.t)
        g._queue_hold_feedback_all()
        yield g
        g._cancel_hold_feedback()
        await drain()


async def drain(n=20):
    """Let the outbound task run."""
    for _ in range(n):
        await asyncio.sleep(0)


def frame(serial, trigger=False, accel=IDLE):
    return pb.GameplayData(
        serial=serial,
        accel=pb.Vector3(x=accel[0], y=accel[1], z=accel[2]),
        trigger_pressed=trigger,
    )


async def step(game, clock, serial, trigger, dt=DT):
    clock.t += dt
    await game._process_controller_state(frame(serial, trigger))


def fill(game, serial, seconds):
    game.players[serial].hold_energy = seconds


async def start_holding(game, clock, serial="p1", energy=3.0):
    fill(game, serial, energy)
    await step(game, clock, serial, False)  # arm (UC-030 BR-005)
    await step(game, clock, serial, True)
    assert game.players[serial].hold_started_at


class TestUC040HoldFeedbackCommands:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_game_start_sends_every_player_the_energy_layer(self, game):
        """UC-040 BR-026: at the start of the game loop each player gets SHIELD_ENERGY with the latched values."""
        await drain()
        for serial in ("p1", "p2"):
            effects = game.gameplay_stream.effects(serial)
            assert [name for name, _, _ in effects] == ["GAME_EFFECT_SHIELD_ENERGY"]
            _, duration_ms, state = effects[0]
            assert duration_ms == 5000
            assert (state.energy_ms, state.cap_ms, state.full_ms, state.min_burst_ms) == (0, 5000, 5000, 300)
            assert state.cue == pb.SHIELD_HOLD_CUE_NONE
            assert (
                state.cues.rumble_intensity,
                state.cues.pulse_ms,
                state.cues.ending_lead_ms,
                state.cues.hold_rumble_intensity,
                state.cues.empty_pulse_ms,
            ) == (200, 120, 1500, 90, 500)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_activation_sends_shield_hold_with_the_energy_left_and_one_sound(self, game, clock):
        """UC-030 A7 step 1 / UC-040 BR-026: SHIELD_HOLD for the usable energy; shield_up once."""
        await drain()
        game.gameplay_stream.writes.clear()
        await start_holding(game, clock, energy=3.0)
        await drain()

        ((name, duration_ms, state),) = game.gameplay_stream.effects("p1")
        assert name == "GAME_EFFECT_SHIELD_HOLD"
        assert duration_ms == 3000
        assert state.energy_ms == 3000 and state.cues.hold_rumble_intensity == 90
        game._play_sound.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_release_ends_the_layer_and_sends_the_energy_left(self, game, clock):
        """UC-030 A7 step 4: SHIELD_HOLD 0, then SHIELD_ENERGY with what is left, no cue."""
        await start_holding(game, clock, energy=3.0)
        await drain()
        game.gameplay_stream.writes.clear()
        for _ in range(60):  # hold for 1 s
            await step(game, clock, "p1", True)
        await step(game, clock, "p1", False)
        await drain()

        effects = game.gameplay_stream.effects("p1")
        assert [(n, d) for n, d, _ in effects] == [("GAME_EFFECT_SHIELD_HOLD", 0), ("GAME_EFFECT_SHIELD_ENERGY", 5000)]
        state = effects[1][2]
        assert 1950 <= state.energy_ms <= 2000
        assert state.cue == pb.SHIELD_HOLD_CUE_NONE
        assert state.full_ms == pytest.approx(5000, abs=1)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_a_release_inside_the_minimum_burst_tail_sends_nothing(self, game, clock):
        """UC-040 BR-026: a release in the tail sends nothing; SHIELD_HOLD 0 goes out when the tail ends."""
        await start_holding(game, clock, energy=3.0)
        await drain()
        game.gameplay_stream.writes.clear()
        await step(game, clock, "p1", False)  # released after one frame: the 0.3 s tail runs
        await step(game, clock, "p1", False)
        await drain()
        assert game.gameplay_stream.effects("p1") == []

        await step(game, clock, "p1", False, dt=0.3)
        await drain()
        assert [n for n, _, _ in game.gameplay_stream.effects("p1")] == [
            "GAME_EFFECT_SHIELD_HOLD",
            "GAME_EFFECT_SHIELD_ENERGY",
        ]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-026"])
    async def test_running_out_while_held_sends_the_empty_cue(self, game, clock):
        """UC-040 A39 step 4: held until the energy ran out: SHIELD_HOLD 0, then SHIELD_ENERGY with EMPTY."""
        await start_holding(game, clock, energy=0.5)
        await drain()
        game.gameplay_stream.writes.clear()
        for _ in range(40):
            await step(game, clock, "p1", True)
        await drain()

        effects = game.gameplay_stream.effects("p1")
        assert [n for n, _, _ in effects] == ["GAME_EFFECT_SHIELD_HOLD", "GAME_EFFECT_SHIELD_ENERGY"]
        assert effects[1][2].cue == pb.SHIELD_HOLD_CUE_EMPTY
        assert effects[1][2].energy_ms == 0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-026"])
    async def test_a_partial_grant_is_a_top_up_and_a_filling_grant_is_full(self, game):
        """UC-040 A36 step 4 (maintainer decision): full pattern only when the pool is full, else one pulse."""
        await drain()
        game.gameplay_stream.writes.clear()
        assert await game.grant_shield("p1", 2.0)
        await drain()
        assert await game.grant_shield("p1", 10.0)
        await drain()

        effects = game.gameplay_stream.effects("p1")
        assert [(n, s.cue, s.energy_ms) for n, _, s in effects] == [
            ("GAME_EFFECT_SHIELD_ENERGY", pb.SHIELD_HOLD_CUE_TOP_UP, 2000),
            ("GAME_EFFECT_SHIELD_ENERGY", pb.SHIELD_HOLD_CUE_FULL, 5000),
        ]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-026"])
    async def test_a_grant_during_the_hold_moves_the_end(self, game, clock):
        """UC-040 A36 step 2: SHIELD_HOLD again with the later end and the cue; no second sound."""
        await start_holding(game, clock, energy=1.0)
        await drain()
        game.gameplay_stream.writes.clear()
        assert await game.grant_shield("p1", 2.0)
        await drain()

        ((name, duration_ms, state),) = game.gameplay_stream.effects("p1")
        assert name == "GAME_EFFECT_SHIELD_HOLD"
        assert 2950 <= duration_ms <= 3000
        assert state.cue == pb.SHIELD_HOLD_CUE_TOP_UP
        game._play_sound.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A38", br=["BR-026"])
    async def test_the_idle_energy_carries_the_recharge_rate(self, clock, flags):
        """UC-040 A38 step 3: the controller gets the rate, so it shows the recharge and detects full itself."""
        flags.values["shield.recharge_per_second"] = 0.25
        g = _make_ffa()
        g._latch_shield_hold()
        g._start_shield_hold_clock(clock.t)
        g._queue_hold_feedback_all()
        await drain()
        _, _, state = g.gameplay_stream.effects("p1")[0]
        assert state.rate_ms_per_s == 250
        g._cancel_hold_feedback()


class TestUC040HoldFeedbackOrdering:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_frames_never_wait_for_the_stream(self, game, clock):
        """UC-030 BR-008: with a stream that never completes a write, toggling frames still finish."""
        blocked = asyncio.Event()

        async def never(_message):
            await blocked.wait()

        game.gameplay_stream.write = never
        fill(game, "p1", 5.0)

        async def frames():
            for i in range(40):
                await step(game, clock, "p1", i % 2 == 1, dt=0.4)

        await asyncio.wait_for(frames(), timeout=2)
        blocked.set()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_changes_between_two_sends_coalesce(self, game, clock):
        """UC-040 BR-026: an activation that ended before the task ran sends only the idle state."""
        await drain()
        game.gameplay_stream.writes.clear()
        fill(game, "p1", 3.0)
        await step(game, clock, "p1", False)
        player = game.players["p1"]
        game._start_shield_hold(player, clock.t, game._shield_hold)
        clock.t += 0.5
        game._end_shield_hold(player, base_module.HOLD_OUTCOME_RELEASED, clock.t)
        await drain()

        assert [n for n, _, _ in game.gameplay_stream.effects("p1")] == ["GAME_EFFECT_SHIELD_ENERGY"]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_shield_up_plays_at_most_once_per_two_seconds(self, game, clock):
        """UC-040 BR-026: a mashed trigger never queues sound RPCs."""
        fill(game, "p1", 5.0)
        activations = 0
        for _ in range(8):  # one activation about every 0.37 s, about 2.9 s in all
            await step(game, clock, "p1", False, dt=0.35)
            await step(game, clock, "p1", True)
            activations += bool(game.players["p1"].hold_started_at)
            await drain()
        assert activations == 8
        assert game._play_sound.await_count == 2  # the first activation, then the first one 2 s later

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-026"])
    async def test_a_dead_player_gets_nothing(self, game, clock):
        """UC-040 A39: a death drops the pool; the death effect, not a hold command, owns the controller."""
        await start_holding(game, clock, energy=3.0)
        game.players["p1"].alive = False
        game._drop_shield_hold(game.players["p1"], base_module.HOLD_OUTCOME_DEATH, clock.t)
        await drain()
        assert [n for n, _, _ in game.gameplay_stream.effects("p1")] == ["GAME_EFFECT_SHIELD_ENERGY"]  # game start

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-026"])
    async def test_game_end_stops_the_task_and_ends_every_hold_layer(self, game, clock):
        """UC-040 A39 / BR-026: no hold command after the game end; SHIELD 0 ends the layers of every player."""
        await start_holding(game, clock, energy=3.0)
        await drain()
        game.gameplay_stream.writes.clear()
        await game._end_shield_feedback()
        game._queue_hold_feedback(game.players["p2"])  # a late mark (a grant racing the end)
        await drain()

        game._queue_hold_feedback_all()  # a reconnect re-send racing the end
        game._wake_hold_feedback()
        await drain()

        written = [
            (m.game_effect.serial, m.game_effect.effect, m.game_effect.duration_ms) for m in game.gameplay_stream.writes
        ]
        assert sorted(written) == [("p1", pb.GAME_EFFECT_SHIELD, 0), ("p2", pb.GAME_EFFECT_SHIELD, 0)]
        assert game._hold_feedback_task is None

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-026", "BR-021"])
    async def test_a_reconnect_sends_the_state_again(self, game, clock):
        """UC-040 BR-021 / BR-026: after a mid-game reconnect the hold state of every living player is re-sent."""
        await start_holding(game, clock, energy=3.0)
        await drain()
        game.gameplay_stream.writes.clear()
        await game._resend_shield_layers()
        await drain()
        assert [n for n, _, _ in game.gameplay_stream.effects("p1")] == ["GAME_EFFECT_SHIELD_HOLD"]
        assert [n for n, _, _ in game.gameplay_stream.effects("p2")] == ["GAME_EFFECT_SHIELD_ENERGY"]


class TestUC040HoldCueLatch:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-026", "BR-027"])
    async def test_cue_values_are_read_once_and_a_flip_mid_game_changes_nothing(self, game, clock, flags):
        """UC-040 BR-027: the cue flags are read with the hold values; a later stage flip applies next game."""
        cue_keys = set(base_module.SHIELD_HOLD_CUE_FLAGS)
        assert sorted(k for k in flags.reads if k in cue_keys) == sorted(cue_keys)
        flags.values["shield.hold_rumble_intensity"] = 10
        await start_holding(game, clock, energy=3.0)
        await drain()
        _, _, state = game.gameplay_stream.effects("p1")[-1]
        assert state.cues.hold_rumble_intensity == 90
        assert sorted(k for k in flags.reads if k in cue_keys) == sorted(cue_keys)

    @pytest.mark.uc("UC-040", br=["BR-026"])
    @pytest.mark.parametrize(
        ("key", "raw", "field", "want"),
        [
            ("shield.hold_rumble_intensity", 999, "hold_rumble_intensity", 150),
            ("shield.hold_rumble_intensity", "loud", "hold_rumble_intensity", 70),
            ("shield.empty_pulse_ms", 10, "empty_pulse_ms", 100),
            ("shield.ending_rumble_seconds", 2.25, "ending_lead_ms", 2250),
            ("shield.charge_rumble_intensity", -5, "rumble_intensity", 0),
        ],
    )
    def test_cue_values_are_validated(self, clock, flags, key, raw, field, want):  # noqa: ARG002
        """flags.md §4: a malformed value gives the default, any other is clamped to the registry range."""
        flags.values[key] = raw
        g = _make_ffa()
        assert getattr(g._read_shield_hold_cues(), field) == want

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-019", "BR-026"])
    async def test_a_trigger_game_sends_no_hold_command(self, clock, flags):
        """UC-040 BR-019: a game not latched to hold has no outbound task and sends no hold command."""
        flags.values["shield.activation"] = "trigger"
        g = _make_ffa()
        g._latch_shield_hold()
        g._start_shield_hold_clock(clock.t)
        g._queue_hold_feedback_all()
        await drain()
        assert g._hold_feedback_task is None
        assert g.gameplay_stream.writes == []
        assert not any(k in base_module.SHIELD_HOLD_CUE_FLAGS for k in flags.reads)


@contextlib.asynccontextmanager
async def aio_loopback_call():
    """A real grpc.aio StreamGameplayData call against a loopback server; yields (call, received messages)."""
    import grpc
    from grpc import aio

    received = []

    async def handler(request_iterator, _context):
        async for raw in request_iterator:
            received.append(pb.GameplayStreamControl.FromString(raw))
        yield b""

    server = aio.server()
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "controller_manager.ControllerManager",
                {"StreamGameplayData": grpc.stream_stream_rpc_method_handler(handler)},
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        call = channel.stream_stream(
            "/controller_manager.ControllerManager/StreamGameplayData",
            request_serializer=lambda m: m.SerializeToString(),
        )()
        await call.write(pb.GameplayStreamControl())
        yield call, received
    finally:
        await channel.close()
        await server.stop(0)


class OverlapCheckingStream(RecordingStream):
    """A gameplay call like grpc.aio: a write that starts while another is pending fails (#2096 review F1)."""

    def __init__(self):
        super().__init__()
        self.in_flight = 0
        self.max_in_flight = 0

    async def write(self, message):
        if self.in_flight:
            raise RuntimeError("GRPC_CALL_ERROR_TOO_MANY_OPERATIONS: a write is already in flight")
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)  # the write is in flight across a loop turn
            await asyncio.sleep(0)
            self.writes.append(message)
        finally:
            self.in_flight -= 1


class TestUC030OneWriteInFlight:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-006", "BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_the_hold_task_and_the_frame_path_never_overlap_writes(self, game):
        """UC-030 BR-006: the hold task's writes and the frame path's warnings share one call, one write at a time."""
        stream = OverlapCheckingStream()
        game.gameplay_stream = stream
        for _ in range(60):
            game._queue_hold_feedback_all()
            game.players["p2"].alive = True
            await game._warn_player("p2", 30.0, 20.0)
            await asyncio.sleep(0)
        await drain()
        assert stream.max_in_flight == 1
        assert any(m.game_effect.effect == pb.GAME_EFFECT_SHIELD_ENERGY for m in stream.writes)
        assert any(m.game_effect.effect == pb.GAME_EFFECT_PLAYER_WARNING for m in stream.writes)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="main", steps=[13], br=["BR-026"])
    async def test_the_agent_cue_never_queues_a_batch_ahead_of_a_frame_write(self):
        """UC-040 BR-026: the cue pulses one player after the other, so a frame write waits for one pulse only."""
        from unittest.mock import AsyncMock

        from services.game_coordinator.interventions import INTERVENTION_SPECS, InterventionManager
        from services.game_coordinator.stream_writes import serialized_write

        class GatedStream:
            def __init__(self):
                self.order = []
                self.in_flight = 0
                self.max_in_flight = 0
                self.release = asyncio.Event()
                self.first_started = asyncio.Event()

            async def write(self, message):
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                self.first_started.set()
                try:
                    await self.release.wait()
                    self.release.clear()
                    self.order.append(message.game_effect.serial or "frame")
                finally:
                    self.in_flight -= 1

        stream = GatedStream()
        players = {s: Player(serial=s, alive=True, color=(0, 255, 0)) for s in ("a", "b", "c", "d")}
        game = type("Game", (), {"players": players, "gameplay_stream": stream})()
        manager = InterventionManager(event_publisher=AsyncMock(), get_game=lambda: None)
        manager._user_client = None  # the cue is on by default
        spec = next(s for s in INTERVENTION_SPECS if s.type_id == "end_game")

        cue = asyncio.create_task(manager._emit_agent_cue(spec, None, game))
        await asyncio.wait_for(stream.first_started.wait(), timeout=1)  # pulse 1 in flight
        frame = asyncio.create_task(serialized_write(stream, pb.GameplayStreamControl()))
        for _ in range(5):
            await drain()
            stream.release.set()
        await asyncio.wait_for(asyncio.gather(cue, frame), timeout=2)

        assert stream.max_in_flight == 1
        assert len(stream.order) == 5
        assert stream.order[1] == "frame", stream.order  # right after the one pulse in flight

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-006", "BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_a_real_aio_call_survives_hold_marks_and_warnings(self, game):
        """UC-030 BR-006 on a real grpc.aio loopback call: 300 marks plus warnings never end the call."""
        async with aio_loopback_call() as (call, _received):
            game.gameplay_stream = call
            for _ in range(300):
                assert not call.done(), f"the call ended: {call.code()}"
                game._queue_hold_feedback_all()
                game.players["p2"].alive = True
                await game._warn_player("p2", 30.0, 20.0)
                await asyncio.sleep(0)
            await asyncio.sleep(0.2)
            assert not call.done(), f"the call ended: {call.code() if call.done() else None}"
            game._cancel_hold_feedback()
            await drain()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-026"])
    @pytest.mark.uc("UC-030", br=["BR-006"])
    async def test_the_game_end_during_a_hold_write_keeps_the_call_and_sends_shield_0(self, game):
        """UC-040 A39: stopping the task while its write is in flight neither cancels the call nor loses SHIELD 0."""
        async with aio_loopback_call() as (call, received):
            started = asyncio.Event()

            class Stream:
                async def write(self, message):
                    started.set()
                    await call.write(message)

            game.gameplay_stream = Stream()
            game._queue_hold_feedback_all()
            await asyncio.wait_for(started.wait(), timeout=2)  # the hold task is inside the grpc write
            mark = len(received)
            await game._end_shield_feedback()
            await asyncio.sleep(0.3)

            assert not call.done(), f"the game end cancelled the gameplay call: {call.code()}"
            shield_0 = [
                m.game_effect.serial
                for m in received[mark:]
                if m.game_effect.effect == pb.GAME_EFFECT_SHIELD and m.game_effect.duration_ms == 0
            ]
            assert sorted(shield_0) == ["p1", "p2"]
