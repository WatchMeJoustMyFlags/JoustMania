"""
Cross-set consistency of the coordinator's intervention gate (UC-053 A9, #1552).

The coordinator reads the ``interventions`` and ``agent`` flag sets through two
in-process sync streams. After a flagd restart they reconnect in no fixed order,
so for a few seconds the ``interventions`` set it holds can be newer than the
``interventions_allowed`` allow-list it holds. The flag service stamps both with
its ``snapshotEpoch`` / ``snapshotVersion``; the coordinator defers every
intervention while its allow-list is older than its interventions set, then
re-evaluates when the ``agent`` stream catches up.

Both directions are covered, each on a fake pair of in-process stores:

- narrower stale allow-list (#1508): without the gate the value is blocked
  ``not_allowed`` and, since only a changed value is re-evaluated, lost;
- wider stale allow-list: without the gate the value is applied against an
  allow-list flagd no longer serves (the fail-closed second check is bypassed).
"""

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from openfeature.flag_evaluation import FlagEvaluationDetails

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from lib.types import GameEvent
from services.game_coordinator.interventions import InterventionManager, SessionView
from services.game_coordinator.lifecycle_handlers import register_lifecycle_handlers
from services.game_coordinator.tests.conftest import FakeInterventionGame

AMBIENT = "play_audio_cue,send_controller_effect,adjust_volume"
STANDARD = AMBIENT + ",adjust_music_tempo,set_pacing_profile,adjust_player_sensitivity,grant_shield"
FULL = STANDARD + ",adjust_global_sensitivity,adjust_global_difficulty,eliminate_player,revive_player,end_game"
BASELINE_EVENTS = {"epoch": "", "seq": 0, "events": []}

GAME = "game_a"
SERIALS = ("MOCK0001", "MOCK0002")


class _Store:
    """One in-process flagd store: every read carries the snapshot stamp the
    flag service rendered into it (None = an unstamped, file-sourced set)."""

    def __init__(self, revision: tuple[str, int] | None):
        self.revision = revision

    def meta(self) -> dict:
        if self.revision is None:
            return {}
        epoch, version = self.revision
        return {"snapshotEpoch": epoch, "snapshotVersion": version}


class InterventionsStore(_Store):
    def __init__(self, revision):
        super().__init__(revision)
        self.state: dict[str, object] = {}  # flag -> game-scoped state value
        self.per_serial: dict[tuple[str, str], tuple[str, object]] = {}  # (flag, serial) -> (variant, value)
        self.events: dict[str, dict] = {}  # flag -> event list

    def _details(self, key, value, variant="none"):
        return FlagEvaluationDetails(flag_key=key, value=value, variant=variant, flag_metadata=self.meta())

    def get_object_details(self, key, default, _ctx=None):
        return self._details(key, self.events.get(key, BASELINE_EVENTS))

    def get_float_details(self, key, default, ctx=None):
        variant, value = self.per_serial.get((key, ctx.targeting_key), ("none", default))
        return self._details(key, value, variant)

    def get_string_details(self, key, default, ctx=None):
        variant, value = self.per_serial.get((key, ctx.targeting_key), ("none", default))
        return self._details(key, value, variant)

    def get_float_value(self, key, default, _ctx=None):
        return self.state.get(key, default)

    def get_integer_value(self, key, default, _ctx=None):
        return self.state.get(key, default)

    def get_string_value(self, key, default, _ctx=None):
        return self.state.get(key, default)


class AgentStore(_Store):
    def __init__(self, revision, allowed: str):
        super().__init__(revision)
        self.allowed = allowed

    def get_string_details(self, key, default, _ctx=None):
        value = self.allowed if key == "interventions_allowed" else default
        return FlagEvaluationDetails(flag_key=key, value=value, variant="v", flag_metadata=self.meta())

    def get_string_value(self, key, default, _ctx=None):
        return self.allowed if key == "interventions_allowed" else default

    def get_integer_value(self, key, default, _ctx=None):
        return {"policy.coordinator_backstop_per_minute": 1000, "policy.battery_threshold": 0}.get(key, default)

    def get_float_value(self, key, default, _ctx=None):
        return default

    def get_object_value(self, key, default, _ctx=None):
        return default


