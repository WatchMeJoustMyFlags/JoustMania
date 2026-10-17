"""Flag service client for the integration suite (epic #1407, UC-053).

Tests change runtime flags ONLY through the flag service's HTTP API
(docs/specs/services.md §7.2), exactly like the agent, the menu, the
connect-proxy and the stage scripts do. Nothing writes a flag file.

The ``flag_service`` fixture (tests/integration/conftest.py) hands out a
:class:`FlagService` that remembers every stack-wide overlay it set and
``DELETE``s it at teardown (re-review N4: fixtures delete what they set; they
never persist). Game-scoped values need no cleanup: the flag service drops them
when the coordinator ends the game (UC-053 BR-003).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

WRITER = "integration-test"


class FlagServiceError(AssertionError):
    """A flag-service request was rejected; carries the status and reason code."""

    def __init__(self, method: str, path: str, status: int, body: dict):
        self.status = status
        self.reason = body.get("error", "")
        self.detail = body.get("detail", "")
        super().__init__(
            f"{method} {path} -> HTTP {status} {self.reason}: {self.detail}"
        )


class FlagService:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        # Stack-wide overlays this client set, as (set, flag, persist) to clear.
        self._overlays: list[tuple[str, str, bool]] = []

    @classmethod
    def from_compose(cls, docker_compose) -> "FlagService":
        host = docker_compose.get_service_host("flag-service", 8070)
        port = docker_compose.get_service_port("flag-service", 8070)
        return cls(f"http://{host}:{port}")

    # ------------------------------------------------------------------ raw
    def request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, (json.loads(raw) if raw else {})
            except ValueError:
                return err.code, {
                    "error": "non_json",
                    "detail": raw.decode(errors="replace"),
                }

    def _ok(self, method: str, path: str, body: dict | None = None) -> dict:
        status, resp = self.request(method, path, body)
        if status // 100 != 2:
            raise FlagServiceError(method, path, status, resp)
        return resp

    @staticmethod
    def _path(flag_set: str, flag: str) -> str:
        return f"/{urllib.parse.quote(flag_set, safe='')}/{urllib.parse.quote(flag, safe='')}"

    # ------------------------------------------------------------------ writes
    def put(
        self,
        flag_set: str,
        flag: str,
        *,
        variant: str | None = None,
        value=None,
        scope: dict | None = None,
        ttl_seconds: float | None = None,
        persist: bool = False,
        writer: str = WRITER,
        reason: str = "",
    ) -> dict:
        """PUT /{set}/{flag}: exactly one of ``variant`` and ``value``."""
        body: dict = {"writer": writer}
        if variant is not None:
            body["variant"] = variant
        else:
            body["value"] = value
        if scope:
            body["scope"] = scope
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        if persist:
            body["persist"] = True
        if reason:
            body["reason"] = reason
        result = self._ok("PUT", self._path(flag_set, flag), body)
        if not scope:
            self._overlays.append((flag_set, flag, persist))
        return result

    def post(
        self, flag_set: str, flag: str, *, game_id: str, payload: dict | None = None
    ) -> dict:
        """POST /{set}/{flag}: append one event to the game's event list."""
        return self._ok(
            "POST",
            self._path(flag_set, flag),
            {"scope": {"game_id": game_id}, "payload": payload or {}, "writer": WRITER},
        )

    def delete(self, flag_set: str, flag: str, **query) -> None:
        """DELETE /{set}/{flag}?<scope>: clear a value (no scope: the stack overlay)."""
        path = self._path(flag_set, flag)
        query = {
            k: str(v).lower() if isinstance(v, bool) else v
            for k, v in query.items()
            if v not in (None, "")
        }
        if query:
            path += "?" + urllib.parse.urlencode(query)
        self._ok("DELETE", path)

    def cleanup(self) -> None:
        """Clear every stack-wide overlay this client set (newest first)."""
        while self._overlays:
            flag_set, flag, persist = self._overlays.pop()
            try:
                if persist:
                    self.delete(flag_set, flag, persist=True)
                self.delete(flag_set, flag)
            except FlagServiceError:
                pass

    # ------------------------------------------------------------------ reads
    def get(self, flag_set: str, flag: str | None = None) -> dict:
        path = f"/{urllib.parse.quote(flag_set, safe='')}"
        if flag is not None:
            path = self._path(flag_set, flag)
        return self._ok("GET", path)

    def state(self) -> dict:
        return self._ok("GET", "/state")

    def games(self) -> list[dict]:
        return self._ok("GET", "/games")["games"]

    def wait_game_registered(self, game_id: str, timeout: float = 5.0) -> dict:
        """Wait until the coordinator registered ``game_id`` (FR-135)."""
        deadline = time.monotonic() + timeout
        while True:
            for game in self.games():
                if game["game_id"] == game_id:
                    return game
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"game {game_id} was not registered with the flag service within {timeout}s"
                )
            time.sleep(0.05)


# --------------------------------------------------------------------------- #
# Module-level helpers for tests whose helper functions take no fixture: the
# ``flag_service`` fixture binds its client here, so their writes are cleaned up
# with the test. Outside a fixture they fall back to FLAG_SERVICE_TEST_URL
# (default http://localhost:8070, the port docker-compose.override.yml publishes).
# --------------------------------------------------------------------------- #
_bound: FlagService | None = None


def bind(client: FlagService | None) -> None:
    global _bound
    _bound = client


def default() -> FlagService:
    if _bound is not None:
        return _bound
    import os

    return FlagService(os.getenv("FLAG_SERVICE_TEST_URL", "http://localhost:8070"))


INTERVENTIONS = "interventions"


def variant_value(flag_set: str, flag: str, variant: str):
    """The value of a declared variant, read from the flag service (R-readers)."""
    return default().get(flag_set, flag)["variant_values"][variant]


def set_game_value(flag: str, game_id: str, value) -> dict:
    """A game-scoped intervention value (state flags), FR-136."""
    return default().put(INTERVENTIONS, flag, value=value, scope={"game_id": game_id})


def set_player_value(flag: str, game_id: str, serial: str, value) -> dict:
    """A per-player value in one game (levels and grants; every write is a fresh variant)."""
    return default().put(
        INTERVENTIONS, flag, value=value, scope={"game_id": game_id, "serial": serial}
    )


def clear_game_value(flag: str, game_id: str, serial: str = "") -> None:
    """Clear a game (or game+serial) value: the game resolves the baseline again."""
    default().delete(INTERVENTIONS, flag, game_id=game_id, serial=serial)


def append_event(flag: str, game_id: str, payload: dict | None = None) -> int:
    """Append a one-shot event to the game's event list; returns its seq (FR-137)."""
    return int(
        default().post(INTERVENTIONS, flag, game_id=game_id, payload=payload)["seq"]
    )


def nudge_sync() -> None:
    """Make the flag service push a new configuration without touching any
    intervention: every Python domain provider then re-evaluates (the fan-out of
    re-review N1). Used to prove an already-applied event is not applied again."""
    import uuid

    client = default()
    client.put(
        "observability",
        "flagservice.propagation_probe",
        value=f"nudge-{uuid.uuid4().hex[:8]}",
    )
