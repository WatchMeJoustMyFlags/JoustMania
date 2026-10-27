"""Cue pacing of the operator pad (#2105, UC-015 BR-009)."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from lib.types import Sound
from proto import audio_pb2
from services.operator_pad.cues import LIMIT_CUE_MIN_GAP_SECONDS, LIMIT_CUES, REFUSAL_CUE, CuePlayer
from services.operator_pad.tests.fakes import GatedSleep

LIMIT_CUE = LIMIT_CUES[True]


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def played(stub) -> list[str]:
    return [c.args[0].file_path for c in stub.PlaySound.await_args_list]


@pytest.fixture
def stub():
    audio = AsyncMock()
    audio.PlaySound = AsyncMock()
    return audio


@pytest.mark.uc("UC-015", flow="A7", br=["BR-009"])
async def test_a_burst_of_limit_refusals_plays_one_limit_cue(stub):
    clock = FakeClock()
    cues = CuePlayer(stub, clock=clock)
    for _ in range(14):  # the hardware check's 14 at_limit refusals in a row
        await cues.limit()
        clock.now += 0.1
    assert played(stub) == [LIMIT_CUE.value, LIMIT_CUE.value]  # 1.4 s of refusals: one cue per second


@pytest.mark.uc("UC-015", flow="A7", br=["BR-009"])
async def test_the_limit_cue_plays_again_after_the_gap(stub):
    clock = FakeClock()
    cues = CuePlayer(stub, clock=clock)
    await cues.limit()
    clock.now += LIMIT_CUE_MIN_GAP_SECONDS - 0.01
    await cues.limit()
    clock.now += 0.01
    await cues.limit()
    assert played(stub) == [LIMIT_CUE.value, LIMIT_CUE.value]


@pytest.mark.uc("UC-015", flow="A1", br=["BR-009"])
async def test_other_cues_are_not_paced_by_the_limit_gap(stub):
    cues = CuePlayer(stub, clock=FakeClock())
    await cues.limit()
    await cues.refusal()
    await cues.refusal()
    assert played(stub) == [LIMIT_CUE.value, REFUSAL_CUE.value, REFUSAL_CUE.value]


# --- words, labels, background sequences (#2106, BR-009, BR-011) ------------------------------


@pytest.mark.uc("UC-015", flow="A7", br=["BR-009"])
async def test_the_limit_word_follows_the_direction(stub):
    clock = FakeClock()
    cues = CuePlayer(stub, clock=clock)
    await cues.limit(True)
    clock.now += LIMIT_CUE_MIN_GAP_SECONDS
    await cues.limit(False)
    assert played(stub) == [Sound.MENU_VOX_STATUS_MAXIMUM.value, Sound.MENU_VOX_STATUS_MINIMUM.value]


@pytest.mark.uc("UC-015", flow="A1", br=["BR-009"])
@pytest.mark.parametrize(
    ("cue", "word"),
    [
        ("refusal", Sound.MENU_VOX_STATUS_NOT_POSSIBLE),
        ("cancel", Sound.MENU_VOX_STATUS_CANCELLED),
    ],
)
async def test_status_cues_are_words(stub, cue, word):
    await getattr(CuePlayer(stub), cue)()
    assert played(stub) == [word.value]


@pytest.mark.uc("UC-015", flow="A6", br=["BR-009"])
async def test_lock_directions_say_pad_locked_and_unlocked(stub):
    cues = CuePlayer(stub)
    await cues.lock(True)
    await cues.lock(False)
    assert played(stub) == [Sound.MENU_VOX_PAD_LOCKED.value, Sound.MENU_VOX_PAD_UNLOCKED.value]


@pytest.mark.uc("UC-015", flow="A3", br=["BR-009"])
async def test_failure_is_two_beeps_then_no_connection_in_the_background(stub):
    sleep = GatedSleep()
    cues = CuePlayer(stub, sleep=sleep)
    await cues.failure()  # returns at once: the sequence runs as a task
    await asyncio.sleep(0)
    assert played(stub) == [Sound.SFX_BEEP_LOUD.value]
    sleep.gate.set()
    await cues.drain()
    assert played(stub) == [
        Sound.SFX_BEEP_LOUD.value,
        Sound.SFX_BEEP_LOUD.value,
        Sound.MENU_VOX_STATUS_NO_CONNECTION.value,
    ]


@pytest.mark.uc("UC-015", br=["BR-011"])
async def test_announce_plays_the_label_then_the_value_after_the_label_ends(stub):
    stub.PlaySound.return_value = audio_pb2.PlaySoundResponse(success=True, duration_seconds=0.7)
    sleep = GatedSleep()
    cues = CuePlayer(stub, sleep=sleep)
    await cues.announce(Sound.MENU_VOX_LABEL_BACKEND, Sound.MENU_VOX_BACKEND_CLAUDE)
    await asyncio.sleep(0)
    assert played(stub) == [Sound.MENU_VOX_LABEL_BACKEND.value]  # the value waits for the label
    assert sleep.calls == [pytest.approx(0.7)]
    sleep.gate.set()
    await cues.drain()
    assert played(stub) == [Sound.MENU_VOX_LABEL_BACKEND.value, Sound.MENU_VOX_BACKEND_CLAUDE.value]


@pytest.mark.uc("UC-015", br=["BR-011"])
async def test_announce_with_no_label_length_plays_the_value_at_once(stub):
    stub.PlaySound.side_effect = [TimeoutError("audio down"), audio_pb2.PlaySoundResponse(success=True)]
    sleep = GatedSleep()
    cues = CuePlayer(stub, sleep=sleep)
    await cues.announce(Sound.MENU_VOX_LABEL_TEAMS, Sound.MENU_VOX_ADMINOP_3)
    await cues.drain()
    assert played(stub) == [Sound.MENU_VOX_LABEL_TEAMS.value, Sound.MENU_VOX_ADMINOP_3.value]
    assert sleep.calls == []
