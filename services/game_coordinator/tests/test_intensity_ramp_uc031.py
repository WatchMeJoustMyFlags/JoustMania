"""UC-031 step 3 / BR-005 (#1833): one tempo intensity drives the thresholds; the music follows.

Every test runs on a fake clock (``games.base.time.time`` patched), with no sleeps.
The ramp flags are patched through ``games.base.read_float_flag`` so each test
controls ``tempo.ramp_up_seconds`` / ``tempo.ramp_down_seconds``.
"""

import asyncio
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proto import audio_pb2, controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.difficulty_handlers import handle_music_tempo_override
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import (
    DEFAULT_RAMP_DOWN_SECONDS,
    DEFAULT_RAMP_UP_SECONDS,
    FAST_MAX,
    FAST_MUSIC_SPEED,
    FAST_WARNING,
    RAMP_CURVE_LINEAR,
    RAMP_SECONDS_MAX,
    RAMP_SECONDS_MIN,
    SLOW_MAX,
    SLOW_MUSIC_SPEED,
    SLOW_WARNING,
    IntensityRamp,
    Player,
    RampDescriptor,
    resolve_ramp_seconds,
)
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.interventions import INTERVENTION_SPECS, InterventionContext
from services.game_coordinator.tests.conftest import EventCollector, MockControllerManagerService

MEDIUM = 2
T0 = 10_000.0
UP_KEY = "tempo.ramp_up_seconds"
DOWN_KEY = "tempo.ramp_down_seconds"


