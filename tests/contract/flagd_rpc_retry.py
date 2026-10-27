"""Retry a flagd RPC evaluation whose response did not deserialize (#2184).

Test tiers only. Every service reads flags through the in-process resolver
(sync port 8015: lib/feature_flags, services/agent/flags, services/rust-hid);
the RPC resolver (evaluation port 8013) is used only by test probes that check
flagd's server-side evaluation.

On rare CI runs (about one resolve in several thousand, never reproduced
locally) a ResolveInt response from flagd v0.16.0 arrives with bytes that do
not parse as ``flagd.evaluation.v2.ResolveIntResponse``. grpcio then fails the
call with INTERNAL ``Exception deserializing response!``, the provider maps it
to ``GENERAL received grpc status code StatusCode.INTERNAL``, and a test that
checks the getter, value or targeting fails although flagd resolved the flag.
The same request a moment later parses. The provider's own gRPC retry policy
covers only UNAVAILABLE and UNKNOWN, so nothing retries it.

``retry_decode_failures(provider)`` routes the provider's evaluation stub
through a client interceptor that repeats a unary call ONLY when it failed
with exactly that client-side decode failure, at most ``MAX_ATTEMPTS`` times
in total, with a warning per retry so the flake stays visible in the log.
Any other status (NOT_FOUND, INVALID_ARGUMENT, a server INTERNAL, a deadline)
and every typed result (TYPE_MISMATCH, a wrong value) pass through untouched,
so the contract the tests check is not weakened.

Remove with the flagd image and Python provider upgrade (#1945) if the
decode failure no longer shows there.
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


def retry_decode_failures(provider):
    """Route an RPC ``FlagdProvider``'s evaluation stub through ``RetryDecodeFailure``.

    Call before ``api.set_provider``: the event stream starts on initialize and
    uses the same stub (stream calls pass the interceptor unchanged). Fails
    loudly when the provider no longer has ``resolver.channel`` / ``resolver.stub``
    (a provider upgrade, #1945) instead of silently dropping the retry.
    """
    from openfeature.schemas.protobuf.flagd.evaluation.v2 import evaluation_pb2_grpc

    resolver = provider.resolver
    if not hasattr(resolver, "stub") or not hasattr(resolver, "channel"):
        raise TypeError(
            f"{type(resolver).__name__} has no stub/channel: the #2184 retry needs updating"
        )
    resolver.stub = evaluation_pb2_grpc.ServiceStub(
        grpc.intercept_channel(resolver.channel, RetryDecodeFailure())
    )
    return provider
