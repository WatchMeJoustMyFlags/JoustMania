"""
TC-002: FFA game to a winner on mock controllers (docs/test_cases/TC-002-ffa-mock-game-to-winner.md).

One journey through the real stack on four unreserved mock controllers: the
menu lobby, the web ``select_game`` command, ready-up, an FFA game played to a
single winner, the lobby restore, and a second game started from that lobby
(step 9: an ended primary session must not pin the slot, #793).

Every assertion is scoped to the game ids this test starts. Steps carry a
``# Step n`` comment matching the TC Flow table; failure messages name the step.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from lib.colors import Colors
from proto import menu_pb2
from tests.integration.helpers import (
    GameEventCollector,
    MenuEventCollector,
    ObservabilityObserver,
    async_poll_until,
    force_end_game_by_id,
    get_controller_color,
    get_mock_controller_serials,
    get_game_client,
    get_menu_client,
    get_mock_client,
    jaeger_has_root_span,
    kill_player_verified,
    mark_controllers_ready,
    select_game_mode,
    setup_mock_controllers,
    wait_for_lobby_colors,
    wait_until_running,
)

# Root span of an FFA game (the coordinator's get_game_name()).
_FFA_ROOT_SPAN = "Joust Free-for-All"
# Collector batch + Jaeger indexing lag behind the game; same bound as UC-035 A3.
_JAEGER_TIMEOUT = 60.0

_TERMINAL_EVENTS = ("game_ended", "game_force_ended", "game_error")

# The menu lights CONNECTED controllers in the selected mode's colour at 30 %
# brightness (services/menu/utils/led.py GAME_MODE_COLORS + DIM_FACTOR).
_DIM_FACTOR = 0.3
FFA_LOBBY_COLOR = tuple(int(c * _DIM_FACTOR) for c in Colors.Orange.value)
# Brightest channel any CONNECTED (dimmed) lobby colour can have.
_MAX_DIM_CHANNEL = int(255 * _DIM_FACTOR) + 1
# Exact dimmed colour, so only integer rounding is tolerated.
_LOBBY_TOLERANCE = 2
# Rainbow is a 20 Hz HSV cycle for winner_rainbow_duration_ms (CI: 1000 ms);
# a static colour or a two-colour flash never reaches this many distinct colours.
_MIN_RAINBOW_COLORS = 5
# UC-030 step 9 death effect (feedback_manager._effect_player_death): rumble at
# full intensity, solid red, then a fade that ends at black.
_DEATH_RED = Colors.Red.value
_BLACK = (0, 0, 0)
_DEATH_RUMBLE = 255


def _death_effect_trace(
    observer: ObservabilityObserver, serial: str, death_ms: int
) -> tuple[list, list]:
    """LED colours and rumble intensities on ``serial`` since its player_death."""
    since = death_ms - 500  # coordinator and controller-manager share the host clock
    leds = [
        (e.led_change.r, e.led_change.g, e.led_change.b)
        for e in observer.get_led_events(serial)
        if e.timestamp_ms >= since
    ]
    rumbles = [
        e.rumble_change.intensity
        for e in observer.get_events()
        if e.serial == serial
        and e.HasField("rumble_change")
        and e.timestamp_ms >= since
    ]
    return leds, rumbles


def _death_effect_seen(
    observer: ObservabilityObserver, serial: str, death_ms: int
) -> bool:
    """Red, then black, and the full-intensity rumble, all after the player_death.

    The black write ends the 300 ms red hold + 700 ms fade, so for the last
    victim it lands about 1.0-1.1 s after the death, i.e. around the time
    game_ended arrives: callers poll this instead of checking once.
    Caveat: when update_all_leds() holds _led_lock, the direct write is skipped
    and the 20 Hz loop sends only the latest colour
    (services/controller_manager/multiplexer/multiplexer_backend.py:471), so the
    black can merge with the menu's lobby-colour restore and never appear.
    """
    leds, rumbles = _death_effect_trace(observer, serial, death_ms)
    return (
        _DEATH_RED in leds
        and _BLACK in leds[leds.index(_DEATH_RED) :]
        and _DEATH_RUMBLE in rumbles
    )


async def _assert_death_effect(
    observer: ObservabilityObserver, serial: str, death_ms: int
) -> None:
    """The victim's LED goes red and then black, and it rumbles (UC-030 step 9)."""
    seen = await async_poll_until(
        lambda: _death_effect_seen(observer, serial, death_ms), timeout=3.0
    )
    leds, rumbles = _death_effect_trace(observer, serial, death_ms)
    assert seen, (
        f"Step 6: no death effect (red, then black, rumble {_DEATH_RUMBLE}) on "
        f"{serial} after its player_death: leds={leds} rumbles={rumbles}"
    )