class FakeClock:
    """Patches ``game_clock.now``; ``now`` is moved by hand."""

    def __init__(self, now: float = T0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class RampFlags:
    """Stands in for ``read_float_flag``: ramp keys from ``values``, every other key its default."""

    def __init__(self, up: object = 3.0, down: object = 2.0):
        self.values = {UP_KEY: up, DOWN_KEY: down}
        self.reads: list[str] = []

    def __call__(self, domain, key, default, game_id=None):
        if key in self.values:
            self.reads.append(key)
            return self.values[key]
        return default


class TempoAudio:
    """Audio stub recording every ChangeTempo as (new_tempo, transition_duration)."""

    def __init__(self):
        self.tempo_calls: list[tuple[float, float]] = []
        self.calls: list[str] = []
        self.fail_next = 0
        self.refuse = False

    async def ChangeTempo(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append("ChangeTempo")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("audio down")
        self.tempo_calls.append((round(request.new_tempo, 4), round(request.transition_duration, 4)))
        return audio_pb2.ChangeTempoResponse(success=not self.refuse, error="track mismatch" if self.refuse else "")

    async def StopMusic(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append("StopMusic")
        return audio_pb2.StopMusicResponse(success=True)

    async def SetVolume(self, request):  # noqa: N802 - gRPC stub name
        return audio_pb2.SetVolumeResponse(success=True)

    async def PlayMusic(self, request):  # noqa: N802 - gRPC stub name
        return audio_pb2.PlayMusicResponse(track_id="track-1", success=True)


def make_game(audio=None, track_id="track-1"):
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=EventCollector().publish,
        audio_client=audio,
        game_id="test_intensity_ramp",
        sensitivity=MEDIUM,
    )
    game.music_track_id = track_id
    game.speed_up = True
    game.gameplay_span = None
    for serial in ("p1", "p2"):
        game.players[serial] = Player(serial=serial, alive=True, color=(255, 0, 0))
    return game


@pytest.fixture
def clock():
    fake = FakeClock()
    with patch.object(game_clock, "now", fake):
        yield fake


@pytest.fixture
def flags():
    fake = RampFlags()
    with patch.object(base_module, "read_float_flag", fake):
        yield fake


def start_held(game, clock, intensity=0.0, change_due=True):
    """Hold the intensity and make the next scheduled change due now."""
    game._intensity = IntensityRamp.hold(intensity, clock.now)
    game.change_time = clock.now if change_due else clock.now + 1_000.0


def lerp(a, b, t):
    return a * (1 - t) + b * t


# --------------------------------------------------------------------------- #
# BR-005: the pure ramp
# --------------------------------------------------------------------------- #


@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"])
def test_intensity_ramp_is_a_clamped_linear_function_of_the_clock():
    ramp = IntensityRamp(start=0.0, target=1.0, start_time=T0, duration=3.0)
    assert ramp.value_at(T0 - 1.0) == 0.0  # before the start
    assert ramp.value_at(T0) == 0.0
    assert ramp.value_at(T0 + 1.5) == pytest.approx(0.5)
    assert ramp.value_at(T0 + 3.0) == 1.0
    assert ramp.value_at(T0 + 99.0) == 1.0  # holds the target after the end
    assert not ramp.is_complete(T0 + 2.9)
    assert ramp.is_complete(T0 + 3.0)
    assert ramp.end_time == T0 + 3.0
    # Clamped to [0, 1] even for an out-of-range segment or hold.
    assert IntensityRamp(start=-1.0, target=2.0, start_time=T0, duration=1.0).value_at(T0 + 0.9) == 1.0
    assert IntensityRamp.hold(1.7).value_at(T0) == 1.0
    assert IntensityRamp.hold(0.4).is_complete(T0)


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"])
async def test_a_half_swing_takes_half_the_flag_duration(clock):
    """BR-005: segment duration = |target - i(now)| x the flag for the direction."""
    game = make_game(TempoAudio())
    start_held(game, clock, intensity=0.5)

    await game._check_music_speed()

    assert game._intensity.duration == pytest.approx(0.5 * 3.0)
    assert game._intensity.start == pytest.approx(0.5)
    assert game.audio_client.tempo_calls == [(FAST_MUSIC_SPEED, 1.5)]


# --------------------------------------------------------------------------- #
# Step 3 / UC-030 step 5: thresholds follow the intensity every frame
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"])
@pytest.mark.uc("UC-030", flow="main", steps=[5])
async def test_thresholds_follow_the_intensity_halfway_through_the_speed_up(clock):
    """Halfway through a 3 s speed-up at MEDIUM: warning 1.75 g, death 2.3 g (not the fast table yet)."""
    game = make_game(TempoAudio())
    start_held(game, clock)
    player = game.players["p1"]

    await game._check_music_speed()
    assert game._compute_effective_thresholds(player) == pytest.approx((SLOW_WARNING[MEDIUM], SLOW_MAX[MEDIUM]))

    clock.now = T0 + 1.5
    warn, death = game._compute_effective_thresholds(player)
    assert warn == pytest.approx(1.75)
    assert death == pytest.approx(2.3)
    assert game.music_speed == pytest.approx(1.15)

    clock.now = T0 + 3.0
    assert game._compute_effective_thresholds(player) == pytest.approx((FAST_WARNING[MEDIUM], FAST_MAX[MEDIUM]))


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"])
async def test_one_change_tempo_glide_per_segment(clock):
    """BR-005: one ChangeTempo per segment, to the target over the whole segment; ticks inside it send none."""
    audio = TempoAudio()
    game = make_game(audio)
    start_held(game, clock)

    await game._check_music_speed()
    for step in range(1, 30):  # 100 ms ticks through the 3 s ramp
        clock.now = T0 + step * 0.1
        await game._check_music_speed()

    assert audio.tempo_calls == [(FAST_MUSIC_SPEED, 3.0)]


@pytest.mark.asyncio
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"])
async def test_the_up_and_down_flags_set_their_own_direction(clock, flags):
    """BR-005: a speed-up uses tempo.ramp_up_seconds, a slow-down tempo.ramp_down_seconds."""
    flags.values = {UP_KEY: 3.0, DOWN_KEY: 1.0}
    audio = TempoAudio()
    game = make_game(audio)
    start_held(game, clock)

    await game._check_music_speed()  # speed-up
    clock.now = game.change_time  # the window after the ramp end
    await game._check_music_speed()  # slow-down

    assert audio.tempo_calls == [(FAST_MUSIC_SPEED, 3.0), (SLOW_MUSIC_SPEED, 1.0)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="A3", br=["BR-005"])
async def test_a_reversal_mid_ramp_restarts_from_the_intensity_now(clock):
    """BR-005: an override to 1.0x halfway up starts a 0.5 x down segment from i=0.5; no jump."""
    audio = TempoAudio()
    game = make_game(audio)
    start_held(game, clock)
    player = game.players["p1"]
    await game._check_music_speed()

    clock.now = T0 + 1.5
    before = game._compute_effective_thresholds(player)
    game.tempo_override = SLOW_MUSIC_SPEED
    await game._check_music_speed()

    assert game._intensity.start == pytest.approx(0.5)
    assert game._intensity.duration == pytest.approx(0.5 * 2.0)
    assert game._compute_effective_thresholds(player) == pytest.approx(before)  # continuous
    assert audio.tempo_calls[-1] == (SLOW_MUSIC_SPEED, 1.0)
    clock.now = T0 + 2.0
    assert game.tempo_intensity == pytest.approx(0.25)


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"])
async def test_the_next_window_counts_from_the_ramp_end(clock):
    """Step 3: change_time = ramp end + window; the window is drawn for the NEW direction."""
    game = make_game(TempoAudio())
    start_held(game, clock)
    seen_speed_up = []

    def window():
        seen_speed_up.append(game.speed_up)
        return 7.0

    with patch.object(game, "_decide_next_change_delay", side_effect=window):
        await game._check_music_speed()

    assert game.change_time == pytest.approx(T0 + 3.0 + 7.0)
    assert seen_speed_up == [False]  # flipped before the window was drawn (fast window next)

    # The next change waits for both the window and the end of the segment.
    clock.now = T0 + 9.9
    await game._check_music_speed()
    assert game.speed_up is False
    assert len(game.audio_client.tempo_calls) == 1


# --------------------------------------------------------------------------- #
# Flags: read per ramp, validated
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (3.0, 3.0),
        (5, 5.0),
        (0.01, RAMP_SECONDS_MIN),
        (1e9, RAMP_SECONDS_MAX),
        (-1.0, DEFAULT_RAMP_UP_SECONDS),
        (0.0, DEFAULT_RAMP_UP_SECONDS),
        (math.nan, DEFAULT_RAMP_UP_SECONDS),
        (math.inf, DEFAULT_RAMP_UP_SECONDS),
        ("3.0", DEFAULT_RAMP_UP_SECONDS),
        (True, DEFAULT_RAMP_UP_SECONDS),
        (None, DEFAULT_RAMP_UP_SECONDS),
    ],
)
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"])
def test_ramp_seconds_are_validated(value, expected):
    assert resolve_ramp_seconds(value, DEFAULT_RAMP_UP_SECONDS) == pytest.approx(expected)


