"""
FlagdSpanEnrichmentProcessor — Configurable span attribute enrichment.

Reads the ``span_enrichment_config`` structured flag from flagd and adds
diagnostic attributes to every span. The flag controls *which* attribute
sets are added, enabling progressive enrichment during live demos.

Attribute sets (same across Python, Rust, and Go):
  service_identity  — demo.enriched, demo.language, demo.service, demo.namespace
  flag_values       — demo.flag.trace_sampling_rate, demo.flag.verbose_logging, …
  runtime           — demo.runtime.version, demo.runtime.pid, demo.runtime.threads
  system            — demo.system.hostname, demo.system.cpu_count, demo.system.memory_mb
"""

import logging
import os
import platform
import sys
import threading
from collections.abc import Callable

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

logger = logging.getLogger(__name__)


class FlagdSpanEnrichmentProcessor(SpanProcessor):
    """Adds diagnostic attributes to spans based on flagd config."""

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        config = self._get_config()
        if not config.get("enabled", False):
            return

        attribute_sets = config.get("attribute_sets", [])
        for attr_set in attribute_sets:
            attrs = _ATTRIBUTE_BUILDERS.get(attr_set)
            if attrs:
                for key, value in attrs().items():
                    span.set_attribute(key, value)

    def on_end(self, span: ReadableSpan) -> None:  # noqa: ARG002
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        return True

    # ------------------------------------------------------------------

    @staticmethod
    def _get_config() -> dict:
        try:
            from lib.feature_flags import get_flag_client

            client = get_flag_client("performance")
            result = client.get_object_value(
                "span_enrichment_config",
                {"enabled": False, "attribute_sets": []},
            )
            return result if isinstance(result, dict) else {}
        except Exception:
            logger.debug("Failed to read span_enrichment_config from flagd", exc_info=True)
            return {"enabled": False, "attribute_sets": []}


# ── Attribute set builders ───────────────────────────────────────────


def _service_identity_attrs() -> dict[str, str | bool]:
    return {
        "demo.enriched": True,
        "demo.language": "python",
        "demo.service": os.getenv("OTEL_SERVICE_NAME", "unknown"),
        "demo.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "unknown"),
    }


def _flag_values_attrs() -> dict[str, float | bool]:
    try:
        from lib.feature_flags import get_flag_client

        client = get_flag_client("performance")
        return {
            "demo.flag.trace_sampling_rate": client.get_float_value("trace_sampling_rate", 1.0),
            "demo.flag.verbose_logging": client.get_boolean_value("verbose_logging", False),
            "demo.flag.grpc_rpc_spans": client.get_boolean_value("grpc_rpc_spans", False),
        }
    except Exception:
        logger.debug("Failed to read flag values from flagd", exc_info=True)
        return {}


def _runtime_attrs() -> dict[str, str | int]:
    return {
        "demo.runtime.version": platform.python_version(),
        "demo.runtime.pid": os.getpid(),
        "demo.runtime.threads": threading.active_count(),
    }


def _system_attrs() -> dict[str, str | int]:
    cpu_count = os.cpu_count() or 0
    try:
        import resource

        if sys.platform == "darwin":
            mem_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # already bytes on macOS
        else:
            mem_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024  # KB on Linux
        mem_mb = mem_bytes // (1024 * 1024)
    except Exception:
        logger.debug("Failed to read system memory usage", exc_info=True)
        mem_mb = 0
    return {
        "demo.system.hostname": platform.node(),
        "demo.system.cpu_count": cpu_count,
        "demo.system.memory_mb": mem_mb,
    }


_ATTRIBUTE_BUILDERS: dict[str, Callable[[], dict]] = {
    "service_identity": _service_identity_attrs,
    "flag_values": _flag_values_attrs,
    "runtime": _runtime_attrs,
    "system": _system_attrs,
}
