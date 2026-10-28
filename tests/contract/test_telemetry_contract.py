"""Telemetry producer/consumer contract (#1357, docs/specs/telemetry.md §4.4 and §6).

Every telemetry name a consumer depends on must have a live producer:

(a) the Go agent ingest names (``gamecontext/extract_metrics.go``,
    ``gamecontext/extract_spans.go``, ``infracontext/extract_spans.go``) are
    produced by the Python/Rust services; for metrics that means a definition
    AND a recording call site (#1334 / #1335 were defined but never recorded);
(b) every metric (and Python label matcher) in the Grafana dashboards and the
    dashboard SPA's VictoriaMetrics queries has a producer. (Prometheus alerting
    was removed in #1425: nothing received the alerts; operators watch Grafana
    and the dashboard.)

Strength of the evidence, per producer kind:
* Python metrics: a definition AND a recording use resolved through the using
  file's imports (``metrics.var.set(...)``, ``from .metrics import var``,
  ``self.metrics.var``, or a helper in the defining module that another file
  reaches). A same-named parameter or local never counts, and ``.set(0)``
  (a reset) never counts. See ``telemetry_inventory`` and the scanner fixtures
  in ``test_scanner_*`` below.
* Go (agent, connect-proxy) and Rust (rust-hid) metrics: definition only. Their
  instruments are created where they are recorded, but a Go/Rust metric that is
  created and never recorded would still pass. Label matchers are checked for
  Python metrics only.
* Span names, events and attribute keys: the string literal exists in non-test
  Python/Rust producer code (docstrings excluded). That proves the name is
  spelled the same somewhere, not that it is set on the span the agent reads.

Known gaps are listed below, keyed by the issue that tracks them. The lists may
only shrink: an entry whose gap is fixed fails ``test_known_gaps_are_still_gaps``
until it is removed, so the PR that fixes a gap also deletes its entry here.
"""

from __future__ import annotations

import pytest
import telemetry_inventory as ti

# --------------------------------------------------------------------------- #
# Known gaps (issue-keyed). Remove an entry in the PR that fixes it.
# --------------------------------------------------------------------------- #

# (a) Agent-ingested metric names with no recording producer.
AGENT_METRIC_KNOWN_GAPS: dict[str, str] = {}  # empty since #1376 / #1379

# (b) Metrics referenced by dashboards that no code records.
CONSUMED_METRIC_KNOWN_GAPS: dict[
    str, str
] = {}  # #1275 entries went with the alerts (#1425)

# (b) (metric, label) matchers on a Python metric that does not carry that label.
CONSUMED_LABEL_KNOWN_GAPS: dict[tuple[str, str], str] = {}  # empty since #1353

# Python metric definitions that nothing records (no consumer depends on them).
DEAD_DEFINITION_KNOWN_GAPS: dict[str, str] = {}  # empty since #1322 item 7

# Metrics produced outside this repo's code (not gaps). Matched by exact name or prefix.
EXTERNAL_METRICS = {
    "up": "collector prometheus receiver scrape synthetic",
    "scrape_": "collector prometheus receiver scrape synthetic",
    "go_": "Go runtime of VictoriaMetrics / flagd self-metrics",
    "vm_": "VictoriaMetrics self-metrics",
    "system_": "OTel collector hostmetrics receiver (services/otel-collector/config.yaml)",
    "traces_span_metrics_": "OTel collector spanmetrics connector over agent.decide spans "
    "(services/otel-collector/config.yaml; test_collector_spanmetrics.py)",
    "feature_flag_evaluation_": "standard OpenFeature OTel metrics hook: go-sdk-contrib "
    "hooks/open-telemetry in the agent (flags/otelhooks.go), the same names from "
    "lib/openfeature_otel_hooks.py in Python (test_openfeature_hook_metrics_are_the_ones_consumed)",
}

