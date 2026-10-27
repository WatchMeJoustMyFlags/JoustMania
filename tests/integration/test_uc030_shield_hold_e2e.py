"""
UC-030 A7: a player holds the shield in a hold-mode FFA game, end to end (#2096).

The real stack on unreserved mock controllers: an operator stage overlay selects
``shield.activation=hold`` (a stack-only flag, UC-040 BR-019) with a full starting
pool, the menu starts an FFA game, and ``SimulateButton`` holds and releases the
trigger. Observable only: the controller's LED and rumble (UC-040 BR-026) and the
game state. Latency is not asserted here (UC-030 BR-004: only the order of events).

The second test adds an agent grant into the empty default pool (UC-040 A36), reads the
``player_shield_energy`` events (UC-035 BR-008) and the hold gauges in VictoriaMetrics
(telemetry.md §6), and measures press-to-protection on the mock path: the
``SimulateButton`` call to the server timestamp of the ``active`` energy event. It is
recorded (``record_property``), not asserted (UC-030 A7 latency note).
"""

import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import pytest
from openfeature.evaluation_context import EvaluationContext

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import controller_manager_mock_pb2
from tests.integration.flag_service import set_player_value, variant_value
from tests.integration.helpers import (
    GameEventCollector,
    ObservabilityObserver,
    async_poll_until,
    flagd_probe_client,
    get_controller_serials,
    get_game_client,
    get_mock_client,
    get_player_states,
    intervention_env,  # binds the flag-service client, pins the rate-limit budget
    kill_player_verified,
    poll_until,
    set_interventions_allowed,
    setup_mock_controllers,
    start_game_via_menu,
    wait_until_running,
)

_WHITE = (255, 255, 255)
_HOLD_RUMBLE = 70  # shield.hold_rumble_intensity default (flags.md §4)
_TRIGGER = controller_manager_mock_pb2.ButtonRequest.Button.TRIGGER
_GRANT_SECONDS = (
    3  # below the 5 s cap (shield.energy_max_seconds default): no pool_full refusal
)
_HELD_SECONDS = 1.5
_VICTORIA_METRICS_API = "http://127.0.0.1:8428/api/v1"
# Gauges reach VictoriaMetrics with the OTLP export interval (about 3 s) plus the remote write.
_GAUGE_TIMEOUT_SECONDS = 30.0

__all__ = ["intervention_env"]


async def _trigger(mock_client, serial: str, pressed: bool) -> None:
    await mock_client.SimulateButton(
        controller_manager_mock_pb2.ButtonRequest(
            serial=serial, button=_TRIGGER, pressed=pressed
        )
    )


def _since(observer: ObservabilityObserver, serial: str, mark: int):
    """LED colours and rumble intensities on ``serial`` from observer event ``mark`` on."""
    events = [e for e in observer.get_events()[mark:] if e.serial == serial]
    leds = [
        (e.led_change.r, e.led_change.g, e.led_change.b)
        for e in events
        if e.HasField("led_change")
    ]
    rumbles = [e.rumble_change.intensity for e in events if e.HasField("rumble_change")]
    return leds, rumbles


