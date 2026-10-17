"""
Flag value -> InterventionManager -> registered handler -> real game (#1422 step 6).

These tests replace the docker-compose scenarios of
``tests/integration/test_parallel_intervention_flow.py`` and
``tests/integration/test_f6_flow.py``. Those scenarios proved coordinator
application logic (change detection, nonce exactly-once, revert paths, the
allow-list and mode matrix) through the whole stack; the part only a real flagd
can show (gameId / targetingKey scoping, in-place reloads) lives in the flagd
contract tier (tests/contract/test_flagd_resolution.py).

Each test drives ``InterventionManager.evaluate_all()`` with an in-memory flag
client, the handlers the servicer registers (``register_lifecycle_handlers`` /
``register_difficulty_handlers``) and a real game object, then asserts both the
published ``agent_intervention`` events and the effect on the game.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

test_dir = Path(__file__).parent
project_root = test_dir.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService, async_noop
from openfeature.flag_evaluation import FlagEvaluationDetails

from lib.types import GameEvent
from services.game_coordinator import difficulty_handlers as dh
from services.game_coordinator.difficulty_handlers import register_difficulty_handlers
from services.game_coordinator.games.base import Player, resolve_music_windows
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame
from services.game_coordinator.interventions import (
    BLOCK_MODE_UNSUPPORTED,
    BLOCK_NOT_ALLOWED,
    INTERVENTION_SPECS,
    MODE_CAPABILITY_DENY,
    InterventionManager,
)
from services.game_coordinator.lifecycle_handlers import register_lifecycle_handlers

ALL_TYPES = ",".join(sorted(spec.type_id for spec in INTERVENTION_SPECS))
FLAGD_DIR = project_root / "services" / "flagd"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class PerSerialFlagClient:
    """In-memory interventions domain.

    A value is either a scalar (served to everyone) or a ``{serial: value}``
    dict resolved by ``targeting_key`` like flagd's per-serial ladder; serials
    without an entry get the caller's default (the neutral variant).
    """

    def __init__(self, values=None):
        self.values = dict(values or {})

    def _resolve(self, key, default, ctx):
        value = self.values.get(key, default)
        if isinstance(value, dict):
            serial = getattr(ctx, "targeting_key", None) if ctx is not None else None
            return value.get(serial, default)
        return value

    def get_string_value(self, key, default, ctx=None):
        return str(self._resolve(key, default, ctx))

    def get_integer_value(self, key, default, ctx=None):
        return int(self._resolve(key, default, ctx))

    def get_float_value(self, key, default, ctx=None):
        return float(self._resolve(key, default, ctx))

    def get_object_value(self, key, default, ctx=None):
        return self._resolve(key, default, ctx)

    def get_string_details(self, key, default, ctx=None):
        return FlagEvaluationDetails(flag_key=key, value=self.get_string_value(key, default, ctx))

    def get_float_details(self, key, default, ctx=None):
        return FlagEvaluationDetails(flag_key=key, value=self.get_float_value(key, default, ctx))


class AgentStub:
    """Agent domain: a fixed allow-list, generous backstop, no battery gate."""

    def __init__(self, allowed: str = ALL_TYPES):
        self.allowed = allowed

    def get_integer_value(self, key, default, _ctx=None):
        if key == "policy.coordinator_backstop_per_minute":
            return 1000
        if key == "policy.battery_threshold":
            return 0
        return default

    def get_float_value(self, _key, default, _ctx=None):
        return default

    def get_string_value(self, key, default, _ctx=None):
        if key == "interventions_allowed":
            return self.allowed
        return default

    def get_object_value(self, _key, default, _ctx=None):
        return default


class RecordingGameplayStream:
    def __init__(self):
        self.writes = []

    async def write(self, message):
        self.writes.append(message)


def make_ffa_game(num_players: int = 4) -> FFAGame:
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=num_players),
        event_publisher=async_noop,
        audio_client=AsyncMock(),
        game_id="test_flag_to_effect",
    )
    game.gameplay_stream = RecordingGameplayStream()
    for i in range(num_players):
        serial = f"p{i + 1}"
        game.players[serial] = Player(serial=serial, alive=True, color=(255, 0, 0))
    return game


def make_manager(game, interventions=None, allowed: str = ALL_TYPES):
    """A manager on the legacy single-game path, with the servicer's handlers."""
    events = []

    async def publisher(event_type, data):
        events.append((event_type, data))

    mgr = InterventionManager(event_publisher=publisher, get_game=lambda: game)
    mgr._interventions_client = PerSerialFlagClient(interventions)
    mgr._agent_client = AgentStub(allowed)
    mgr._rate_limiter.set_budget(1000.0)
    register_lifecycle_handlers(mgr)
    register_difficulty_handlers(mgr)
    return mgr, events