# Labels added by the pipeline (resource attributes, scrape config, histogram buckets).
PIPELINE_LABELS = {
    "job", "instance", "le", "quantile", "service", "service_name", "service_namespace",
    "collector", "pipeline", "cluster", "environment", "exported_job",
}  # fmt: skip


def _is_external(name: str) -> bool:
    return any(
        name == k or (k.endswith("_") and name.startswith(k)) for k in EXTERNAL_METRICS
    )


def _base_name(name: str, known: set[str]) -> str:
    """Strip the Prometheus histogram series suffixes when the base is a known metric."""
    for suffix in ("_bucket", "_sum", "_count"):
        if name.endswith(suffix) and name[: -len(suffix)] in known:
            return name[: -len(suffix)]
    return name


@pytest.fixture(scope="module")
def python_metrics():
    return ti.python_metrics()


@pytest.fixture(scope="module")
def produced(python_metrics):
    return ti.produced_metric_names()


@pytest.fixture(scope="module")
def defined(python_metrics):
    return {ti.prometheus_name(n) for n in python_metrics} | ti.produced_metric_names()


@pytest.fixture(scope="module")
def consumer_queries():
    """(where, PromQL) for every Grafana dashboard query and dashboard SPA query."""
    queries = [(f"{f} / {title}", q) for f, title, q in ti.dashboard_queries()]
    queries += [(f"dashboard SPA {f} / {name}", q) for f, name, q in ti.spa_queries()]
    return queries


# --------------------------------------------------------------------------- #
# Parser sanity: a broken parser must not turn the contract into a no-op.
# --------------------------------------------------------------------------- #
def test_inventory_parsers_find_the_known_surface(python_metrics, consumer_queries):
    assert len(python_metrics) >= 140, (
        "Python metric definition scan found too few metrics"
    )
    ingest = ti.agent_ingest()
    assert len(ingest.metric_names) >= 20, (
        "extract_metrics.go constant scan found too few metric names"
    )
    assert ingest.span_events, "extract_spans.go event constants not found"
    assert ingest.infra_span_names, "infracontext span-name constants not found"
    assert len(ti.spa_queries()) >= 2, (
        "dashboard SPA *_QUERY scan found too few queries"
    )
    # Parser sanity check derived from the files (#1427), so consolidating
    # dashboards never needs a hand-tuned number: every dashboard file yields a
    # query, and the JSON walk's "expr" hits equal a plain text scan's count of
    # non-empty "expr" fields in each file.
    raw = ti.raw_dashboard_expr_counts()
    assert raw, "no dashboard files found"
    parsed: dict[str, int] = dict.fromkeys(raw, 0)
    for dashboard, _, _ in ti.dashboard_queries():
        parsed[dashboard] += 1
    empty = sorted(name for name, n in parsed.items() if n == 0)
    assert not empty, f"dashboards that yield no query: {empty}"
    walked = ti.walked_dashboard_expr_counts()
    mismatch = {name: (walked[name], n) for name, n in raw.items() if walked[name] != n}
    assert not mismatch, (
        "dashboard query walk found a different number of 'expr' fields than the raw "
        f"text scan (walked, raw): {mismatch}; a Loki/Jaeger expr is skipped by the walk"
    )


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("controller_battery_level < 2", {"controller_battery_level"}),
        (
            'histogram_quantile(0.95, sum by (le, job) (rate(foo_seconds_bucket{job=~"$job"}[$__rate_interval])))',
            {"foo_seconds_bucket"},
        ),
        ("sum(rate(a_total[5m])) / ignoring(mode) group_left b", {"a_total", "b"}),
        ("label_values(controller_info, serial)", {"controller_info"}),
        ('count(up{job="x"} == 0) or vector(0)', {"up"}),
    ],
)
def test_promql_metric_name_scan(expr, expected):
    assert ti.promql_metric_names(expr) == expected


def test_promql_selector_label_scan():
    expr = 'rate(grpc_x_count{grpc_code!="OK", service=~"$svc"}[5m])'
    assert ti.promql_selector_labels(expr) == [
        ("grpc_x_count", "grpc_code"),
        ("grpc_x_count", "service"),
    ]


