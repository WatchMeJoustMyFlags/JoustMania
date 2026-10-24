"""FlagServiceWriter: the menu's admin writes through the flag service (UC-053 A2, FR-018)."""

import logging
import urllib.error

import pytest

from lib.flag_service_writer import (
    STEP_AT_LIMIT,
    STEP_UNAVAILABLE,
    FlagServiceWriter,
    VariantStep,
    match_variant,
    variant_value,
)


class FakeService:
    """Minimal stand-in for the flag service: one flag set, persisted overlays."""

    def __init__(self, flags: dict[str, tuple[dict, str]]):
        # flag -> (variants in declared order, baseline default)
        self.flags = flags
        self.overlay: dict[str, str] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.down = False
        self.persist_ok = True

    def __call__(self, method, url, body):
        self.requests.append((method, url, body))
        if self.down:
            raise urllib.error.URLError("connection refused")
        flag = url.rsplit("/", 1)[-1]
        if flag not in self.flags:
            return 404, {"error": "unknown_flag", "detail": flag}
        variants, default = self.flags[flag]
        if method == "GET":
            return 200, {
                "flag": flag,
                "variants": list(variants),
                "variant_values": variants,
                "effective_default": self.overlay.get(flag, default),
            }
        if method == "PUT":
            if body["variant"] not in variants:
                return 422, {"error": "out_of_bounds", "detail": "not declared"}
            self.overlay[flag] = body["variant"]
            return 200, {"variant": body["variant"], "persisted": bool(body.get("persist")) and self.persist_ok}
        return 405, {}


SENSITIVITY = ({"ultra_slow": 0, "slow": 1, "medium": 2, "fast": 3, "ultra_fast": 4}, "medium")
ALLOWED = ({"none": "", "ambient": "a", "standard": "a,b", "full": "a,b,c"}, "ambient")


@pytest.fixture
def svc():
    return FakeService({"sensitivity": SENSITIVITY, "interventions_allowed": ALLOWED})


@pytest.mark.uc("UC-053", flow="A2", br=["BR-004"])
def test_update_is_a_persisted_put_with_the_menu_label(svc):
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.update_default_variant("sensitivity", "fast") is True
    method, url, body = svc.requests[-1]
    assert (method, url) == ("PUT", "http://fs:8070/game/sensitivity")
    assert body == {"variant": "fast", "writer": "menu", "persist": True}
    assert w.get_current_variant("sensitivity") == "fast"


@pytest.mark.uc("UC-053", flow="A2")
def test_cycle_follows_the_declared_order_and_wraps(svc):
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.cycle_variant("sensitivity") == "fast"
    assert w.cycle_variant("sensitivity") == "ultra_fast"
    assert w.cycle_variant("sensitivity") == "ultra_slow"
    assert w.cycle_variant("sensitivity", forward=False) == "ultra_fast"


@pytest.mark.uc("UC-053", flow="A2")
def test_reads(svc):
    w = FlagServiceWriter("agent", "http://fs:8070", transport=svc)
    assert w.get_variant_names("interventions_allowed") == ["none", "ambient", "standard", "full"]
    assert w.get_variant_value("interventions_allowed", "standard") == "a,b"
    assert w.match_variant_for_value("interventions_allowed", "a,b,c") == "full"
    assert w.match_variant_for_value("interventions_allowed", "zzz") is None


@pytest.mark.uc("UC-053", flow="A2")
def test_one_view_answers_every_read(svc):
    w = FlagServiceWriter("agent", "http://fs:8070", transport=svc)
    view = w.get_flag_view("interventions_allowed")
    assert len(svc.requests) == 1
    assert view["effective_default"] == "ambient"
    assert variant_value(view, "standard") == "a,b"
    assert match_variant(view, "a,b,c") == "full"
    assert variant_value(None, "standard") is None
    assert match_variant(None, "a") is None
    assert w.get_flag_view("nope") is None


@pytest.mark.uc("UC-053", flow="A5")
@pytest.mark.uc("UC-053", flow="A8")
def test_refusals_and_outage_fail_closed(svc, caplog):
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.update_default_variant("sensitivity", "warp") is False
    assert w.update_default_variant("nope", "x") is False
    assert w.get_current_variant("nope") is None
    assert w.get_variant_names("nope") == []
    svc.down = True
    sent = len(svc.requests)
    assert w.update_default_variant("sensitivity", "fast") is False
    # A8: the failed write is sent once and not retried.
    assert [m for m, _, _ in svc.requests[sent:]] == ["PUT"]
    assert w.cycle_variant("sensitivity") is None
    assert "NOT applied" in caplog.text


@pytest.mark.uc("UC-013", flow="A3")
@pytest.mark.uc("UC-013", cond=["Post-F-1"])
def test_unreachable_flag_service_leaves_the_agent_policy_unchanged_and_logs_critical(svc, caplog):
    """UC-013 Post-F-1: the PUT fails, the stored value is untouched, the failure is CRITICAL."""
    w = FlagServiceWriter("agent", "http://fs:8070", transport=svc)
    svc.down = True

    with caplog.at_level(logging.WARNING, logger="lib.flag_service_writer"):
        assert w.update_default_variant("interventions_allowed", "none") is False

    assert svc.requests[-1][0] == "PUT"
    assert svc.overlay == {}  # nothing was stored
    critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(critical) == 1
    assert "interventions_allowed=none NOT applied" in critical[0].getMessage()
    svc.down = False
    assert w.get_current_variant("interventions_allowed") == "ambient"  # baseline default


