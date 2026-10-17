"""
Admin-setting writer over the flag service (epic #1407, UC-053 A2, FR-018).

The menu's admin settings (sensitivity, team count, force-all-start, game
selection, instructions, the agent kill switch) change through the flag
service's HTTP API, never by writing a flag file (C-032, C-033). A host change
is an admin setting: it is written with ``"persist": true``, so the flag service
keeps it in its own state file across restarts (D10, re-review N4), and it
takes effect at once (the flag service pushes it to flagd).

``FlagServiceWriter`` keeps the interface the menu already uses
(``update_default_variant``, ``cycle_variant``, ``get_variant_names``,
``get_current_variant``, ``get_variant_value``, ``match_variant_for_value``),
so a handler swaps writers without changing its logic. Reads return what the
flag service serves (the effective default: an in-memory stage overlay, else
the persisted admin value, else the baseline).

Every call has a short deadline (0.5 s) and fails closed: a failed write returns
False / None and is logged; nothing is retried.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://flag-service:8070"
REQUEST_TIMEOUT_SECONDS = 0.5
WRITER_LABEL = "menu"

# transport(method, url, body) -> (status, parsed JSON body or {}); injectable for tests.
Transport = Callable[[str, str, dict | None], tuple[int, dict]]


def _urllib_transport(method: str, url: str, body: dict | None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:  # noqa: S310 - in-network URL
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, (json.loads(raw) if raw else {})
        except ValueError:
            return err.code, {}


class FlagServiceWriter:
    """Admin writes and reads for one flag set through the flag service."""

    def __init__(
        self,
        flag_set: str,
        base_url: str | None = None,
        *,
        persist: bool = True,
        writer: str = WRITER_LABEL,
        transport: Transport | None = None,
    ):
        self.flag_set = flag_set
        self.base_url = (base_url or os.getenv("FLAG_SERVICE_URL") or DEFAULT_URL).rstrip("/")
        self.persist = persist
        self.writer = writer
        self._transport = transport or _urllib_transport

    # Kept for log messages and callers that printed the old file path.
    @property
    def file_path(self) -> str:
        return f"{self.base_url}/{self.flag_set}"

    def _flag_url(self, flag_key: str) -> str:
        return f"{self.base_url}/{urllib.parse.quote(self.flag_set, safe='')}/{urllib.parse.quote(flag_key, safe='')}"

    def _view(self, flag_key: str) -> dict | None:
        try:
            status, body = self._transport("GET", self._flag_url(flag_key), None)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            logger.error(f"Flag service unreachable reading {self.flag_set}/{flag_key}: {e}")
            return None
        if status != 200:
            logger.warning(f"Flag service: GET {self.flag_set}/{flag_key} -> {status} {body.get('error', '')}")
            return None
        return body

    def update_default_variant(self, flag_key: str, variant_name: str) -> bool:
        """Set the flag to ``variant_name`` (persisted admin setting). True on success."""
        body = {"variant": variant_name, "writer": self.writer}
        if self.persist:
            body["persist"] = True
        try:
            status, resp = self._transport("PUT", self._flag_url(flag_key), body)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            logger.critical(
                f"Flag service unreachable: admin setting {self.flag_set}/{flag_key}={variant_name} NOT applied: {e}"
            )
            return False
        if status != 200:
            logger.error(
                f"Flag service refused {self.flag_set}/{flag_key}={variant_name}: "
                f"HTTP {status} {resp.get('error', '')}: {resp.get('detail', '')}"
            )
            return False
        if self.persist and not resp.get("persisted", False):
            # Applied in memory, but the state file write failed: it will not
            # survive a flag-service restart (flagservice_admin_persist_errors_total).
            logger.error(f"Admin setting {self.flag_set}/{flag_key}={variant_name} applied but NOT persisted")
        logger.info(f"Updated flag '{self.flag_set}/{flag_key}' -> '{variant_name}' via the flag service")
        return True

    def get_variant_names(self, flag_key: str) -> list[str]:
        """The declared variants, in the baseline file's order (for cycling)."""
        view = self._view(flag_key)
        return list(view.get("variants", [])) if view else []

    def get_current_variant(self, flag_key: str) -> str | None:
        """The variant the flag service serves now."""
        view = self._view(flag_key)
        return view.get("effective_default") if view else None

    def get_variant_value(self, flag_key: str, variant_name: str):
        view = self._view(flag_key)
        return (view.get("variant_values") or {}).get(variant_name) if view else None

    def match_variant_for_value(self, flag_key: str, value) -> str | None:
        """The first declared variant whose value equals ``value`` (#818 display)."""
        view = self._view(flag_key)
        if not view:
            return None
        values = view.get("variant_values") or {}
        for name in view.get("variants", []):
            if values.get(name) == value:
                return name
        return None

    def cycle_variant(self, flag_key: str, forward: bool = True) -> str | None:
        """Move the flag to the next (or previous) declared variant; the new name, or None."""
        view = self._view(flag_key)
        if not view or not view.get("variants"):
            return None
        variants = list(view["variants"])
        current = view.get("effective_default")
        if current not in variants:
            new_variant = variants[0]
        else:
            idx = variants.index(current)
            new_variant = variants[(idx + 1) % len(variants) if forward else (idx - 1) % len(variants)]
        return new_variant if self.update_default_variant(flag_key, new_variant) else None
