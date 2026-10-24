"""Cue pacing of the operator pad (#2105, UC-015 BR-009)."""

from unittest.mock import AsyncMock

import pytest

from services.operator_pad.cues import LIMIT_CUE, LIMIT_CUE_MIN_GAP_SECONDS, REFUSAL_CUE, CuePlayer


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