@pytest.mark.asyncio
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"])
async def test_flags_are_read_at_each_ramp_start_and_a_ramp_in_flight_keeps_its_duration(clock, flags):
    audio = TempoAudio()
    game = make_game(audio)
    start_held(game, clock)

    await game._check_music_speed()  # speed-up, 3 s
    flags.values[UP_KEY] = 5.0  # changed on stage mid-ramp
    clock.now = T0 + 1.5
    await game._check_music_speed()
    assert game.tempo_intensity == pytest.approx(0.5)  # still the 3 s ramp

    clock.now = game.change_time
    await game._check_music_speed()  # slow-down
    clock.now = game.change_time
    await game._check_music_speed()  # speed-up with the new value

    assert [duration for _, duration in audio.tempo_calls] == [3.0, 2.0, 5.0]
    assert flags.reads == [UP_KEY, DOWN_KEY, UP_KEY]


@pytest.mark.asyncio
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"])
async def test_a_malformed_flag_uses_the_default(clock, flags):
    flags.values = {UP_KEY: math.nan, DOWN_KEY: -1.0}
    audio = TempoAudio()
    game = make_game(audio)
    start_held(game, clock)

    await game._check_music_speed()
    clock.now = game.change_time
    await game._check_music_speed()

    assert audio.tempo_calls == [
        (FAST_MUSIC_SPEED, DEFAULT_RAMP_UP_SECONDS),
        (SLOW_MUSIC_SPEED, DEFAULT_RAMP_DOWN_SECONDS),
    ]


