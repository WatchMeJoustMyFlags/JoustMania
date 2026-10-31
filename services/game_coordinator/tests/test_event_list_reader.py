"""
Per-game event lists for one-shot interventions (FR-137, UC-053 A4 / BR-006, #1410).

The flag service renders each one-shot flag, for one game's ``gameId``, as
``{"epoch","seq","events":[{"seq","payload"}, ...]}``; the coordinator applies
every event above the last ``(epoch, seq)`` it applied (architecture note §6).
Also the single-flight ``evaluate_all`` (re-review N1).
"""

import asyncio
import itertools
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import FlagEvaluationDetails, Reason

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from lib.types import GameEvent
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    InterventionManager,
    SessionView,
)
from services.game_coordinator.lifecycle_handlers import register_lifecycle_handlers
from services.game_coordinator.tests.conftest import FakeInterventionGame

ALL_TYPES = {spec.type_id for spec in INTERVENTION_SPECS}
BASELINE = {"epoch": "", "seq": 0, "events": []}


class PerGameFlagClient:
    """flagd stand-in for flag-service rendering: values keyed by (flag, gameId)."""

    def __init__(self):
        self.lists: dict[tuple[str, str], dict] = {}
        self.failing: set[str] = set()
        self.reads = 0

    def put_events(self, flag, game_id, epoch, events):
        seq = max((e["seq"] for e in events), default=0)
        self.lists[(flag, game_id)] = {"epoch": epoch, "seq": seq, "events": events}

    def get_object_details(self, key, default, ctx=None):
        self.reads += 1
        if key in self.failing:
            return FlagEvaluationDetails(
                flag_key=key,
                value=default,
                reason=Reason.ERROR,
                error_code=ErrorCode.PROVIDER_NOT_READY,
                error_message="flagd unavailable",
            )
        game_id = (ctx.attributes or {}).get("gameId", "") if ctx else ""
        return FlagEvaluationDetails(flag_key=key, value=self.lists.get((key, game_id), BASELINE))

    # State-shaped and grant flags read their neutral defaults here.
    def get_string_details(self, key, default, _ctx=None):
        return FlagEvaluationDetails(flag_key=key, value=default, variant="none")

    def get_float_details(self, key, default, _ctx=None):
        return FlagEvaluationDetails(flag_key=key, value=default, variant="none")

    def get_string_value(self, key, default, _ctx=None):
        return default

    def get_integer_value(self, key, default, _ctx=None):
        return default

    def get_float_value(self, key, default, _ctx=None):
        return default


class AgentStub:
    def get_integer_value(self, key, default, _ctx=None):
        return {"policy.coordinator_backstop_per_minute": 1000, "policy.battery_threshold": 0}.get(key, default)

    def get_float_value(self, key, default, _ctx=None):
        return default

    def get_string_value(self, key, default, _ctx=None):
        return ",".join(sorted(ALL_TYPES)) if key == "interventions_allowed" else default

    def get_object_value(self, key, default, _ctx=None):
        return default


class Harness:
    def __init__(self):
        self.flags = PerGameFlagClient()
        self.views: list[SessionView] = []
        self.events: dict[str, list] = {}
        self.end_game_fn = AsyncMock(return_value=(True, ""))
        counter = itertools.count()
        self.mgr = InterventionManager(
            event_publisher=self._publisher("__primary__"),
            get_sessions=lambda: list(self.views),
            time_fn=lambda: 1000.0 + next(counter) * 0.0,
            end_game_fn=self.end_game_fn,
        )
        self.mgr._interventions_client = self.flags
        self.mgr._agent_client = AgentStub()
        self.mgr._rate_limiter.set_budget(1000.0)
        self.mgr.register_ambient_handlers()
        register_lifecycle_handlers(self.mgr)

    def _publisher(self, game_id):
        bucket = self.events.setdefault(game_id, [])

        async def publish(event_type, data):
            bucket.append((event_type, data))

        return publish

    def start_game(self, game_id, real=True) -> FakeInterventionGame:
        game = FakeInterventionGame(serials=("MOCK0001", "MOCK0002", "MOCK0003"))
        self.views.append(SessionView(game_id=game_id, game=game, publish=self._publisher(game_id), is_real=real))
        return game

    async def flag_change(self):
        with patch("services.game_coordinator.metrics.interventions_total"):
            await self.mgr.evaluate_all()

    def applied(self, game_id, flag_key):
        type_id = next(s.type_id for s in INTERVENTION_SPECS if s.flag_key == flag_key)
        return [
            d
            for et, d in self.events.get(game_id, [])
            if et == GameEvent.AGENT_INTERVENTION and d["type"] == type_id and d["blocked"] == "false"
        ]


def ev(seq, **payload):
    return {"seq": seq, "payload": payload}