def published(events, type_id, blocked=None):
    out = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == type_id]
    if blocked is not None:
        out = [d for d in out if d["blocked"] == blocked]
    return out


async def evaluate(mgr):
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()


def _shipped_window_presets() -> dict:
    """The calm / frantic ``windows`` variants of the committed game.json."""
    variants = json.loads((FLAGD_DIR / "game.json").read_text())["flags"]["windows"]["variants"]
    return {name: variants[name] for name in ("calm", "frantic")}


def shipped_allow_list(variant: str, flag_dir: Path) -> str:
    doc = json.loads((flag_dir / "agent.json").read_text())
    return doc["flags"]["interventions_allowed"]["variants"][variant]


# --------------------------------------------------------------------------- #
# Edge flags: eliminate exactly once, a fresh nonce applies again
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="main", steps=[13], br=["BR-006"])
@pytest.mark.uc("UC-020", flow="A3")
async def test_eliminate_nonce_kills_once_and_fresh_nonce_kills_again():
    """The same eliminate nonce read again does not kill again; a new nonce for
    another player does (was _scenario_eliminate_exactly_once)."""
    game = make_ffa_game()
    mgr, events = make_manager(game, {"eliminate_player": "n1:p1"})

    await evaluate(mgr)
    assert game.players["p1"].alive is False
    assert len(published(events, "eliminate_player", blocked="false")) == 1

    await evaluate(mgr)  # same nonce served again (a flagd reload)
    assert len(published(events, "eliminate_player", blocked="false")) == 1
    assert game.players["p2"].alive is True

    mgr._interventions_client.values["eliminate_player"] = "n2:p2"
    await evaluate(mgr)
    applied = published(events, "eliminate_player", blocked="false")
    assert [d["target"] for d in applied] == ["p1", "p2"]
    assert game.players["p2"].alive is False
    assert game.players["p3"].alive is True and game.players["p4"].alive is True


# --------------------------------------------------------------------------- #
# Mode matrix on the real game names
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A22", br=["BR-004"])
def test_mode_matrix_keys_match_real_game_names():
    """The capability matrix is keyed by get_game_name(): revive is denied for the
    real FFA game and not for the real respawn mode, so a renamed mode cannot
    silently fall out of (or into) the matrix."""
    ffa = make_ffa_game(num_players=2)
    nonstop = NonstopJoustGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=async_noop,
        audio_client=None,
        game_id="test_flag_to_effect_nonstop",
    )
    assert "revive_player" in MODE_CAPABILITY_DENY[ffa.get_game_name()]
    assert "revive_player" not in MODE_CAPABILITY_DENY.get(nonstop.get_game_name(), frozenset())


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="main", steps=[13])
async def test_revive_applies_in_respawn_mode():
    """revive_player passes the mode matrix in a respawn mode and the dead player
    is back (was _scenario_revive_allowed_respawn). The FFA negative is
    test_lifecycle_interventions.py::test_revive_player_in_real_ffa_is_mode_unsupported."""
    mock_cm = MockControllerManagerService(num_controllers=2)
    game = NonstopJoustGame(
        controller_manager_client=mock_cm,
        event_publisher=async_noop,
        audio_client=None,
        game_id="test_flag_to_effect_nonstop",
    )
    game.gameplay_stream = RecordingGameplayStream()
    await game._initialize_players_impl(mock_cm.controllers)
    victim = next(iter(game.players))
    game.players[victim].alive = False
    mgr, events = make_manager(game, {"revive_player": f"n1:{victim}"})

    await evaluate(mgr)

    revive = published(events, "revive_player")
    assert len(revive) == 1
    assert revive[0]["blocked"] == "false"
    assert revive[0].get("block_reason") != BLOCK_MODE_UNSUPPORTED
    assert game.players[victim].alive is True


