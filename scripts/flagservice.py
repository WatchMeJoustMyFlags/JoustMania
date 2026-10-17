"""Flag service client for the operator and stage scripts (epic #1407, UC-053).

Standard library only (the scripts need nothing but python3). Runtime flags
change only through the flag service's HTTP API (docs/specs/services.md §7.2);
the flag files are the read-only baseline (C-033).

    from flagservice import FlagService
    fs = FlagService.from_env()            # FLAG_SERVICE_URL, default http://localhost/flags
    fs.set_variant("agent", "decision.backend", "jev", writer="stage")
    fs.effective("agent")                  # {flag: (variant, value)}
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://localhost/flags"


class FlagServiceError(Exception):
    def __init__(self, method: str, path: str, status: int, body: dict):
        self.status = status
        self.reason = body.get("error", "")
        self.detail = body.get("detail", "")
        super().__init__(f"{method} {path}: HTTP {status} {self.reason}: {self.detail}")


class FlagServiceUnreachable(Exception):
    pass


class FlagService:
    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "FlagService":
        return cls(os.environ.get("FLAG_SERVICE_URL") or DEFAULT_URL)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {
                    "error": "non_json",
                    "detail": raw.decode(errors="replace")[:200],
                }
            raise FlagServiceError(method, path, err.code, parsed) from None
        except (urllib.error.URLError, OSError) as err:
            raise FlagServiceUnreachable(
                f"flag service unreachable at {self.base_url}: {err}"
            ) from None

    @staticmethod
    def _flag_path(flag_set: str, flag: str) -> str:
        return f"/{urllib.parse.quote(flag_set, safe='')}/{urllib.parse.quote(flag, safe='')}"

    # ------------------------------------------------------------------ reads
    def state(self) -> dict:
        return self._request("GET", "/state")

    def flag_set(self, flag_set: str) -> dict:
        return self._request("GET", "/" + urllib.parse.quote(flag_set, safe=""))

    def flag(self, flag_set: str, flag: str) -> dict:
        return self._request("GET", self._flag_path(flag_set, flag))

    def effective(self, flag_set: str) -> dict[str, tuple[str, object]]:
        """{flag: (effective default variant, its value)} for every flag of a set."""
        out = {}
        for view in self.flag_set(flag_set)["flags"]:
            variant = view["effective_default"]
            out[view["flag"]] = (variant, self.value_of(view, variant))
        return out

    @staticmethod
    def value_of(view: dict, variant: str):
        for key in ("stack_overlay", "admin_value", "real_overlay"):
            entry = view.get(key) or {}
            if entry.get("variant") == variant:
                return entry.get("value")
        return (view.get("variant_values") or {}).get(variant)

    # ------------------------------------------------------------------ writes
    def set_variant(
        self,
        flag_set: str,
        flag: str,
        variant: str,
        *,
        writer: str,
        persist: bool = False,
    ) -> dict:
        body = {"variant": variant, "writer": writer}
        if persist:
            body["persist"] = True
        return self._request("PUT", self._flag_path(flag_set, flag), body)

    def clear(self, flag_set: str, flag: str, *, persist: bool = False) -> None:
        path = self._flag_path(flag_set, flag) + ("?persist=true" if persist else "")
        self._request("DELETE", path)

    def clear_writer(self, flag_set: str, writer: str) -> None:
        self._request(
            "DELETE",
            "/"
            + urllib.parse.quote(flag_set, safe="")
            + "?writer="
            + urllib.parse.quote(writer),
        )
