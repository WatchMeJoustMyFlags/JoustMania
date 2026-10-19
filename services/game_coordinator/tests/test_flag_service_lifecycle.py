"""
Game lifecycle reports to the flag service (FR-135, UC-053 main / A6 / A10, #1410).

A real local HTTP server stands in for the flag service, so the requests the
coordinator sends (paths, bodies, timing) are what the service receives.
"""

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from services.game_coordinator.flag_service_client import (
    FlagServiceClient,
    FlagServiceLifecycle,
    GameRecord,
)


class FakeFlagService:
    """Records requests; ``delay`` slows every response; ``fail`` answers 503;
    ``epoch`` is the boot epoch every response carries (change it to simulate a
    restarted service). ``completed`` records requests in the order they finished."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.completed: list[tuple[str, str]] = []
        self.delay = 0.0
        self.register_delay = 0.0  # slows only PUT /games/{id}
        self.fail = False
        self.epoch = "boot-a"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                owner.requests.append((self.command, self.path, body))
                time.sleep(owner.delay)
                if self.command == "PUT" and self.path.startswith("/games/"):
                    time.sleep(owner.register_delay)
                owner.completed.append((self.command, self.path))
                status = 503 if owner.fail else (204 if self.command == "DELETE" else 200)
                self.send_response(status)
                self.end_headers()
                if status == 200:
                    self.wfile.write(json.dumps({"epoch": owner.epoch}).encode())

            do_PUT = do_DELETE = do_GET = _handle  # noqa: N815 - BaseHTTPRequestHandler's method names

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def paths(self, method=None):
        return [p for m, p, _ in self.requests if method in (None, m)]


@pytest.fixture
def service():
    s = FakeFlagService()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_metrics():
    with patch("services.game_coordinator.metrics.flag_service_calls_total"):
        yield


async def wait_for(predicate, limit=3.0):
    deadline = time.monotonic() + limit
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


GAME = GameRecord(game_id="game_0a1b2c3d4e5f", game_kind="real", serials=("MOCK0001", "MOCK0002"))


@pytest.mark.uc("UC-053", flow="main", br=["BR-003"])
@pytest.mark.asyncio
async def test_register_then_end(service):
    live = [GAME]
    lc = FlagServiceLifecycle(FlagServiceClient(service.url), lambda: list(live), instance="host/abcd")
    lc.start()
    # Step 1: the start-up reconcile announces this instance's games
    await wait_for(lambda: service.paths("PUT") == ["/coordinators/host%2Fabcd/games"])
    # Step 2: register before RUNNING
    assert await lc.register(GAME) is True
    method, path, body = service.requests[-1]
    assert (method, path) == ("PUT", "/games/game_0a1b2c3d4e5f")
    assert body == {
        "game_id": GAME.game_id,
        "coordinator_instance": "host/abcd",
        "game_kind": "real",
        "serials": ["MOCK0001", "MOCK0002"],
        "experiment_id": "",
        "arm": "",
    }
    # Step 3: end is queued (callable under a lock, from any thread) and sent
    threading.Thread(target=lc.end, args=(GAME.game_id,)).start()
    await wait_for(lambda: service.paths("DELETE") == ["/games/game_0a1b2c3d4e5f"])
    await lc.stop()


@pytest.mark.uc("UC-053", flow="main", steps=[1], cond=["Post-F-2"])
@pytest.mark.asyncio
async def test_slow_registration_is_bounded_at_200ms_and_the_game_starts(service):
    service.delay = 1.0
    lc = FlagServiceLifecycle(FlagServiceClient(service.url, timeout=2.0), lambda: [GAME], instance="h/1")
    started = time.monotonic()
    assert await lc.register(GAME) is False
    assert time.monotonic() - started < 0.4
    assert lc._needs_reconcile.is_set()


@pytest.mark.uc("UC-053", flow="A8", cond=["Post-F-2"])
@pytest.mark.asyncio
async def test_unreachable_service_never_raises_and_reconciles_on_recovery(service):
    live = [GAME]
    lc = FlagServiceLifecycle(FlagServiceClient(service.url), lambda: list(live), instance="h/1", retry_interval=0.05)
    service.fail = True
    assert await lc.register(GAME) is False
    lc.start()
    await wait_for(lambda: len(service.paths("PUT")) >= 3)  # keeps retrying the reconcile
    service.fail = False
    await wait_for(lambda: not lc._needs_reconcile.is_set())
    # A /healthz probe can land after the reconcile, so select by path, not position.
    _, path, body = [r for r in service.requests if r[1].startswith("/coordinators/")][-1]
    assert path == "/coordinators/h%2F1/games" and [g["game_id"] for g in body["games"]] == [GAME.game_id]
    await lc.stop()


@pytest.mark.uc("UC-053", flow="A10")
@pytest.mark.asyncio
async def test_two_coordinators_reconcile_their_own_instances(service):
    a = FlagServiceLifecycle(FlagServiceClient(service.url), lambda: [GAME], instance="host-a/1")
    b = FlagServiceLifecycle(FlagServiceClient(service.url), lambda: [], instance="host-b/2")
    await a.reconcile()
    await b.reconcile()
    paths = service.paths("PUT")
    assert paths == ["/coordinators/host-a%2F1/games", "/coordinators/host-b%2F2/games"]
    assert all(g["coordinator_instance"] == "host-a/1" for g in service.requests[0][2]["games"])


@pytest.mark.uc("UC-053", flow="main")
@pytest.mark.asyncio
async def test_disabled_without_flag_service_url(monkeypatch):
    monkeypatch.delenv("FLAG_SERVICE_URL", raising=False)
    lc = FlagServiceLifecycle.from_env(lambda: [])
    assert not lc.enabled
    lc.start()
    assert await lc.register(GAME) is False
    lc.end("game_x")  # no-op, no error
    await lc.stop()


# --------------------------------------------------------------------------- #
# Servicer integration: register before the game runs, outside _sessions_lock;
# report the end at retire.
# --------------------------------------------------------------------------- #
class RecordingLifecycle:
    def __init__(self, servicer):
        self.servicer = servicer
        self.registered: list[tuple[GameRecord, bool, bool]] = []
        self.ended: list[str] = []

    async def register(self, game):
        session = self.servicer.sessions[game.game_id]
        # (record, lock held?, game thread started?)
        self.registered.append(
            (game, self.servicer._sessions_lock.locked(), getattr(session, "game_thread", None) is not None)
        )
        return True

    def end(self, game_id):
        self.ended.append(game_id)

    async def stop(self):
        pass


@pytest.mark.uc("UC-053", flow="main", br=["BR-003"])
@pytest.mark.asyncio
async def test_servicer_registers_before_the_game_runs_and_ends_at_retire():
    from proto import game_coordinator_pb2
    from services.game_coordinator import game_session as game_session_mod
    from services.game_coordinator.servicer import GameCoordinatorServicer

    servicer = GameCoordinatorServicer()
    rec = RecordingLifecycle(servicer)
    servicer.flag_service = rec
    config = game_coordinator_pb2.StartGameConfig(
        game_name="FFA",
        players=[game_coordinator_pb2.Player(serial=s) for s in ("MOCK0001", "MOCK0002")],
        origin=game_coordinator_pb2.GAME_ORIGIN_MENU,
    )

    class _Span:
        def get_span_context(self):
            return None

    with patch.object(game_session_mod.GameSession, "start_thread", lambda _self: None):
        ok, game_id = await servicer._start_game_from_config(config, _Span())
    assert ok, game_id
    [(record, lock_held, thread_started)] = rec.registered
    assert record.game_id == game_id and record.game_kind == "real"
    assert record.serials == ("MOCK0001", "MOCK0002")
    assert not lock_held and not thread_started
    assert [g.game_id for g in servicer._flag_service_live_games()] == [game_id]

    servicer._retire_session(game_id)
    assert rec.ended == [game_id]
    assert servicer._flag_service_live_games() == []


@pytest.mark.uc("UC-053", flow="A10")
def test_instance_id_is_stable_across_restarts(monkeypatch):
    """A10: the id survives a restart (hostname, or FLAG_SERVICE_INSTANCE), so the
    restarted coordinator's reconcile drops the games its previous process left."""
    from services.game_coordinator import flag_service_client as fsc

    monkeypatch.delenv("FLAG_SERVICE_INSTANCE", raising=False)
    assert fsc.coordinator_instance_id() == fsc.coordinator_instance_id()
    monkeypatch.setenv("FLAG_SERVICE_INSTANCE", "game-coordinator")
    assert fsc.coordinator_instance_id() == "game-coordinator"