# --------------------------------------------------------------------------- #
# Post-F-1: audio failures
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"], cond=["Post-F-1"])
async def test_a_failed_glide_does_not_hold_the_intensity_and_the_rest_is_resent(clock):
    audio = TempoAudio()
    audio.fail_next = 1
    game = make_game(audio)
    start_held(game, clock)

    await game._check_music_speed()  # must not raise
    assert game.speed_up is False  # the change happened
    clock.now = T0 + 0.3
    await game._check_music_speed()  # inside the 0.5 s back-off: no resend
    assert audio.calls == ["ChangeTempo"]
    assert game.tempo_intensity == pytest.approx(0.1)  # the intensity moved anyway

    clock.now = T0 + 0.6
    await game._check_music_speed()  # resend the remaining glide
    assert audio.tempo_calls == [(FAST_MUSIC_SPEED, 2.4)]

    for step in range(7, 40):
        clock.now = T0 + step * 0.1
        await game._check_music_speed()
    assert audio.tempo_calls == [(FAST_MUSIC_SPEED, 2.4)]  # synced: no more sends


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"], cond=["Post-F-1"])
async def test_resends_back_off_exponentially(clock):
    audio = TempoAudio()
    audio.fail_next = 3
    game = make_game(audio)
    start_held(game, clock)

    attempts = []
    for step in range(0, 40):
        clock.now = T0 + step * 0.1
        before = len(audio.calls)
        await game._check_music_speed()
        if len(audio.calls) > before:
            attempts.append(round(clock.now - T0, 1))

    assert attempts == [0.0, 0.5, 1.5, 3.5]  # back-off 0.5, 1.0, 2.0 s
    assert audio.tempo_calls == [(FAST_MUSIC_SPEED, base_module.MIN_GLIDE_SECONDS)]  # past the end: the target


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"], cond=["Post-F-1"])
async def test_a_refused_glide_stops_tempo_requests_for_that_track(clock):
    """A ChangeTempo the audio service refuses (track replaced or stopped) is not resent."""
    audio = TempoAudio()
    audio.refuse = True
    game = make_game(audio)
    start_held(game, clock)

    with patch.object(game, "_decide_next_change_delay", return_value=20.0):
        await game._check_music_speed()
        for step in range(1, 100):  # no resend through the ramp and the hold
            clock.now = T0 + step * 0.1
            await game._check_music_speed()
        clock.now = game.change_time
        await game._check_music_speed()  # the next change: still nothing sent

    assert audio.calls == ["ChangeTempo"]
    assert game.music_track_id is None
    assert game.speed_up is True  # the schedule went on: up, then down


# --------------------------------------------------------------------------- #
# A1 / D1: no music track
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="A1", br=["BR-005"])
async def test_muted_music_still_moves_the_intensity_and_sends_nothing(clock):
    audio = TempoAudio()
    game = make_game(audio, track_id=base_module.MUTED_TRACK_ID)
    start_held(game, clock)
    player = game.players["p1"]

    await game._check_music_speed()
    clock.now = T0 + 3.0
    await game._check_music_speed()

    assert game._compute_effective_thresholds(player) == pytest.approx((FAST_WARNING[MEDIUM], FAST_MAX[MEDIUM]))
    assert audio.calls == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"])
