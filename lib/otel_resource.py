"""
Shared OTEL Resource builder for JoustMania Python services.

Centralizes resource attribute construction so all three signal pipelines
(traces, metrics, logs) emit identical resource metadata. These standard
OTEL attributes drive service detection, release tracking, and topology
linking in the local observability stack.
"""

import os
import platform
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


def get_otel_resource() -> Resource:
    """Build a shared OTEL Resource with standard semantic-convention attributes.

    Attributes and their observability impact:
    - service.name: Service entity detection
    - service.namespace: Service grouping
    - service.version: Release tracking, version-aware error analytics
    - service.instance.id: Per-instance metrics
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
            "service.instance.id": os.getenv("HOSTNAME", platform.node()),
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