def _reconciled_games(service) -> list[list[str]]:
    return [
        [g["game_id"] for g in body["games"]]
        for m, p, body in service.requests
        if m == "PUT" and p.startswith("/coordinators/")
    ]


@pytest.mark.uc("UC-053", flow="A9")
@pytest.mark.asyncio
async def test_restarted_flag_service_gets_the_live_games_again(service):
    """A restarted flag service (new boot epoch) lost every game scope; the
    coordinator re-registers its live games without waiting for a failed call."""
    live = [GAME]
    lc = FlagServiceLifecycle(FlagServiceClient(service.url), lambda: list(live), instance="h/1", retry_interval=0.05)
    lc.start()
    await wait_for(lambda: len(_reconciled_games(service)) == 1)
    assert await lc.register(GAME) is True
    await asyncio.sleep(0.2)  # several epoch checks: same epoch, no reconcile
    assert len(_reconciled_games(service)) == 1

    service.epoch = "boot-b"  # the service restarted
    await wait_for(lambda: len(_reconciled_games(service)) == 2)
    assert _reconciled_games(service)[-1] == [GAME.game_id]
    await lc.stop()


@pytest.mark.uc("UC-053", flow="A9")
@pytest.mark.asyncio
async def test_reconcile_runs_periodically(service):
    lc = FlagServiceLifecycle(
        FlagServiceClient(service.url), lambda: [GAME], instance="h/1", retry_interval=0.02, reconcile_period=0.1
    )
    lc.start()
    await wait_for(lambda: len(_reconciled_games(service)) >= 3)
    await lc.stop()


