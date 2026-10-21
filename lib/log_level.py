"""Runtime log level from the ``log_level`` flag (#1788, FR-150, FR-151, flags.md §15).

Resolution order, the flag always wins: the ``observability/log_level`` flag when flagd
resolves it; only when it cannot be resolved (flagd unreachable, flag missing, no first
sync yet, unknown value) the deprecated ``LOG_LEVEL`` env var when set; else INFO.

The level is applied to the root logger and to every OTLP log handler registered with
:func:`register_handler`. It is re-applied on the provider READY and configuration-changed
events and by a slow safety-net poll, so a flag flip takes effect without a restart.
The services' ``logging.basicConfig`` at import keeps reading ``LOG_LEVEL``: flagd is not
reachable yet at that point, and the first sync replaces that level.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

DOMAIN = "observability"
FLAG_KEY = "log_level"
DEFAULT_LEVEL = logging.INFO
POLL_INTERVAL_SECONDS = 5.0

_handlers: list[logging.Handler] = []
_floors: list[Callable[[], int | None]] = []
_start_lock = threading.Lock()
_started = False
_stop = threading.Event()


def parse_level(name: object) -> int | None:
    """Map a level name (``debug``, ``WARN``, ...) to a logging level, None when unknown."""
    if not isinstance(name, str):
        return None
    text = name.strip().upper()
    if text == "WARN":
        text = "WARNING"
    level = logging.getLevelName(text)
    return level if isinstance(level, int) else None


def env_level() -> int | None:
    """The deprecated ``LOG_LEVEL`` env var as a level (INFO when unset), None when unknown."""
    return parse_level(os.environ.get("LOG_LEVEL", "info"))


def flag_level() -> int | None:
    """The ``log_level`` flag as a level, None when flagd cannot resolve it."""
    try:
        from openfeature.evaluation_context import EvaluationContext
        from openfeature.flag_evaluation import Reason

        from lib.feature_flags import get_flag_client, init_flag_domain

        init_flag_domain(DOMAIN)
        details = get_flag_client(DOMAIN).get_string_details(
            FLAG_KEY,
            "",
            EvaluationContext(attributes={"service": os.environ.get("OTEL_SERVICE_NAME", "unknown")}),
        )
    except Exception:
        return None
    if details.reason == Reason.ERROR:
        return None
    return parse_level(details.value)


def resolve_log_level() -> int:
    """Flag, else env, else INFO."""
    for level in (flag_level(), env_level()):
        if level is not None:
            return level
    return DEFAULT_LEVEL


def register_handler(handler: logging.Handler) -> None:
    """Have the handler's level follow the resolved level (the OTLP log handler)."""
    _handlers.append(handler)


def register_floor(floor: Callable[[], int | None]) -> None:
    """Let a service lower the level further: the result is min(resolved, floor()).

    The pairing daemon uses it so its `debug` flag keeps meaning "log at DEBUG".
    """
    _floors.append(floor)


def refresh_log_level() -> int:
    """Resolve the level now and apply it to the root logger and registered handlers."""
    level = resolve_log_level()
    for floor in _floors:
        floor_level = floor()
        if floor_level is not None:
            level = min(level, floor_level)
    root = logging.getLogger()
    if root.level != level:
        logger.info("log level -> %s", logging.getLevelName(level))
        root.setLevel(level)
    for handler in _handlers:
        handler.setLevel(level)
    return level


def _poll() -> None:
    while not _stop.wait(POLL_INTERVAL_SECONDS):
        try:
            refresh_log_level()
        except Exception:
            logger.debug("log level refresh failed", exc_info=True)


def start_runtime_log_level() -> None:
    """Apply the level now and keep it following the flag. Safe to call twice."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    try:
        from openfeature.provider import ProviderEvent

        from lib.feature_flags import get_flag_client, init_flag_domain

        init_flag_domain(DOMAIN)
        client = get_flag_client(DOMAIN)
        client.add_handler(ProviderEvent.PROVIDER_READY, lambda _details: refresh_log_level())
        client.add_handler(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, lambda _details: refresh_log_level())
    except Exception:
        logger.warning("log level flag handlers not registered, polling only", exc_info=True)
    refresh_log_level()
    threading.Thread(target=_poll, name="log-level-poll", daemon=True).start()


def reset_for_tests() -> None:
    """Forget handlers and the started state. Test-only helper."""
    global _started
    _stop.set()
    with _start_lock:
        _started = False
    _handlers.clear()
    _floors.clear()
    _stop.clear()