async def test_without_an_audio_client_the_schedule_starts_and_runs(clock):
    """D1: _start_game_music without an audio client still starts the schedule."""
    game = make_game(audio=None, track_id=None)
    player = game.players["p1"]

    with patch.object(game, "_decide_next_change_delay", return_value=12.0):
        await game._start_game_music()
    assert game.change_time == pytest.approx(T0 + 12.0)
    assert game.tempo_intensity == 0.0

    clock.now = game.change_time
    await game._check_music_speed()
    clock.now += 3.0
    assert game._compute_effective_thresholds(player) == pytest.approx((FAST_WARNING[MEDIUM], FAST_MAX[MEDIUM]))


# --------------------------------------------------------------------------- #
# A3: override and ramp_tempo
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="A3", br=["BR-005"])
async def test_the_override_ramps_at_the_bounded_rate_and_leaves_the_schedule_alone(clock):
    audio = TempoAudio()
    game = make_game(audio)
    start_held(game, clock, change_due=False)
    change_time = game.change_time
    game.tempo_override = FAST_MUSIC_SPEED

    await game._check_music_speed()
    clock.now = T0 + 1.5
    await game._check_music_speed()

    assert game.music_speed == pytest.approx(1.15)
    assert audio.tempo_calls == [(FAST_MUSIC_SPEED, 3.0)]
    assert game.speed_up is True
    assert game.change_time == change_time


@pytest.mark.asyncio
@pytest.mark.uc("UC-031", flow="A3")
async def test_the_override_is_clamped_to_the_music_range():
    game = make_game(TempoAudio())
    spec = next(s for s in INTERVENTION_SPECS if s.flag_key == "music_tempo_override")
    ctx = InterventionContext(spec=spec, value=2.0, payload="", target_serial=None, game=game, objective="balanced")
    await handle_music_tempo_override(ctx)
    assert game.tempo_override == FAST_MUSIC_SPEED


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="A3", br=["BR-005"])
async def test_a_linear_ramp_tempo_sends_one_glide_per_descriptor(clock):
    audio = TempoAudio()
    game = make_game(audio)
    start_held(game, clock, change_due=False)
    game.tempo_ramp = RampDescriptor(SLOW_MUSIC_SPEED, FAST_MUSIC_SPEED, T0, 8.0, RAMP_CURVE_LINEAR)

    for step in range(0, 10):
        clock.now = T0 + step * 0.1
        await game._check_music_speed()
    assert audio.tempo_calls == [(FAST_MUSIC_SPEED, 8.0)]

    clock.now = T0 + 4.0
    assert game.music_speed == pytest.approx(1.15)
    # A new descriptor with the same values is a new curve: one more glide.
    game.tempo_ramp = RampDescriptor(game.music_speed, SLOW_MUSIC_SPEED, clock.now, 2.0, RAMP_CURVE_LINEAR)
    await game._check_music_speed()
    await game._check_music_speed()
    assert audio.tempo_calls == [(FAST_MUSIC_SPEED, 8.0), (SLOW_MUSIC_SPEED, 2.0)]