def _events_for(
    collector: GameEventCollector, game_id: str, event_type: str | None = None
) -> list:
    """Events of one game (optionally one type) from an unfiltered primary collector."""
    return [e for e in collector.get_events(event_type) if e.game_id == game_id]


def _game_started_ids(collector: GameEventCollector) -> list[str]:
    return [e.game_id for e in collector.get_events("game_started") if e.game_id]


async def _ready_up_until_started(
    mock_client, serials, collector, known_ids, timeout=25.0
) -> str:
    """Press the trigger on every controller; return the id of the NEW game that starts.

    Retries the ready presses once, like ``start_game_via_menu``: a single
    dropped press under CI load otherwise hard-fails the step.
    """

    def _new_id():
        return next(
            (gid for gid in _game_started_ids(collector) if gid not in known_ids), None
        )

    for _attempt in range(2):
        await mark_controllers_ready(mock_client, serials)
        if await async_poll_until(lambda: _new_id() is not None, timeout=timeout):
            return _new_id()
    seen = [(e.event_type, e.game_id) for e in collector.get_events()]
    raise AssertionError(
        f"no new game_started after ready-up (known {known_ids}); events: {seen}"
    )


async def _wait_for_unique_game_colors(
    mock_client, serials, timeout=10.0
) -> dict[str, tuple]:
    """Poll until every controller shows a non-black colour, all different, none the lobby colour."""
    colors: dict[str, tuple] = {}

    async def _distinct() -> bool:
        for serial in serials:
            colors[serial] = await get_controller_color(mock_client, serial)
        values = list(colors.values())
        return (
            all(sum(c) > 0 for c in values)
            and len(set(values)) == len(values)
            and FFA_LOBBY_COLOR not in values
        )

    assert await async_poll_until(_distinct, timeout=timeout), (
        f"Step 5: controllers did not get unique non-black game colours: {colors}"
    )
    return dict(colors)


def _distinct_colors(
    observer: ObservabilityObserver, serial: str, start_ms: int, end_ms: int
) -> set:
    return {
        (e.led_change.r, e.led_change.g, e.led_change.b)
        for e in observer.get_led_events(serial)
        if start_ms <= e.timestamp_ms <= end_ms
    }