@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"], tc="TC-018")
@pytest.mark.asyncio
async def test_two_eliminations_in_one_read_both_apply_once():
    h = Harness()
    game = h.start_game("game_a")
    # Step 1: two eliminations for different players land within one flagd push
    h.flags.put_events("eliminate_player", "game_a", "e1", [ev(1, serial="MOCK0001"), ev(2, serial="MOCK0002")])
    await h.flag_change()
    # Step 2: both apply, in seq order; re-evaluation applies nothing again
    assert game.killed == ["MOCK0001", "MOCK0002"]
    await h.flag_change()
    assert game.killed == ["MOCK0001", "MOCK0002"]
    h.flags.put_events(
        "eliminate_player",
        "game_a",
        "e1",
        [ev(1, serial="MOCK0001"), ev(2, serial="MOCK0002"), ev(3, serial="MOCK0003")],
    )
    await h.flag_change()
    assert game.killed == ["MOCK0001", "MOCK0002", "MOCK0003"]


@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"], tc="TC-018")
@pytest.mark.asyncio
async def test_failed_read_applies_nothing_and_does_not_replay():
    """N2: an error read neither applies nor moves (epoch, seq); the default
    {"epoch": ""} it returns is never mistaken for a new epoch. Uses audio_cue,
    which is not idempotent (every apply plays the sound again), so a replay
    would show as a second play (PR #1417 review)."""
    h = Harness()
    game = h.start_game("game_a")
    h.flags.put_events("audio_cue", "game_a", "e1", [ev(1, sound="agent_cue")])
    await h.flag_change()
    assert game.sounds == [("agent_cue", 2)]
    # Step 1: flagd errors on the next read
    h.flags.failing.add("audio_cue")
    await h.flag_change()
    assert game.sounds == [("agent_cue", 2)]
    assert h.mgr._last_event["game_a"]["audio_cue"] == ("e1", 1)
    # Step 2: flagd recovers with the same list: nothing replays
    h.flags.failing.clear()
    await h.flag_change()
    assert game.sounds == [("agent_cue", 2)], "the event was applied again after a failed read"
    assert h.mgr._last_event["game_a"]["audio_cue"] == ("e1", 1)
    # Step 3: a new event after the recovery applies once
    h.flags.put_events("audio_cue", "game_a", "e1", [ev(1, sound="agent_cue"), ev(2, sound="agent_cue")])
    await h.flag_change()
    assert game.sounds == [("agent_cue", 2), ("agent_cue", 2)]


@pytest.mark.uc("UC-053", flow="A9", br=["BR-006"], tc="TC-019")
@pytest.mark.uc("UC-040", flow="main", br=["BR-006"])
@pytest.mark.asyncio
async def test_only_a_new_nonempty_epoch_resets_the_sequence():
    h = Harness()
    game = h.start_game("game_a")
    h.flags.put_events("eliminate_player", "game_a", "e1", [ev(1, serial="MOCK0001"), ev(2, serial="MOCK0002")])
    await h.flag_change()
    # The baseline value (empty epoch) after a restart without a write: no reset, nothing applied.
    h.flags.lists.pop(("eliminate_player", "game_a"))
    await h.flag_change()
    assert h.mgr._last_event["game_a"]["eliminate_player"] == ("e1", 2)
    # A restarted flag service (new epoch) appends seq 1 again: it applies.
    h.flags.put_events("eliminate_player", "game_a", "e2", [ev(1, serial="MOCK0003")])
    await h.flag_change()
    assert game.killed == ["MOCK0001", "MOCK0002", "MOCK0003"]


@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"])
@pytest.mark.asyncio
async def test_lower_seq_in_the_same_epoch_is_ignored():
    h = Harness()
    game = h.start_game("game_a")
    h.flags.put_events("eliminate_player", "game_a", "e1", [ev(1, serial="MOCK0001"), ev(2, serial="MOCK0002")])
    await h.flag_change()
    h.flags.put_events("eliminate_player", "game_a", "e1", [ev(1, serial="MOCK0003")])
    await h.flag_change()
    assert game.killed == ["MOCK0001", "MOCK0002"]


@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"])
@pytest.mark.asyncio
async def test_an_already_applied_event_still_listed_is_not_applied_again():
    """The list keeps the last K events: after seq 1 applied, the list [1, 2]
    applies event 2 only (PR #1417 review B3)."""
    h = Harness()
    game = h.start_game("game_a")
    h.flags.put_events("eliminate_player", "game_a", "e1", [ev(1, serial="MOCK0001")])
    await h.flag_change()
    assert game.killed == ["MOCK0001"]
    h.flags.put_events("eliminate_player", "game_a", "e1", [ev(1, serial="MOCK0001"), ev(2, serial="MOCK0002")])
    await h.flag_change()
    assert game.killed == ["MOCK0001", "MOCK0002"]


