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
    * Grafana dashboards (``services/grafana/dashboards/<folder>/*.json``) and the dashboard
      SPA's VictoriaMetrics queries: metric names in PromQL.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path


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
def _source_files(base: Path, suffix: str, root: Path = REPO_ROOT) -> list[Path]:
    """Non-test production source files under ``base`` with ``suffix``."""
    out = []
    for path in base.rglob(f"*{suffix}"):
        if _SKIP_DIR_PARTS.intersection(path.relative_to(root).parts):
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


def python_sources(root: Path = REPO_ROOT) -> list[Path]:
    return _source_files(root / "lib", ".py", root) + _source_files(
        root / "services", ".py", root
    )


def _dotted(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _service_prefix(dotted: str) -> str:
    """``services.<svc>`` for a service module, ``lib`` for a lib module."""
    parts = dotted.split(".")
    return ".".join(parts[:2]) if parts[0] == "services" else parts[0]


# --------------------------------------------------------------------------- #
# Python metric producers
# --------------------------------------------------------------------------- #
@dataclass
class PyMetric:
    name: str
    kind: str  # Counter | Gauge | Histogram
    var: str  # the module-level variable it is bound to
    module: str  # dotted defining module, e.g. services.game_coordinator.metrics
    labels: tuple[str, ...] = ()
    recorded_at: list[str] = field(
        default_factory=list
    )  # "file:line" of recording uses


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _label_names(
    call: ast.Call, constants: dict[str, ast.expr] | None = None
) -> tuple[str, ...]:
    """The label-name list of a ``Counter/Gauge/Histogram(name, doc, [labels])`` call.

    The list may be a literal or a module-level constant (``GAME_STATE_LABELS``).
    """
    node = call.args[2] if len(call.args) > 2 else None
    for kw in call.keywords:
        if kw.arg in {"labelnames", "labels", "label_names"}:
            node = kw.value
    if isinstance(node, ast.Name) and constants and node.id in constants:
        node = constants[node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        return tuple(
            e.value
            for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        )
    return ()


def _python_metric_definitions(root: Path) -> list[PyMetric]:
    """Module-level ``var = Counter|Gauge|Histogram("name", ...)`` assignments."""
    defs: list[PyMetric] = []
    for path in python_sources(root):
        if path.name == "otel_metrics.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        constants = {
            n.targets[0].id: n.value
            for n in tree.body
            if isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Name)
            and isinstance(n.value, (ast.List, ast.Tuple))
        }
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if _call_name(call.func) not in _PY_METRIC_CLASSES or not call.args:
                continue
            first = call.args[0]
            target = node.targets[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and isinstance(target, ast.Name)
            ):
                defs.append(
                    PyMetric(
                        first.value,
                        _call_name(call.func),
                        target.id,
                        _dotted(path, root),
                        _label_names(call, constants),
                    )
                )
    return defs


# Receivers are resolved through the importing file's bindings, never by bare
# variable name: a parameter or local that happens to share a metric's name
# (``def f(current_game_mode): log(current_game_mode)``) is not the metric.
# Recognised forms, for a metric ``var`` defined in module ``M``:
#   * ``alias.var`` where ``alias`` is bound to ``M`` (``from . import metrics``,
#     ``import metrics``, ``from services.x import metrics as m``, ...);
#   * ``name`` imported from ``M`` (``from .metrics import var as name``);
#   * ``self.metrics.var`` / ``self._metrics.var``: the menu injects its service's
#     ``metrics`` module this way (MetricsAware handlers);
#   * a bare ``var`` inside ``M`` itself, but only within a top-level function or
#     class that another file reaches (``metrics.set_controller_info(...)``,
#     ``get_metrics_interceptors()`` -> the interceptor classes); module-level
#     statements and unreached helpers in ``M`` never count.
# ``.set(0)`` is a reset, not a recording, and never counts.
_INJECTED_MODULE_ATTRS = {"metrics", "_metrics"}


@dataclass
class _Bindings:
    modules: dict[str, str] = field(
        default_factory=dict
    )  # local alias -> defining module
    names: dict[str, tuple[str, str]] = field(
        default_factory=dict
    )  # local name -> (module, var)
    injected: str | None = None  # module behind self.metrics / self._metrics


def _resolve_module(candidate: str, defining: set[str], importer: str) -> str | None:
    matches = [d for d in defining if d == candidate or d.endswith("." + candidate)]
    if len(matches) > 1:  # e.g. ``import metrics``: the importer's own service wins
        matches = [
            d for d in matches if _service_prefix(d) == _service_prefix(importer)
        ]
    return matches[0] if len(matches) == 1 else None


def _bindings(tree: ast.Module, module: str, defining: set[str]) -> _Bindings:
    b = _Bindings()
    package = module.rsplit(".", 1)[0] if "." in module else ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve_module(alias.name, defining, module)
                if target and (alias.asname or "." not in alias.name):
                    b.modules[alias.asname or alias.name] = target
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(parts + ([node.module] if node.module else []))
            for alias in node.names:
                local = alias.asname or alias.name
                target = _resolve_module(
                    f"{base}.{alias.name}" if base else alias.name, defining, module
                )
                if target:
                    b.modules[local] = target
                elif base and (owner := _resolve_module(base, defining, module)):
                    b.names[local] = (owner, alias.name)
    own_metrics = _resolve_module("metrics", defining, module)
    if own_metrics and _service_prefix(own_metrics) == _service_prefix(module):
        b.injected = own_metrics
    return b


def _resolve_ref(
    expr: ast.expr, b: _Bindings, local_vars: dict[str, str] | None = None
) -> tuple[str, str] | None:
    """(module, var) of a metric reference, unwrapping ``.labels(...)``; None if unresolvable."""
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "labels"
    ):
        expr = expr.func.value
    if isinstance(expr, ast.Attribute):
        owner = expr.value
        if isinstance(owner, ast.Name) and owner.id in b.modules:
            return b.modules[owner.id], expr.attr
        if (
            b.injected
            and isinstance(owner, ast.Attribute)
            and owner.attr in _INJECTED_MODULE_ATTRS
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
        ):
            return b.injected, expr.attr
        return None
    if isinstance(expr, ast.Name):
        if expr.id in b.names:
            return b.names[expr.id]
        if local_vars and expr.id in local_vars:
            return local_vars[expr.id], expr.id
    return None


_NON_RECORDING_CALLS = {
    # metric API calls that put no sample on the wire
    "remove", "clear", "labels",
    # introspection / formatting
    "isinstance", "getattr", "hasattr", "id", "repr", "str", "len", "format", "print",
    # logging (logger.<level>(...), logging.log(...))
    "debug", "info", "warning", "warn", "error", "exception", "critical", "log",
}  # fmt: skip


def _records_on(body: list[ast.stmt], name: str) -> bool:
    """True when ``body`` calls a recording method (not a reset) on the loop variable ``name``."""
    for stmt in body:
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _RECORDING_METHODS
                and not _is_reset(node)
            ):
                receiver = node.func.value
                if isinstance(receiver, ast.Call) and isinstance(
                    receiver.func, ast.Attribute
                ):
                    receiver = receiver.func.value  # x.labels(...).inc()
                if isinstance(receiver, ast.Name) and receiver.id == name:
                    return True
    return False


def _escapes(node: ast.expr, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when a metric (or its labelled child) is handed on to be recorded elsewhere.

    Covers the indirect patterns this codebase uses: passing the metric to a
    recording helper (``start_system_metrics_collector(cpu_counter=metrics.x)``),
    caching labelled children for a hot loop (``self._gauges[s] = {"x": metrics.m.labels(serial=s)}``)
    and iterating a tuple of metrics whose loop body records on the loop variable.
    Logging, formatting and ``.remove()`` / ``.clear()`` are not recordings, and a
    ``for g in (metrics.a, ...)`` loop counts only if its body records on ``g``.
    """
    parent = parents.get(node)
    if parent is None:
        return False
    if isinstance(parent, ast.Call):
        return (
            node is not parent.func
            and _call_name(parent.func) not in _NON_RECORDING_CALLS
        )
    if isinstance(parent, ast.keyword):
        call = parents.get(parent)
        return (
            isinstance(call, ast.Call)
            and _call_name(call.func) not in _NON_RECORDING_CALLS
        )
    if isinstance(parent, ast.Dict):
        return any(v is node for v in parent.values)
    if isinstance(parent, (ast.Assign, ast.AnnAssign)):
        return parent.value is node
    if isinstance(parent, ast.Return):
        return True
    if isinstance(parent, (ast.List, ast.Tuple)):
        loop = parents.get(parent)
        if isinstance(loop, (ast.For, ast.AsyncFor)) and loop.iter is parent:
            return isinstance(loop.target, ast.Name) and _records_on(
                loop.body, loop.target.id
            )
        return _escapes(parent, parents)  # a collection escapes only as a whole
    return False


def _is_reset(call: ast.Call) -> bool:
    return (
        call.func.attr == "set"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == 0
    )


def _uses(
    tree: ast.AST, b: _Bindings, local_vars: dict[str, str] | None = None
) -> list[tuple[tuple[str, str], int]]:
    """((module, var), line) for every recording call or escape in ``tree``."""
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    out = []
    for node in ast.walk(tree):
        ref = None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _RECORDING_METHODS
        ):
            if not _is_reset(node):
                ref = _resolve_ref(node.func.value, b, local_vars)
        if ref is None and isinstance(node, (ast.Attribute, ast.Name, ast.Call)):
            if not isinstance(getattr(node, "ctx", ast.Load()), (ast.Store, ast.Del)):
                parent = parents.get(node)
                if parent is not None and _escapes(node, parents):
                    ref = _resolve_ref(node, b, local_vars)
        if ref:
            out.append((ref, node.lineno))
    return out


def recorded_python_metric_definitions(root: Path = REPO_ROOT) -> list[PyMetric]:
    """Every Python metric definition (same-named ones NOT merged), with its recording sites."""
    defs = _python_metric_definitions(root)
    defining = {d.module for d in defs}
    vars_by_module: dict[str, set[str]] = {}
    for d in defs:
        vars_by_module.setdefault(d.module, set()).add(d.var)

    sites: dict[tuple[str, str], list[str]] = {}
    helper_sites: dict[
        tuple[str, str], list[tuple[str, str]]
    ] = {}  # (module, def) -> [(var, where)]
    helper_refs: dict[
        tuple[str, str], set[str]
    ] = {}  # (module, def) -> same-module defs it references
    external_refs: set[tuple[str, str]] = (
        set()
    )  # (module, name) referenced from another file
    for path in python_sources(root):
        module = _dotted(path, root)
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(root)
        b = _bindings(tree, module, defining)
        for ref, line in _uses(tree, b):
            sites.setdefault(ref, []).append(f"{rel}:{line}")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Attribute, ast.Name)) and (
                ref := _resolve_ref(node, b)
            ):
                external_refs.add(ref)
        if module in vars_by_module:
            own = dict.fromkeys(vars_by_module[module], module)
            top = {
                n.name
                for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            }
            for fn in tree.body:
                if fn.__class__.__name__ in {
                    "FunctionDef",
                    "AsyncFunctionDef",
                    "ClassDef",
                }:
                    key = (module, fn.name)
                    helper_sites[key] = [
                        (ref[1], f"{rel}:{line}")
                        for ref, line in _uses(fn, _Bindings(), own)
                    ]
                    helper_refs[key] = {
                        n.id
                        for n in ast.walk(fn)
                        if isinstance(n, ast.Name) and n.id in top
                    } - {fn.name}
    # A helper (function or class) in a defining module records only if another
    # file reaches it, directly or through other helpers of the same module.
    reachable = {k for k in helper_sites if k in external_refs}
    frontier = list(reachable)
    while frontier:
        module, name = frontier.pop()
        for other in helper_refs.get((module, name), ()):
            if (module, other) not in reachable:
                reachable.add((module, other))
                frontier.append((module, other))
    for key in reachable:
        for var, where in helper_sites[key]:
            sites.setdefault((key[0], var), []).append(where)

    for d in defs:
        d.recorded_at = list(sites.get((d.module, d.var), []))
    return defs


def python_metrics(root: Path = REPO_ROOT) -> dict[str, PyMetric]:
    """All Python metric definitions keyed by metric name, with recording sites."""
    out: dict[str, PyMetric] = {}
    for d in recorded_python_metric_definitions(root):
        prev = out.get(d.name)
        if prev is None:
            out[d.name] = replace(d, recorded_at=list(d.recorded_at))
        else:  # same name defined by several services (process_*): merge the evidence
            prev.recorded_at += d.recorded_at
            prev.labels = tuple(dict.fromkeys(prev.labels + d.labels))
    return out


def recorded_python_metrics(root: Path = REPO_ROOT) -> set[str]:
    return {n for n, m in python_metrics(root).items() if m.recorded_at}


# --------------------------------------------------------------------------- #
# Go / Rust metric producers (definition evidence)
# --------------------------------------------------------------------------- #
_GO_INSTRUMENT_RE = re.compile(
    r"\.(?:Int64|Float64)(?:Counter|UpDownCounter|Histogram|Gauge|ObservableGauge|ObservableCounter|ObservableUpDownCounter)\(\s*\"([^\"]+)\"",
)
_RUST_INSTRUMENT_RE = re.compile(
    r"\.(?:u64|i64|f64)_(?:counter|up_down_counter|histogram|gauge|observable_gauge|observable_counter|observable_up_down_counter)\(\s*\"([^\"]+)\"",
)
# semconv GenAI client instruments created via go.opentelemetry.io/otel/semconv genaiconv:
# (OTel name, Prometheus name as VictoriaMetrics stores it). prometheus_remote_write
# appends the unit suffix: `s` becomes `_seconds`; the `{token}` annotation adds none.
_GO_SEMCONV_INSTRUMENTS = {
    "NewClientOperationDuration": (
        "gen_ai.client.operation.duration",
        "gen_ai_client_operation_duration_seconds",
    ),
    "NewClientTokenUsage": ("gen_ai.client.token.usage", "gen_ai_client_token_usage"),
}


def go_metrics() -> set[str]:
    names: set[str] = set()
    for path in _source_files(REPO_ROOT / "services", ".go"):
        text = path.read_text()
        names.update(_GO_INSTRUMENT_RE.findall(text))
        for ctor, (name, prom_name) in _GO_SEMCONV_INSTRUMENTS.items():
            if f"genaiconv.{ctor}(" in text:
                names.update((name, prom_name))
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
# Dashboards (consumers)
# --------------------------------------------------------------------------- #
DASHBOARD_DIR = REPO_ROOT / "services" / "grafana" / "dashboards"

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


def _walk_json(node, out: list[tuple[str, str, str]], where: str) -> None:
    """Append (title, query, key) for every query string; key is the JSON key that held it."""
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
                out.append((title, val, key))
            elif isinstance(val, dict) and isinstance(val.get("query"), str):
                out.append((title, val["query"], key))
        for v in node.values():
            _walk_json(v, out, title)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, out, where)


def dashboard_files() -> list[Path]:
    return sorted(DASHBOARD_DIR.rglob("*.json"))


def _dashboard_hits() -> list[tuple[str, str, str, str]]:
    """(dashboard file, panel/variable title, PromQL, JSON key) for every dashboard query."""
    out = []
    for path in dashboard_files():
        found: list[tuple[str, str, str]] = []
        _walk_json(json.loads(path.read_text()), found, path.stem)
        out.extend((path.name, title, q, key) for title, q, key in found)
    return out


def dashboard_queries() -> list[tuple[str, str, str]]:
    """(dashboard file, panel/variable title, PromQL) for every dashboard query."""
    return [(f, title, q) for f, title, q, _ in _dashboard_hits()]


def walked_dashboard_expr_counts() -> dict[str, int]:
    """Queries the JSON walk found under an ``expr`` key, per dashboard file."""
    counts = dict.fromkeys((p.name for p in dashboard_files()), 0)
    for f, _, _, key in _dashboard_hits():
        if key == "expr":
            counts[f] += 1
    return counts


_RAW_EXPR_RE = re.compile(r'"expr"\s*:\s*"(?:[^"\\]|\\.)+"')


def raw_dashboard_expr_counts() -> dict[str, int]:
    """Non-empty ``"expr"`` fields per dashboard file, by a plain text scan.

    Independent of the JSON walk in ``dashboard_queries``, so the contract test can
    check the walk against it: the walk's ``expr`` hits must equal this count per
    file (variable ``query`` / ``definition`` strings are extra). A Loki or Jaeger
    panel's ``expr`` is counted here but skipped by the walk.
    """
    return {p.name: len(_RAW_EXPR_RE.findall(p.read_text())) for p in dashboard_files()}


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


SPA_DIR = REPO_ROOT / "services" / "dashboard" / "src"
_SPA_QUERY_RE = re.compile(r"const\s+(\w*QUERY\w*)\s*=\s*(['\"`])(.*?)\2", re.S)


def spa_queries() -> list[tuple[str, str, str]]:
    """(file, const name, PromQL) for the dashboard SPA's VictoriaMetrics queries.

    Every ``const *_QUERY = '...'`` in ``services/dashboard/src/*.ts`` except the
    Loki ones (``*LOG*``, LogQL). Template ``${...}`` parts are Grafana-style
    variables to the PromQL scan.
    """
    out = []
    for path in sorted(SPA_DIR.glob("*.ts")):
        for name, _, body in _SPA_QUERY_RE.findall(path.read_text()):
            if "LOG" not in name:
                out.append((path.name, name, body))
    return out


# --------------------------------------------------------------------------- #
# Consumers of the Python metrics: the generated §6.2 catalog (#1456, #1280)
# --------------------------------------------------------------------------- #
# The §6.2 Consumers vocabulary: which consumer scan each cell token stands for.
CONSUMER_KINDS = ("agent", "dashboard", "grafana", "scripts", "tools")


def _literal_metric_uses(base: Path, names: set[str]) -> set[str]:
    """Names that appear as a whole word in the non-test ``.py`` / ``.sh`` files under ``base``."""
    text = "\n".join(
        p.read_text(errors="ignore")
        for suffix in (".py", ".sh")
        for p in _source_files(base, suffix)
    )
    words = set(re.findall(r"[A-Za-z_:][A-Za-z0-9_:]*", text))
    return names & words


def python_metric_consumers(names: set[str]) -> dict[str, set[str]]:
    """The consumer kinds (``CONSUMER_KINDS``) that name each Python metric, from the code.

    grafana / dashboard: the PromQL metric scan of the Grafana dashboards / the SPA
    queries (histogram ``_bucket`` / ``_sum`` / ``_count`` series count for their
    base metric); agent: the Go agent ingest constants; tools / scripts: the name as
    a literal word in non-test Python or shell source under ``tools/`` / ``scripts/``
    (the demo harness and the talk / calibration scripts build their queries from it).
    """

    def base(name: str) -> str:
        for suffix in ("_bucket", "_sum", "_count"):
            if name.endswith(suffix) and name[: -len(suffix)] in names:
                return name[: -len(suffix)]
        return name

    def promql(queries) -> set[str]:
        return {base(n) for q in queries for n in promql_metric_names(q)} & names

    scans = {
        "agent": set(agent_ingest().metric_names.values()) & names,
        "dashboard": promql(q for _, _, q in spa_queries()),
        "grafana": promql(q for _, _, q in dashboard_queries()),
        "scripts": _literal_metric_uses(REPO_ROOT / "scripts", names),
        "tools": _literal_metric_uses(REPO_ROOT / "tools", names),
    }
    return {n: {kind for kind, found in scans.items() if n in found} for n in names}