class Harness:
    def __init__(self, interventions, agent, time_fn=None):
        self.events: list[dict] = []
        self.game = FakeInterventionGame(serials=SERIALS)

        async def publish(event_type, data):
            if event_type == GameEvent.AGENT_INTERVENTION:
                self.events.append(data)

        self.view = SessionView(game_id=GAME, game=self.game, publish=publish, is_real=True)
        kwargs = {"time_fn": time_fn} if time_fn is not None else {}
        self.mgr = InterventionManager(event_publisher=publish, get_sessions=lambda: [self.view], **kwargs)
        self.mgr._interventions_client = interventions
        self.mgr._agent_client = agent
        self.mgr.register_ambient_handlers()
        register_lifecycle_handlers(self.mgr)

    async def flag_change(self):
        """One PROVIDER_CONFIGURATION_CHANGED from either stream."""
        with patch("services.game_coordinator.metrics.interventions_total"):
            await self.mgr.evaluate_all()

    def outcomes(self, type_id):
        return [(e["blocked"], e["block_reason"]) for e in self.events if e["type"] == type_id]


# --------------------------------------------------------------------------- #
# Narrower stale allow-list: the #1508 case (value deferred, then applied)
# --------------------------------------------------------------------------- #


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.uc("UC-040", flow="A22")
@pytest.mark.asyncio
async def test_level_written_after_restart_waits_for_the_allow_list_then_applies():
    # The flag service restarted (new epoch e2). The interventions stream already
    # reconnected and holds a level; the agent stream still holds the
    # restrictive pre-restart allow-list.
    interventions = InterventionsStore(("e2", 7))
    interventions.per_serial[("player_sensitivity_factor", "MOCK0001")] = ("r_e2_1", 0.6)
    agent = AgentStore(("e1", 41), AMBIENT)
    h = Harness(interventions, agent)

    await h.flag_change()
    assert h.outcomes("adjust_player_sensitivity") == []  # deferred: neither applied nor blocked

    # The agent stream catches up with the allow-list flagd serves now.
    agent.revision, agent.allowed = ("e2", 7), STANDARD
    await h.flag_change()
    assert h.outcomes("adjust_player_sensitivity") == [("false", "")]


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_state_value_newer_than_the_allow_list_is_not_lost():
    interventions = InterventionsStore(("e1", 12))
    interventions.state["music_tempo_override"] = 1.2
    agent = AgentStore(("e1", 11), AMBIENT)  # same epoch, one write behind
    h = Harness(interventions, agent)

    await h.flag_change()
    assert h.outcomes("adjust_music_tempo") == []

    agent.revision, agent.allowed = ("e1", 12), STANDARD
    await h.flag_change()
    assert h.outcomes("adjust_music_tempo") == [("false", "")]
    await h.flag_change()  # applied once: the baseline moved with the apply
    assert h.outcomes("adjust_music_tempo") == [("false", "")]


# --------------------------------------------------------------------------- #
# Wider stale allow-list: the second check stays fail closed
# --------------------------------------------------------------------------- #


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.uc("UC-040", flow="A22")
@pytest.mark.asyncio
async def test_event_is_not_applied_against_a_wider_stale_allow_list():
    # The restart dropped a `full` overlay: flagd now serves `ambient`, but the
    # agent stream has not reconnected yet and still holds `full`. A manual
    # elimination (which skips the agent's own check) lands on the reconnected
    # interventions stream.
    interventions = InterventionsStore(("e2", 3))
    interventions.events["eliminate_player"] = {
        "epoch": "e2",
        "seq": 1,
        "events": [{"seq": 1, "payload": {"serial": "MOCK0001"}}],
    }
    agent = AgentStore(("e1", 90), FULL)
    h = Harness(interventions, agent)

    await h.flag_change()
    assert h.game.killed == []
    assert h.outcomes("eliminate_player") == []

    agent.revision, agent.allowed = ("e2", 3), AMBIENT
    await h.flag_change()
    assert h.game.killed == []
    assert h.outcomes("eliminate_player") == [("true", "not_allowed")]


