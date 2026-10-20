"""In-process fakes of the service tier's external dependencies."""

from .claude import FakeClaude
from .openai import FakeOpenAI
from .otlp import LogRecord, MetricPoint, MetricRecord, OtlpSink, SpanEventRecord, SpanRecord
from .tcp_proxy import TcpProxy

__all__ = [
    "FakeClaude",
    "FakeOpenAI",
    "LogRecord",
    "MetricPoint",
    "MetricRecord",
    "OtlpSink",
    "SpanEventRecord",
    "SpanRecord",
    "TcpProxy",
]
