"""
Tests for #1109: feature-flag-gated tempo-schedule SOURCE + live config listener.

Covers the re-scoped design (maintainer refinements on #1109):
- ONE pacing pathway: ``_get_music_change_time`` -> ``_decide_next_change_delay``.
  The historical random-window timing is the rule engine's DEFAULT RULE
  (``_default_rule_delay``), NOT a separate legacy branch. There is no ``random``
  mode; the flag selects ``rule`` | ``agent`` only.
- Default-safe / byte-identical: with the default mode, the delay drawn for a
  fixed RNG seed + state is identical to calling the default rule directly (proof
  that ``rule``'s default policy reproduces pre-#1109 timing exactly).
- ``agent`` mode is a clean STUB: no directive => fall back to the default rule
  (pacing never freezes); a directive => honored.
- The PROVIDER_CONFIGURATION_CHANGED listener (TempoScheduleManager) swaps the
  mode LIVE mid-game, game_id-scoped, without a restart.
- ``game.tempo_schedule_mode`` flag JSON is well-formed (rule/agent, default rule).
"""

import json
import random
import sys
import time
from pathlib import Path
from unittest.mock import patch

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector

from services.game_coordinator.games.base import (
    TEMPO_MODE_AGENT,
    TEMPO_MODE_RULE,
    TEMPO_SCHEDULE_MODE_DEFAULT,
    resolve_tempo_schedule_mode,
)
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.interventions import SessionView
from services.game_coordinator.tempo_schedule import TempoScheduleManager

GAME_JSON = project_root / "services" / "flagd" / "game.json"


def _make_ffa(game_id: str = ""):
    return FFAGame(
        controller_manager_client=None,
        event_publisher=EventCollector().publish,
        sensitivity=2,
        game_id=game_id,
    )


def _arm_game(game, *, players=2, dead=0, speed_up=True):
    game.players = {chr(ord("a") + i): object() for i in range(players)}
    game.dead_count = dead
    game.speed_up = speed_up


# ========================================================================
# resolve_tempo_schedule_mode
# ========================================================================


def test_resolve_known_modes():
    assert resolve_tempo_schedule_mode("rule") == TEMPO_MODE_RULE
    assert resolve_tempo_schedule_mode("agent") == TEMPO_MODE_AGENT


def test_resolve_unknown_falls_back_to_rule():
    # missing / non-string / a stale "random" all resolve to the default rule.
    for bad in (None, "", "random", "RULE", 5, [], True, "agentx"):
        assert resolve_tempo_schedule_mode(bad) == TEMPO_SCHEDULE_MODE_DEFAULT == TEMPO_MODE_RULE


# ========================================================================
# Default mode = rule, init-resolved (flagd unavailable in unit tests => "rule")
# ========================================================================


def test_default_mode_is_rule():
    game = _make_ffa()
    assert game.tempo_schedule_mode == TEMPO_MODE_RULE
    assert game.agent_tempo_next_delay is None


# ========================================================================
# Single pathway + byte-identical default-rule proof
# ========================================================================


def test_default_mode_byte_identical_to_default_rule():
    """In ``rule`` mode the seam returns EXACTLY the default rule's draw.

    Seed the per-game RNG identically before each call: the value the single
    pathway produces must equal a direct ``_default_rule_delay()`` draw — proof
    the windowed-RNG timing is folded into the default rule with no behavior
    change (no separate legacy branch alters the result).
    """
    game = _make_ffa()
    _arm_game(game, players=4, dead=1, speed_up=True)
    assert game.tempo_schedule_mode == TEMPO_MODE_RULE

    for seed in range(50):
        game._rng = random.Random(seed)
        via_seam = game._decide_next_change_delay()
        game._rng = random.Random(seed)
        via_rule = game._default_rule_delay()
        assert via_seam == via_rule


def test_get_music_change_time_uses_seam():
    """``_get_music_change_time`` == now + seam delay (single pathway)."""
    game = _make_ffa()
    _arm_game(game)
    with patch.object(game, "_decide_next_change_delay", return_value=7.5):
        before = time.time()
        result = game._get_music_change_time()
    assert abs(result - (before + 7.5)) < 0.05