# --------------------------------------------------------------------------- #
# Factors, game end, hot path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-030", flow="main", steps=[5])
async def test_difficulty_and_handicap_apply_after_the_intensity_lerp(clock):
    game = make_game(TempoAudio())
    start_held(game, clock)
    player = game.players["p1"]
    await game._check_music_speed()
    clock.now = T0 + 1.5
    base_warn = lerp(SLOW_WARNING[MEDIUM], FAST_WARNING[MEDIUM], 0.5)
    base_death = lerp(SLOW_MAX[MEDIUM], FAST_MAX[MEDIUM], 0.5)

    game.global_difficulty_factor = 1.5
    player.handicap_factor = 2.0
    assert game._compute_effective_thresholds(player) == pytest.approx((base_warn / 1.5 * 2.0, base_death / 1.5 * 2.0))

    game.global_difficulty_factor = 3.0  # clamped to 2.0
    assert game._compute_effective_thresholds(player) == pytest.approx((base_warn / 2.0 * 2.0, base_death / 2.0 * 2.0))


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"])
async def test_game_end_mid_ramp_sends_no_tempo_change_after_stop_music(clock):
    audio = TempoAudio()
    game = make_game(audio)
    start_held(game, clock)
    game.running = True
    game.game_cycle_context = None

    task = asyncio.create_task(game._music_loop())
    for _ in range(5):
        await asyncio.sleep(0)  # let the loop run its first tick (starts the ramp)
    assert audio.calls == ["ChangeTempo"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await game._stop_game_music()
    clock.now = T0 + 1.0  # mid-ramp

    assert audio.calls == ["ChangeTempo", "StopMusic"]
    assert game.music_track_id is None


@pytest.mark.asyncio
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-005"])
async def test_the_gameplay_hot_path_never_reads_a_flag_during_a_ramp(clock, flags):
    """600 frames through _process_gameplay_update mid-ramp: no ramp-flag read per frame."""
    game = make_game(TempoAudio())
    start_held(game, clock)
    await game._check_music_speed()
    reads_after_start = len(flags.reads)
    game._kill_player = AsyncMock()
    game._warn_player = AsyncMock()

    thresholds = MagicMock(wraps=game._compute_effective_thresholds)
    with (
        patch.object(base_module, "read_float_flag", MagicMock(side_effect=flags)) as flag_read,
        patch.object(game, "_compute_effective_thresholds", thresholds),
    ):
        for frame in range(600):
            clock.now = T0 + frame / 600 * 2.0
            update = controller_manager_pb2.GameplayDataUpdate(
                controllers=[
                    controller_manager_pb2.GameplayData(
                        serial="p1",
                        battery=80,
                        accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0),
                        gyro=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=0.0),
                    )
                ]
            )
            await game._process_gameplay_update(update)
        assert flag_read.call_count == 0
    assert thresholds.call_count >= 600  # the frames really reached the threshold path
    assert len(flags.reads) == reads_after_start


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-001", "BR-005"])
async def test_without_the_tempo_loop_the_intensity_stays_at_zero(clock):
    """BR-001/BR-005: a mode that never runs the music loop keeps i = 0 (the slow table) after music starts."""
    game = make_game(TempoAudio())
    await game._start_game_music()
    clock.now = T0 + 100.0
    assert game.tempo_intensity == 0.0
    assert game.music_speed == SLOW_MUSIC_SPEED


# --------------------------------------------------------------------------- #
# A3: control returns to the schedule mid-ramp (review of #1840)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="A3", br=["BR-005"])
@pytest.mark.parametrize("owner", ["ramp_tempo", "override"])
async def test_a_revert_mid_ramp_holds_the_intensity_reached(clock, owner):
    """A3: a reverted ramp_tempo or a cleared override stops where it is; it does not glide on."""
    audio = TempoAudio()
    game = make_game(audio)
    start_held(game, clock, change_due=False)
    if owner == "ramp_tempo":
        game.tempo_ramp = RampDescriptor(SLOW_MUSIC_SPEED, FAST_MUSIC_SPEED, T0, 12.0, RAMP_CURVE_LINEAR)
    else:
        game.tempo_override = FAST_MUSIC_SPEED  # 3 s ramp at the up flag
    await game._check_music_speed()

    clock.now = T0 + (6.0 if owner == "ramp_tempo" else 0.3)
    held = game.tempo_intensity
    assert 0.0 < held < 1.0
    game.tempo_ramp = None
    game.tempo_override = None
    await game._check_music_speed()

    assert audio.tempo_calls[-1] == (pytest.approx(1.0 + 0.3 * held, abs=1e-4), base_module.MIN_GLIDE_SECONDS)
    clock.now = T0 + 120.0  # long after the old target time; the window is not due
    await game._check_music_speed()
    assert game.tempo_intensity == pytest.approx(held)
    assert len(audio.tempo_calls) == 2  # the agent's glide and the hold, nothing more