@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"])
@pytest.mark.asyncio
async def test_a_gap_larger_than_k_is_logged_and_counted():
    h = Harness()
    game = h.start_game("game_a")
    # Events 1..3 already fell out of a K-long list before the first read.
    h.flags.put_events("eliminate_player", "game_a", "e1", [ev(4, serial="MOCK0001"), ev(5, serial="MOCK0002")])
    with patch("services.game_coordinator.metrics.intervention_event_gaps_total") as gaps:
        await h.flag_change()
    gaps.labels.assert_called_once_with(flag="eliminate_player")
    assert game.killed == ["MOCK0001", "MOCK0002"]


@pytest.mark.uc("UC-053", flow="main", br=["BR-003", "BR-006"])
@pytest.mark.asyncio
async def test_events_apply_only_in_their_game():
    h = Harness()
    game_a = h.start_game("game_a")
    game_b = h.start_game("game_b", real=False)
    h.flags.put_events("audio_cue", "game_b", "e1", [ev(1, sound="agent_cue")])
    h.flags.put_events("end_game", "game_b", "e1", [ev(1)])
    await h.flag_change()
    assert game_a.sounds == [] and game_b.sounds == [("agent_cue", 2)]
    h.end_game_fn.assert_awaited_once_with("game_b", "agent_intervention")
    assert h.applied("game_a", "end_game") == []


@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"])
@pytest.mark.asyncio
async def test_payload_strings_reach_the_handlers():
    h = Harness()
    h.start_game("game_a")
    seen = []

    async def record(ctx):
        seen.append((ctx.spec.flag_key, ctx.payload, ctx.target_serial))

    for flag in ("controller_effect", "revive_player"):
        h.mgr.register_handler(flag, record)
    h.flags.put_events(
        "controller_effect", "game_a", "e1", [ev(1, serial="MOCK0002", effect="rumble"), ev(2, effect="pulse")]
    )
    h.flags.put_events("revive_player", "game_a", "e1", [ev(1, serial="MOCK0001"), ev(2)])  # ev 2: no serial
    await h.flag_change()
    assert ("controller_effect", "MOCK0002:rumble", "MOCK0002") in seen
    assert ("controller_effect", ":pulse", None) in seen
    assert [s for s in seen if s[0] == "revive_player"] == [("revive_player", "MOCK0001", "MOCK0001")]


@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"])
@pytest.mark.asyncio
async def test_a_retired_game_gets_nothing():
    h = Harness()
    game = h.start_game("game_a")
    h.mgr.forget_session("game_a")
    h.flags.put_events("eliminate_player", "game_a", "e1", [ev(1, serial="MOCK0001")])
    await h.mgr._evaluate_event_list(
        next(s for s in INTERVENTION_SPECS if s.flag_key == "eliminate_player"), h.views[0]
    )
    assert game.killed == []


@pytest.mark.uc("UC-053", flow="main")
@pytest.mark.asyncio
async def test_evaluate_all_is_single_flight():
    """N1: N overlapping change events cost at most two passes, never interleaved."""
    h = Harness()
    h.start_game("game_a")
    passes, running, overlap = [], [0], []

    async def slow_pass():
        running[0] += 1
        overlap.append(running[0])
        passes.append(1)
        await asyncio.sleep(0.01)
        running[0] -= 1

    with patch.object(h.mgr, "_evaluate_all_once", side_effect=slow_pass):
        await asyncio.gather(*(h.mgr.evaluate_all() for _ in range(8)))
    assert len(passes) == 2 and max(overlap) == 1
    assert h.mgr._eval_running is False


@pytest.mark.uc("UC-053", flow="main")
@pytest.mark.asyncio
async def test_single_flight_recovers_after_a_failing_pass():
    h = Harness()
    with patch.object(h.mgr, "_evaluate_all_once", side_effect=RuntimeError("boom")), pytest.raises(RuntimeError):
        await h.mgr.evaluate_all()
    assert h.mgr._eval_running is False


@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"], tc="TC-018")
@pytest.mark.asyncio
async def test_seq_advances_before_dispatch():
    """A pass that suspends inside a handler cannot let an overlapping pass
    apply the same event again: the seq moved before dispatch."""
    h = Harness()
    h.start_game("game_a")
    await h.flag_change()  # the game's first pass creates its (epoch, seq) bucket
    gate = asyncio.Event()
    calls = []

    async def blocking(ctx):
        calls.append(ctx.payload)
        await gate.wait()

    h.mgr.register_handler("eliminate_player", blocking)
    h.flags.put_events("eliminate_player", "game_a", "e1", [ev(1, serial="MOCK0001")])
    spec = next(s for s in INTERVENTION_SPECS if s.flag_key == "eliminate_player")
    first = asyncio.create_task(h.mgr._evaluate_event_list(spec, h.views[0]))
    await asyncio.sleep(0)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await h.mgr._evaluate_event_list(spec, h.views[0])
    gate.set()
    with patch("services.game_coordinator.metrics.interventions_total"):
        await first
    assert calls == ["MOCK0001"]