# --------------------------------------------------------------------------- #
# (a) Go agent ingest  <-  Python / Rust producers
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-041", flow="main", steps=[4])
def test_agent_ingested_metrics_are_recorded_by_a_producer(produced):
    """UC-041 step 4: every metric name the agent extracts is defined AND recorded."""
    ingest = ti.agent_ingest()
    missing = {
        f"{const}={name}"
        for const, name in ingest.metric_names.items()
        if name not in produced and name not in AGENT_METRIC_KNOWN_GAPS
    }
    assert not missing, (
        "Agent ingests metrics no producer records (definition without .inc/.set/.observe "
        f"is a dead metric, cf. #1334): {sorted(missing)}"
    )


@pytest.mark.uc("UC-041", flow="main", steps=[4, 5], br=["BR-004"])
def test_agent_ingested_labels_are_carried_by_an_ingested_metric(python_metrics):
    """UC-041 steps 4-5, BR-004: every label key the agent reads is on a metric it ingests."""
    ingest = ti.agent_ingest()
    ingested = set(ingest.metric_names.values())
    carried = {
        label
        for name, m in python_metrics.items()
        if name in ingested
        for label in m.labels
    }
    missing = sorted(
        f"{c}={label}"
        for c, label in ingest.metric_labels.items()
        if label not in carried
    )
    assert not missing, (
        f"Agent reads metric labels no ingested Python metric declares: {missing}"
    )


@pytest.mark.uc("UC-041", flow="main", steps=[4])
def test_agent_ingested_span_attributes_and_events_exist_in_producers():
    """UC-041 step 4: span attribute keys and the player_death event exist in producer code."""
    ingest = ti.agent_ingest()
    literals = ti.producer_string_literals()
    wanted = {**ingest.span_attrs, **ingest.span_events}
    missing = sorted(f"{c}={v}" for c, v in wanted.items() if v not in literals)
    assert not missing, (
        f"Agent span keys not found in any Python/Rust producer: {missing}"
    )


@pytest.mark.uc("UC-041", flow="A1")
def test_agent_infra_span_contract_exists_in_producers():
    """UC-041 A1: the controller.bluetooth_health span, its sample event and attributes are produced."""
    ingest = ti.agent_ingest()
    literals = ti.producer_string_literals()
    wanted = {**ingest.infra_span_names, **ingest.infra_span_attrs}
    missing = sorted(f"{c}={v}" for c, v in wanted.items() if v not in literals)
    assert not missing, (
        f"Agent infra span keys not found in any Python/Rust producer: {missing}"
    )


# --------------------------------------------------------------------------- #
# (b) Grafana dashboards and the dashboard SPA  <-  producers
# --------------------------------------------------------------------------- #
def _consumed_metrics(queries, defined: set[str]) -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for where, expr in queries:
        for name in ti.promql_metric_names(expr):
            used.setdefault(_base_name(name, defined), set()).add(where)
    return used


@pytest.mark.uc("UC-035", flow="main", steps=[3, 6])
def test_dashboard_metrics_have_a_recording_producer(
    consumer_queries, produced, defined
):
    """UC-035 steps 3, 6: every metric a Grafana panel or the dashboard SPA reads is recorded somewhere."""
    used = _consumed_metrics(consumer_queries, defined)
    missing = {
        name: sorted(where)[:3]
        for name, where in used.items()
        if name not in produced
        and not _is_external(name)
        and name not in CONSUMED_METRIC_KNOWN_GAPS
    }
    assert not missing, f"Dashboards reference metrics nothing records: {missing}"


