"""OtlpSink: an in-process OTLP/gRPC receiver that records spans and metrics.

It stands in for the otel-collector (and, through it, Jaeger) in the service
tier. The services export to it with their real OTLP exporters
(``OTEL_EXPORTER_OTLP_ENDPOINT=sink.endpoint``); a test then waits on what
arrived::

    span = sink.wait_span("player_lifecycle", {"game.id": game_id}, event="player_disconnected")
    point = sink.wait_metric("game_players_alive", {"game_id": game_id})

Both ``wait_*`` block (poll a condition, never a sleep); an async test calls
the ``*_async`` twins, which run them on a worker thread. Logs are recorded too
(body, trace id, attributes; ``logs()``), so a test can follow a game's trace id
from a span to its log lines.

Tee: ``OtlpSink(forward_to="127.0.0.1:<port>")`` forwards every trace and
metrics export request unchanged to another OTLP/gRPC receiver (the agent's, as
the collector's ``otlp/agent`` exporter does in its traces and metrics
pipelines), best effort: a failed forward is counted in ``forward_errors`` and
never fails the service's export.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Mapping
from concurrent import futures
from dataclasses import dataclass, field
from typing import Any

import grpc
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2, logs_service_pb2_grpc
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2, metrics_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

DEFAULT_WAIT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.1
FORWARD_TIMEOUT_SECONDS = 2.0


# --------------------------------------------------------------------------- #
# Decoded records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SpanEventRecord:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpanLinkRecord:
    trace_id: str  # hex
    span_id: str  # hex
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpanRecord:
    name: str
    service: str  # resource service.name
    trace_id: str  # hex
    span_id: str  # hex
    parent_span_id: str  # hex, "" for a root span
    attributes: dict[str, Any] = field(default_factory=dict)
    events: tuple[SpanEventRecord, ...] = ()
    resource: dict[str, Any] = field(default_factory=dict)
    start_time_unix_nano: int = 0
    links: tuple[SpanLinkRecord, ...] = ()

    @property
    def is_root(self) -> bool:
        return not self.parent_span_id

    def event_names(self) -> list[str]:
        return [e.name for e in self.events]


@dataclass(frozen=True)
class MetricPoint:
    attributes: dict[str, Any]
    # Gauge/sum: the number. Histogram/summary: the sum of the observations.
    value: float
    # Histogram/summary: the observation count; None for gauge/sum.
    count: int | None = None


@dataclass(frozen=True)
class MetricRecord:
    """One metric of one export request (a later export of it is a new record)."""

    name: str
    service: str
    kind: str  # gauge | sum | histogram | exponential_histogram | summary
    points: tuple[MetricPoint, ...]
    resource: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LogRecord:
    service: str  # resource service.name
    body: str
    trace_id: str  # hex, "" when the line was logged outside a span
    span_id: str  # hex
    attributes: dict[str, Any] = field(default_factory=dict)


def _any_value(value) -> Any:
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [_any_value(v) for v in value.array_value.values]
    if kind == "kvlist_value":
        return _attributes(value.kvlist_value.values)
    return getattr(value, kind)


def _attributes(key_values) -> dict[str, Any]:
    return {kv.key: _any_value(kv.value) for kv in key_values}


def _matches(attributes: Mapping[str, Any], wanted: Mapping[str, Any] | None) -> bool:
    return not wanted or all(attributes.get(k) == v for k, v in wanted.items())


def _decode_spans(request) -> list[SpanRecord]:
    spans = []
    for resource_spans in request.resource_spans:
        resource = _attributes(resource_spans.resource.attributes)
        service = str(resource.get("service.name", ""))
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                spans.append(
                    SpanRecord(
                        name=span.name,
                        service=service,
                        trace_id=span.trace_id.hex(),
                        span_id=span.span_id.hex(),
                        parent_span_id=span.parent_span_id.hex(),
                        attributes=_attributes(span.attributes),
                        events=tuple(SpanEventRecord(e.name, _attributes(e.attributes)) for e in span.events),
                        resource=resource,
                        start_time_unix_nano=span.start_time_unix_nano,
                        links=tuple(
                            SpanLinkRecord(link.trace_id.hex(), link.span_id.hex(), _attributes(link.attributes))
                            for link in span.links
                        ),
                    )
                )
    return spans


def _decode_logs(request) -> list[LogRecord]:
    records = []
    for resource_logs in request.resource_logs:
        service = str(_attributes(resource_logs.resource.attributes).get("service.name", ""))
        for scope_logs in resource_logs.scope_logs:
            for record in scope_logs.log_records:
                records.append(
                    LogRecord(
                        service=service,
                        body=str(_any_value(record.body)),
                        trace_id=record.trace_id.hex(),
                        span_id=record.span_id.hex(),
                        attributes=_attributes(record.attributes),
                    )
                )
    return records


def _decode_points(metric) -> tuple[str, tuple[MetricPoint, ...]]:
    kind = metric.WhichOneof("data")
    if kind is None:
        return "", ()
    data = getattr(metric, kind)
    points = []
    for point in data.data_points:
        attributes = _attributes(point.attributes)
        if kind in ("gauge", "sum"):
            number = point.WhichOneof("value")
            points.append(MetricPoint(attributes, float(getattr(point, number)) if number else 0.0))
        else:
            points.append(MetricPoint(attributes, float(point.sum), int(point.count)))
    return kind, tuple(points)


def _decode_metrics(request) -> list[MetricRecord]:
    metrics = []
    for resource_metrics in request.resource_metrics:
        resource = _attributes(resource_metrics.resource.attributes)
        service = str(resource.get("service.name", ""))
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                kind, points = _decode_points(metric)
                metrics.append(MetricRecord(metric.name, service, kind, points, resource))
    return metrics


# --------------------------------------------------------------------------- #
# gRPC servicers
# --------------------------------------------------------------------------- #
class _TraceServicer(trace_service_pb2_grpc.TraceServiceServicer):
    def __init__(self, sink: OtlpSink):
        self._sink = sink

    def Export(self, request, context):  # noqa: N802, ARG002
        self._sink._on_traces(request)
        return trace_service_pb2.ExportTraceServiceResponse()


class _MetricsServicer(metrics_service_pb2_grpc.MetricsServiceServicer):
    def __init__(self, sink: OtlpSink):
        self._sink = sink

    def Export(self, request, context):  # noqa: N802, ARG002
        self._sink._on_metrics(request)
        return metrics_service_pb2.ExportMetricsServiceResponse()


class _LogsServicer(logs_service_pb2_grpc.LogsServiceServicer):
    def __init__(self, sink: OtlpSink):
        self._sink = sink

    def Export(self, request, context):  # noqa: N802, ARG002
        self._sink._on_logs(request)
        return logs_service_pb2.ExportLogsServiceResponse()


# --------------------------------------------------------------------------- #
# The sink
# --------------------------------------------------------------------------- #
class OtlpSink:
    def __init__(self, forward_to: str | None = None):
        """``forward_to``: ``host:port`` of an OTLP/gRPC receiver to tee trace and metrics exports to."""
        self.forward_to = forward_to
        self._lock = threading.Lock()
        self._spans: list[SpanRecord] = []
        self._metrics: list[MetricRecord] = []
        self._logs: list[LogRecord] = []
        self._forward_ok = 0
        self._forward_errors = 0
        self._server: grpc.Server | None = None
        self._forward_channel: grpc.Channel | None = None
        self._forward_stub: trace_service_pb2_grpc.TraceServiceStub | None = None
        self._forward_metrics_stub: metrics_service_pb2_grpc.MetricsServiceStub | None = None
        self.port: int | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> int:
        # so_reuseport=0 like the services' get_server_options: no second process can share the port.
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=8), options=[("grpc.so_reuseport", 0)])
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(_TraceServicer(self), self._server)
        metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(_MetricsServicer(self), self._server)
        logs_service_pb2_grpc.add_LogsServiceServicer_to_server(_LogsServicer(self), self._server)
        self.port = self._server.add_insecure_port("127.0.0.1:0")
        self._server.start()
        if self.forward_to:
            self._forward_channel = grpc.insecure_channel(self.forward_to)
            self._forward_stub = trace_service_pb2_grpc.TraceServiceStub(self._forward_channel)
            self._forward_metrics_stub = metrics_service_pb2_grpc.MetricsServiceStub(self._forward_channel)
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop(grace=1).wait()
            self._server = None
        if self._forward_channel is not None:
            self._forward_channel.close()
            self._forward_channel = None
            self._forward_stub = None
            self._forward_metrics_stub = None

    @property
    def endpoint(self) -> str:
        """The value for ``OTEL_EXPORTER_OTLP_ENDPOINT``."""
        if self.port is None:
            raise RuntimeError("OtlpSink is not started")
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------- receiving
    def _on_traces(self, request) -> None:
        spans = _decode_spans(request)
        with self._lock:
            self._spans.extend(spans)
        # Read once: stop() may clear the stub and close its channel while a forward is in flight.
        self._forward(self._forward_stub, request)

    def _on_metrics(self, request) -> None:
        metrics = _decode_metrics(request)
        with self._lock:
            self._metrics.extend(metrics)
        self._forward(self._forward_metrics_stub, request)

    def _forward(self, stub, request) -> None:
        if stub is None:
            return
        try:
            stub.Export(request, timeout=FORWARD_TIMEOUT_SECONDS)
            ok = True
        except Exception:  # RpcError, or ValueError on a channel stop() closed: best effort
            ok = False
        with self._lock:
            if ok:
                self._forward_ok += 1
            else:
                self._forward_errors += 1

    def _on_logs(self, request) -> None:
        records = _decode_logs(request)
        with self._lock:
            self._logs.extend(records)

    # --------------------------------------------------------------- reading
    @property
    def forward_ok(self) -> int:
        with self._lock:
            return self._forward_ok

    @property
    def forward_errors(self) -> int:
        with self._lock:
            return self._forward_errors

    @property
    def log_record_count(self) -> int:
        with self._lock:
            return len(self._logs)

    def logs(self, *, service: str | None = None, trace_id: str | None = None) -> list[LogRecord]:
        """Recorded log records matching every given filter, oldest first."""
        with self._lock:
            records = list(self._logs)
        return [
            r
            for r in records
            if (service is None or r.service == service) and (trace_id is None or r.trace_id == trace_id)
        ]

    def spans(
        self,
        name: str | None = None,
        attrs: Mapping[str, Any] | None = None,
        *,
        service: str | None = None,
        event: str | None = None,
        root: bool | None = None,
    ) -> list[SpanRecord]:
        """Recorded spans matching every given filter (``attrs`` is a subset match)."""
        with self._lock:
            spans = list(self._spans)
        return [
            s
            for s in spans
            if (name is None or s.name == name)
            and (service is None or s.service == service)
            and (root is None or s.is_root == root)
            and (event is None or event in s.event_names())
            and _matches(s.attributes, attrs)
        ]

    def metric_points(
        self,
        name: str,
        attrs: Mapping[str, Any] | None = None,
        *,
        service: str | None = None,
    ) -> list[MetricPoint]:
        """Every recorded data point of ``name`` whose attributes include ``attrs``, oldest first."""
        with self._lock:
            metrics = list(self._metrics)
        return [
            p
            for m in metrics
            if m.name == name and (service is None or m.service == service)
            for p in m.points
            if _matches(p.attributes, attrs)
        ]

    def metric_names(self) -> set[str]:
        with self._lock:
            return {m.name for m in self._metrics}

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()
            self._metrics.clear()
            self._logs.clear()

    # --------------------------------------------------------------- waiting
    def wait_span(
        self,
        name: str | None,
        attrs: Mapping[str, Any] | None = None,
        *,
        service: str | None = None,
        event: str | None = None,
        root: bool | None = None,
        timeout: float = DEFAULT_WAIT_SECONDS,
    ) -> SpanRecord:
        """The OLDEST span matching the filters (see ``spans``; ``name=None`` matches any name);
        ``TimeoutError`` when none arrives.

        A span is exported when it ENDS: the game-session span at game end, a
        ``player_lifecycle`` span at the player's elimination or game end.
        The sink lives as long as the (module-scoped) stack and is not cleared
        between tests: filter by ``game.id`` so an earlier test's span cannot match.
        """
        found = self._poll(lambda: self.spans(name, attrs, service=service, event=event, root=root), timeout)
        if found:
            return found[0]
        seen = sorted({s.name for s in self.spans()})
        raise TimeoutError(
            f"no span {name!r} attrs={dict(attrs or {})} service={service} event={event} root={root} "
            f"within {timeout}s; span names seen: {seen}"
        )

    def wait_metric(
        self,
        name: str,
        attrs: Mapping[str, Any] | None = None,
        *,
        service: str | None = None,
        predicate: Callable[[MetricPoint], bool] | None = None,
        timeout: float = DEFAULT_WAIT_SECONDS,
    ) -> MetricPoint:
        """The LATEST data point of ``name`` matching ``attrs`` (and ``predicate``); ``TimeoutError`` when none.

        Metrics are pushed every export interval (``metrics_export_interval_ms``),
        cumulative: a later point supersedes an earlier one.
        """

        def matching() -> list[MetricPoint]:
            points = self.metric_points(name, attrs, service=service)
            return [p for p in points if predicate is None or predicate(p)]

        found = self._poll(matching, timeout)
        if found:
            return found[-1]
        raise TimeoutError(
            f"no metric point {name!r} attrs={dict(attrs or {})} service={service} within {timeout}s; "
            f"metric names seen: {sorted(self.metric_names())}"
        )

    def wait_log(
        self,
        *,
        service: str | None = None,
        trace_id: str | None = None,
        timeout: float = DEFAULT_WAIT_SECONDS,
    ) -> LogRecord:
        """The OLDEST log record matching the filters; ``TimeoutError`` when none arrives."""
        found = self._poll(lambda: self.logs(service=service, trace_id=trace_id), timeout)
        if found:
            return found[0]
        raise TimeoutError(f"no log record service={service} trace_id={trace_id} within {timeout}s")

    async def wait_log_async(self, **kwargs) -> LogRecord:
        return await asyncio.to_thread(self.wait_log, **kwargs)

    async def wait_span_async(self, name: str | None, attrs: Mapping[str, Any] | None = None, **kwargs) -> SpanRecord:
        return await asyncio.to_thread(self.wait_span, name, attrs, **kwargs)

    async def wait_metric_async(self, name: str, attrs: Mapping[str, Any] | None = None, **kwargs) -> MetricPoint:
        return await asyncio.to_thread(self.wait_metric, name, attrs, **kwargs)

    @staticmethod
    def _poll(condition: Callable[[], list], timeout: float) -> list:
        deadline = time.monotonic() + timeout
        while True:
            found = condition()
            if found or time.monotonic() >= deadline:
                return found
            time.sleep(POLL_INTERVAL_SECONDS)