@pytest.mark.uc("UC-040", flow="A22")
@pytest.mark.asyncio
async def test_ambient_allow_list_blocks_eliminate_player_and_victim_stays_alive():
    # Same stamp on both sets, so no deferral: the plain coordinator re-check.
    # `ambient` does not name eliminate_player (replaces the removed
    # integration test test_intervention_flow.py, #1422).
    assert "eliminate_player" not in AMBIENT.split(",")
    interventions = InterventionsStore(("e1", 3))
    interventions.events["eliminate_player"] = {
        "epoch": "e1",
        "seq": 1,
        "events": [{"seq": 1, "payload": {"serial": "MOCK0001"}}],
    }
    h = Harness(interventions, AgentStore(("e1", 3), AMBIENT))

    await h.flag_change()

    assert h.outcomes("eliminate_player") == [("true", "not_allowed")]
    assert h.game.killed == []
    assert h.game.players["MOCK0001"].alive


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_grant_is_not_applied_against_a_wider_stale_allow_list():
    interventions = InterventionsStore(("e1", 20))
    interventions.per_serial[("shield_seconds", "MOCK0002")] = ("r_e1_5", 3.0)
    agent = AgentStore(("e1", 19), STANDARD)  # still allows grant_shield
    h = Harness(interventions, agent)

    await h.flag_change()
    assert h.outcomes("grant_shield") == []

    agent.revision, agent.allowed = ("e1", 20), AMBIENT  # the kill switch landed in the same push
    await h.flag_change()
    assert h.outcomes("grant_shield") == [("true", "not_allowed")]


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_unstamped_allow_list_defers_a_stamped_value():
    """Fail closed: a stamped interventions set is never checked against an
    allow-list with no stamp (e.g. one from a flag service before #1552)."""
    interventions = InterventionsStore(("e2", 1))
    interventions.state["volume_override"] = 0.4
    agent = AgentStore(None, AMBIENT)
    h = Harness(interventions, agent)

    await h.flag_change()
    assert h.outcomes("adjust_volume") == []

    agent.revision = ("e2", 1)
    await h.flag_change()
    assert h.outcomes("adjust_volume") == [("false", "")]


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_newer_allow_list_and_unstamped_sets_apply_at_once():
    # An allow-list newer than the interventions set is the fresher truth.
    interventions = InterventionsStore(("e1", 5))
    interventions.state["volume_override"] = 0.4
    h = Harness(interventions, AgentStore(("e1", 9), AMBIENT))
    await h.flag_change()
    assert h.outcomes("adjust_volume") == [("false", "")]

    # Unstamped sets (file-sourced flagd) keep the previous behaviour.
    interventions = InterventionsStore(None)
    interventions.state["volume_override"] = 0.4
    h = Harness(interventions, AgentStore(None, AMBIENT))
    await h.flag_change()
    assert h.outcomes("adjust_volume") == [("false", "")]


# --------------------------------------------------------------------------- #
# Read order: value, then probe, then allow-list (review of #1559, finding 5)
# --------------------------------------------------------------------------- #