@pytest.mark.asyncio
@pytest.mark.usefixtures("flags")
@pytest.mark.uc("UC-031", flow="A3", br=["BR-005"])
async def test_a_ramp_adopted_late_starts_from_the_intensity_now(clock):
    """BR-005: a ramp_tempo armed under an override and adopted after it clears does not jump."""
    audio = TempoAudio()
    game = make_game(audio)
    start_held(game, clock, change_due=False)
    game.tempo_override = SLOW_MUSIC_SPEED  # holds at i = 0
    game.tempo_ramp = RampDescriptor(SLOW_MUSIC_SPEED, FAST_MUSIC_SPEED, T0, 2.0, RAMP_CURVE_LINEAR)
    await game._check_music_speed()

    clock.now = T0 + 10.0  # the descriptor's end has long passed
    game.tempo_override = None
    await game._check_music_speed()

    assert game.tempo_intensity == 0.0  # no jump to the descriptor's target
    assert audio.tempo_calls == [(FAST_MUSIC_SPEED, 3.0)]  # bounded rate: the up flag
    clock.now = T0 + 11.5
    assert game.tempo_intensity == pytest.approx(0.5)


@pytest.mark.asyncio
@pytest.mark.usefixtures("clock", "flags")
@pytest.mark.uc("UC-031", flow="main", steps=[3], cond=["Post-F-1"])
async def test_a_failing_tempo_tick_does_not_end_the_music_loop():
    """A non-audio error in one tick is logged; the loop runs the next tick."""
    game = make_game(TempoAudio())
    game.running = True
    game.game_cycle_context = None
    ticks = []

    async def tick():
        ticks.append(len(ticks))
        if len(ticks) == 1:
            raise RuntimeError("pacing seam failed")
        game.running = False

    with patch.object(game, "_check_music_speed", side_effect=tick):
        await asyncio.wait_for(game._music_loop(), timeout=5.0)

    assert ticks == [0, 1]


@pytest.mark.uc("UC-031", flow="main", steps=[3], cond=["Post-F-1"])
def test_persistent_tick_failure_logs_once_then_periodic_summaries(clock, caplog):
    """#1841: 100 ms failing ticks log 1 warning, a summary per interval, and a new error at once."""
    game = make_game(TempoAudio())
    warnings = lambda: [r.getMessage() for r in caplog.records if "Tempo tick failed" in r.getMessage()]  # noqa: E731
    with caplog.at_level("WARNING"):
        for _ in range(40):  # 4 s of ticks, below the interval
            game._log_tick_failure(RuntimeError("boom"))
            clock.now += 0.1
        assert len(warnings()) == 1

        clock.now += 5.0
        game._log_tick_failure(RuntimeError("boom"))
        assert len(warnings()) == 2
        assert "39 similar suppressed" in warnings()[1]

        game._log_tick_failure(RuntimeError("other"))  # new text logs at once
        assert len(warnings()) == 3
        assert "suppressed" not in warnings()[2]


@pytest.mark.asyncio
@pytest.mark.usefixtures("clock", "flags")
@pytest.mark.uc("UC-031", flow="main", steps=[3], cond=["Post-F-1"])
async def test_flapping_tick_failure_keeps_the_log_window_and_new_error_resets_count(caplog):
    """#1841: fail/ok/fail/ok/fail inside the interval logs once; a new error logs at once with no count."""
    game = make_game(TempoAudio())
    game.running = True
    game.game_cycle_context = None
    outcomes = [RuntimeError("a"), None, RuntimeError("a"), None, RuntimeError("a"), RuntimeError("b")]

    async def tick():
        outcome = outcomes.pop(0)
        if not outcomes:
            game.running = False
        if outcome is not None:
            raise outcome

    with caplog.at_level("WARNING"), patch.object(game, "_check_music_speed", side_effect=tick):
        await asyncio.wait_for(game._music_loop(), timeout=5.0)

    msgs = [r.getMessage() for r in caplog.records if "Tempo tick failed" in r.getMessage()]
    assert len(msgs) == 2  # "a" once despite the ok ticks, then "b" at once
    assert "RuntimeError('a')" in msgs[0]
    assert "RuntimeError('b')" in msgs[1]
    assert "suppressed" not in msgs[1]
