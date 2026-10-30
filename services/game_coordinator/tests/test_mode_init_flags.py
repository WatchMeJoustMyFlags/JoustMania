"""Mode-specific init flags: one validated, immutable snapshot per mode, read once (#2359)."""

import dataclasses
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_duration_flags import (  # noqa: E402
    _make_fight_club,
    _make_nonstop,
    _make_tournament,
    _make_traitor,
    _make_werewolf,
    _make_zombie,
)

from services.game_coordinator.games import fight_club, nonstop_joust, tournament, traitor, werewolf, zombie


def _read_keys(mock):
    return [c.args[1] for c in mock.call_args_list]


def test_werewolf_snapshot_read_once_and_frozen():
    with (
        patch.object(werewolf, "read_object_flag", return_value={}) as objects,
        patch.object(werewolf, "read_float_flag", return_value=0.5) as floats,
    ):
        game = _make_werewolf()
    assert _read_keys(objects) == ["werewolf.thresholds"]
    assert _read_keys(floats) == ["werewolf.werewolf_fraction"]
    assert game.werewolf_fraction == game.mode_flags.werewolf_fraction == 0.5
    with pytest.raises(dataclasses.FrozenInstanceError):
        game.mode_flags.werewolf_fraction = 0.1


def test_werewolf_from_raw_malformed_gives_defaults():
    flags = werewolf.WerewolfInitFlags.from_raw(thresholds="junk", werewolf_fraction=1.5)
    assert dict(flags.thresholds) == werewolf.WEREWOLF_THRESHOLDS
    assert flags.werewolf_fraction == werewolf.WEREWOLF_FRACTION


def test_nonstop_snapshot_read_once_and_frozen():
    with (
        patch.object(nonstop_joust, "read_object_flag", return_value={"base": 7, "death_penalty": 2}) as objects,
        patch.object(nonstop_joust, "read_float_flag", side_effect=[4.0, 1.0]) as floats,
    ):
        game = _make_nonstop()
    assert _read_keys(objects) == ["nonstop.scoring"]
    assert _read_keys(floats) == ["nonstop.respawn_seconds", "nonstop.spawn_protection_seconds"]
    assert (game.respawn_duration, game.spawn_protection_duration) == (4.0, 1.0)
    assert (game.score_base, game.score_death_penalty) == (7, 2)
    with pytest.raises(dataclasses.FrozenInstanceError):
        game.mode_flags.score_base = 1


def test_nonstop_from_raw_malformed_gives_defaults():
    flags = nonstop_joust.NonstopInitFlags.from_raw(respawn_seconds=-1, spawn_protection_seconds="x", scoring=[1])
    assert flags.respawn_seconds == nonstop_joust.RESPAWN_DURATION
    assert flags.spawn_protection_seconds == nonstop_joust.SPAWN_PROTECTION_DURATION
    assert (flags.score_base, flags.score_death_penalty) == (
        nonstop_joust.SCORE_BASE,
        nonstop_joust.SCORE_DEATH_PENALTY,
    )


def test_fight_club_snapshot_read_once_and_frozen():
    with patch.object(fight_club, "read_float_flag", return_value=30.0) as floats:
        game = _make_fight_club()
    assert _read_keys(floats) == ["fight_club.round_seconds"]
    assert game._round_duration == game.mode_flags.round_seconds == 30.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        game.mode_flags.round_seconds = 1.0


def test_fight_club_from_raw_zero_round_falls_back():
    assert fight_club.FightClubInitFlags.from_raw(round_seconds=0.0).round_seconds == fight_club.ROUND_DURATION


def test_tournament_snapshot_read_once_and_frozen():
    with patch.object(tournament, "read_float_flag", side_effect=[50.0, 0.0]) as floats:
        game = _make_tournament()
    assert _read_keys(floats) == ["tournament.match_seconds", "tournament.time_between_matches_seconds"]
    assert (game._match_duration, game._time_between_matches) == (50.0, 0.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        game.mode_flags.match_seconds = 1.0


def test_tournament_from_raw_zero_match_falls_back_zero_pause_allowed():
    flags = tournament.TournamentInitFlags.from_raw(match_seconds=0.0, time_between_matches_seconds=0.0)
    assert flags.match_seconds == tournament.MATCH_DURATION
    assert flags.time_between_matches_seconds == 0.0


def test_zombie_snapshot_read_once_and_frozen():
    with (
        patch.object(zombie, "read_object_flag", return_value={}) as objects,
        patch.object(zombie, "read_int_flag", return_value=3) as ints,
        patch.object(zombie, "read_float_flag", side_effect=[1.0, 6.0]) as floats,
    ):
        game = _make_zombie()
    assert _read_keys(objects) == ["zombie.thresholds"]
    assert _read_keys(ints) == ["zombie.initial_count"]
    assert _read_keys(floats) == ["zombie.respawn_min_seconds", "zombie.respawn_max_seconds"]
    assert (game.initial_zombies, game.zombie_respawn_min, game.zombie_respawn_max) == (3, 1.0, 6.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        game.mode_flags.initial_count = 9


def test_zombie_from_raw_malformed_gives_defaults():
    flags = zombie.ZombieInitFlags.from_raw(
        thresholds=None, initial_count=0, respawn_min_seconds=9.0, respawn_max_seconds=2.0
    )
    assert dict(flags.thresholds) == zombie.ZOMBIE_THRESHOLDS
    assert flags.initial_count == zombie.INITIAL_ZOMBIES
    assert (flags.respawn_min_seconds, flags.respawn_max_seconds) == (
        zombie.ZOMBIE_RESPAWN_MIN,
        zombie.ZOMBIE_RESPAWN_MAX,
    )


def test_traitor_snapshot_read_once_and_frozen():
    custom = {"tiers": [{"max_players": 3, "count": 1}], "fallback_divisor": 4}
    with patch.object(traitor, "read_object_flag", return_value=custom) as objects:
        game = _make_traitor()
    assert _read_keys(objects) == ["traitor.count_tiers"]
    assert game._count_fallback_divisor == game.mode_flags.count_fallback_divisor == 4
    assert game._count_tiers == [{"max_players": 3, "count": 1}]
    with pytest.raises(dataclasses.FrozenInstanceError):
        game.mode_flags.count_fallback_divisor = 1


def test_traitor_from_raw_malformed_gives_defaults():
    flags = traitor.TraitorInitFlags.from_raw(count_tiers={"tiers": [], "fallback_divisor": 2})
    assert [dict(t) for t in flags.count_tiers] == traitor.DEFAULT_TRAITOR_COUNT_TIERS
    assert flags.count_fallback_divisor == traitor.DEFAULT_TRAITOR_FALLBACK_DIVISOR