@pytest.mark.uc("UC-035", flow="main", steps=[6])
def test_dashboard_label_matchers_exist_on_the_metric(
    consumer_queries, python_metrics, defined
):
    """UC-035 step 6: a label matcher on a Python metric names a label the metric carries."""
    labels = {
        ti.prometheus_name(n): set(m.labels) | PIPELINE_LABELS
        for n, m in python_metrics.items()
    }
    bad: dict[tuple[str, str], list[str]] = {}
    for where, expr in consumer_queries:
        for metric, label in ti.promql_selector_labels(expr):
            base = _base_name(metric, defined)
            if (
                base in labels
                and label not in labels[base]
                and (base, label) not in CONSUMED_LABEL_KNOWN_GAPS
            ):
                bad.setdefault((base, label), []).append(where)
    assert not bad, (
        f"Label matchers on labels the metric does not carry (match nothing or everything): {bad}"
    )


def test_python_metric_definitions_are_recorded(python_metrics):
    """A defined metric that nothing records is a silent gap for any future consumer (#1334)."""
    dead = sorted(
        n
        for n, m in python_metrics.items()
        if not m.recorded_at and n not in DEAD_DEFINITION_KNOWN_GAPS
    )
    assert not dead, (
        f"Metrics defined but never recorded (record them or delete the definition): {dead}"
    )


# docs/specs/telemetry.md §6.2 and its counts are generated from python_metrics and
# python_metric_consumers by scripts/gen_specs.py (`make specs`); `make specs-check`
# fails CI when docs/specs/generated/ is stale (#1280).


# --------------------------------------------------------------------------- #
# The known-gap lists only shrink.
# --------------------------------------------------------------------------- #
def test_known_gaps_are_still_gaps(python_metrics, produced, consumer_queries, defined):
    stale = []
    stale += [
        f"AGENT_METRIC_KNOWN_GAPS[{n}]"
        for n in AGENT_METRIC_KNOWN_GAPS
        if n in produced or n not in set(ti.agent_ingest().metric_names.values())
    ]
    used = _consumed_metrics(consumer_queries, defined)
    stale += [
        f"CONSUMED_METRIC_KNOWN_GAPS[{n}]"
        for n in CONSUMED_METRIC_KNOWN_GAPS
        if n in produced or n not in used
    ]
    still_matched = {
        (_base_name(m, defined), label)
        for _, e in consumer_queries
        for m, label in ti.promql_selector_labels(e)
    }
    for key in CONSUMED_LABEL_KNOWN_GAPS:
        metric, label = key
        carried = label in (
            python_metrics[metric].labels if metric in python_metrics else ()
        )
        if carried or key not in still_matched:
            stale.append(f"CONSUMED_LABEL_KNOWN_GAPS[{key}]")
    stale += [
        f"DEAD_DEFINITION_KNOWN_GAPS[{n}]"
        for n in DEAD_DEFINITION_KNOWN_GAPS
        if n not in python_metrics or python_metrics[n].recorded_at
    ]
    assert not stale, f"Fixed gaps still allow-listed; delete these entries: {stale}"


# --------------------------------------------------------------------------- #
# Scanner regression fixtures (review of #1392): a dead metric must stay dead.
# --------------------------------------------------------------------------- #
_FIXTURE_METRICS = """\
from lib.otel_metrics import Gauge

current_game_mode = Gauge("svc_current_mode", "mode", ["mode"])
music_tempo = Gauge("svc_music_tempo", "tempo")
active_players = Gauge("svc_active_players", "players")


def reset_game_metrics():
    music_tempo.set(0)
"""

_FIXTURE_GAME = """\
from .. import metrics


def _probe(logger, current_game_mode):
    logger.info("mode %s", current_game_mode)


def count(active_players):
    return len(active_players)


def end_game():
    metrics.reset_game_metrics()
"""


def _fixture_tree(tmp_path, game_extra: str = ""):
    (tmp_path / "lib").mkdir()
    svc = tmp_path / "services" / "svc"
    (svc / "games").mkdir(parents=True)
    (svc / "metrics.py").write_text(_FIXTURE_METRICS)
    (svc / "games" / "mode.py").write_text(_FIXTURE_GAME + game_extra)
    return tmp_path


