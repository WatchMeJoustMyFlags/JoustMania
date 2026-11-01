"""
Shared OTEL Resource builder for JoustMania Python services.

Centralizes resource attribute construction so all three signal pipelines
(traces, metrics, logs) emit identical resource metadata. These standard
OTEL attributes drive service detection, release tracking, and topology
linking in the local observability stack.
"""

import os
import platform
import secrets
import sys

from opentelemetry.sdk.resources import Resource


def _get_host_name() -> str:
    """Resolve host.name from OTEL_RESOURCE_ATTRIBUTES or fall back to platform.node().

    Inside Docker, platform.node() returns the container ID. The
    OTEL_RESOURCE_ATTRIBUTES env var (set in docker-compose.yml) provides
    the real host name for topology linking in the observability stack.
    """
    otel_attrs = os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")
    for pair in otel_attrs.split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            if key.strip() == "host.name":
                return value.strip()
    return platform.node()


# This process's service.instance.id, drawn once so traces, metrics and logs share it.
_instance_id: str | None = None


def _reset_instance_id_in_child() -> None:
    """A forked child is a second writer: it must draw its own id (#2543)."""
    global _instance_id
    _instance_id = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_instance_id_in_child)


def _get_instance_id() -> str:
    """service.instance.id, unique per process: ``<HOSTNAME>-<8 hex>`` (#2543).

    It becomes the ``instance`` label, and ``job`` + ``instance`` is the series
    identity in VictoriaMetrics. The container id alone is not unique: two hosts
    booted from the same Docker state share it, so their cumulative counters
    interleaved into one series and every interleave read as a counter reset.
    The container id stays as the prefix so ``docker ps`` still maps the label.
    """
    global _instance_id
    if _instance_id is None:
        _instance_id = f"{os.getenv('HOSTNAME') or platform.node()}-{secrets.token_hex(4)}"
    return _instance_id


def get_otel_resource() -> Resource:
    """Build a shared OTEL Resource with standard semantic-convention attributes.

    Attributes and their observability impact:
    - service.name: Service entity detection
    - service.namespace: Service grouping
    - service.version: Release tracking, version-aware error analytics
    - service.instance.id: Per-process series identity (``instance`` label, #2543)
    - deployment.environment: Environment filtering, release comparison
    - host.name: Host entity linking in topology
    - container.name: Container entity linking (from OTEL_SERVICE_NAME)
    - process.*: Process group detection, runtime identification
    - telemetry.sdk.*: SDK identification and language detection
    """
    service_name = os.getenv("OTEL_SERVICE_NAME", "unknown")
    return Resource(
        attributes={
            "service.name": service_name,
            "service.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "joustmania"),
            "service.version": os.getenv("SERVICE_VERSION", "0.0.0-dev"),
            "service.instance.id": _get_instance_id(),
            "deployment.environment": os.getenv("DEPLOYMENT_ENVIRONMENT", "development"),
            "host.name": _get_host_name(),
            "container.name": service_name,
            "process.pid": os.getpid(),
            "process.executable.name": os.path.basename(sys.executable),
            "process.runtime.name": platform.python_implementation().lower(),
            "process.runtime.version": platform.python_version(),
            "telemetry.sdk.name": "opentelemetry",
            "telemetry.sdk.language": "python",
        }
    )
