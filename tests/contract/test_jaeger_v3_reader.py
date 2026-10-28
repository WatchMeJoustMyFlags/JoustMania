"""The integration tests' Jaeger v3 reader against answers recorded from the stage Pi (#2283).

``tests/integration/jaeger_v3.py`` replaces the legacy ``/api/traces`` reads (removed
in Jaeger 2.21). The fixtures are the dashboard's (``services/dashboard/src/__tests__/
fixtures/jaeger-v3-*.json``, recorded from Jaeger 2.19's ``/jaeger/api/v3``), so the
SPA and the tests are held to the same answer shape.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
import urllib.parse
from unittest import mock

import infra_config as ic
import pytest

_PATH = ic.REPO_ROOT / "tests" / "integration" / "jaeger_v3.py"
_spec = importlib.util.spec_from_file_location("jaeger_v3", _PATH)
jaeger_v3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jaeger_v3)

FIXTURES = ic.REPO_ROOT / "services" / "dashboard" / "src" / "__tests__" / "fixtures"
TRACE_ID = "6941c034ab0b93c2af2e439d02580244"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _answer(status: int, body: dict):
    """A urlopen stand-in: 200 returns ``body``, anything else raises HTTPError with it."""
    payload = json.dumps(body).encode()

    def urlopen(url, timeout=None):
        if status == 200:
            return io.BytesIO(payload)
        raise urllib.error.HTTPError(url, status, "x", {}, io.BytesIO(payload))

    return mock.patch.object(jaeger_v3.urllib.request, "urlopen", side_effect=urlopen)


@pytest.mark.uc("UC-035", flow="main")
def test_search_url_is_v3_with_fixed_window():
    url = jaeger_v3.search_url(
        "http://h/jaeger/api",
        "game-coordinator-service",
        attributes={"game.id": "g1"},
        limit=20,
    )
    parts = urllib.parse.urlsplit(url)
    assert parts.path == "/jaeger/api/v3/traces"
    assert dict(urllib.parse.parse_qsl(parts.query)) == {
        "query.serviceName": "game-coordinator-service",
        "query.searchDepth": "20",
        "query.startTimeMin": "1970-01-01T00:00:00Z",
        "query.startTimeMax": "2100-01-01T00:00:00Z",
        "query.attributes": '{"game.id": "g1"}',
    }


@pytest.mark.uc("UC-035", flow="main")
def test_recorded_answer_reads_like_the_legacy_shape():
    with _answer(200, _fixture("jaeger-v3-decide-trace.json")):
        traces = jaeger_v3.find_traces("http://h/jaeger/api", "agent", "agent.decide")
    assert [t["traceID"] for t in traces] == [TRACE_ID]
    spans = {s["operationName"]: s for s in traces[0]["spans"]}
    decide = spans["agent.decide"]
    assert decide["spanID"] == "c66f7c2a844e006d"
    assert decide["startTime"] == 1_793_139_266_961_628
    assert decide["duration"] == 206
    assert decide["references"] == [
        {
            "refType": "CHILD_OF",
            "traceID": TRACE_ID,
            "spanID": spans["agent.signal_received"]["spanID"],
        }
    ]
    tags = {t["key"]: t["value"] for t in decide["tags"]}
    assert tags["decide.blocked"] is True and tags["decide.latency_ms"] == 0
    assert tags["game.id"] == "game_d1a30cc80da4"
    # A link is FOLLOWS_FROM, never a parent: the game root stays a root for jaeger_root_spans.
    assert [r["refType"] for r in spans["agent.decision"]["references"]] == [
        "CHILD_OF",
        "FOLLOWS_FROM",
    ]
    processes = traces[0]["processes"]
    services = {
        name: processes[s["processID"]]["serviceName"] for name, s in spans.items()
    }
    assert services["Joust Free-for-All"] == "game-coordinator-service"
    assert services["agent.decide"] == "agent"
    events = spans["agent.flag_state"]["logs"]
    assert events[0]["fields"][0] == {
        "key": "event",
        "value": "feature_flag.evaluation",
    }


@pytest.mark.uc("UC-035", flow="A3")
def test_no_traces_is_empty_and_other_errors_raise():
    with _answer(404, _fixture("jaeger-v3-no-traces.json")):
        assert jaeger_v3.find_traces("http://h/jaeger/api", "agent", "x") == []
        assert jaeger_v3.get_trace("http://h/jaeger/api", TRACE_ID) == {}
    with (
        _answer(500, {"error": {"httpCode": 500, "message": "badger: closed"}}),
        pytest.raises(urllib.error.HTTPError),
    ):
        jaeger_v3.find_traces("http://h/jaeger/api", "agent", "x")


@pytest.mark.uc("UC-035", flow="main")
def test_services_reads_the_v3_list():
    with _answer(200, _fixture("jaeger-v3-services.json")):
        names = jaeger_v3.services("http://h/jaeger/api")
    assert "agent" in names and "game-coordinator-service" in names
