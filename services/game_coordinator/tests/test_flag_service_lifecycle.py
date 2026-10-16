"""
Game lifecycle reports to the flag service (FR-125, UC-053 main / A6 / A10, #1410).

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
    """Records requests; ``delay`` slows every response; ``fail`` answers 503."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.delay = 0.0
        self.fail = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                owner.requests.append((self.command, self.path, body))
                time.sleep(owner.delay)
                status = 503 if owner.fail else (204 if self.command == "DELETE" else 200)
                self.send_response(status)
                self.end_headers()
                if status == 200:
                    self.wfile.write(b"{}")

            do_PUT = do_DELETE = _handle  # noqa: N815 - BaseHTTPRequestHandler's method names

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


@pytest.mark.uc("UC-053", flow="A6")
@pytest.mark.asyncio
async def test_slow_registration_is_bounded_at_200ms_and_the_game_starts(service):
    service.delay = 1.0
    lc = FlagServiceLifecycle(FlagServiceClient(service.url, timeout=2.0), lambda: [GAME], instance="h/1")
    started = time.monotonic()
    assert await lc.register(GAME) is False
    assert time.monotonic() - started < 0.4
    assert lc._needs_reconcile.is_set()


@pytest.mark.uc("UC-053", flow="A8")
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
    _, path, body = service.requests[-1]
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