# --------------------------------------------------------------------------- #
# Session-scoped state flags: apply, quiet revert
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="main", steps=[13])
async def test_global_difficulty_applies_then_reverts_without_event():
    """1.0 -> 1.5 applies (one unblocked event, factor 1.5); back to 1.0 runs the
    revert handler (factor 1.0) without a new event (was _scenario_global_difficulty)."""
    game = make_ffa_game()
    mgr, events = make_manager(game, {"global_difficulty_factor": 1.0})
    await evaluate(mgr)  # neutral baseline
    assert published(events, "adjust_global_difficulty") == []

    mgr._interventions_client.values["global_difficulty_factor"] = 1.5
    await evaluate(mgr)
    assert len(published(events, "adjust_global_difficulty", blocked="false")) == 1
    assert game.global_difficulty_factor == pytest.approx(1.5)

    mgr._interventions_client.values["global_difficulty_factor"] = 1.0
    await evaluate(mgr)
    assert len(published(events, "adjust_global_difficulty")) == 1
    assert game.global_difficulty_factor == pytest.approx(1.0)


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="main", steps=[13])
async def test_pacing_profile_calm_frantic_then_quiet_revert():
    """calm and frantic each apply (two unblocked events, windows swapped);
    ``none`` restores the init windows without a new event
    (was test_f6_flow.py::test_pacing_profile_applies_and_reverts)."""
    presets = _shipped_window_presets()
    game = make_ffa_game()
    init = game.init_music_windows
    mgr, events = make_manager(game, {"pacing_profile": "none"})

    def fake_windows(_domain, _flag, profile, _default):
        return presets.get(profile, {})

    with patch.object(dh, "read_object_flag_variant", side_effect=fake_windows):
        await evaluate(mgr)  # neutral baseline
        mgr._interventions_client.values["pacing_profile"] = "calm"
        await evaluate(mgr)
        assert game.music_windows == resolve_music_windows(presets["calm"]) != init
        mgr._interventions_client.values["pacing_profile"] = "frantic"
        await evaluate(mgr)
        assert game.music_windows == resolve_music_windows(presets["frantic"]) != init
        assert len(published(events, "set_pacing_profile", blocked="false")) == 2

        mgr._interventions_client.values["pacing_profile"] = "none"
        await evaluate(mgr)

    assert len(published(events, "set_pacing_profile")) == 2
    assert game.music_windows is init


# --------------------------------------------------------------------------- #
# Allow-list: the shipped "standard" variant blocks global difficulty
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A22", br=["BR-004"])
@pytest.mark.parametrize("flag_dir", [FLAGD_DIR, FLAGD_DIR / "ci"], ids=["shipped", "ci"])
async def test_shipped_standard_allow_list_blocks_global_difficulty(flag_dir):
    """Under the committed ``standard`` allow-list, a global_difficulty_factor
    write is blocked with not_allowed and never reaches the game, while
    set_pacing_profile (in ``standard``) is not blocked
    (was test_f6_flow.py::test_global_difficulty_factor_blocked_without_permission)."""
    allowed = shipped_allow_list("standard", flag_dir)
    game = make_ffa_game()
    mgr, events = make_manager(game, {"global_difficulty_factor": 1.0}, allowed=allowed)
    await evaluate(mgr)

    mgr._interventions_client.values["global_difficulty_factor"] = 1.5
    with patch.object(dh, "read_object_flag_variant", return_value={}):
        mgr._interventions_client.values["pacing_profile"] = "calm"
        await evaluate(mgr)

    difficulty = published(events, "adjust_global_difficulty")
    assert len(difficulty) == 1
    assert difficulty[0]["blocked"] == "true"
    assert difficulty[0]["block_reason"] == BLOCK_NOT_ALLOWED
    assert game.global_difficulty_factor == pytest.approx(1.0)
    assert [d["blocked"] for d in published(events, "set_pacing_profile")] == ["false"]


# --------------------------------------------------------------------------- #
# Per-player level: only the target moves, every distinct write applies
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="main", steps=[13])
async def test_player_sensitivity_follows_each_write_for_the_target_only():
    """Each new per-serial value for the target dispatches and lands on that
    player only; the game ends on the last value written
    (was _scenario_player_sensitivity and _scenario_repeated_writes)."""
    game = make_ffa_game()
    mgr, events = make_manager(game, {"player_sensitivity_factor": {}})
    await evaluate(mgr)  # everyone neutral
    assert published(events, "adjust_player_sensitivity") == []

    sequence = [1.5, 0.7, 1.5, 0.5, 2.0]
    for value in sequence:
        mgr._interventions_client.values["player_sensitivity_factor"] = {"p1": value}
        await evaluate(mgr)
        assert game.players["p1"].sensitivity_factor == pytest.approx(value)
        assert all(game.players[s].sensitivity_factor == pytest.approx(1.0) for s in ("p2", "p3", "p4"))

    applied = published(events, "adjust_player_sensitivity", blocked="false")
    assert len(applied) == len(sequence)