async def _alive(game_client, game_id: str, serial: str) -> bool:
    players = await get_player_states(game_client, game_id=game_id)
    return bool(players) and players[serial].alive


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
@pytest.mark.uc("UC-040", br=["BR-019", "BR-026"])
async def test_uc030_a7_holding_the_trigger_protects_and_shows_white_with_a_rumble(
    docker_compose, ensure_game_stopped, flag_service
):
    """UC-030 A7: held = white LED, light rumble, no death; released = rumble off, the next death lands."""
    mock_client, _mock_channel = await get_mock_client(docker_compose)
    game_client, _game_channel = await get_game_client(docker_compose)

    # Step 1: the operator selects hold with a full starting pool (stage overlays, cleared at teardown).
    flag_service.put("game", "shield.activation", variant="hold")
    flag_service.put("game", "shield.energy_initial_seconds", variant="full")
    with flagd_probe_client(docker_compose, "game") as probe:
        assert poll_until(
            lambda: (
                probe.get_string_value("shield.activation", "", EvaluationContext())
                == "hold"
                and probe.get_float_value(
                    "shield.energy_initial_seconds", 0.0, EvaluationContext()
                )
                == 5.0
            ),
        ), "Step 1: flagd did not serve shield.activation=hold with a 5 s starting pool"

    await setup_mock_controllers(docker_compose, count=2)
    collector = GameEventCollector(game_client)
    observer = ObservabilityObserver(mock_client)
    holder = None
    try:
        await collector.start()
        await observer.start()

        # Step 2: an FFA game through the menu (a real game: shadow games never run hold).
        await start_game_via_menu(docker_compose, "JoustFFA", event_collector=collector)
        game_id = collector.get_events("game_started")[-1].game_id
        serials = await get_controller_serials(docker_compose)
        running = await wait_until_running(game_client, {game_id}, timeout=15.0)
        assert game_id in running, f"Step 2: {game_id} never RUNNING ({running})"
        holder = serials[0]
        await asyncio.sleep(
            1.0
        )  # past the 0.3 s start grace; the trigger reads released once (armed)

        # Step 3: hold the trigger: the controller turns white and rumbles lightly (UC-040 BR-026).
        mark = len(observer.get_events())
        await _trigger(mock_client, holder, True)

        def _held_feedback() -> bool:
            leds, rumbles = _since(observer, holder, mark)
            return _WHITE in leds and _HOLD_RUMBLE in rumbles

        assert await async_poll_until(_held_feedback, timeout=5.0), (
            f"Step 3: no white LED with the {_HOLD_RUMBLE} hold rumble on {holder}: "
            f"{_since(observer, holder, mark)}"
        )

        # Step 4: a death-level motion while held does not kill (UC-030 A7 step 2).
        for _ in range(3):
            response = await mock_client.SimulateDeath(
                controller_manager_mock_pb2.DeathRequest(serial=holder)
            )
            assert response.success
            await asyncio.sleep(0.2)
        assert await _alive(game_client, game_id, holder), (
            f"Step 4: {holder} died while holding the shield"
        )

        # Step 5: release: the hold rumble stops and the LED leaves white (UC-030 A7 steps 3-4).
        mark = len(observer.get_events())
        await _trigger(mock_client, holder, False)

        def _released_feedback() -> bool:
            leds, rumbles = _since(observer, holder, mark)
            return 0 in rumbles and bool(leds) and leds[-1] != _WHITE

        assert await async_poll_until(_released_feedback, timeout=5.0), (
            f"Step 5: the hold feedback did not end on {holder}: {_since(observer, holder, mark)}"
        )

        # Step 6: the protection is gone: the next death lands.
        landed = await kill_player_verified(
            mock_client, game_client, holder, lambda p: not p.alive, game_id=game_id
        )
        assert landed or game_id not in await wait_until_running(
            game_client, {game_id}, timeout=0
        ), f"Step 6: {holder} could not be killed after the release"
    finally:
        if holder is not None:
            await _trigger(mock_client, holder, False)
        await observer.stop()
        await collector.stop()


def _energy_events(
    collector: GameEventCollector, game_id: str, serial: str, mark: int = 0
) -> list:
    """``player_shield_energy`` events of ``serial`` in ``game_id`` from collector event ``mark`` on."""
    return [
        e
        for e in collector.get_events()[mark:]
        if e.event_type == "player_shield_energy"
        and e.game_id == game_id
        and e.data.get("serial") == serial
    ]


def _vm_value(query: str) -> float | None:
    """The single value of an instant VictoriaMetrics query; None while no series (or no answer)."""
    url = f"{_VICTORIA_METRICS_API}/query?query={urllib.parse.quote(query)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - loopback test URL
            result = json.load(response)["data"]["result"]
    except OSError:
        return None
    return float(result[0]["value"][1]) if result else None