class BumpOnValueRead(InterventionsStore):
    """A store whose revision moves on the first read of one flag's value: a
    write lands between the value read and the probe. A gate that probed
    before reading the value would see the older stamp and let a newer value
    through against the older allow-list."""

    def __init__(self, revision, bump_key, bumped_revision):
        super().__init__(revision)
        self.bump_key, self.bumped_revision = bump_key, bumped_revision

    def _bump(self, key):
        if key == self.bump_key and self.bumped_revision is not None:
            self.revision, self.bumped_revision = self.bumped_revision, None

    def get_float_value(self, key, default, _ctx=None):
        self._bump(key)
        return super().get_float_value(key, default, _ctx)

    def get_float_details(self, key, default, ctx=None):
        self._bump(key)
        return super().get_float_details(key, default, ctx)


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_state_gate_probes_after_the_value_read():
    interventions = BumpOnValueRead(("e1", 12), "music_tempo_override", ("e1", 13))
    interventions.state["music_tempo_override"] = 1.2  # written at version 13
    agent = AgentStore(("e1", 12), AMBIENT)  # the allow-list of version 12 forbids tempo
    h = Harness(interventions, agent)

    await h.flag_change()
    assert h.outcomes("adjust_music_tempo") == []  # deferred, not blocked against version 12

    agent.revision, agent.allowed = ("e1", 13), STANDARD
    await h.flag_change()
    assert h.outcomes("adjust_music_tempo") == [("false", "")]


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_level_gate_probes_after_the_value_read():
    interventions = BumpOnValueRead(("e1", 12), "player_sensitivity_factor", ("e1", 13))
    interventions.per_serial[("player_sensitivity_factor", "MOCK0001")] = ("r_e1_13", 0.6)
    agent = AgentStore(("e1", 12), AMBIENT)
    h = Harness(interventions, agent)

    await h.flag_change()
    assert h.outcomes("adjust_player_sensitivity") == []

    agent.revision, agent.allowed = ("e1", 13), STANDARD
    await h.flag_change()
    assert h.outcomes("adjust_player_sensitivity") == [("false", "")]


# --------------------------------------------------------------------------- #
# Fail closed on a probe that raises (finding 6)
# --------------------------------------------------------------------------- #


class ProbeRaises(InterventionsStore):
    def __init__(self, revision):
        super().__init__(revision)
        self.raising = True

    def get_object_details(self, key, default, _ctx=None):
        if key == "end_game" and self.raising:
            raise RuntimeError("store unavailable")
        return super().get_object_details(key, default, _ctx)


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_probe_that_raises_defers_instead_of_applying_ungated():
    interventions = ProbeRaises(("e1", 4))
    interventions.state["volume_override"] = 0.4
    h = Harness(interventions, AgentStore(("e1", 4), AMBIENT))

    await h.flag_change()
    assert h.outcomes("adjust_volume") == []

    interventions.raising = False
    await h.flag_change()
    assert h.outcomes("adjust_volume") == [("false", "")]


# --------------------------------------------------------------------------- #
# A deferral is visible (finding 1)
# --------------------------------------------------------------------------- #


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_a_lasting_deferral_is_counted_aged_and_warned(caplog):
    interventions = InterventionsStore(("e2", 7))
    interventions.state["volume_override"] = 0.4
    agent = AgentStore(("e1", 41), AMBIENT)  # stuck on the pre-restart epoch
    clock = Clock()
    h = Harness(interventions, agent, time_fn=clock)

    with (
        patch("services.game_coordinator.metrics.intervention_deferrals_total") as episodes,
        patch("services.game_coordinator.metrics.intervention_deferral_age_seconds") as age,
        caplog.at_level(logging.DEBUG, logger="services.game_coordinator.interventions"),
    ):
        await h.flag_change()
        assert episodes.inc.call_count == 1  # one episode, however many specs deferred
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert h.mgr._deferral_recheck is not None  # re-checked even if no flag changes

        clock.now += 6.0
        await h.flag_change()
        assert episodes.inc.call_count == 1
        assert age.set.call_args.args[0] == pytest.approx(6.0)
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1  # rate limited: one per DEFERRAL_WARN_INTERVAL_S
        assert "('e2', 7)" in warnings[0] and "('e1', 41)" in warnings[0]

        agent.revision = ("e2", 7)
        await h.flag_change()
        assert age.set.call_args.args[0] == 0
        assert h.mgr._deferral_recheck is None
        assert h.outcomes("adjust_volume") == [("false", "")]