@pytest.mark.uc("UC-053", flow="A2")
@pytest.mark.uc("UC-013", flow="A4")
def test_unpersisted_write_is_logged(svc, caplog):
    svc.persist_ok = False
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.update_default_variant("sensitivity", "slow") is True
    assert "NOT persisted" in caplog.text


@pytest.mark.uc("UC-012", flow="A5")
@pytest.mark.uc("UC-013", flow="A3")
def test_admin_write_failures_log_error_and_critical_on_outage(svc, caplog):
    """A refused write logs an ERROR, an unreachable flag service a CRITICAL; an applied but
    unpersisted write logs an ERROR and still counts as applied."""
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)

    caplog.clear()
    assert w.update_default_variant("sensitivity", "warp") is False
    assert [(r.levelname, "refused" in r.getMessage()) for r in caplog.records] == [("ERROR", True)]

    caplog.clear()
    svc.persist_ok = False
    assert w.update_default_variant("sensitivity", "slow") is True
    assert [r.levelname for r in caplog.records if "NOT persisted" in r.getMessage()] == ["ERROR"]

    caplog.clear()
    svc.down = True
    assert w.update_default_variant("sensitivity", "fast") is False
    assert [(r.levelname, "NOT applied" in r.getMessage()) for r in caplog.records] == [("CRITICAL", True)]


@pytest.mark.uc("UC-012", cond=["Post-F-1"])
def test_refused_or_unreachable_write_leaves_the_setting_unchanged(svc):
    """Failure postcondition: after a refused write or an outage the flag service still serves the old value."""
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.get_current_variant("sensitivity") == "medium"

    assert w.update_default_variant("sensitivity", "warp") is False
    assert w.get_current_variant("sensitivity") == "medium"

    svc.down = True
    assert w.cycle_variant("sensitivity") is None
    svc.down = False
    assert w.get_current_variant("sensitivity") == "medium"
    assert svc.overlay == {}


def test_base_url_from_env(monkeypatch):
    monkeypatch.setenv("FLAG_SERVICE_URL", "http://elsewhere:9/")
    assert FlagServiceWriter("user").base_url == "http://elsewhere:9"
    monkeypatch.delenv("FLAG_SERVICE_URL")
    assert FlagServiceWriter("user").base_url == "http://flag-service:8070"


# --- step_variant: the clamped step of the operator pad (#2050, UC-015 A7, BR-005) -----------


def _puts(svc):
    return [body["variant"] for method, _url, body in svc.requests if method == "PUT"]


@pytest.mark.uc("UC-015", flow="A7", br=["BR-005"])
def test_step_variant_moves_n_steps_with_one_write(svc):
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.step_variant("sensitivity", True, steps=2) == VariantStep("medium", "ultra_fast")
    assert _puts(svc) == ["ultra_fast"]


@pytest.mark.uc("UC-015", flow="A7", br=["BR-005"])
def test_step_variant_clamps_a_step_past_the_end_to_the_end(svc):
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.step_variant("sensitivity", False, steps=9) == VariantStep("medium", "ultra_slow")


@pytest.mark.uc("UC-015", flow="A7")
@pytest.mark.parametrize(("start", "forward"), [("ultra_fast", True), ("ultra_slow", False)])
def test_step_variant_at_the_end_writes_nothing_and_reports_at_limit(svc, start, forward):
    svc.overlay["sensitivity"] = start
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.step_variant("sensitivity", forward) == VariantStep(start, start, STEP_AT_LIMIT)
    assert _puts(svc) == []


def test_step_variant_with_wrap_goes_round_like_cycle_variant(svc):
    svc.overlay["sensitivity"] = "ultra_fast"
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.step_variant("sensitivity", True, wrap=True) == VariantStep("ultra_fast", "ultra_slow")


@pytest.mark.uc("UC-015", flow="A7")
def test_step_variant_follows_the_given_order_instead_of_the_declared_one():
    svc = FakeService({"force_all_start": ({"on": True, "off": False}, "off")})
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.step_variant("force_all_start", True, order=["off", "on"]) == VariantStep("off", "on")
    assert w.step_variant("force_all_start", True, order=["off", "on"]).error == STEP_AT_LIMIT


@pytest.mark.uc("UC-015", flow="A3", br=["BR-010"])
def test_step_variant_fails_closed_when_the_flag_service_is_down(svc):
    svc.down = True
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.step_variant("sensitivity", True) == VariantStep(None, None, STEP_UNAVAILABLE)
    assert len(svc.requests) == 1  # one read, no retry


@pytest.mark.uc("UC-015", flow="A3", br=["BR-010"])
def test_step_variant_reports_a_refused_write_as_unavailable(svc):
    def refuse_puts(method, url, body):
        if method == "PUT":
            return 503, {"error": "no_sync_stream"}
        return svc(method, url, body)

    w = FlagServiceWriter("game", "http://fs:8070", transport=refuse_puts)
    assert w.step_variant("sensitivity", True) == VariantStep("medium", None, STEP_UNAVAILABLE)
