"""Game lifecycle reports to the flag service (FR-135, UC-053, #1410).

The game coordinator tells the flag service which games are live, so the
service accepts game-scoped flag writes only for registered games and drops a
game's values when it ends (UC-053 BR-003):

- ``register`` (``PUT /games/{id}``) is awaited for at most 200 ms before the
  game is RUNNING, never under ``_sessions_lock``. On a timeout or an error the
  game still starts (C-004) and the lifecycle falls back to reconciling.
- ``end`` (``DELETE /games/{id}``) is queued and sent by a background worker,
  so it can be called from any thread, under any lock. It waits for a
  still-running registration of the same game first, so a late ``PUT`` never
  re-registers an ended game.
- ``reconcile`` (``PUT /coordinators/{instance}/games``) sends this
  coordinator's full set of live games: at start (an empty set after a
  restart, UC-053 A10), whenever a call failed (until it succeeds), whenever
  the service's boot epoch changes (a restarted flag service lost every game
  scope, UC-053 A9 step 3; the worker reads the epoch from ``GET /healthz``
  every 2 s and from every response), and every 15 s regardless. It only
  touches this instance's games; a second coordinator
  has its own instance id (review C3). The id is stable across restarts, so a
  restarted coordinator's first reconcile drops its previous process's games.

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
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from services.game_coordinator import metrics

logger = logging.getLogger(__name__)

# Bounded registration await before the game is RUNNING (FR-135, NFR-052).
REGISTER_AWAIT_SECONDS = 0.2
# Per-request HTTP timeout for the background calls (end, reconcile).
REQUEST_TIMEOUT_SECONDS = 0.5
# How often the worker retries reconciling while the service is unreachable,
# and how often it checks the service's boot epoch.
RECONCILE_RETRY_SECONDS = 2.0
# Unconditional reconcile period: the backstop for anything the epoch check misses.
RECONCILE_PERIOD_SECONDS = 15.0
# A repeated identical failure (e.g. the flag service rejecting this coordinator
# at its game cap) is retried every RECONCILE_RETRY_SECONDS; it logs once, then
# at most this often, with the count of suppressed repeats.
FAILURE_LOG_INTERVAL_SECONDS = 60.0
# Shutdown gives the queued ends this long to reach the flag service before the
# worker is stopped; an unreachable service costs at most this.
FLUSH_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class GameRecord:
    """One live game as the flag service registers it (entity GAME_SCOPE)."""

    game_id: str
    game_kind: str  # flag vocabulary: "real" | "shadow" (FR-041)
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


# This process's boot id: logged next to the instance id, never part of it.
BOOT_ID = uuid.uuid4().hex[:8]


def coordinator_instance_id() -> str:
    """A STABLE id per coordinator: ``FLAG_SERVICE_INSTANCE``, which every compose
    stack sets (``game-coordinator``; a second coordinator needs its own id, review C3).
    It survives a restart and a container recreate on purpose:
    the new process's reconcile then drops the games its predecessor
    registered (UC-053 A10), instead of leaving them for the 1 h expiry. The
    hostname fallback is for runs outside compose only: in a container it is the
    container id, which changes when the container is recreated."""
    return os.environ.get("FLAG_SERVICE_INSTANCE") or socket.gethostname()


class FlagServiceLifecycle:
    """Reports game registration, end and reconciliation to the flag service."""

    def __init__(
        self,
        client: FlagServiceClient | None,
        live_games: Callable[[], list[GameRecord]],
        instance: str | None = None,
        register_await: float = REGISTER_AWAIT_SECONDS,
        retry_interval: float = RECONCILE_RETRY_SECONDS,
        reconcile_period: float = RECONCILE_PERIOD_SECONDS,
    ):
        self._client = client
        self._live_games = live_games
        self.instance = instance or coordinator_instance_id()
        self._register_await = register_await
        self._retry_interval = retry_interval
        self._reconcile_period = reconcile_period
        self._last_reconcile = 0.0
        # The service's boot epoch as last seen; a different one means it restarted.
        self._epoch: str | None = None
        # In-flight registrations per game, awaited before that game's DELETE.
        self._registering: dict[str, asyncio.Future] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        # Starts "dirty" so the first worker pass reconciles: after a restart the
        # service still holds this instance's games from the previous process;
        # announcing the (empty) set drops them (UC-053 A10).
        self._needs_reconcile = threading.Event()
        self._needs_reconcile.set()
        # Failure-log rate limit, one slot per call: [error text of the last
        # warning, when it was logged, identical failures suppressed since].
        self._failure_log: dict[str, list] = {}

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
        logger.info("Flag service lifecycle reporting enabled (instance %s, boot %s)", self.instance, BOOT_ID)

    async def flush(self, limit_s: float = FLUSH_TIMEOUT_SECONDS) -> bool:
        """Wait up to ``limit_s`` s for the queued ends to be sent (#1849).

        True when every queued end was attempted; False on timeout (the rest
        is left to the next reconcile). Never raises.
        """
        queue = self._queue
        if self._worker is None or queue is None:
            return True
        await asyncio.sleep(0)  # let end() calls scheduled from other threads land
        try:
            await asyncio.wait_for(queue.join(), timeout=limit_s)
        except TimeoutError:
            logger.warning("Flag service end flush timed out after %.1fs; the next reconcile retires the rest", limit_s)
            return False
        return True

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    def _observe_epoch(self, resp: dict) -> None:
        epoch = resp.get("epoch") if isinstance(resp, dict) else None
        if not epoch:
            return
        if self._epoch is not None and epoch != self._epoch:
            logger.warning("Flag service restarted (epoch %s -> %s); re-registering live games", self._epoch, epoch)
            self._needs_reconcile.set()
        self._epoch = epoch

    async def _call(self, call: str, method: str, path: str, body: dict | None = None) -> bool:
        assert self._client is not None
        try:
            resp = await asyncio.to_thread(self._client.request, method, path, body)
        except FlagServiceError as e:
            self._log_failure(call, e)
            metrics.flag_service_calls_total.labels(call=call, result="error").inc()
            self._needs_reconcile.set()
            return False
        metrics.flag_service_calls_total.labels(call=call, result="ok").inc()
        self._observe_epoch(resp)
        self._failure_log.pop(call, None)
        return True

    def _log_failure(self, call: str, error: FlagServiceError) -> None:
        self._warn_limited(call, str(error), "Flag service %s failed: %s (will reconcile)", call, error)

    def _warn_limited(self, call: str, text: str, fmt: str, *args) -> None:
        """Warn on the first failure of ``call``, on a changed ``text``, then once
        per interval. Slots are per call, so another call's success or failure
        never resets this one's throttle."""
        now = time.monotonic()
        slot = self._failure_log.get(call)
        if slot is not None and slot[0] == text and now - slot[1] < FAILURE_LOG_INTERVAL_SECONDS:
            slot[2] += 1
            return
        suppressed = slot[2] if slot is not None and slot[0] == text else 0
        self._failure_log[call] = [text, now, 0]
        logger.warning(fmt + "%s", *args, f" [{suppressed} identical failure(s) suppressed]" if suppressed else "")

    async def check_epoch(self) -> None:
        """Read the service's boot epoch; a change schedules a reconcile (UC-053 A9)."""
        assert self._client is not None
        try:
            resp = await asyncio.to_thread(self._client.request, "GET", "/healthz")
        except FlagServiceError:
            # /healthz answers 503 while the service starts; the next tick retries.
            return
        self._observe_epoch(resp)

    async def register(self, game: GameRecord) -> bool:
        """Register a game, waiting at most 200 ms. Never raises; never blocks longer."""
        if not self.enabled:
            return False
        path = "/games/" + urllib.parse.quote(game.game_id, safe="")
        task = asyncio.ensure_future(self._call("register", "PUT", path, game.body(self.instance)))
        self._registering[game.game_id] = task
        task.add_done_callback(lambda _t, gid=game.game_id: self._registering.pop(gid, None))
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self._register_await)
        except TimeoutError:
            # The thread may still finish the PUT; the reconcile makes it whole either way.
            self._warn_limited(
                "register_timeout",
                "timeout",
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
        self._last_reconcile = asyncio.get_running_loop().time()
        ok = await self._call("reconcile", "PUT", path, {"games": games})
        if ok:
            logger.info("Flag service reconciled %d live game(s) for %s", len(games), self.instance)
        return ok

    async def _run(self) -> None:
        assert self._queue is not None
        loop = asyncio.get_running_loop()
        while True:
            if loop.time() - self._last_reconcile >= self._reconcile_period:
                self._needs_reconcile.set()
            if self._needs_reconcile.is_set():
                await self.reconcile()
            try:
                game_id = await asyncio.wait_for(self._queue.get(), timeout=self._retry_interval)
            except TimeoutError:
                await self.check_epoch()
                continue
            try:
                pending = self._registering.get(game_id)
                if pending is not None:
                    # A registration still in flight would land after the DELETE and
                    # re-register the ended game; let it finish first.
                    with contextlib.suppress(Exception):
                        await pending
                await self._call("end", "DELETE", "/games/" + urllib.parse.quote(game_id, safe=""))
            finally:
                self._queue.task_done()  # lets flush() see the end as attempted
