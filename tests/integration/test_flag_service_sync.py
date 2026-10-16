"""
UC-053: the flag service as flagd's gRPC sync source (epic #1407, #1408-#1410).

- C1 exit test (architecture note §5.2): EVERY flag of EVERY set resolves
  through its ``flagSetId=<set>`` selector with a reason other than DEFAULT and
  ERROR, on the in-process resolver (8015) and on RPC (8013). One gRPC source
  carries all sets with per-flag ``metadata.flagSetId``; a broken merge shows up
  here as FLAG_NOT_FOUND (ERROR) or a fall-through (DEFAULT).
- The write path end to end: a write to the service reaches both resolvers
  (via the observability propagation probe, a flag no service reads).
- Validation and the game lifecycle over HTTP.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

import pytest
from openfeature.evaluation_context import EvaluationContext

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tests.integration.helpers import flagd_probe_client, poll_until  # noqa: E402

FLAG_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "services", "flagd", "ci"
)
FLAG_SETS = (
    "system",
    "controller",
    "observability",
    "game",
    "user",
    "agent",
    "interventions",
    "rollout",
)
PROBE_SET, PROBE_FLAG = "observability", "flagservice.propagation_probe"

# Flags whose baseline targeting can fall through (reason DEFAULT): probe each
# with a context that matches a rule. A new targeted flag that falls through
# fails the test until it gets a probe context here.
PROBE_CONTEXTS = {
    "sensitivity": {"game_mode": "Werewolf"},
    "windows": {"targetingKey": "calm"},
    "metrics_export_interval_ms": {"service": "controller-manager-service"},
    "otel_bsp_schedule_delay_millis": {"service": "controller-manager-service"},
}
BAD_REASONS = {"DEFAULT", "ERROR"}


def _flag_service_url(docker_compose) -> str:
    host = docker_compose.get_service_host("flag-service", 8070)
    port = docker_compose.get_service_port("flag-service", 8070)
    return f"http://{host}:{port}"


def _http(docker_compose, method: str, path: str, body=None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _flag_service_url(docker_compose) + path,
        method=method,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as err:
        raw = err.read()
        return err.code, (json.loads(raw) if raw else {})


def _load_set(flag_set: str) -> dict:
    with open(os.path.join(FLAG_DIR, f"{flag_set}.json")) as f:
        return json.load(f)["flags"]


def _details(client, key: str, default, ctx):
    if isinstance(default, bool):
        return client.get_boolean_details(key, default, ctx)
    if isinstance(default, int):
        return client.get_integer_details(key, default, ctx)
    if isinstance(default, float):
        return client.get_float_details(key, default, ctx)
    if isinstance(default, str):
        return client.get_string_details(key, default, ctx)
    return client.get_object_details(key, default, ctx)


def _reason(details) -> str:
    return str(getattr(details.reason, "value", details.reason)).upper()


@pytest.mark.uc("UC-053", flow="main", br=["BR-007"])
@pytest.mark.parametrize("resolver", ["in_process", "rpc"])
@pytest.mark.parametrize("flag_set", FLAG_SETS)
def test_every_flag_resolves_through_its_flag_set_selector(
    docker_compose, flag_set, resolver
):
    """C1: every flag resolves via flagSetId=<set>, reason neither DEFAULT nor ERROR."""
    flags = _load_set(flag_set)
    failures = []
    with flagd_probe_client(docker_compose, flag_set, resolver=resolver) as client:
        # The provider connects asynchronously; wait until the set is served.
        first_key = sorted(flags)[0]
        first_default = flags[first_key]["variants"][flags[first_key]["defaultVariant"]]
        poll_until(
            lambda: (
                _reason(_details(client, first_key, first_default, EvaluationContext()))
                != "ERROR"
            ),
            timeout=15.0,
        )
        for key, flag in sorted(flags.items()):
            default = flag["variants"][flag["defaultVariant"]]
            probe = dict(PROBE_CONTEXTS.get(key, {}))
            ctx = EvaluationContext(
                targeting_key=probe.pop("targetingKey", None), attributes=probe
            )
            details = _details(client, key, default, ctx)
            if _reason(details) in BAD_REASONS or details.error_code is not None:
                failures.append(
                    f"{key}: reason={_reason(details)} error={details.error_code} {details.error_message or ''}"
                )
    assert not failures, f"{flag_set} via {resolver}: " + "; ".join(failures)


@pytest.mark.uc("UC-053", flow="main")
def test_flagd_holds_one_sync_stream(docker_compose):
    """flagd opens ONE SyncFlags stream that carries every served set (N1, N6)."""
    assert poll_until(
        lambda: _http(docker_compose, "GET", "/state")[1].get("sync_streams") == 1,
        timeout=15.0,
    )
    status, state = _http(docker_compose, "GET", "/state")
    assert status == 200 and state["epoch"]
    assert {"system", "observability"} <= set(state["served_sets"])


@pytest.mark.uc("UC-053", flow="A1", br=["BR-001"])
def test_write_reaches_both_resolvers_and_clears(docker_compose):
    """A stack overlay written to the service is served by flagd, then cleared."""
    nonce = f"probe-{uuid.uuid4().hex[:8]}"
    ctx = EvaluationContext()
    with (
        flagd_probe_client(docker_compose, PROBE_SET) as in_process,
        flagd_probe_client(docker_compose, PROBE_SET, resolver="rpc") as rpc,
    ):
        status, body = _http(
            docker_compose,
            "PUT",
            f"/{PROBE_SET}/{PROBE_FLAG}",
            {"value": nonce, "writer": "integration-test"},
        )
        try:
            assert status == 200, body
            assert body["variant"].startswith("o") and body["epoch"] in body["variant"]
            started = time.monotonic()
            assert poll_until(
                lambda: in_process.get_string_value(PROBE_FLAG, "", ctx) == nonce,
                timeout=5.0,
            )
            assert poll_until(
                lambda: rpc.get_string_value(PROBE_FLAG, "", ctx) == nonce, timeout=5.0
            )
            print(
                f"\nwrite -> both resolvers: {1000 * (time.monotonic() - started):.0f} ms"
            )
        finally:
            assert (
                _http(docker_compose, "DELETE", f"/{PROBE_SET}/{PROBE_FLAG}")[0] == 204
            )
        assert poll_until(
            lambda: in_process.get_string_value(PROBE_FLAG, "x", ctx) == "", timeout=5.0
        )


@pytest.mark.uc("UC-053", flow="A5", br=["BR-001", "BR-002"])
def test_rejected_writes(docker_compose):
    cases = [
        ("PUT", "/system/idle.enabled", {"variant": "off"}, 422, "flag_read_only"),
        ("PUT", "/nope/x", {"variant": "a"}, 404, "unknown_flag_set"),
        ("PUT", f"/{PROBE_SET}/nope", {"variant": "a"}, 404, "unknown_flag"),
        (
            "PUT",
            f"/{PROBE_SET}/{PROBE_FLAG}",
            {"variant": "none", "value": "x"},
            400,
            "malformed",
        ),
        ("PUT", f"/{PROBE_SET}/{PROBE_FLAG}", {"value": 42}, 422, "out_of_bounds"),
        (
            "PUT",
            f"/{PROBE_SET}/{PROBE_FLAG}",
            {"value": "x", "persist": True},
            400,
            "malformed",
        ),
    ]
    for method, path, body, want_status, want_reason in cases:
        status, resp = _http(docker_compose, method, path, body)
        assert (status, resp.get("error")) == (want_status, want_reason), (
            path,
            body,
            status,
            resp,
        )


@pytest.mark.uc("UC-053", flow="main", br=["BR-003"])
def test_game_scope_lifecycle_over_http(docker_compose):
    """PUT /games registers, a repeat is already_registered, DELETE drops it."""
    game_id = f"game_{uuid.uuid4().hex[:12]}"
    body = {
        "coordinator_instance": "integration/1",
        "game_kind": "shadow",
        "serials": ["MOCK9001"],
    }
    assert _http(docker_compose, "PUT", f"/games/{game_id}", body) == (
        200,
        {
            "game_id": game_id,
            "status": "registered",
            "epoch": _http(docker_compose, "GET", "/state")[1]["epoch"],
        },
    )
    assert (
        _http(docker_compose, "PUT", f"/games/{game_id}", body)[1]["status"]
        == "already_registered"
    )
    assert any(
        g["game_id"] == game_id
        for g in _http(docker_compose, "GET", "/games")[1]["games"]
    )
    assert _http(docker_compose, "DELETE", f"/games/{game_id}")[0] == 204
    assert not any(
        g["game_id"] == game_id
        for g in _http(docker_compose, "GET", "/games")[1]["games"]
    )