@pytest.mark.asyncio
@pytest.mark.uc("UC-003", flow="main", steps=[1, 2, 3, 4, 5, 6], tc="TC-002")
@pytest.mark.uc("UC-003", tc="TC-002", cond=["Pre-1"])
@pytest.mark.uc("UC-002", flow="main", steps=[6], tc="TC-002")
@pytest.mark.uc("UC-011", flow="A1", tc="TC-002")
@pytest.mark.uc("UC-010", flow="main", steps=[1, 3, 4, 5, 7, 8], tc="TC-002")
@pytest.mark.uc("UC-020", flow="main", steps=[1, 2, 5, 6, 7], tc="TC-002")
@pytest.mark.uc("UC-030", flow="main", steps=[8, 9, 10], tc="TC-002")
async def test_tc002_ffa_game_to_winner_then_restart(
    docker_compose, ensure_game_stopped
):
    """TC-002: four mock controllers play FFA to one winner; the lobby comes back and a new game starts."""
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    menu_client, menu_channel = await get_menu_client(docker_compose)

    collector = GameEventCollector(game_client)  # primary stream, unfiltered
    menu_events = MenuEventCollector(menu_client)
    observer = ObservabilityObserver(mock_client)
    try:
        await collector.start()
        await menu_events.start()
        await observer.start()

        # Step 1: Add mock controllers (four menu-visible, unreserved).
        serials = await setup_mock_controllers(docker_compose, count=4)
        assert len(serials) == 4, f"Step 1: expected 4 mock controllers, got {serials}"
        visible = await get_mock_controller_serials(docker_compose)
        assert sorted(visible) == sorted(serials), (
            f"Step 1: the lobby must hold exactly these 4 unreserved controllers, got {visible}"
        )
        victims, winner = serials[:3], serials[3]

        # Step 2: Verify lobby. Restart the menu so it registers exactly these
        # controllers; each is CONNECTED: LED on, at lobby (30 %) brightness.
        await menu_client.StopMenu(menu_pb2.StopMenuRequest())
        started_menu = await menu_client.StartMenu(menu_pb2.StartMenuRequest())
        assert started_menu.success, f"Step 2: StartMenu failed: {started_menu.error}"
        lobby: dict[str, tuple] = {}

        async def _all_connected() -> bool:
            for serial in serials:
                lobby[serial] = await get_controller_color(mock_client, serial)
            return all(
                0 < sum(c) and max(c) <= _MAX_DIM_CHANNEL for c in lobby.values()
            )

        assert await async_poll_until(_all_connected, timeout=10.0), (
            f"Step 2: not every controller shows a dimmed CONNECTED lobby colour: {lobby}"
        )

        # Step 3: Select FFA through the web command (UC-011 A1 -> step 5 events).
        assert await select_game_mode(menu_client, "JoustFFA"), (
            "Step 3: select_game rejected"
        )
        for menu_event_type in ("selection_changed", "game_selection_changed"):
            evt = await menu_events.wait_for_event(menu_event_type, timeout=5.0)
            assert evt.data["game_name"] == "JoustFFA", (
                f"Step 3: {menu_event_type} names {dict(evt.data)}, expected JoustFFA"
            )

        # Step 4: Ready up; the game starts once all four are READY.
        known_ids = set(_game_started_ids(collector))
        game_id = await _ready_up_until_started(
            mock_client, serials, collector, known_ids
        )

        # Step 5: Verify game colours: players_initialized, game_started, and a
        # unique non-black game colour per controller.
        initialized = _events_for(collector, game_id, "players_initialized")
        assert len(initialized) == 1, (
            f"Step 5: players_initialized x{len(initialized)} for {game_id}"
        )
        assert initialized[0].data["player_count"] == "4", (
            f"Step 5: {dict(initialized[0].data)}"
        )
        # data values are str(): the serial list arrives as "['MOCK0001', 'MOCK0002', ...]".
        initialized_serials = {
            item.strip(" '\"[]") for item in initialized[0].data["serials"].split(",")
        }
        assert initialized_serials == set(serials), (
            f"Step 5: players_initialized serials {initialized_serials} != {set(serials)}"
        )
        running = await wait_until_running(game_client, {game_id}, timeout=15.0)
        assert game_id in running, (
            f"Step 5: {game_id} never RUNNING (running: {running})"
        )
        game_colors = await _wait_for_unique_game_colors(mock_client, serials)

        # Step 6: Eliminate players one after the other; each kill is verified
        # against GetGameState (dropped deaths during warm-up are retried).
        for serial in victims:
            await asyncio.sleep(0.1)
            landed = await kill_player_verified(
                mock_client, game_client, serial, lambda p: not p.alive
            )
            if not landed:
                # Only the last kill may end the game before the poll sees it.
                assert serial == victims[-1], (
                    f"Step 6: game stopped running before {serial} was eliminated"
                )

        # Step 7: Verify game end: the last player wins, game_ended within 15 s.
        ended = await collector.wait_for_any_event(list(_TERMINAL_EVENTS), timeout=15.0)
        assert (ended.event_type, ended.game_id) == ("game_ended", game_id), (
            f"Step 7: first terminal event is {ended.event_type} for {ended.game_id} "
            f"({dict(ended.data)}), expected game_ended for {game_id}"
        )
        assert ended.data["outcome"] == "winner", (
            f"Step 7: game_ended {dict(ended.data)}"
        )
        assert float(ended.data["duration"]) > 0, (
            f"Step 7: game_ended {dict(ended.data)}"
        )

        deaths = _events_for(collector, game_id, "player_death")
        assert [(d.data["serial"], d.data["alive_remaining"]) for d in deaths] == [
            (victims[0], "3"),
            (victims[1], "2"),
            (victims[2], "1"),
        ], (
            f"Step 6: one player_death per eliminated player, got {[dict(d.data) for d in deaths]}"
        )
        for death in deaths:
            await _assert_death_effect(observer, death.data["serial"], death.timestamp)

        winners = _events_for(collector, game_id, "game_winner")
        assert [w.data["serial"] for w in winners] == [winner], (
            f"Step 7: game_winner {[dict(w.data) for w in winners]}, expected {winner}"
        )
        assert not _events_for(collector, game_id, "game_tie"), (
            "Step 7: unexpected game_tie"
        )

        analytics = _events_for(collector, game_id, "player_analytics")
        assert sorted(a.data["serial"] for a in analytics) == sorted(serials), (
            f"Step 7: player_analytics per player, got {[a.data['serial'] for a in analytics]}"
        )
        assert [a.data["serial"] for a in analytics if a.data["winner"] == "True"] == [
            winner
        ], "Step 7: exactly the winner's player_analytics has winner=True"

        rainbow = _distinct_colors(
            observer, winner, winners[0].timestamp - 500, ended.timestamp + 1500
        )
        assert len(rainbow) >= _MIN_RAINBOW_COLORS, (
            f"Step 7: no winner rainbow on {winner}: only {len(rainbow)} colours {rainbow}"
        )

        # Step 8: Verify lobby restored: every controller back to the FFA lobby
        # colour (orange at 30 %), un-readied, within 10 s. The menu lights only
        # CONNECTED controllers at 30 % (READY is full brightness, connected.py /
        # ready.py), so this colour is the observable for "CONNECTED, un-readied".
        await wait_for_lobby_colors(
            mock_client,
            serials,
            expected_color=FFA_LOBBY_COLOR,
            tolerance=_LOBBY_TOLERANCE,
            timeout=10.0,
        )

        # Validation 1: exactly one terminal event for the game, and it is game_ended.
        terminal = [
            e.event_type
            for e in _events_for(collector, game_id)
            if e.event_type in _TERMINAL_EVENTS
        ]
        assert terminal == ["game_ended"], (
            f"Validation 1: terminal events for {game_id}: {terminal}"
        )

        # Validation 2: each controller's LED went from its game colour back to the
        # lobby colour, and the last colour on the stream is the lobby colour (not black).
        for serial in serials:
            stream = [
                (e.led_change.r, e.led_change.g, e.led_change.b)
                for e in observer.get_led_events(serial)
            ]
            assert game_colors[serial] in stream, (
                f"Validation 2: {serial} game colour never on the LED stream"
            )
            last_game = len(stream) - 1 - stream[::-1].index(game_colors[serial])
            lobby_after = [
                c
                for c in stream[last_game + 1 :]
                if all(
                    abs(a - b) <= _LOBBY_TOLERANCE for a, b in zip(c, FFA_LOBBY_COLOR)
                )
            ]
            assert lobby_after, (
                f"Validation 2: {serial} never returned from {game_colors[serial]} to the lobby colour"
            )
            assert sum(stream[-1]) > 0, f"Validation 2: {serial} final LED is black"

        # Step 9: Start the next game. The ended primary session must not pin
        # the slot (#793): the lobby is un-readied (step 8), so a fresh ready-up
        # starts a new game with a new id.
        restart_id = await _ready_up_until_started(
            mock_client, serials, collector, {game_id, *known_ids}
        )
        assert restart_id != game_id, "Step 9: the next game must get a fresh game_id"
        running = await wait_until_running(game_client, {restart_id}, timeout=15.0)
        assert restart_id in running, (
            f"Step 9: next game {restart_id} never RUNNING (running: {running})"
        )

        # Validation 3: the collector delivered the first game's root span to Jaeger.
        assert await async_poll_until(
            lambda: jaeger_has_root_span(game_id, _FFA_ROOT_SPAN),
            timeout=_JAEGER_TIMEOUT,
            interval=1.0,
        ), (
            f"Validation 3: Jaeger has no root span {_FFA_ROOT_SPAN!r} with "
            f"game.id={game_id} after {_JAEGER_TIMEOUT:.0f} s"
        )

        # Cleanup (Postconditions): stop the second game by id. A force-ended FFA
        # game sends game_force_ended (and game_ended with outcome force_ended,
        # UC-027 step 3); that path is TC-004's journey, so only its arrival is checked.
        assert await force_end_game_by_id(
            game_client, restart_id, reason="tc002_cleanup"
        ), "force-end of the second game failed"

        def _restart_force_ended() -> bool:
            return bool(_events_for(collector, restart_id, "game_force_ended"))

        assert await async_poll_until(_restart_force_ended, timeout=15.0), (
            "second game sent no game_force_ended"
        )
    finally:
        await observer.stop()
        await menu_events.stop()
        await collector.stop()
        await menu_channel.close()
        await game_channel.close()
        await mock_channel.close()
