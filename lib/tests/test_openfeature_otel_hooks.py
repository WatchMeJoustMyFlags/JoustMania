"""OpenFeature evaluation metrics with the standard OTel hook names (#1427, telemetry.md §6)."""

from __future__ import annotations

import pytest
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from openfeature.flag_evaluation import FlagEvaluationDetails, FlagType
from openfeature.hook import HookContext
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from lib.openfeature_otel_hooks import (
    EVALUATION_ACTIVE,
    EVALUATION_ERRORS,
    EVALUATION_REQUESTS,
    EVALUATION_SUCCESS,
    MetricsHook,
    metric_variant,
)

DOMAIN = "otel-hooks-test"


@pytest.mark.uc("UC-035", flow="main", steps=[6])
@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("jev", "jev"),
        ("default", "default"),
        ("r7f3a1b2c_17", "scoped_value"),
        ("o7f3a1b2c_3", "overlay_value"),
        ("r_17", "r_17"),
        ("route_1", "route_1"),
        ("o7f3a1b2c_x", "o7f3a1b2c_x"),
        ("", ""),
        (None, None),
    ],
)
def test_metric_variant_bounds_generated_names(variant, expected):
    assert metric_variant(variant) == expected


@pytest.fixture
def evaluated():
    """Evaluate three flags (two found, one missing) through a client carrying the hook."""
    reader = InMemoryMetricReader()
    hook = MetricsHook(MeterProvider(metric_readers=[reader]))
    api.set_provider(
        InMemoryProvider(
            {
                "sensitivity": InMemoryFlag("medium", {"medium": 2, "high": 3}),
                "shield_seconds": InMemoryFlag("r7f3a1b2c_17", {"none": 0.0, "r7f3a1b2c_17": 5.0}),
            }
        ),
        domain=DOMAIN,
    )
    client = api.get_client(DOMAIN)
    client.add_hooks([hook])
    ctx = EvaluationContext(targeting_key="MOCK0001", attributes={"gameId": "game_abc123def456"})
    results = (
        client.get_integer_value("sensitivity", 0, ctx),
        client.get_float_value("shield_seconds", 0.0, ctx),
        client.get_string_value("no.such.flag", "fallback", ctx),
    )
    points = {}
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                points[metric.name] = [(dict(p.attributes), p.value) for p in metric.data.data_points]
    yield results, points
    api.clear_providers()


@pytest.mark.uc("UC-035", flow="main", steps=[6])
def test_hook_does_not_change_the_result(evaluated):
    results, _ = evaluated
    assert results == (2, 5.0, "fallback")


@pytest.mark.uc("UC-035", flow="main", steps=[6])
def test_hook_records_the_go_hook_metric_names(evaluated):
    _, points = evaluated
    assert set(points) == {EVALUATION_ACTIVE, EVALUATION_REQUESTS, EVALUATION_SUCCESS, EVALUATION_ERRORS}
    requests = {a["feature_flag.key"]: v for a, v in points[EVALUATION_REQUESTS]}
    assert requests == {"sensitivity": 1, "shield_seconds": 1, "no.such.flag": 1}
    assert [(a["feature_flag.key"], v) for a, v in points[EVALUATION_ERRORS]] == [("no.such.flag", 1)]
    # Every evaluation that started has finished.
    assert all(v == 0 for _, v in points[EVALUATION_ACTIVE])


@pytest.mark.uc("UC-035", flow="main", steps=[6])
def test_success_labels_are_bounded(evaluated):
    _, points = evaluated
    success = {a["feature_flag.key"]: a for a, _ in points[EVALUATION_SUCCESS]}
    assert success["sensitivity"]["feature_flag.result.variant"] == "medium"
    assert success["sensitivity"]["reason"] == "STATIC"
    # A generated flag-service variant becomes its class, not a new label value per write.
    assert success["shield_seconds"]["feature_flag.result.variant"] == "scoped_value"
    allowed = {"feature_flag.key", "feature_flag.provider.name", "feature_flag.result.variant", "reason"}
    for name in (EVALUATION_REQUESTS, EVALUATION_SUCCESS, EVALUATION_ERRORS, EVALUATION_ACTIVE):
        for attributes, _ in points[name]:
            assert set(attributes) <= allowed, (name, attributes)


@pytest.mark.uc("UC-035", flow="main", steps=[6])
def test_not_ready_provider_does_not_count_down_an_active_evaluation():
    """The SDK runs `finally` but not `before` for a not-ready provider: no -1 without a +1."""
    reader = InMemoryMetricReader()
    hook = MetricsHook(MeterProvider(metric_readers=[reader]))
    ctx = HookContext("k", FlagType.BOOLEAN, False, EvaluationContext())
    hook.error(ctx, RuntimeError("not ready"), {})
    hook.finally_after(ctx, FlagEvaluationDetails("k", False), {})
    names = {
        m.name: [p.value for p in m.data.data_points]
        for rm in reader.get_metrics_data().resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert names.get(EVALUATION_ACTIVE, []) in ([], [0])
    assert names[EVALUATION_ERRORS] == [1]
