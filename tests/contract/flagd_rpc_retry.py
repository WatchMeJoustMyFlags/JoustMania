"""Retry a flagd RPC evaluation whose response did not deserialize (#2184).

Test tiers only. Every service reads flags through the in-process resolver
(sync port 8015: lib/feature_flags, services/agent/flags, services/rust-hid);
the RPC resolver (evaluation port 8013) is used only by test probes that check
flagd's server-side evaluation.

On rare CI runs (never reproduced locally) a ResolveInt response from flagd
v0.16.0 does not parse as ``flagd.evaluation.v2.ResolveIntResponse``. The
cause is not proven: it is either the bytes flagd sends or the grpcio / upb
client (CI runs Python 3.12, local runs 3.14). grpcio fails the call with
INTERNAL ``Exception deserializing response!``, the provider maps it to
``GENERAL received grpc status code StatusCode.INTERNAL``, and a test that
checks the getter, value or targeting fails. The provider's own gRPC retry
policy covers only UNAVAILABLE and UNKNOWN, so nothing retries it.

``retry_decode_failures(provider)`` rebuilds the provider's evaluation stub on
an intercepted channel:

- every unary Resolve* response that does not parse is logged (length and the
  first bytes in hex) before it fails, so the next CI hit carries evidence;
- the interceptor repeats a unary call ONLY when it failed with exactly that
  client-side decode failure, at most ``MAX_ATTEMPTS`` times in total, with a
  warning per retry. Any other status (NOT_FOUND, INVALID_ARGUMENT, a server
  INTERNAL, a deadline) and every typed result (TYPE_MISMATCH, a wrong value)
  pass through untouched, so the contract the tests check is not weakened.

``assert_retry_active(provider)`` fails when the provider no longer uses that
stub (a provider that rebuilds it on connect), so the retry cannot drop out
silently. Remove both with the flagd image and Python provider upgrade
(#1945) if the decode failure no longer shows there.
"""

from __future__ import annotations

import logging

import grpc

logger = logging.getLogger(__name__)

# grpc/_channel.py sets these when the response deserializer raises.
DECODE_FAILURE_CODE = grpc.StatusCode.INTERNAL
DECODE_FAILURE_DETAILS = "Exception deserializing response!"
MAX_ATTEMPTS = 3


def is_decode_failure(outcome) -> bool:
    """True when a finished unary call failed only because its response did not parse."""
    return (
        outcome.code() == DECODE_FAILURE_CODE
        and outcome.details() == DECODE_FAILURE_DETAILS
    )


class RetryDecodeFailure(grpc.UnaryUnaryClientInterceptor):
    """Repeat a unary call that failed with the client-side decode failure."""

    def intercept_unary_unary(self, continuation, client_call_details, request):
        outcome = continuation(client_call_details, request)
        for retry in range(1, MAX_ATTEMPTS):
            if not is_decode_failure(outcome):
                return outcome
            logger.warning(
                "flagd RPC %s: response did not deserialize, retry %d/%d (#2184)",
                client_call_details.method,
                retry,
                MAX_ATTEMPTS - 1,
            )
            outcome = continuation(client_call_details, request)
        return outcome


_RAW_BYTES_LOGGED = 64
_STUB_ATTR = "_joustmania_decode_retry_stub"


def _logging_deserializer(response_type):
    """``response_type.FromString`` that logs the raw bytes of a response that does not parse."""

    def deserialize(raw: bytes):
        try:
            return response_type.FromString(raw)
        except Exception:
            logger.warning(
                "flagd RPC %s did not parse (#2184): %d bytes, first %d hex %s",
                response_type.DESCRIPTOR.full_name,
                len(raw),
                _RAW_BYTES_LOGGED,
                raw[:_RAW_BYTES_LOGGED].hex(),
            )
            raise

    return deserialize


def _service_stub(channel):
    """The v2 evaluation stub, with every unary Resolve* deserializer logging bad bytes."""
    from openfeature.schemas.protobuf.flagd.evaluation.v2 import (
        evaluation_pb2,
        evaluation_pb2_grpc,
    )

    stub = evaluation_pb2_grpc.ServiceStub(channel)
    for kind in ("Boolean", "String", "Float", "Int", "Object"):
        setattr(
            stub,
            f"Resolve{kind}",
            channel.unary_unary(
                f"/flagd.evaluation.v2.Service/Resolve{kind}",
                request_serializer=getattr(
                    evaluation_pb2, f"Resolve{kind}Request"
                ).SerializeToString,
                response_deserializer=_logging_deserializer(
                    getattr(evaluation_pb2, f"Resolve{kind}Response")
                ),
            ),
        )
    return stub


def retry_decode_failures(provider):
    """Route an RPC ``FlagdProvider``'s evaluation stub through ``RetryDecodeFailure``.

    Call before ``api.set_provider``: the event stream starts on initialize and
    uses the same stub (stream calls pass the interceptor unchanged). Fails
    loudly when the provider no longer has ``resolver.channel`` / ``resolver.stub``
    (a provider upgrade, #1945) instead of silently dropping the retry.
    """
    resolver = provider.resolver
    if not hasattr(resolver, "stub") or not hasattr(resolver, "channel"):
        raise TypeError(
            f"{type(resolver).__name__} has no stub/channel: the #2184 retry needs updating"
        )
    resolver.stub = _service_stub(
        grpc.intercept_channel(resolver.channel, RetryDecodeFailure())
    )
    setattr(resolver, _STUB_ATTR, resolver.stub)
    return provider


def assert_retry_active(provider) -> None:
    """Fail when the provider resolves through another stub than ``retry_decode_failures`` set.

    Call after the provider has connected and resolved (the harnesses call it
    when the probe client closes): a provider that rebuilds its stub in
    ``connect()`` / ``initialize()`` would otherwise drop the retry silently.
    """
    resolver = provider.resolver
    wrapped = getattr(resolver, _STUB_ATTR, None)
    if wrapped is None or getattr(resolver, "stub", None) is not wrapped:
        raise AssertionError(
            f"{type(resolver).__name__} no longer resolves through the #2184 retry stub "
            "(rebuilt on connect?): update tests/contract/flagd_rpc_retry.py"
        )