def test_scanner_same_named_parameter_or_local_is_not_a_recording(tmp_path):
    """A parameter named like a metric (``_probe(logger, current_game_mode)``,
    ``len(active_players)``) is not the metric."""
    pm = ti.python_metrics(_fixture_tree(tmp_path))
    assert pm["svc_current_mode"].recorded_at == []
    assert pm["svc_active_players"].recorded_at == []


def test_scanner_reset_in_the_defining_module_is_not_a_recording(tmp_path):
    """Only a ``.set(0)`` reset inside metrics.py (reached via reset_game_metrics) is left."""
    pm = ti.python_metrics(_fixture_tree(tmp_path))
    assert pm["svc_music_tempo"].recorded_at == []


def test_scanner_counts_resolved_recordings(tmp_path):
    """The real forms do count: module alias, imported name, injected self.metrics, reached helper."""
    extra = """
from ..metrics import active_players as players_gauge


class Game:
    def __init__(self, metrics_module):
        self.metrics = metrics_module

    def tick(self, speed, n):
        metrics.music_tempo.set(speed)
        players_gauge.set(n)
        self.metrics.current_game_mode.labels(mode="ffa").set(1)
"""
    pm = ti.python_metrics(_fixture_tree(tmp_path, extra))
    assert pm["svc_music_tempo"].recorded_at
    assert pm["svc_active_players"].recorded_at
    assert pm["svc_current_mode"].recorded_at


def test_scanner_remove_loop_and_logging_are_not_recordings(tmp_path):
    """M3: a metric in a ``for g in (...): g.remove(...)`` cleanup tuple; M4: a metric
    passed to ``logger.debug``. Neither puts a sample on the wire."""
    extra = """
from contextlib import suppress


def retire(labels, logger):
    for gauge in (metrics.current_game_mode, metrics.active_players):
        with suppress(KeyError):
            gauge.remove(*labels)
    logger.debug("mode gauge %r", metrics.music_tempo)
"""
    pm = ti.python_metrics(_fixture_tree(tmp_path, extra))
    assert pm["svc_current_mode"].recorded_at == []
    assert pm["svc_active_players"].recorded_at == []
    assert pm["svc_music_tempo"].recorded_at == []


def test_scanner_loop_that_records_on_the_loop_variable_counts(tmp_path):
    extra = """

def publish(n):
    for gauge in (metrics.active_players,):
        gauge.set(n)
"""
    pm = ti.python_metrics(_fixture_tree(tmp_path, extra))
    assert pm["svc_active_players"].recorded_at


# --------------------------------------------------------------------------- #
# OpenFeature hook metrics (#1427): the dashboards read the names the hooks emit.
# --------------------------------------------------------------------------- #
def _hook_metric_names() -> set[str]:
    """The four metric-name constants of lib/openfeature_otel_hooks.py, in Prometheus spelling."""
    import ast

    tree = ast.parse((ti.REPO_ROOT / "lib" / "openfeature_otel_hooks.py").read_text())
    names = {
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.startswith("EVALUATION_")
        and isinstance(node.value, ast.Constant)
    }
    return {ti.prometheus_name(n) for n in names}


def test_openfeature_hook_metrics_are_the_ones_consumed(consumer_queries, defined):
    """Every feature_flag_evaluation_* name a dashboard reads is one the hooks emit.

    The Python hook mirrors go-sdk-contrib hooks/open-telemetry, whose names the agent's
    flags/otelhooks_test.go pins against the real Go hook.
    """
    hook = _hook_metric_names()
    assert hook == {
        "feature_flag_evaluation_active_count",
        "feature_flag_evaluation_requests_total",
        "feature_flag_evaluation_success_total",
        "feature_flag_evaluation_error_total",
    }
    consumed = {
        name
        for _, q in consumer_queries
        for name in ti.promql_metric_names(q)
        if name.startswith("feature_flag_evaluation_")
    }
    assert consumed, "no dashboard reads the OpenFeature evaluation metrics"
    assert consumed <= hook, sorted(consumed - hook)
