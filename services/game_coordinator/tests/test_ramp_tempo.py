"""
Unit tests for ramp_tempo (#1117, #1103 MVP action 2): a scheduled tempo CURVE
that fills the #1114 ``agent``-mode tempo seam.

Covers:
- parse_ramp_tempo_value: valid parse + bounds (target clamp, seconds cap, curve
  vocabulary) and MALFORMED -> None (safe no-op, never dispatches garbage).
- RampDescriptor.tempo_at: linear + ease interpolation reaches target over the
  duration and HOLDS at target once complete.
- handle_ramp_tempo: arms a descriptor anchored to the live tempo/clock; malformed
  value is a no-op (descriptor unchanged).
- _check_music_speed: drives the live tempo toward target via the SINGLE tempo
  owner (ChangeTempo), holds at target, and suspends the natural schedule while a
  ramp is active (no second writer).
- handle_ramp_tempo_revert: drops the descriptor so the natural schedule resumes.
- Agent-mode integration: with tempo_schedule_mode=agent the ramp drives pacing.
- interventions.json / agent.json flag JSON is well-formed.

Telemetry is disabled via conftest.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService, async_noop

from services.game_coordinator.difficulty_handlers import (
    handle_ramp_tempo,
    handle_ramp_tempo_revert,
    register_difficulty_handlers,
)
from services.game_coordinator.games.base import (
    FAST_MUSIC_SPEED,
    RAMP_CURVE_EASE,
    RAMP_CURVE_LINEAR,
    RAMP_TEMPO_MAX_SECONDS,
    SLOW_MUSIC_SPEED,
    TEMPO_MODE_AGENT,
    Player,
    RampDescriptor,
    parse_ramp_tempo_value,
)
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    InterventionContext,
    InterventionManager,
)

INTERVENTIONS_JSON = project_root / "services" / "flagd" / "interventions.json"
INTERVENTIONS_CI_JSON = project_root / "services" / "flagd" / "ci" / "interventions.json"
AGENT_JSON = project_root / "services" / "flagd" / "agent.json"
AGENT_CI_JSON = project_root / "services" / "flagd" / "ci" / "agent.json"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def make_game(sensitivity=2, num_players=2):
    mock_cm = MockControllerManagerService(num_controllers=num_players)
    mock_audio = AsyncMock()
    game = FFAGame(
        controller_manager_client=mock_cm,
        event_publisher=async_noop,
        audio_client=mock_audio,
        game_id="test_ramp",
        sensitivity=sensitivity,
    )
    game.music_track_id = "track_123"
    game.music_speed = SLOW_MUSIC_SPEED
    game.speed_up = True
    game.gameplay_span = None
    for i in range(num_players):
        s = f"p{i + 1}"
        game.players[s] = Player(serial=s, alive=True, color=(255, 0, 0))
    return game


def _spec(flag_key):
    return next(s for s in INTERVENTION_SPECS if s.flag_key == flag_key)


def make_ctx(value, game):
    return InterventionContext(
        spec=_spec("tempo_ramp"),
        value=value,
        payload="",
        target_serial=None,
        game=game,
        objective="balanced",
    )


# --------------------------------------------------------------------------- #
# parse_ramp_tempo_value
# --------------------------------------------------------------------------- #


def test_parse_valid_linear():
    d = parse_ramp_tempo_value("1.3:8:linear")
    assert d is not None
    assert d.target_tempo == pytest.approx(1.3)
    assert d.duration == pytest.approx(8.0)
    assert d.curve == RAMP_CURVE_LINEAR


def test_parse_valid_ease_with_whitespace():
    d = parse_ramp_tempo_value("  1.1 : 5 : EASE ")
    assert d is not None
    assert d.target_tempo == pytest.approx(1.1)
    assert d.curve == RAMP_CURVE_EASE


def test_parse_clamps_target_to_tempo_range():
    assert parse_ramp_tempo_value("9.9:5:linear").target_tempo == pytest.approx(FAST_MUSIC_SPEED)
    assert parse_ramp_tempo_value("0.1:5:linear").target_tempo == pytest.approx(SLOW_MUSIC_SPEED)


def test_parse_caps_duration():
    assert parse_ramp_tempo_value("1.2:9999:linear").duration == pytest.approx(RAMP_TEMPO_MAX_SECONDS)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "none",
        "NONE",
        "1.3:8",  # too few fields
        "1.3:8:linear:extra",  # too many fields
        "1.3:8:spiral",  # unknown curve
        "abc:8:linear",  # non-numeric target
        "1.3:xyz:linear",  # non-numeric seconds
        "1.3:0:linear",  # non-positive seconds
        "1.3:-5:linear",  # negative seconds
        "nan:8:linear",  # non-finite target
        "1.3:inf:linear",  # non-finite seconds
        42,  # non-string
    ],
)
def test_parse_malformed_returns_none(bad):
    assert parse_ramp_tempo_value(bad) is None


# --------------------------------------------------------------------------- #
# RampDescriptor interpolation
# --------------------------------------------------------------------------- #


def test_linear_interpolation_reaches_target_over_duration():
    d = RampDescriptor(start_tempo=1.0, target_tempo=1.3, start_time=100.0, duration=10.0, curve=RAMP_CURVE_LINEAR)
    assert d.tempo_at(100.0) == pytest.approx(1.0)  # start
    assert d.tempo_at(105.0) == pytest.approx(1.15)  # midpoint, linear
    assert d.tempo_at(110.0) == pytest.approx(1.3)  # end
    assert d.tempo_at(200.0) == pytest.approx(1.3)  # held past end
    assert d.tempo_at(50.0) == pytest.approx(1.0)  # before start
    assert d.is_complete(110.0)
    assert not d.is_complete(105.0)


def test_ease_interpolation_is_smoothstep():
    d = RampDescriptor(start_tempo=1.0, target_tempo=1.3, start_time=0.0, duration=10.0, curve=RAMP_CURVE_EASE)
    # smoothstep hits the endpoints exactly and the midpoint equals linear at 0.5.
    assert d.tempo_at(0.0) == pytest.approx(1.0)
    assert d.tempo_at(10.0) == pytest.approx(1.3)
    assert d.tempo_at(5.0) == pytest.approx(1.15)
    # Early on it is slower than linear (ease-in): at 25% progress smoothstep<0.25.
    linear_at_quarter = 1.0 + 0.3 * 0.25
    assert d.tempo_at(2.5) < linear_at_quarter


# --------------------------------------------------------------------------- #
# handle_ramp_tempo / revert
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_handler_arms_descriptor_anchored_to_live_state():
    game = make_game()
    game.music_speed = 1.05
    with patch("services.game_coordinator.difficulty_handlers.time.time", return_value=500.0):
        await handle_ramp_tempo(make_ctx("1.3:8:linear", game))
    assert game.tempo_ramp is not None
    assert game.tempo_ramp.start_tempo == pytest.approx(1.05)  # anchored to live tempo
    assert game.tempo_ramp.start_time == pytest.approx(500.0)
    assert game.tempo_ramp.target_tempo == pytest.approx(1.3)


@pytest.mark.asyncio
async def test_handler_malformed_is_noop():
    game = make_game()
    game.tempo_ramp = None
    await handle_ramp_tempo(make_ctx("garbage", game))
    assert game.tempo_ramp is None  # unchanged: no garbage dispatched


@pytest.mark.asyncio
async def test_handler_no_game_does_not_raise():
    await handle_ramp_tempo(make_ctx("1.3:8:linear", None))  # must not raise


@pytest.mark.asyncio
async def test_revert_drops_descriptor():
    game = make_game()
    game.tempo_ramp = RampDescriptor(1.0, 1.3, 0.0, 8.0, RAMP_CURVE_LINEAR)
    await handle_ramp_tempo_revert(make_ctx("none", game))
    assert game.tempo_ramp is None


@pytest.mark.asyncio
async def test_revert_no_game_does_not_raise():
    await handle_ramp_tempo_revert(make_ctx("none", None))


# --------------------------------------------------------------------------- #
# _check_music_speed drives the ramp (single tempo owner)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_check_music_speed_drives_ramp_toward_target_and_holds():
    game = make_game()
    game.music_speed = 1.0
    base = 1000.0
    game.tempo_ramp = RampDescriptor(
        start_tempo=1.0, target_tempo=1.3, start_time=base, duration=10.0, curve=RAMP_CURVE_LINEAR
    )

    # Midway through the ramp the loop applies the interpolated tempo.
    with patch("services.game_coordinator.games.base.time.time", return_value=base + 5.0):
        await game._check_music_speed()
    assert game.music_speed == pytest.approx(1.15)
    game.audio_client.ChangeTempo.assert_called_once()

    # Past the end it holds exactly at target...
    game.audio_client.ChangeTempo.reset_mock()
    with patch("services.game_coordinator.games.base.time.time", return_value=base + 50.0):
        await game._check_music_speed()
    assert game.music_speed == pytest.approx(1.3)
    game.audio_client.ChangeTempo.assert_called_once()

    # ...and a further tick at target is a no-op (already there, no churn).
    game.audio_client.ChangeTempo.reset_mock()
    with patch("services.game_coordinator.games.base.time.time", return_value=base + 60.0):
        await game._check_music_speed()
    game.audio_client.ChangeTempo.assert_not_called()


@pytest.mark.asyncio
async def test_active_ramp_suspends_natural_schedule():
    """While a ramp holds at target, the natural slow/fast schedule must not fire."""
    game = make_game()
    game.music_speed = 1.3
    base = 2000.0
    game.tempo_ramp = RampDescriptor(1.0, 1.3, base, 5.0, RAMP_CURVE_LINEAR)
    game.change_time = base  # would normally trigger a scheduled change
    with patch("services.game_coordinator.games.base.time.time", return_value=base + 100.0):
        await game._check_music_speed()
    # Held at target; the scheduled slow/fast hop did NOT run (no speed_up flip).
    assert game.music_speed == pytest.approx(1.3)
    game.audio_client.ChangeTempo.assert_not_called()


@pytest.mark.asyncio
async def test_override_wins_over_ramp():
    """A hard tempo_override takes precedence over a ramp (existing precedence)."""
    game = make_game()
    game.music_speed = 1.0
    game.tempo_override = 1.2
    game.tempo_ramp = RampDescriptor(1.0, 1.3, 0.0, 5.0, RAMP_CURVE_LINEAR)
    await game._check_music_speed()
    assert game.music_speed == pytest.approx(1.2)  # override target, not ramp


# --------------------------------------------------------------------------- #
# Agent-mode integration (#1114 seam)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ramp_drives_pacing_in_agent_mode():
    """With tempo_schedule_mode=agent the ramp drives the live tempo curve."""
    game = make_game()
    game.tempo_schedule_mode = TEMPO_MODE_AGENT
    game.music_speed = 1.0
    base = 3000.0
    with patch("services.game_coordinator.difficulty_handlers.time.time", return_value=base):
        await handle_ramp_tempo(make_ctx("1.3:8:linear", game))
    with patch("services.game_coordinator.games.base.time.time", return_value=base + 4.0):
        await game._check_music_speed()
    assert game.music_speed == pytest.approx(1.15)  # halfway up the 8s linear ramp


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_register_wires_handler_and_revert():
    mgr = InterventionManager(event_publisher=async_noop, get_game=lambda: None)
    register_difficulty_handlers(mgr)
    assert mgr._handlers["tempo_ramp"] is handle_ramp_tempo
    assert mgr._revert_handlers["tempo_ramp"] is handle_ramp_tempo_revert


# --------------------------------------------------------------------------- #
# Spec + flag JSON well-formed
# --------------------------------------------------------------------------- #


def test_spec_registered():
    spec = _spec("tempo_ramp")
    assert spec.type_id == "ramp_tempo"
    assert spec.value_kind == "string"
    assert spec.none_value == "none"
    assert not spec.edge_triggered
    assert not spec.player_targeted


@pytest.mark.parametrize("path", [INTERVENTIONS_JSON, INTERVENTIONS_CI_JSON])
def test_interventions_json_well_formed(path):
    data = json.loads(path.read_text())
    flag = data["flags"]["tempo_ramp"]
    assert flag["state"] == "ENABLED"
    assert flag["defaultVariant"] == "none"
    assert flag["variants"]["none"] == "none"
    # Every non-neutral seed variant must itself parse to a valid descriptor.
    for name, value in flag["variants"].items():
        if name == "none":
            continue
        assert parse_ramp_tempo_value(value) is not None, f"{path}:{name}={value}"


@pytest.mark.parametrize("path", [AGENT_JSON, AGENT_CI_JSON])
def test_ramp_tempo_shadow_only_in_agent_json(path):
    data = json.loads(path.read_text())
    variants = data["flags"]["interventions_allowed"]["variants"]

    # interventions_allowed migrated from a LIST flag to a STRING flag of
    # comma-separated ids (#1127, the flagd RPC list-flag trap). Parse either
    # shape into a set of ids so the membership assertion is exact (not a
    # substring match that could false-positive on shared prefixes).
    def ids(variant):
        if isinstance(variant, str):
            return {tok.strip() for tok in variant.split(",") if tok.strip()}
        return set(variant)

    assert "ramp_tempo" in ids(variants["shadow_experimental"])
    for real in ("ambient", "standard", "full"):
        assert "ramp_tempo" not in ids(variants[real]), f"{path}: {real} must not include ramp_tempo"