# --------------------------------------------------------------------------- #
# Probe cost (finding 4)
# --------------------------------------------------------------------------- #


class CountingAgent(AgentStore):
    def __init__(self, revision, allowed):
        super().__init__(revision, allowed)
        self.snapshot_reads = 0

    def get_string_details(self, key, default, _ctx=None):
        if key == "interventions_allowed":
            self.snapshot_reads += 1
        return super().get_string_details(key, default, _ctx)


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_allow_list_stamp_is_read_once_per_pass():
    agent = CountingAgent(("e1", 3), AMBIENT)
    h = Harness(InterventionsStore(("e1", 3)), agent)
    await h.flag_change()
    assert agent.snapshot_reads == 1


class _Provider:
    def __init__(self, store):
        self.store = store
        self.calls = 0

    def resolve_object_details(self, key, default, ctx=None):
        self.calls += 1
        return self.store.get_object_details(key, default, ctx)


class HooklessInterventions(InterventionsStore):
    """A client with a provider: the probe goes to the provider, so the
    OpenFeature hooks (evaluation metrics and spans) never see it."""

    def __init__(self, revision):
        super().__init__(revision)
        self.provider = _Provider(InterventionsStore(revision))
        self.client_probes = 0

    def get_object_details(self, key, default, _ctx=None):
        if key == "end_game":
            self.client_probes += 1
        return super().get_object_details(key, default, _ctx)


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_probe_reads_through_the_provider_without_hooks():
    interventions = HooklessInterventions(("e1", 3))
    h = Harness(interventions, AgentStore(("e1", 3), AMBIENT))
    await h.flag_change()
    assert interventions.provider.calls > 0
    # Only the end_game spec's own value read goes through the client.
    assert interventions.client_probes == 1


# --------------------------------------------------------------------------- #
# Episode boundaries and shutdown
# --------------------------------------------------------------------------- #


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_deferral_open_when_the_last_game_ends_does_not_leak_into_the_next(caplog):
    interventions = InterventionsStore(("e2", 7))
    interventions.state["volume_override"] = 0.4
    agent = AgentStore(("e1", 41), AMBIENT)
    clock = Clock()
    h = Harness(interventions, agent, time_fn=clock)

    with (
        patch("services.game_coordinator.metrics.intervention_deferrals_total") as episodes,
        patch("services.game_coordinator.metrics.intervention_deferral_age_seconds") as age,
        caplog.at_level(logging.DEBUG, logger="services.game_coordinator.interventions"),
    ):
        await h.flag_change()
        assert episodes.inc.call_count == 1

        # The last game ends while the deferral is open; the next pass sees no session.
        h.mgr._get_sessions = lambda: []
        clock.now += 60.0
        await h.flag_change()
        assert h.mgr._deferral_since is None
        assert h.mgr._deferral_recheck is None

        # The next game defers briefly: a NEW episode with a small age, no WARNING.
        h.mgr._get_sessions = lambda: [h.view]
        clock.now += 1.0
        await h.flag_change()
        assert episodes.inc.call_count == 2
        assert age.set.call_args.args[0] == pytest.approx(0.0)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_stop_cancels_the_pending_deferral_recheck():
    interventions = InterventionsStore(("e2", 7))
    interventions.state["volume_override"] = 0.4
    agent = AgentStore(("e1", 41), AMBIENT)
    h = Harness(interventions, agent)

    await h.flag_change()
    handle = h.mgr._deferral_recheck
    assert handle is not None and not handle.cancelled()

    h.mgr._started = True
    h.mgr.stop()
    assert handle.cancelled()
    assert h.mgr._deferral_recheck is None