async def _gauge_matches(query: str, check) -> tuple[bool, float | None]:
    seen: dict[str, float | None] = {"value": None}

    def _ok() -> bool:
        seen["value"] = _vm_value(query)
        return seen["value"] is not None and check(seen["value"])

    ok = await async_poll_until(_ok, timeout=_GAUGE_TIMEOUT_SECONDS, interval=1.0)
    return ok, seen["value"]


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="A7")
@pytest.mark.uc("UC-040", flow="A36")
@pytest.mark.uc("UC-035", br=["BR-008"])
async def test_uc030_a7_uc040_a36_grant_hold_release_energy_events_and_gauges(
    intervention_env, docker_compose, ensure_game_stopped, flag_service, record_property
):
    """UC-040 A36 + UC-030 A7: a grant fills the empty pool, a hold drains it, the events and gauges follow."""
    mock_client, _mock_channel = await get_mock_client(docker_compose)
    game_client, _game_channel = await get_game_client(docker_compose)

    # Step 1: hold with the default empty pool and no recharge; grant_shield on the allow-list.
    flag_service.put("game", "shield.activation", variant="hold")
    set_interventions_allowed("standard")
    allowed = variant_value("agent", "interventions_allowed", "standard")
    with (
        flagd_probe_client(docker_compose, "game") as game_probe,
        flagd_probe_client(docker_compose, "agent") as agent_probe,
    ):
        ctx = EvaluationContext()
        assert poll_until(
            lambda: (
                game_probe.get_string_value("shield.activation", "", ctx) == "hold"
                and game_probe.get_float_value(
                    "shield.energy_initial_seconds", -1.0, ctx
                )
                == 0.0
                and game_probe.get_float_value("shield.recharge_per_second", -1.0, ctx)
                == 0.0
                and agent_probe.get_string_value("interventions_allowed", "", ctx)
                == allowed
            ),
        ), (
            "Step 1: flagd did not serve hold, an empty pool, no recharge and grant_shield allowed"
        )

    await setup_mock_controllers(docker_compose, count=2)
    collector = GameEventCollector(game_client)
    holder = None
    try:
        await collector.start()

        # Step 2: a real FFA game through the menu.
        await start_game_via_menu(docker_compose, "JoustFFA", event_collector=collector)
        game_id = collector.get_events("game_started")[-1].game_id
        serials = await get_controller_serials(docker_compose)
        running = await wait_until_running(game_client, {game_id}, timeout=15.0)
        assert game_id in running, f"Step 2: {game_id} never RUNNING ({running})"
        holder = serials[0]
        await asyncio.sleep(
            1.0
        )  # past the start grace; the trigger reads released once (armed)

        # Step 3: the pool starts empty (energy keyframes, UC-035 BR-008).
        assert await async_poll_until(
            lambda: bool(_energy_events(collector, game_id, holder)), timeout=5.0
        ), f"Step 3: no player_shield_energy for {holder}"
        first = _energy_events(collector, game_id, holder)[0]
        assert first.data["state"] == "empty" and first.data["energy_ms"] == "0", dict(
            first.data
        )

        # Step 4: the agent grants 3 s: energy 3 s, state charged, source agent (UC-040 A36).
        mark = len(collector.get_events())
        flag_service.wait_game_registered(game_id)
        set_player_value("shield_seconds", game_id, holder, _GRANT_SECONDS)

        def _granted() -> bool:
            return any(
                e.data.get("source") == "agent" and e.data.get("state") == "charged"
                for e in _energy_events(collector, game_id, holder, mark)
            )

        assert await async_poll_until(_granted, timeout=12.0), (
            f"Step 4: no charged energy event with source=agent: "
            f"{[dict(e.data) for e in _energy_events(collector, game_id, holder, mark)]}"
        )
        grants = [
            e
            for e in collector.get_events("agent_intervention")
            if e.game_id == game_id
            and e.data.get("type") == "grant_shield"
            and e.data.get("blocked") == "false"
        ]
        assert grants, (
            f"Step 4: no applied grant_shield agent_intervention: {collector.get_events('agent_intervention')}"
        )
        granted = [
            e
            for e in _energy_events(collector, game_id, holder, mark)
            if e.data.get("source") == "agent"
        ][-1]
        energy_before_ms = int(granted.data["energy_ms"])
        assert energy_before_ms >= _GRANT_SECONDS * 1000, dict(granted.data)

        # Step 5: hold the trigger: the energy event turns active (UC-030 A7 step 1).
        mark = len(collector.get_events())
        pressed_at_ms = time.time() * 1000.0
        await _trigger(mock_client, holder, True)

        def _active() -> bool:
            return any(
                e.data.get("state") == "active"
                for e in _energy_events(collector, game_id, holder, mark)
            )

        assert await async_poll_until(_active, timeout=5.0), (
            f"Step 5: no active energy event: {[dict(e.data) for e in _energy_events(collector, game_id, holder, mark)]}"
        )
        active = next(
            e
            for e in _energy_events(collector, game_id, holder, mark)
            if e.data.get("state") == "active"
        )
        assert active.data["rate_ms_per_s"] == "-1000", dict(active.data)
        assert 0 < int(active.data["remaining_ms"]) <= energy_before_ms, dict(
            active.data
        )
        # Press-to-protection on the mock path: the protection starts in the frame that emits this event.
        latency_ms = active.timestamp - pressed_at_ms
        record_property("hold_press_to_protection_ms", round(latency_ms, 1))
        print(f"UC-030 A7: press-to-protection on the mock path {latency_ms:.1f} ms")

        # Step 6: a death-level motion while held does not kill (UC-030 A7 step 2).
        for _ in range(3):
            response = await mock_client.SimulateDeath(
                controller_manager_mock_pb2.DeathRequest(serial=holder)
            )
            assert response.success
            await asyncio.sleep(0.2)
        assert await _alive(game_client, game_id, holder), (
            f"Step 6: {holder} died while holding the shield"
        )
        await asyncio.sleep(max(0.0, _HELD_SECONDS - 0.6))

        # Step 7: release: the pool keeps the rest, the hold drained it (UC-030 A7 steps 3-4).
        mark = len(collector.get_events())
        await _trigger(mock_client, holder, False)

        def _released() -> bool:
            return any(
                e.data.get("state") == "charged"
                for e in _energy_events(collector, game_id, holder, mark)
            )

        assert await async_poll_until(_released, timeout=5.0), (
            f"Step 7: no charged energy event after the release: "
            f"{[dict(e.data) for e in _energy_events(collector, game_id, holder, mark)]}"
        )
        released = next(
            e
            for e in _energy_events(collector, game_id, holder, mark)
            if e.data.get("state") == "charged"
        )
        energy_after_ms = int(released.data["energy_ms"])
        used_ms = energy_before_ms - energy_after_ms
        assert 0 < energy_after_ms < energy_before_ms, dict(released.data)
        assert used_ms >= (_HELD_SECONDS - 0.2) * 1000, (
            f"Step 7: only {used_ms} ms drained: {dict(released.data)}"
        )
        budget_used_ms = int(released.data["budget_ms"]) - int(
            released.data["budget_remaining_ms"]
        )
        assert abs(budget_used_ms - used_ms) <= 2, dict(released.data)

        # Step 8: the gauges agree (telemetry.md §6; the agent's view, UC-040 A37).
        player = f'serial="{holder}",game_id="{game_id}"'

        def total(name: str) -> str:
            return f'game_player_shield_total{{{player},total="{name}"}}'

        checks = [
            (
                f'game_shield_activation_mode{{mode="hold",game_id="{game_id}"}}',
                lambda v: v == 1,
            ),
            (f"game_player_shield_active{{{player}}}", lambda v: v == 0),
            (
                f"game_player_shield_energy_seconds{{{player}}}",
                lambda v: abs(v * 1000 - energy_after_ms) <= 50,
            ),
            (total("granted_seconds"), lambda v: v >= _GRANT_SECONDS),
            (total("activations"), lambda v: v >= 1),
            (total("used_seconds"), lambda v: abs(v * 1000 - used_ms) <= 50),
        ]
        for query, check in checks:
            ok, value = await _gauge_matches(query, check)
            assert ok, f"Step 8: {query} = {value}"
        assert await _alive(game_client, game_id, holder), f"Step 8: {holder} died"
    finally:
        if holder is not None:
            await _trigger(mock_client, holder, False)
        await collector.stop()
