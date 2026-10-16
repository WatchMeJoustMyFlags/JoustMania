"""Game lifecycle reports to the flag service (FR-125, UC-053, #1410).

The game coordinator tells the flag service which games are live, so the
service accepts game-scoped flag writes only for registered games and drops a
game's values when it ends (UC-053 BR-003):

- ``register`` (``PUT /games/{id}``) is awaited for at most 200 ms before the
  game is RUNNING, never under ``_sessions_lock``. On a timeout or an error the
  game still starts (C-004) and the lifecycle falls back to reconciling.
- ``end`` (``DELETE /games/{id}``) is queued and sent by a background worker,
  so it can be called from any thread, under any lock.
- ``reconcile`` (``PUT /coordinators/{instance}/games``) sends this
  coordinator's full set of live games: at start (an empty set after a
  restart, UC-053 A10) and whenever a call failed, until it succeeds. It only
  touches this instance's games; a second coordinator (agent-coordinator stack)
  has its own instance id (review C3).

Everything here is best effort: the flag service being down never stops a game.
Disabled when ``FLAG_SERVICE_URL`` is unset (unit tests, stacks without it).
"""

import asyncio
import contextlib
import json
import logging
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from services.game_coordinator import metrics

logger = logging.getLogger(__name__)

# Bounded registration await before the game is RUNNING (FR-125, NFR-052).
REGISTER_AWAIT_SECONDS = 0.2
# Per-request HTTP timeout for the background calls (end, reconcile).
REQUEST_TIMEOUT_SECONDS = 0.5
# How often the worker retries reconciling while the service is unreachable.
RECONCILE_RETRY_SECONDS = 2.0


@dataclass(frozen=True)
class GameRecord:
    """One live game as the flag service registers it (entity GAME_SCOPE)."""

    game_id: str
    game_kind: str  # flag vocabulary: "real" | "shadow" (FR-041, #1249)
    serials: tuple[str, ...] = field(default_factory=tuple)
    experiment_id: str = ""
    arm: str = ""

    def body(self, instance: str) -> dict:
        return {
            "game_id": self.game_id,
            "coordinator_instance": instance,
            "game_kind": self.game_kind,
            "serials": list(self.serials),
            "experiment_id": self.experiment_id,
            "arm": self.arm,
        }


class FlagServiceError(Exception):
    """A flag-service call failed (unreachable, timeout or non-2xx)."""


class FlagServiceClient:
    """Minimal synchronous HTTP/JSON client (stdlib only, run in a thread)."""

    def __init__(self, base_url: str, timeout: float = REQUEST_TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, method=method, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - fixed in-network URL
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise FlagServiceError(f"{method} {path}: HTTP {e.code} {detail}") from e
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
            raise FlagServiceError(f"{method} {path}: {e}") from e


def coordinator_instance_id() -> str:
    """``<hostname>/<boot id>``: unique per coordinator process (review C3)."""
    return f"{socket.gethostname()}/{uuid.uuid4().hex[:8]}"


class FlagServiceLifecycle:
    """Reports game registration, end and reconciliation to the flag service."""

    def __init__(
        self,
        client: FlagServiceClient | None,
        live_games: Callable[[], list[GameRecord]],
        instance: str | None = None,
        register_await: float = REGISTER_AWAIT_SECONDS,
        retry_interval: float = RECONCILE_RETRY_SECONDS,
    ):
        self._client = client
        self._live_games = live_games
        self.instance = instance or coordinator_instance_id()
        self._register_await = register_await
        self._retry_interval = retry_interval
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        # Starts "dirty" so the first worker pass reconciles: after a restart the
        # service may still hold this host's games under an old instance id, but
        # the new instance must at least announce its (empty) set (UC-053 A10).
        self._needs_reconcile = threading.Event()
        self._needs_reconcile.set()

    @classmethod
    def from_env(cls, live_games: Callable[[], list[GameRecord]]) -> "FlagServiceLifecycle":
        url = os.getenv("FLAG_SERVICE_URL", "").strip()
        return cls(FlagServiceClient(url) if url else None, live_games)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def start(self) -> None:
        """Start the background worker on the running loop (no-op when disabled)."""
        if not self.enabled or self._worker is not None:
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # constructed outside a loop (tests); start() again later
        self._queue = asyncio.Queue()
        self._worker = self._loop.create_task(self._run())
        logger.info("Flag service lifecycle reporting enabled (instance %s)", self.instance)

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    async def _call(self, call: str, method: str, path: str, body: dict | None = None) -> bool:
        assert self._client is not None
        try:
            await asyncio.to_thread(self._client.request, method, path, body)
        except FlagServiceError as e:
            logger.warning("Flag service %s failed: %s (will reconcile)", call, e)
            metrics.flag_service_calls_total.labels(call=call, result="error").inc()
            self._needs_reconcile.set()
            return False
        metrics.flag_service_calls_total.labels(call=call, result="ok").inc()
        return True

    async def register(self, game: GameRecord) -> bool:
        """Register a game, waiting at most 200 ms. Never raises; never blocks longer."""
        if not self.enabled:
            return False
        path = "/games/" + urllib.parse.quote(game.game_id, safe="")
        try:
            return await asyncio.wait_for(
                self._call("register", "PUT", path, game.body(self.instance)), timeout=self._register_await
            )
        except TimeoutError:
            # The thread may still finish the PUT; the reconcile makes it whole either way.
            logger.warning(
                "Flag service registration of %s exceeded %.0f ms; game starts anyway",
                game.game_id,
                self._register_await * 1000,
            )
            metrics.flag_service_calls_total.labels(call="register", result="timeout").inc()
            self._needs_reconcile.set()
            return False

    def end(self, game_id: str) -> None:
        """Queue ``DELETE /games/{id}``. Thread-safe and non-blocking (no network here)."""
        if not self.enabled or not game_id:
            return
        loop, queue = self._loop, self._queue
        if loop is None or queue is None or loop.is_closed():
            self._needs_reconcile.set()
            return
        loop.call_soon_threadsafe(queue.put_nowait, game_id)

    async def reconcile(self) -> bool:
        """Send this instance's full set of live games."""
        if not self.enabled:
            return False
        games = [g.body(self.instance) for g in self._live_games()]
        path = "/coordinators/" + urllib.parse.quote(self.instance, safe="") + "/games"
        self._needs_reconcile.clear()
        ok = await self._call("reconcile", "PUT", path, {"games": games})
        if ok:
            logger.info("Flag service reconciled %d live game(s) for %s", len(games), self.instance)
        return ok

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            if self._needs_reconcile.is_set():
                await self.reconcile()
            try:
                game_id = await asyncio.wait_for(self._queue.get(), timeout=self._retry_interval)
            except TimeoutError:
                continue
            await self._call("end", "DELETE", "/games/" + urllib.parse.quote(game_id, safe=""))
