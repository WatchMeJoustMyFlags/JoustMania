"""
Unit tests for the wired battery_provider path (#798).

The InterventionManager battery guard was inert in production because the
servicer constructed it with ``battery_provider=None``. These tests exercise the
*real* provider — the servicer's ``_battery_pct_for_serial`` reading live
``Player.battery_pct`` (populated from ``GameplayData.battery``) — rather than an
injected fake, and verify:

- below-threshold battery blocks a player-targeted intervention,
- above-threshold passes,
- unknown/missing battery (no game, unknown serial, no frame yet) does not block,
- ``resolve_player_targets`` drops low-battery serials when gated by the real
  provider shape,
- the 0-5 -> 0-100 normalization matches the controller-manager semantics.

Telemetry is disabled via conftest; the intervention metric is patched per the
repo metric-testing convention.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lib.controller_constants import battery_to_pct
from lib.types import GameEvent
from services.game_coordinator.interventions import (
    BLOCK_LOW_BATTERY,
    INTERVENTION_SPECS,
    InterventionManager,
)
from services.game_coordinator.servicer import GameCoordinatorServicer

ALL_TYPES = {spec.type_id for spec in INTERVENTION_SPECS}


class FakeFlagClient:
    """In-memory flag client matching the OpenFeature get_*_value surface."""

    def __init__(self, values=None):
        self.values = values or {}

    def get_string_value(self, key, default, _ctx=None):
        return str(self.values.get(key, default))

    def get_integer_value(self, key, default, _ctx=None):
        return int(self.values.get(key, default))

    def get_float_value(self, key, default, _ctx=None):
        return float(self.values.get(key, default))

    def get_object_value(self, key, default, _ctx=None):
        return self.values.get(key, default)


class RealishPlayer:
    """Minimal stand-in for games.base.Player carrying the fields the provider
    and targeting resolution read (serial, alive, battery_pct)."""

    def __init__(self, serial, battery_pct=None, alive=True):
        self.serial = serial
        self.battery_pct = battery_pct
        self.alive = alive


class RealishGame:
    def __init__(self, name, players):
        self._name = name
        self.players = players

    def get_game_name(self):
        return self._name


def _make_servicer_with_game(players):
    """Construct a real servicer and attach a live game with the given players.

    Returns the servicer whose ``_battery_pct_for_serial`` is the production
    battery provider under test. ``intervention_manager.start()`` is a no-op when
    OpenFeature is unavailable in tests; we don't rely on it here.

    Multi-session (#775/#793): ``current_game`` is now a read-only property over
    the primary session. We register a minimal primary session carrying the live
    game so the provider resolves it exactly as production does.
    """
    servicer = GameCoordinatorServicer()
    game_id = "game_test"
    session = SimpleNamespace(current_game=RealishGame("FFA", players))
    servicer.sessions[game_id] = session
    servicer._primary_game_id = game_id
    return servicer


def _wire_manager(provider, *, threshold=20, budget=10, game=None):
    """Build a manager wired with the REAL provider callable and fake flag
    clients (so flag reads are deterministic without flagd)."""
    events = []

    async def publisher(event_type, data):
        events.append((event_type, data))

    mgr = InterventionManager(
        event_publisher=publisher,
        get_game=lambda: game,
        battery_provider=provider,
    )
    mgr._interventions_client = FakeFlagClient()
    mgr._agent_client = FakeFlagClient(
        {
            # interventions_allowed is a STRING flag: comma-separated ids (#1127).
            "interventions_allowed": ",".join(sorted(ALL_TYPES)),
            "policy.max_interventions_per_minute": budget,
            "policy.battery_threshold": threshold,
        }
    )
    mgr._rate_limiter.set_budget(float(budget))
    return mgr, events


# --------------------------------------------------------------------------- #
# Provider unit behavior (servicer._battery_pct_for_serial)
# --------------------------------------------------------------------------- #
def test_provider_reads_live_player_battery():
    servicer = _make_servicer_with_game({"ctrl_a": RealishPlayer("ctrl_a", battery_pct=85.0)})
    assert servicer._battery_pct_for_serial("ctrl_a") == 85.0


def test_provider_unknown_serial_returns_none():
    servicer = _make_servicer_with_game({"ctrl_a": RealishPlayer("ctrl_a", battery_pct=85.0)})
    assert servicer._battery_pct_for_serial("ctrl_missing") is None


def test_provider_no_game_returns_none():
    servicer = GameCoordinatorServicer()  # no game running
    assert servicer._battery_pct_for_serial("ctrl_a") is None


def test_provider_no_frame_yet_returns_none():
    # Player exists but no GameplayData frame has set battery_pct yet.
    servicer = _make_servicer_with_game({"ctrl_a": RealishPlayer("ctrl_a", battery_pct=None)})
    assert servicer._battery_pct_for_serial("ctrl_a") is None


# --------------------------------------------------------------------------- #
# Normalization parity with controller_manager (0-5 -> 0-100)
# --------------------------------------------------------------------------- #
def test_battery_to_pct_coarse_scale():
    assert battery_to_pct(1) == 20.0
    assert battery_to_pct(5) == 100.0


def test_battery_to_pct_passthrough_and_clamp():
    assert battery_to_pct(80) == 80.0
    assert battery_to_pct(238) == 100.0  # psmoveapi charging sentinel
    assert battery_to_pct(-1) == 0.0


# --------------------------------------------------------------------------- #
# End-to-end: enforcement chain through the REAL provider
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_wired_provider_blocks_low_battery():
    players = {"ctrl_a": RealishPlayer("ctrl_a", battery_pct=10.0)}  # below threshold 20
    servicer = _make_servicer_with_game(players)
    game = servicer.current_game
    mgr, events = _wire_manager(servicer._battery_pct_for_serial, threshold=20, game=game)
    mgr._interventions_client = FakeFlagClient({"eliminate_player": "1:ctrl_a"})

    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()

    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert elim[0]["blocked"] == "true"
    assert elim[0]["block_reason"] == BLOCK_LOW_BATTERY


@pytest.mark.asyncio
async def test_wired_provider_passes_above_threshold():
    players = {"ctrl_a": RealishPlayer("ctrl_a", battery_pct=90.0)}
    servicer = _make_servicer_with_game(players)
    game = servicer.current_game
    mgr, events = _wire_manager(servicer._battery_pct_for_serial, threshold=20, game=game)
    mgr._interventions_client = FakeFlagClient({"eliminate_player": "1:ctrl_a"})

    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()

    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert elim[0]["blocked"] == "false"


@pytest.mark.asyncio
async def test_wired_provider_missing_data_does_not_block():
    # Player present but no battery frame yet -> provider returns None -> no block.
    players = {"ctrl_a": RealishPlayer("ctrl_a", battery_pct=None)}
    servicer = _make_servicer_with_game(players)
    game = servicer.current_game
    mgr, events = _wire_manager(servicer._battery_pct_for_serial, threshold=20, game=game)
    mgr._interventions_client = FakeFlagClient({"eliminate_player": "1:ctrl_a"})

    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()

    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert elim[0]["blocked"] == "false"


# --------------------------------------------------------------------------- #
# resolve_player_targets battery gate with the real provider shape
# --------------------------------------------------------------------------- #
def test_resolve_player_targets_drops_low_battery_serial():
    players = {
        "ctrl_low": RealishPlayer("ctrl_low", battery_pct=5.0),  # below threshold
        "ctrl_ok": RealishPlayer("ctrl_ok", battery_pct=80.0),  # above threshold
        "ctrl_unknown": RealishPlayer("ctrl_unknown", battery_pct=None),  # unknown -> kept
    }
    servicer = _make_servicer_with_game(players)
    game = servicer.current_game
    mgr, _events = _wire_manager(servicer._battery_pct_for_serial, threshold=20, game=game)
    # FakeFlagClient returns the default for every serial (no targeting rules).
    resolved = mgr.resolve_player_targets("player_sensitivity_factor", 1.0, game, battery_gate=True)

    assert "ctrl_low" not in resolved  # battery-gated out
    assert resolved["ctrl_ok"] == 1.0
    # Unknown battery must NOT be gated (missing data does not block).
    assert resolved["ctrl_unknown"] == 1.0


def test_resolve_player_targets_no_gate_keeps_all():
    players = {"ctrl_low": RealishPlayer("ctrl_low", battery_pct=5.0)}
    servicer = _make_servicer_with_game(players)
    game = servicer.current_game
    mgr, _events = _wire_manager(servicer._battery_pct_for_serial, threshold=20, game=game)
    resolved = mgr.resolve_player_targets("player_sensitivity_factor", 1.0, game, battery_gate=False)
    assert "ctrl_low" in resolved