@pytest.mark.uc("UC-053", flow="main", steps=[1, 7])
@pytest.mark.asyncio
async def test_end_waits_for_a_late_registration(service):
    """A registration that outlived its 200 ms await must not land after the
    game's DELETE (it would re-register an ended game)."""
    lc = FlagServiceLifecycle(FlagServiceClient(service.url, timeout=2.0), lambda: [], instance="h/1")
    await lc.reconcile()
    lc.start()
    service.register_delay = 0.5
    assert await lc.register(GAME) is False  # timed out; the PUT is still in flight
    lc.end(GAME.game_id)
    await wait_for(lambda: ("DELETE", "/games/" + GAME.game_id) in service.completed)
    order = [c for c in service.completed if c[1] == "/games/" + GAME.game_id]
    assert order == [("PUT", "/games/" + GAME.game_id), ("DELETE", "/games/" + GAME.game_id)]
    await lc.stop()


@pytest.mark.uc("UC-053", flow="A10")
@pytest.mark.asyncio
async def test_restarted_coordinator_reconciles_under_the_same_instance(service, monkeypatch):
    """A10 end to end on the client: the first process registers a game; the
    restarted process (fresh lifecycle, no live games, same container) reconciles
    an empty set under the SAME instance path, which the service applies by
    dropping that instance's games (state_test.go TestReconcile_OnlyThatInstance)."""
    monkeypatch.setenv("FLAG_SERVICE_URL", service.url)
    monkeypatch.delenv("FLAG_SERVICE_INSTANCE", raising=False)

    first = FlagServiceLifecycle.from_env(lambda: [GAME])
    assert await first.register(GAME) is True
    await first.stop()

    restarted = FlagServiceLifecycle.from_env(lambda: [])
    restarted.start()
    await wait_for(lambda: len(_reconciled_games(service)) == 1)
    await restarted.stop()

    [(_, register_path, register_body)] = [r for r in service.requests if r[1].startswith("/games/")]
    [(_, reconcile_path, reconcile_body)] = [r for r in service.requests if r[1].startswith("/coordinators/")]
    assert restarted.instance == first.instance == register_body["coordinator_instance"]
    assert reconcile_path == "/coordinators/" + first.instance.replace("/", "%2F") + "/games"
    assert reconcile_body == {"games": []}
