"""Jaeger query API v3 reader for the integration tests (#2283).

Jaeger 2.21 removes the legacy ``/api/services`` and ``/api/traces`` search, so the
tests read Jaeger the way the dashboard does (``services/dashboard/src/jaeger.ts``):
``GET <base>/v3/traces`` with ``query.serviceName``, ``query.operationName``,
``query.attributes`` (a JSON object of string tags), ``query.searchDepth`` and the
required ``query.startTimeMin`` / ``query.startTimeMax`` (RFC 3339). The answer is
OTLP JSON (``result.resourceSpans[].scopeSpans[].spans[]``, every matching trace in
one list); ``legacy_traces`` groups it by ``traceId`` into the trace / span dicts the
tests read (``traceID``, ``spanID``, ``operationName``, ``references``, ``tags``,
``logs``, ``startTime`` / ``duration`` in µs, ``processID`` + the trace's
``processes``), the field names of the legacy API. A ``404`` with ``No traces
found`` is an empty answer.

Stdlib only: ``tests/contract/test_jaeger_v3_reader.py`` loads it by path.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

# Fixed window bounds: the whole store (the tests run on the Jaeger host, but a
# fixed window keeps the query free of any clock, like the dashboard's cold start).
COLD_START_TIME_MIN = "1970-01-01T00:00:00Z"
SEARCH_TIME_MAX = "2100-01-01T00:00:00Z"


def search_url(
    api_base: str,
    service: str,
    operation: str | None = None,
    attributes: dict[str, str] | None = None,
    limit: int = 20,
) -> str:
    """The v3 search URL under ``api_base`` (``.../jaeger/api``)."""
    params = {
        "query.serviceName": service,
        "query.searchDepth": str(limit),
        "query.startTimeMin": COLD_START_TIME_MIN,
        "query.startTimeMax": SEARCH_TIME_MAX,
    }
    if operation:
        params["query.operationName"] = operation
    if attributes:
        params["query.attributes"] = json.dumps(attributes)
    return f"{api_base}/v3/traces?{urllib.parse.urlencode(params)}"


def is_no_traces(status: int, body: object) -> bool:
    """v3's "nothing matched" answer: 404 with ``No traces found``."""
    error = body.get("error") if isinstance(body, dict) else None
    return (
        status == 404
        and isinstance(error, dict)
        and error.get("httpCode") == 404
        and "no traces found" in str(error.get("message", "")).lower()
    )


def get_json(url: str, timeout: float = 10) -> dict:
    """GET ``url`` as JSON; v3's 404 ``No traces found`` is ``{}``. Raises OSError / ValueError otherwise."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback test URL
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"null")
        except ValueError:
            raise exc from None
        if is_no_traces(exc.code, body):
            return {}
        raise


def _value(v: dict | None) -> object:
    """The plain value of an OTLP attribute (int64 arrives as a string)."""
    if not v:
        return ""
    if "stringValue" in v:
        return v["stringValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return v["doubleValue"]
    if "arrayValue" in v:
        return json.dumps([_value(x) for x in v["arrayValue"].get("values", [])])
    if "kvlistValue" in v:
        return json.dumps(
            {
                kv["key"]: _value(kv.get("value"))
                for kv in v["kvlistValue"].get("values", [])
            }
        )
    return v.get("bytesValue", "")


def _tags(attrs: list | None) -> list[dict]:
    return [{"key": a["key"], "value": _value(a.get("value"))} for a in attrs or []]


def _micros(nanos: str | int | None) -> int:
    return int(nanos or 0) // 1000


def legacy_traces(body: dict) -> list[dict]:
    """Group a v3 answer by trace id into legacy-shaped trace dicts (see module doc)."""
    traces: dict[str, dict] = {}
    for rs in (body.get("result") or {}).get("resourceSpans") or []:
        service = next(
            (
                _value(a.get("value"))
                for a in (rs.get("resource") or {}).get("attributes") or []
                if a.get("key") == "service.name"
            ),
            "",
        )
        for ss in rs.get("scopeSpans") or []:
            for s in ss.get("spans") or []:
                trace = traces.setdefault(
                    s["traceId"],
                    {"traceID": s["traceId"], "spans": [], "processes": {}},
                )
                process_id = f"p{service}"
                trace["processes"][process_id] = {"serviceName": service}
                references = []
                if s.get("parentSpanId"):
                    references.append(
                        {
                            "refType": "CHILD_OF",
                            "traceID": s["traceId"],
                            "spanID": s["parentSpanId"],
                        }
                    )
                references += [
                    {
                        "refType": "FOLLOWS_FROM",
                        "traceID": link.get("traceId", ""),
                        "spanID": link["spanId"],
                    }
                    for link in s.get("links") or []
                    if link.get("spanId")
                ]
                start = _micros(s.get("startTimeUnixNano"))
                trace["spans"].append(
                    {
                        "traceID": s["traceId"],
                        "spanID": s["spanId"],
                        "operationName": s["name"],
                        "references": references,
                        "startTime": start,
                        "duration": max(0, _micros(s.get("endTimeUnixNano")) - start),
                        "tags": _tags(s.get("attributes")),
                        "logs": [
                            {
                                "timestamp": _micros(e.get("timeUnixNano")),
                                "fields": [{"key": "event", "value": e.get("name", "")}]
                                + _tags(e.get("attributes")),
                            }
                            for e in s.get("events") or []
                        ],
                        "processID": process_id,
                    }
                )
    return list(traces.values())


def find_traces(
    api_base: str,
    service: str,
    operation: str | None = None,
    attributes: dict[str, str] | None = None,
    limit: int = 20,
    timeout: float = 10,
) -> list[dict]:
    """Search Jaeger (v3); legacy-shaped traces, ``[]`` when nothing matched."""
    return legacy_traces(
        get_json(search_url(api_base, service, operation, attributes, limit), timeout)
    )


def get_trace(api_base: str, trace_id: str, timeout: float = 10) -> dict:
    """One trace by id (v3), legacy-shaped; ``{}`` when Jaeger does not hold it."""
    traces = legacy_traces(get_json(f"{api_base}/v3/traces/{trace_id}", timeout))
    return traces[0] if traces else {}


def services(api_base: str, timeout: float = 10) -> list[str]:
    """The service names Jaeger holds (v3 ``/services``)."""
    return list(get_json(f"{api_base}/v3/services", timeout).get("services") or [])
