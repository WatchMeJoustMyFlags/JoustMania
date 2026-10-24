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

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid

import pytest
from openfeature.evaluation_context import EvaluationContext

from tests.integration.helpers import flagd_probe_client, poll_until
from tests.service.compositions import FLAG_PLANE

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = FLAG_PLANE

FLAG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "services", "flagd", "ci")
FLAG_SETS = (
    "system",
    "controller",
    "observability",
    "game",
    "user",
    "agent",
    "interventions",
    "rollout",
    "operator",
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


def _flag_service_url(stack) -> str:
    host = stack.get_service_host("flag-service", 8070)
    port = stack.get_service_port("flag-service", 8070)
    return f"http://{host}:{port}"


def _http(stack, method: str, path: str, body=None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _flag_service_url(stack) + path,
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


@pytest.mark.uc("UC-053", flow="main", br=["BR-007"], cond=["Pre-2"])
@pytest.mark.parametrize("resolver", ["in_process", "rpc"])
@pytest.mark.parametrize("flag_set", FLAG_SETS)
def test_every_flag_resolves_through_its_flag_set_selector(stack, flag_set, resolver):
    """C1: every flag resolves via flagSetId=<set>, reason neither DEFAULT nor ERROR."""
    flags = _load_set(flag_set)
    failures = []
    with flagd_probe_client(stack, flag_set, resolver=resolver) as client:
        # The provider connects asynchronously; wait until the set is served.
        first_key = sorted(flags)[0]
        first_default = flags[first_key]["variants"][flags[first_key]["defaultVariant"]]
        poll_until(
            lambda: _reason(_details(client, first_key, first_default, EvaluationContext())) != "ERROR",
            timeout=15.0,
        )
        for key, flag in sorted(flags.items()):
            default = flag["variants"][flag["defaultVariant"]]
            probe = dict(PROBE_CONTEXTS.get(key, {}))
            ctx = EvaluationContext(targeting_key=probe.pop("targetingKey", None), attributes=probe)
            details = _details(client, key, default, ctx)
            if _reason(details) in BAD_REASONS or details.error_code is not None:
                failures.append(
                    f"{key}: reason={_reason(details)} error={details.error_code} {details.error_message or ''}"
                )
    assert not failures, f"{flag_set} via {resolver}: " + "; ".join(failures)


@pytest.mark.uc("UC-053", flow="main")
def test_flagd_holds_one_sync_stream(stack):
    """flagd opens ONE SyncFlags stream that carries every served set (N1, N6)."""
    assert poll_until(
        lambda: _http(stack, "GET", "/state")[1].get("sync_streams") == 1,
        timeout=15.0,
    )
    status, state = _http(stack, "GET", "/state")
    assert status == 200 and state["epoch"]
    assert {"system", "observability"} <= set(state["served_sets"])


@pytest.mark.uc("UC-053", flow="A1", br=["BR-001"])
@pytest.mark.usefixtures("fresh_stack")
def test_write_reaches_both_resolvers_and_clears(stack):
    """A stack overlay written to the service is served by flagd, then cleared."""
    nonce = f"probe-{uuid.uuid4().hex[:8]}"
    ctx = EvaluationContext()
    with (
        flagd_probe_client(stack, PROBE_SET) as in_process,
        flagd_probe_client(stack, PROBE_SET, resolver="rpc") as rpc,
    ):
        status, body = _http(
            stack,
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
            assert poll_until(lambda: rpc.get_string_value(PROBE_FLAG, "", ctx) == nonce, timeout=5.0)
            print(f"\nwrite -> both resolvers: {1000 * (time.monotonic() - started):.0f} ms")
        finally:
            assert _http(stack, "DELETE", f"/{PROBE_SET}/{PROBE_FLAG}")[0] == 204
        assert poll_until(lambda: in_process.get_string_value(PROBE_FLAG, "x", ctx) == "", timeout=5.0)


@pytest.mark.uc("UC-053", flow="A5", br=["BR-001", "BR-002"])
def test_rejected_writes(stack):
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
        # A credential-bearing URL takes declared variants only (no key exfiltration).
        (
            "PUT",
            "/agent/decision.openai.base_url",
            {"value": "https://attacker.example/v1"},
            422,
            "out_of_bounds",
        ),
        (
            "PUT",
            f"/{PROBE_SET}/{PROBE_FLAG}",
            {"value": "x", "persist": True},
            400,
            "malformed",
        ),
    ]
    for method, path, body, want_status, want_reason in cases:
        status, resp = _http(stack, method, path, body)
        assert (status, resp.get("error")) == (want_status, want_reason), (
            path,
            body,
            status,
            resp,
        )


@pytest.mark.uc("UC-053", flow="main", br=["BR-003"])
def test_game_scope_lifecycle_over_http(stack):
    """PUT /games registers, a repeat is already_registered, DELETE drops it."""
    game_id = f"game_{uuid.uuid4().hex[:12]}"
    body = {
        "coordinator_instance": "integration/1",
        "game_kind": "shadow",
        "serials": ["MOCK9001"],
    }
    assert _http(stack, "PUT", f"/games/{game_id}", body) == (
        200,
        {
            "game_id": game_id,
            "status": "registered",
            "epoch": _http(stack, "GET", "/state")[1]["epoch"],
        },
    )
    assert _http(stack, "PUT", f"/games/{game_id}", body)[1]["status"] == "already_registered"
    assert any(g["game_id"] == game_id for g in _http(stack, "GET", "/games")[1]["games"])
    assert _http(stack, "DELETE", f"/games/{game_id}")[0] == 204
    assert not any(g["game_id"] == game_id for g in _http(stack, "GET", "/games")[1]["games"])


@pytest.mark.uc("UC-053", flow="A8")
@pytest.mark.nfr("NFR-052")
@pytest.mark.usefixtures("fresh_stack")
def test_flagd_keeps_its_last_snapshot_while_the_flag_service_is_down(stack):
    """A8 step 2: with the flag service stopped, flagd still serves the last value
    it received, to a client that was connected and to one that connects later."""
    nonce = f"last-{uuid.uuid4().hex[:8]}"
    ctx = EvaluationContext()
    status, body = _http(stack, "PUT", f"/{PROBE_SET}/{PROBE_FLAG}", {"value": nonce, "writer": "service-test"})
    assert status == 200, body
    with flagd_probe_client(stack, PROBE_SET) as connected:
        assert poll_until(lambda: connected.get_string_value(PROBE_FLAG, "", ctx) == nonce, timeout=5.0)

        stack.flag_plane.flag_service.stop()  # returns once the process has exited
        assert not stack.flag_plane.flag_service.running
        with pytest.raises(OSError):
            _http(stack, "PUT", f"/{PROBE_SET}/{PROBE_FLAG}", {"value": "unreachable", "writer": "service-test"})

        # Nothing can push a configuration now: whatever flagd serves is its snapshot.
        assert connected.get_string_value(PROBE_FLAG, "", ctx) == nonce
        with (
            flagd_probe_client(stack, PROBE_SET) as late_in_process,
            flagd_probe_client(stack, PROBE_SET, resolver="rpc") as late_rpc,
        ):
            assert poll_until(lambda: late_in_process.get_string_value(PROBE_FLAG, "", ctx) == nonce, timeout=15.0), (
                "flagd did not serve its last snapshot to a client that connected during the outage"
            )
            assert poll_until(lambda: late_rpc.get_string_value(PROBE_FLAG, "", ctx) == nonce, timeout=15.0)
        assert connected.get_string_value(PROBE_FLAG, "", ctx) == nonce


def _baseline_digest(flag_dir) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(flag_dir.glob("*.json"))}


@pytest.mark.uc("UC-053", cond=["Post-S-4"], br=["BR-004"])
@pytest.mark.usefixtures("fresh_stack")
def test_writes_never_change_a_baseline_flag_file(stack):
    """Post-S-4: a stack overlay, a persisted admin setting, a game-scoped write and
    their clean-up leave every baseline file the flag service loaded byte-identical."""
    flag_dir = stack.flag_plane.flag_dir
    before = _baseline_digest(flag_dir)
    assert {"game.json", "interventions.json", "observability.json"} <= set(before), before
    game_id = f"game_{uuid.uuid4().hex[:12]}"
    writes = [
        ("PUT", f"/{PROBE_SET}/{PROBE_FLAG}", {"value": "overlay", "writer": "service-test"}),
        ("PUT", "/game/sensitivity", {"variant": "slow", "persist": True, "writer": "menu"}),
        (
            "PUT",
            f"/games/{game_id}",
            {"coordinator_instance": "service-test/1", "game_kind": "shadow", "serials": ["MOCK9001"]},
        ),
        (
            "PUT",
            "/interventions/shield_seconds",
            {"value": 5, "scope": {"game_id": game_id, "serial": "MOCK9001"}, "writer": "agent"},
        ),
    ]
    for method, path, body in writes:
        status, resp = _http(stack, method, path, body)
        assert status == 200, (path, status, resp)
    assert stack.flag_plane.state_file.exists(), "the admin setting was not persisted to the state file"
    assert _http(stack, "DELETE", f"/games/{game_id}")[0] == 204
    assert _http(stack, "DELETE", f"/{PROBE_SET}/{PROBE_FLAG}")[0] == 204
    assert _baseline_digest(flag_dir) == before
