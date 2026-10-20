"""In-process fakes of the service tier's external dependencies."""

from .openai import FakeOpenAI
from .otlp import LogRecord, MetricPoint, MetricRecord, OtlpSink, SpanEventRecord, SpanRecord
from .tcp_proxy import TcpProxy

__all__ = [
    "FakeOpenAI",
    "LogRecord",
    "MetricPoint",
    "MetricRecord",
    "OtlpSink",
    "SpanEventRecord",
    "SpanRecord",
    "TcpProxy",
]
