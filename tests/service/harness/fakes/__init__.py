"""In-process fakes of the service tier's external dependencies."""

from .otlp import MetricPoint, MetricRecord, OtlpSink, SpanEventRecord, SpanRecord

__all__ = ["MetricPoint", "MetricRecord", "OtlpSink", "SpanEventRecord", "SpanRecord"]
