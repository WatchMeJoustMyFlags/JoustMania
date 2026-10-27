"""The #2184 retry for flagd RPC test probes (flagd_rpc_retry), against a fake flagd.

The fake serves ``flagd.evaluation.v2.Service`` on localhost with raw bytes, so
a test can hand the real FlagdProvider a response that does not parse (the CI
failure: DecodeError on ResolveIntResponse -> INTERNAL -> GENERAL) and check
that only that failure is retried. No docker needed.

Harness test: it covers test infrastructure, not a use-case flow, so it carries
no ``uc`` tag.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from concurrent import futures

import grpc
import pytest

openfeature = pytest.importorskip("openfeature.contrib.provider.flagd")

from openfeature import api  # noqa: E402
from openfeature.contrib.provider.flagd import FlagdProvider  # noqa: E402
from openfeature.contrib.provider.flagd.config import (  # noqa: E402
    CacheType,
    ResolverType,
)
from openfeature.evaluation_context import EvaluationContext  # noqa: E402
from openfeature.exception import ErrorCode  # noqa: E402
from openfeature.provider import ProviderStatus  # noqa: E402
from openfeature.schemas.protobuf.flagd.evaluation.v2 import (  # noqa: E402
    evaluation_pb2,
)

import flagd_rpc_retry  # noqa: E402

SERVICE = "flagd.evaluation.v2.Service"
# A varint that never ends: upb raises DecodeError, as in the CI runs.
UNPARSABLE = b"\x08\xff\xff"
GOOD = evaluation_pb2.ResolveIntResponse(
    value=70, reason="STATIC", variant="light"
).SerializeToString()


class FakeFlagd:
    """ResolveInt answers from a script (bytes or a status code); EventStream says ready."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0
        self._stop = threading.Event()
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        self.server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    SERVICE,
                    {
                        "ResolveInt": grpc.unary_unary_rpc_method_handler(
                            self._resolve_int
                        ),
                        "EventStream": grpc.unary_stream_rpc_method_handler(
                            self._event_stream
                        ),
                    },
                ),
            )
        )
        self.port = self.server.add_insecure_port("127.0.0.1:0")

    def _resolve_int(self, request, context):
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        if isinstance(answer, grpc.StatusCode):
            context.abort(answer, "scripted failure")
        return answer

    def _event_stream(self, request, context):
        yield evaluation_pb2.EventStreamResponse(
            type="provider_ready"
        ).SerializeToString()
        while context.is_active() and not self._stop.wait(0.05):
            pass

    def __enter__(self):
        self.server.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.server.stop(grace=None)


@contextlib.contextmanager
def _client(fake, *, retry: bool):
    provider = FlagdProvider(
        host="127.0.0.1",
        port=fake.port,
        resolver_type=ResolverType.RPC,
        cache=CacheType.DISABLED,
        deadline_ms=2000,
    )
    if retry:
        flagd_rpc_retry.retry_decode_failures(provider)
    domain = f"rpc_retry_{uuid.uuid4().hex[:6]}"
    api.set_provider(provider, domain=domain)
    try:
        client = api.get_client(domain)
        deadline = time.monotonic() + 5
        while client.get_provider_status() != ProviderStatus.READY:
            assert time.monotonic() < deadline, "fake flagd: provider never ready"
            time.sleep(0.02)
        yield client
    finally:
        provider.shutdown()


def _resolve(client):
    return client.get_integer_details(
        "shield.hold_rumble_intensity", -1, EvaluationContext()
    )


def test_unretried_decode_failure_is_the_ci_error():
    """Baseline: without the retry an unparsable response is GENERAL / INTERNAL."""
    with FakeFlagd([UNPARSABLE, GOOD]) as fake, _client(fake, retry=False) as client:
        details = _resolve(client)
    assert details.error_code == ErrorCode.GENERAL
    assert "StatusCode.INTERNAL" in (details.error_message or "")
    assert fake.calls == 1


def test_decode_failure_is_retried_until_the_response_parses():
    with (
        FakeFlagd([UNPARSABLE, UNPARSABLE, GOOD]) as fake,
        _client(fake, retry=True) as client,
    ):
        details = _resolve(client)
    assert details.error_code is None, details.error_message
    assert (details.value, details.variant) == (70, "light")
    assert fake.calls == 3


def test_decode_failure_retries_are_bounded():
    with FakeFlagd([UNPARSABLE]) as fake, _client(fake, retry=True) as client:
        details = _resolve(client)
    assert details.error_code == ErrorCode.GENERAL
    assert fake.calls == flagd_rpc_retry.MAX_ATTEMPTS


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        (grpc.StatusCode.INTERNAL, ErrorCode.GENERAL),
        (grpc.StatusCode.NOT_FOUND, ErrorCode.FLAG_NOT_FOUND),
        (grpc.StatusCode.INVALID_ARGUMENT, ErrorCode.TYPE_MISMATCH),
    ],
)
def test_other_failures_are_not_retried(status, error_code):
    """A server-side INTERNAL, a missing flag or a type mismatch is the result under test."""
    with FakeFlagd([status, GOOD]) as fake, _client(fake, retry=True) as client:
        details = _resolve(client)
    assert details.error_code == error_code
    assert fake.calls == 1