def test_default_rule_stays_within_windows():
    """Default rule keeps drawing within the same constant ranges as pre-#1109."""
    from services.game_coordinator.games.base import (
        MAX_MUSIC_FAST_TIME,
        MAX_MUSIC_SLOW_TIME,
        MIN_MUSIC_FAST_TIME,
        MIN_MUSIC_SLOW_TIME,
    )

    game = _make_ffa()
    _arm_game(game, players=2, dead=0)
    for speed_up, lo, hi in (
        (True, MIN_MUSIC_SLOW_TIME, MAX_MUSIC_SLOW_TIME),
        (False, MIN_MUSIC_FAST_TIME, MAX_MUSIC_FAST_TIME),
    ):
        game.speed_up = speed_up
        for _ in range(200):
            delay = game._decide_next_change_delay()
            assert lo - 0.001 <= delay <= hi + 0.001


# ========================================================================
# agent mode STUB: no directive => fall back to default rule; directive => honor
# ========================================================================


def test_agent_mode_no_directive_falls_back_to_default_rule():
    """``agent`` with no directive draws the same delay as the default rule."""
    game = _make_ffa()
    _arm_game(game, players=4, dead=1, speed_up=False)
    game.tempo_schedule_mode = TEMPO_MODE_AGENT
    game.agent_tempo_next_delay = None  # no #1103 directive yet

    for seed in range(50):
        game._rng = random.Random(seed)
        agent_delay = game._decide_next_change_delay()
        game._rng = random.Random(seed)
        rule_delay = game._default_rule_delay()
        assert agent_delay == rule_delay


def test_agent_mode_honors_directive():
    """A present, non-negative directive is used verbatim (no RNG draw)."""
    game = _make_ffa()
    _arm_game(game)
    game.tempo_schedule_mode = TEMPO_MODE_AGENT
    game.agent_tempo_next_delay = 3.25
    for _ in range(20):
        assert game._decide_next_change_delay() == 3.25


def test_agent_mode_negative_directive_falls_back():
    """A malformed (negative) directive is ignored => default rule."""
    game = _make_ffa()
    _arm_game(game)
    game.tempo_schedule_mode = TEMPO_MODE_AGENT
    game.agent_tempo_next_delay = -1.0
    game._rng = random.Random(123)
    via_seam = game._decide_next_change_delay()
    game._rng = random.Random(123)
    assert via_seam == game._default_rule_delay()


# ========================================================================
# apply_tempo_schedule_mode: atomic swap, default-safe
# ========================================================================


def test_apply_mode_swaps_atomically():
    game = _make_ffa()
    assert game.tempo_schedule_mode == TEMPO_MODE_RULE
    game.apply_tempo_schedule_mode("agent")
    assert game.tempo_schedule_mode == TEMPO_MODE_AGENT
    game.apply_tempo_schedule_mode("rule")
    assert game.tempo_schedule_mode == TEMPO_MODE_RULE


def test_apply_unknown_mode_resolves_to_rule():
    game = _make_ffa()
    game.apply_tempo_schedule_mode("agent")
    game.apply_tempo_schedule_mode("garbage")  # unknown => default rule
    assert game.tempo_schedule_mode == TEMPO_MODE_RULE


# ========================================================================
# Live listener: TempoScheduleManager swaps the mode mid-game, game_id-scoped
# ========================================================================


