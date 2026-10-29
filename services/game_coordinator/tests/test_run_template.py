"""Every game mode goes through the one base run() template (#2359).

A mode changes its flow through the run() hooks, never by overriding run():
the copies of run() in four modes had drifted from the base (#2357).
"""

import pytest

from lib.types import Games
from services.game_coordinator.game_factory import _GAME_CLASSES
from services.game_coordinator.games.base import BaseGameMode
from services.game_coordinator.games.streamed_intro import StreamedIntroGameMode

STREAMED_INTRO_MODES = {Games.FightClub, Games.Zombies, Games.Werewolf, Games.Tournament}


@pytest.mark.parametrize("mode", list(_GAME_CLASSES), ids=[m.name for m in _GAME_CLASSES])
@pytest.mark.uc("UC-020", flow="main")
def test_mode_runs_the_base_run_template(mode):
    """UC-020 main: no mode (or intermediate base) overrides run()."""
    cls = _GAME_CLASSES[mode]
    assert cls.run is BaseGameMode.run
    overriders = [klass.__name__ for klass in cls.__mro__ if klass is not BaseGameMode and "run" in vars(klass)]
    assert overriders == []


@pytest.mark.parametrize("mode", list(_GAME_CLASSES), ids=[m.name for m in _GAME_CLASSES])
@pytest.mark.uc("UC-020", flow="main")
def test_streamed_intro_flow_is_exactly_the_intro_first_modes(mode):
    """UC-020 main: Fight Club, Zombies, Werewolf and Tournament run the streamed-intro hooks, each in its own span."""
    cls = _GAME_CLASSES[mode]
    is_streamed = issubclass(cls, StreamedIntroGameMode)
    assert is_streamed == (mode in STREAMED_INTRO_MODES)
    if is_streamed:
        assert cls.RUN_SPAN_NAME.endswith("_game")
        assert cls.RUN_SPAN_NAME != StreamedIntroGameMode.RUN_SPAN_NAME
