"""Static telemetry inventory for the producer/consumer contract test (#1357).

Parses the repository WITHOUT importing any service code, so the contract test
runs in a bare ``uv run --with pytest --with pyyaml`` environment in seconds.

Producers
    * Python: ``lib.otel_metrics`` ``Counter`` / ``Gauge`` / ``Histogram``
      definitions (AST) plus their RECORDING call sites (``.inc`` / ``.set`` /
      ``.dec`` / ``.observe`` / ``.time``, directly or through ``.labels(...)``).
      A definition without a recording call site is a dead metric (#1334/#1335).
    * Go (agent, connect-proxy): ``meter.Int64Counter("name")`` style calls and
      the semconv GenAI instruments.
    * Rust (rust-hid): ``.f64_observable_counter("name")`` style calls.
    * Span names, span event names and span attribute keys: string literals in
      Python/Rust producer source (definition and use are the same thing for a
      span, so a literal in non-test source is the evidence).

Consumers
    * Go agent ingest (docs/specs/telemetry.md §4.4): the metric-name and label
      constants of ``gamecontext/extract_metrics.go``, the span attribute/event
      constants of ``gamecontext/extract_spans.go`` and ``infracontext/extract_spans.go``.
    * Grafana dashboards (``services/grafana/dashboards/*.json``) and Prometheus
      alerts (``services/prometheus/alerts.yml``): metric names in PromQL.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

_SKIP_DIR_PARTS = {
    "tests",
    ".venv",
    "node_modules",
    "__pycache__",
    "target",
    "generated",
    "build",
    "dist",
}
_PY_METRIC_CLASSES = {"Counter", "Gauge", "Histogram"}
_RECORDING_METHODS = {"inc", "dec", "set", "observe", "time"}


# --------------------------------------------------------------------------- #
# Source discovery
# --------------------------------------------------------------------------- #
def _source_files(root: Path, suffix: str) -> list[Path]:
    """Non-test production source files under ``root`` with ``suffix``."""
    out = []
    for path in root.rglob(f"*{suffix}"):
        rel_parts = path.relative_to(REPO_ROOT).parts
        if _SKIP_DIR_PARTS.intersection(rel_parts):
            continue
        name = path.name
        if (
            name.startswith("test_")
            or name.endswith("_test.go")
            or name == "conftest.py"
        ):
            continue
        out.append(path)
    return sorted(out)


def python_sources() -> list[Path]:
    return _source_files(REPO_ROOT / "lib", ".py") + _source_files(
        REPO_ROOT / "services", ".py"
    )


def _service_root(path: Path) -> Path:
    """``services/<svc>`` for a service file, ``lib`` for a lib file."""
    rel = path.relative_to(REPO_ROOT).parts
    if rel[0] == "services":
        return REPO_ROOT / rel[0] / rel[1]
    return REPO_ROOT / rel[0]


# --------------------------------------------------------------------------- #
# Python metric producers
# --------------------------------------------------------------------------- #
@dataclass
class PyMetric:
    name: str
    kind: str  # Counter | Gauge | Histogram
    var: str  # the module-level variable it is bound to
    path: Path
    labels: tuple[str, ...] = ()
    recorded_at: list[str] = field(
        default_factory=list
    )  # "file:line" of recording (or escaping) uses


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _python_metric_definitions() -> list[PyMetric]:
    defs: list[PyMetric] = []
    for path in python_sources():
        if path.name == "otel_metrics.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            kind = _call_name(call.func)
            if kind not in _PY_METRIC_CLASSES or not call.args:
                continue
            first = call.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            target = node.targets[0]
            var = target.id if isinstance(target, ast.Name) else _call_name(target)
            if var:
                defs.append(PyMetric(first.value, kind, var, path, _label_names(call)))
    return defs


def _label_names(call: ast.Call) -> tuple[str, ...]:
    """The label-name list of a ``Counter/Gauge/Histogram(name, doc, [labels])`` call."""
    node = call.args[2] if len(call.args) > 2 else None
    for kw in call.keywords:
        if kw.arg in {"labelnames", "labels", "label_names"}:
            node = kw.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return tuple(
            e.value
            for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        )
    return ()


def _receiver_name(expr: ast.expr) -> str | None:
    """Terminal name of a metric receiver, unwrapping ``.labels(...)``.

    ``metrics.foo.labels(a=1).inc()`` -> ``foo``; ``foo.set(1)`` -> ``foo``.
    """
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "labels"
    ):
        expr = expr.func.value
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Name):
        return expr.id
    return None


def _metric_ref_name(expr: ast.expr) -> str | None:
    """Terminal name of a bare metric reference or its ``.labels(...)`` child."""
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "labels"
    ):
        expr = expr.func.value
    if isinstance(expr, (ast.Attribute, ast.Name)):
        return _receiver_name(expr)
    return None


_NON_RECORDING_CALLS = {
    "remove",
    "clear",
    "labels",
    "isinstance",
    "getattr",
    "hasattr",
    "id",
    "repr",
    "str",
}


def _escapes(node: ast.expr, parent: ast.AST) -> bool:
    """True when a metric (or its labelled child) is handed on to be recorded elsewhere.

    Covers the two indirect patterns this codebase uses: passing the metric to a
    recording helper (``start_system_metrics_collector(cpu_counter, ...)``) and
    caching labelled children for a hot loop (``self._gauges[s] = {"x": m.labels(serial=s)}``).
    """
    if isinstance(parent, ast.Call) and node is not parent.func:
        callee = _call_name(parent.func)
        return callee not in _NON_RECORDING_CALLS
    if isinstance(parent, ast.keyword):
        return True
    if isinstance(parent, ast.Dict):
        return any(v is node for v in parent.values)
    if isinstance(parent, (ast.Assign, ast.AnnAssign)):
        return parent.value is node
    return isinstance(parent, (ast.Return, ast.List, ast.Tuple))


def _recording_calls(paths: list[Path]) -> dict[str, list[str]]:
    """Map metric variable name -> recording call sites ("relpath:line").

    A site is a recording method call on the metric (directly or through
    ``.labels(...)``), or a place where the metric escapes to code that records
    it (see :func:`_escapes`). Bare ``.labels(...)`` statements, ``.remove()``
    and ``.clear()`` do not count: they never put a sample on the wire.
    """
    sites: dict[str, list[str]] = {}
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(REPO_ROOT)
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in _RECORDING_METHODS:
                    name = _receiver_name(node.func.value)
            is_load = not isinstance(
                getattr(node, "ctx", ast.Load()), (ast.Store, ast.Del)
            )
            if (
                name is None
                and is_load
                and isinstance(node, (ast.Attribute, ast.Name, ast.Call))
            ):
                parent = parents.get(node)
                if parent is not None and _escapes(node, parent):
                    name = _metric_ref_name(node)
            if name:
                sites.setdefault(name, []).append(f"{rel}:{node.lineno}")
    return sites


def python_metrics() -> dict[str, PyMetric]:
    """All Python metric definitions keyed by metric name, with recording sites.

    A recording site counts when it is in the defining service (or anywhere, for
    a metric defined in ``lib/``) and calls a recording method on the variable
    the metric is bound to.
    """
    defs = _python_metric_definitions()
    all_py = python_sources()
    sites_by_root: dict[Path, dict[str, list[str]]] = {}
    for root in {_service_root(d.path) for d in defs}:
        if root == REPO_ROOT / "lib":
            scope = all_py
        else:
            scope = [p for p in all_py if root in p.parents]
        sites_by_root[root] = _recording_calls(scope)
    out: dict[str, PyMetric] = {}
    for d in defs:
        d.recorded_at = sites_by_root[_service_root(d.path)].get(d.var, [])
        prev = out.get(d.name)
        if prev is None:
            out[d.name] = d
        else:  # same name defined by several services (process_*): merge the evidence
            prev.recorded_at += d.recorded_at
            prev.labels = tuple(dict.fromkeys(prev.labels + d.labels))
    return out


def recorded_python_metrics() -> set[str]:
    return {n for n, m in python_metrics().items() if m.recorded_at}


# --------------------------------------------------------------------------- #
# Go / Rust metric producers (definition evidence)
# --------------------------------------------------------------------------- #
_GO_INSTRUMENT_RE = re.compile(
    r"\.(?:Int64|Float64)(?:Counter|UpDownCounter|Histogram|Gauge|ObservableGauge|ObservableCounter|ObservableUpDownCounter)\(\s*\"([^\"]+)\"",
)
_RUST_INSTRUMENT_RE = re.compile(
    r"\.(?:u64|i64|f64)_(?:counter|up_down_counter|histogram|gauge|observable_gauge|observable_counter|observable_up_down_counter)\(\s*\"([^\"]+)\"",
)
# semconv GenAI client instruments created via go.opentelemetry.io/otel/semconv genaiconv.
_GO_SEMCONV_INSTRUMENTS = {
    "NewClientOperationDuration": "gen_ai.client.operation.duration",
    "NewClientTokenUsage": "gen_ai.client.token.usage",
}


def go_metrics() -> set[str]:
    names: set[str] = set()
    for path in _source_files(REPO_ROOT / "services", ".go"):
        text = path.read_text()
        names.update(_GO_INSTRUMENT_RE.findall(text))
        for ctor, name in _GO_SEMCONV_INSTRUMENTS.items():
            if f"genaiconv.{ctor}(" in text:
                names.add(name)
    return names


def rust_metrics() -> set[str]:
    names: set[str] = set()
    for path in _source_files(REPO_ROOT / "services" / "rust-hid", ".rs"):
        names.update(_RUST_INSTRUMENT_RE.findall(path.read_text()))
    return names


def prometheus_name(otel_name: str) -> str:
    """OTLP -> Prometheus name normalisation (dots become underscores)."""
    return otel_name.replace(".", "_")


def produced_metric_names() -> set[str]:
    """Every metric name with producer evidence, in Prometheus spelling.

    Python names require a recording call site; Go and Rust names require a
    definition (their instruments are created where they are recorded).
    """
    names = recorded_python_metrics() | go_metrics() | rust_metrics()
    return {prometheus_name(n) for n in names}


# --------------------------------------------------------------------------- #
# String-literal evidence for span names / events / attribute keys
# --------------------------------------------------------------------------- #
def producer_string_literals() -> set[str]:
    """All string literals (and f-string constant parts, not docstrings) in Python and Rust producers."""
    literals: set[str] = set()
    for path in python_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        docstrings = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                literals.add(node.value)
    for path in _source_files(REPO_ROOT / "services" / "rust-hid", ".rs"):
        literals.update(re.findall(r"\"([^\"\\]*)\"", path.read_text()))
    return literals


# --------------------------------------------------------------------------- #
# Go agent ingest (consumer)
# --------------------------------------------------------------------------- #
AGENT_DIR = REPO_ROOT / "services" / "agent"
_GO_CONST_RE = re.compile(r"^\s*(\w+)\s*=\s*\"([^\"]*)\"", re.MULTILINE)


def _go_consts(path: Path) -> dict[str, str]:
    return dict(_GO_CONST_RE.findall(path.read_text()))


@dataclass
class AgentIngest:
    metric_names: dict[str, str]  # const -> metric name
    metric_labels: dict[str, str]  # const -> label key
    span_attrs: dict[str, str]  # const -> span attribute key (game path)
    span_events: dict[str, str]  # const -> span event name
    infra_span_names: dict[str, str]
    infra_span_attrs: dict[str, str]


def agent_ingest() -> AgentIngest:
    metrics_consts = _go_consts(AGENT_DIR / "gamecontext" / "extract_metrics.go")
    spans_consts = _go_consts(AGENT_DIR / "gamecontext" / "extract_spans.go")
    infra_consts = _go_consts(AGENT_DIR / "infracontext" / "extract_spans.go")
    return AgentIngest(
        metric_names={
            k: v for k, v in metrics_consts.items() if k.startswith("metric")
        },
        metric_labels={k: v for k, v in metrics_consts.items() if k.startswith("attr")},
        span_attrs={k: v for k, v in spans_consts.items() if k.startswith("spanAttr")},
        span_events={
            k: v for k, v in spans_consts.items() if k.startswith("spanEvent")
        },
        infra_span_names={
            k: v for k, v in infra_consts.items() if k.startswith("Span")
        },
        infra_span_attrs={
            k: v for k, v in infra_consts.items() if k.startswith(("Attr", "Event"))
        },
    )


# --------------------------------------------------------------------------- #
# Dashboards and alerts (consumers)
# --------------------------------------------------------------------------- #
DASHBOARD_DIR = REPO_ROOT / "services" / "grafana" / "dashboards"
ALERTS_FILE = REPO_ROOT / "services" / "prometheus" / "alerts.yml"

_PROMQL_FUNCS_AND_KEYWORDS = {
    # aggregations / keywords
    "sum", "min", "max", "avg", "count", "stddev", "stdvar", "topk", "bottomk", "quantile",
    "count_values", "group", "by", "without", "on", "ignoring", "group_left", "group_right",
    "bool", "and", "or", "unless", "offset", "inf", "nan", "atan2", "limitk", "limit_ratio",
    # Grafana template functions
    "label_values", "query_result", "metrics", "label_names",
}  # fmt: skip

_STRING_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|`[^`]*`")
_MATCHER_RE = re.compile(r"\{[^{}]*\}")
_RANGE_RE = re.compile(r"\[[^\]]*\]")
_GROUPING_RE = re.compile(
    r"\b(by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)"
)
_GRAFANA_VAR_RE = re.compile(r"\$\{[^}]*\}|\$\w+")
_IDENT_RE = re.compile(r"(?<![\w.:])([a-zA-Z_:][a-zA-Z0-9_:]*)(?![\w(])(?!\s*\()")
_NUMBER_SUFFIX_RE = re.compile(r"\b\d+(\.\d+)?(e[+-]?\d+)?[smhdwy]?\b")


def promql_metric_names(expr: str) -> set[str]:
    """Metric names referenced by a PromQL (or Grafana templated PromQL) expression.

    A deliberately simple static scan: strip strings, label matchers, ranges,
    grouping clauses and Grafana variables, then take identifiers that are not
    function calls or keywords. Good enough for the expressions in this repo and
    biased toward false positives (which the test surfaces) over silent misses.
    """
    e = _STRING_RE.sub(" ", expr)
    e = _GRAFANA_VAR_RE.sub(" ", e)
    # label_values(metric, label): the second arg is a label, not a metric
    e = re.sub(r"label_values\(\s*([^,()]*?)\s*,\s*\w+\s*\)", r"(\1)", e)
    e = re.sub(r"label_values\(\s*\w+\s*\)", " ", e)
    prev = None
    while prev != e:  # nested matchers are not a thing, but grouping can repeat
        prev = e
        e = _MATCHER_RE.sub(" ", e)
    e = _RANGE_RE.sub(" ", e)
    e = _GROUPING_RE.sub(" ", e)
    e = _NUMBER_SUFFIX_RE.sub(" ", e)
    names = set()
    for m in _IDENT_RE.finditer(e):
        ident = m.group(1)
        if ident.lower() in _PROMQL_FUNCS_AND_KEYWORDS:
            continue
        names.add(ident)
    return names


def _walk_json(node, out: list[tuple[str, str]], where: str) -> None:
    if isinstance(node, dict):
        ds = node.get("datasource")
        ds_type = ds.get("type") if isinstance(ds, dict) else ds
        if isinstance(ds_type, str) and ds_type.lower() in {
            "loki",
            "jaeger",
            "tempo",
            "-- grafana --",
            "grafana",
        }:
            return
        title = node.get("title") or where
        for key in ("expr", "query", "definition"):
            val = node.get(key)
            if isinstance(val, str) and val.strip():
                out.append((title, val))
            elif isinstance(val, dict) and isinstance(val.get("query"), str):
                out.append((title, val["query"]))
        for v in node.values():
            _walk_json(v, out, title)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, out, where)


def dashboard_queries() -> list[tuple[str, str, str]]:
    """(dashboard file, panel/variable title, PromQL) for every dashboard query."""
    out = []
    for path in sorted(DASHBOARD_DIR.glob("*.json")):
        found: list[tuple[str, str]] = []
        _walk_json(json.loads(path.read_text()), found, path.stem)
        out.extend((path.name, title, q) for title, q in found)
    return out


def alert_rules() -> list[dict]:
    doc = yaml.safe_load(ALERTS_FILE.read_text())
    return [rule for group in doc["groups"] for rule in group["rules"]]


_SELECTOR_RE = re.compile(r"(?<![\w.:])([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\{([^{}]*)\}")
_MATCHER_LABEL_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=~|!~|!=|=)")


def promql_selector_labels(expr: str) -> list[tuple[str, str]]:
    """(metric, label) pairs for every label matcher in a ``metric{...}`` selector."""
    e = _GRAFANA_VAR_RE.sub("VAR", expr)
    pairs = []
    for metric, body in _SELECTOR_RE.findall(e):
        body = _STRING_RE.sub('""', body)
        pairs.extend((metric, label) for label in _MATCHER_LABEL_RE.findall(body))
    return pairs