def test_listener_switches_mode_live_midgame():
    """Flipping the flag via the PROVIDER_CONFIGURATION_CHANGED handler switches
    a RUNNING game's source mode without a restart.

    Drive the manager directly through its handler (``_on_flags_changed``) the way
    flagd's change event would, with the flag read mocked to flip from rule->agent
    ->rule. After each change-event the live game's ``tempo_schedule_mode`` reflects
    the new value — the music loop's next ``_decide_next_change_delay`` honors it.
    """
    game = _make_ffa(game_id="game_abc123")
    _arm_game(game)
    assert game.tempo_schedule_mode == TEMPO_MODE_RULE

    sessions = [SessionView(game_id="game_abc123", game=game, publish=EventCollector().publish)]

    flag_value = {"v": TEMPO_MODE_RULE}

    def fake_read(domain, key, default, game_id=None):  # noqa: ARG001
        # Only this game's id is queried (game_id-scoped read).
        assert game_id == "game_abc123"
        return flag_value["v"]

    manager = TempoScheduleManager(get_sessions=lambda: sessions)

    with patch("services.game_coordinator.tempo_schedule.read_string_flag", side_effect=fake_read):
        # Operator flips the flag to agent -> change event fires.
        flag_value["v"] = TEMPO_MODE_AGENT
        manager._on_flags_changed(None)
        assert game.tempo_schedule_mode == TEMPO_MODE_AGENT  # live, no restart

        # Flip back to rule -> next change event reverts it.
        flag_value["v"] = TEMPO_MODE_RULE
        manager._on_flags_changed(None)
        assert game.tempo_schedule_mode == TEMPO_MODE_RULE


def test_listener_scopes_per_game():
    """Two live games get their own gameId-scoped mode (shadow != primary)."""
    primary = _make_ffa(game_id="game_primary00")
    shadow = _make_ffa(game_id="game_shadow000")
    _arm_game(primary)
    _arm_game(shadow)

    sessions = [
        SessionView(game_id="game_primary00", game=primary, publish=EventCollector().publish),
        SessionView(game_id="game_shadow000", game=shadow, publish=EventCollector().publish),
    ]

    # Primary stays rule; shadow flips to agent (targeting by gameId).
    per_game = {"game_primary00": TEMPO_MODE_RULE, "game_shadow000": TEMPO_MODE_AGENT}

    def fake_read(domain, key, default, game_id=None):  # noqa: ARG001
        return per_game.get(game_id, default)

    manager = TempoScheduleManager(get_sessions=lambda: sessions)
    with patch("services.game_coordinator.tempo_schedule.read_string_flag", side_effect=fake_read):
        manager._on_flags_changed(None)

    assert primary.tempo_schedule_mode == TEMPO_MODE_RULE
    assert shadow.tempo_schedule_mode == TEMPO_MODE_AGENT


def test_listener_skips_sessions_without_game():
    """A session with no constructed game yet is skipped (no crash)."""
    sessions = [SessionView(game_id="game_x", game=None, publish=EventCollector().publish)]
    manager = TempoScheduleManager(get_sessions=lambda: sessions)
    with patch("services.game_coordinator.tempo_schedule.read_string_flag", return_value="agent"):
        manager._on_flags_changed(None)  # must not raise


def test_listener_one_bad_session_does_not_stop_others():
    good = _make_ffa(game_id="game_good")

    class Boom:
        def apply_tempo_schedule_mode(self, value):
            raise RuntimeError("boom")

    sessions = [
        SessionView(game_id="game_bad", game=Boom(), publish=EventCollector().publish),
        SessionView(game_id="game_good", game=good, publish=EventCollector().publish),
    ]
    manager = TempoScheduleManager(get_sessions=lambda: sessions)
    with patch("services.game_coordinator.tempo_schedule.read_string_flag", return_value="agent"):
        manager._on_flags_changed(None)  # bad session logged, good still applied
    assert good.tempo_schedule_mode == TEMPO_MODE_AGENT


# ========================================================================
# game.json flag is well-formed
# ========================================================================


def test_game_json_tempo_schedule_mode_well_formed():
    data = json.loads(GAME_JSON.read_text())
    flag = data["flags"]["tempo_schedule_mode"]
    assert flag["state"] == "ENABLED"
    assert set(flag["variants"]) == {"rule", "agent"}
    # No "random" variant (maintainer re-scope).
    assert "random" not in flag["variants"]
    # Default variant is the rule engine => back-compat.
    assert flag["defaultVariant"] == "rule"
    assert resolve_tempo_schedule_mode(flag["variants"][flag["defaultVariant"]]) == TEMPO_MODE_RULE
