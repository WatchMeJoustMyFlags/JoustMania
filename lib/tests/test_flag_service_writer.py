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


# --- step_variant: numeric variants step by value (#2105, UC-015 BR-006) ----------------------


def _real_master_volume() -> tuple[dict, str]:
    """`user/audio_volume.master` exactly as services/flagd/user.json declares it."""
    import json
    from pathlib import Path

    user = json.loads((Path(__file__).resolve().parents[2] / "services/flagd/user.json").read_text())
    flag = user["flags"]["audio_volume.master"]
    return flag["variants"], flag["defaultVariant"]


class _NameOrderedService(FakeService):
    """A flag service that lists the variant names alphabetically (as a map without order does)."""

    def __call__(self, method, url, body):
        status, resp = super().__call__(method, url, body)
        if method == "GET" and status == 200:
            resp["variants"] = sorted(resp["variants"])
        return status, resp


def _walk(writer, flag: str, forward: bool) -> list[str]:
    """Step one detent at a time until the end of the range; the variants written, in order."""
    seen: list[str] = []
    for _ in range(10):
        step = writer.step_variant(flag, forward)
        if step.error == STEP_AT_LIMIT:
            return seen
        seen.append(step.new)
    raise AssertionError(f"no limit reached: {seen}")


@pytest.mark.uc("UC-015", br=["BR-006", "BR-005"])
@pytest.mark.parametrize("service_cls", [FakeService, _NameOrderedService])
def test_master_volume_steps_by_value_in_both_directions_and_stops_at_the_ends(service_cls):
    variants, _default = _real_master_volume()
    svc = service_cls({"audio_volume.master": (variants, "mute")})
    w = FlagServiceWriter("user", "http://fs:8070", transport=svc)

    assert _walk(w, "audio_volume.master", True) == ["quiet", "normal", "loud", "max"]
    assert w.step_variant("audio_volume.master", True) == VariantStep("max", "max", STEP_AT_LIMIT)

    assert _walk(w, "audio_volume.master", False) == ["loud", "normal", "quiet", "mute"]
    assert w.step_variant("audio_volume.master", False) == VariantStep("mute", "mute", STEP_AT_LIMIT)
    assert [variants[name] for name in _puts(svc)] == [0.3, 0.6, 0.8, 1.0, 0.8, 0.6, 0.3, 0.0]


@pytest.mark.uc("UC-015", br=["BR-006"])
def test_master_volume_multi_step_counts_by_value_under_a_name_ordered_listing():
    variants, default = _real_master_volume()
    svc = _NameOrderedService({"audio_volume.master": (variants, default)})
    w = FlagServiceWriter("user", "http://fs:8070", transport=svc)
    assert w.step_variant("audio_volume.master", False) == VariantStep("max", "loud")
    assert w.step_variant("audio_volume.master", False, steps=2) == VariantStep("loud", "quiet")


@pytest.mark.uc("UC-015", br=["BR-004"])
def test_non_numeric_variants_keep_the_declared_order():
    svc = _NameOrderedService({"interventions_allowed": ALLOWED})
    w = FlagServiceWriter("agent", "http://fs:8070", transport=svc)
    # Listed alphabetically (ambient, full, none, standard): no value sort, the listing is the ladder.
    assert w.step_variant("interventions_allowed", True) == VariantStep("ambient", "full")


# --- A PUT that times out client-side (#2095) ---------------------------------------------


class _SlowPutService(FakeService):
    """A flag service whose PUT answers after the client deadline.

    ``applies`` says whether the write landed server-side anyway; ``reread_down`` makes the
    GET after the timed-out PUT fail too; ``as_url_error`` wraps the timeout as urllib does on connect.
    """

    def __init__(self, *, applies: bool, reread_down: bool = False, as_url_error: bool = False):
        super().__init__({"interventions_allowed": ALLOWED})
        self.applies = applies
        self.reread_down = reread_down
        self.as_url_error = as_url_error
        self._timed_out = False

    def __call__(self, method, url, body):
        if method == "PUT":
            self.requests.append((method, url, body))
            if self.applies:
                self.overlay["interventions_allowed"] = body["variant"]
            self._timed_out = True
            timeout = TimeoutError("timed out")
            raise urllib.error.URLError(timeout) if self.as_url_error else timeout
        if self._timed_out and self.reread_down:
            self.requests.append((method, url, body))
            raise TimeoutError("timed out")
        return super().__call__(method, url, body)


@pytest.mark.uc("UC-013", flow="A3", br=["BR-003"])
@pytest.mark.uc("UC-053", flow="A8")
@pytest.mark.parametrize("as_url_error", [False, True])
def test_timed_out_put_that_applied_counts_as_applied_after_a_reread(caplog, as_url_error):
    """UC-013 A3: a timeout is an unknown outcome; the re-read shows the write landed, so it is reported applied."""
    svc = _SlowPutService(applies=True, as_url_error=as_url_error)
    writer = FlagServiceWriter("agent", "http://fs:8070", transport=svc)
    with caplog.at_level(logging.INFO, logger="lib.flag_service_writer"):
        assert writer.update_default_variant("interventions_allowed", "none") is True
    assert [m for m, _u, _b in svc.requests] == ["PUT", "GET"]
    assert not any("NOT applied" in r.getMessage() for r in caplog.records)
    assert any("timed out" in r.getMessage() and "applied" in r.getMessage() for r in caplog.records)


@pytest.mark.uc("UC-013", flow="A3")
@pytest.mark.uc("UC-053", flow="A8")
def test_timed_out_put_that_did_not_apply_is_reported_not_applied(caplog):
    """UC-013 A3: the re-read serves another level, so the write is reported as not applied (critical)."""
    svc = _SlowPutService(applies=False)
    writer = FlagServiceWriter("agent", "http://fs:8070", transport=svc)
    with caplog.at_level(logging.CRITICAL, logger="lib.flag_service_writer"):
        assert writer.update_default_variant("interventions_allowed", "none") is False
    assert svc.overlay == {}
    assert any(r.levelname == "CRITICAL" and "NOT applied" in r.getMessage() for r in caplog.records)


@pytest.mark.uc("UC-013", flow="A3")
@pytest.mark.uc("UC-053", flow="A8")
def test_timed_out_put_with_a_failed_reread_is_reported_unknown_not_as_applied(caplog):
    """UC-013 A3: neither answer arrived: report a failure, and say the outcome is unknown, never "NOT applied"."""
    svc = _SlowPutService(applies=True, reread_down=True)
    writer = FlagServiceWriter("agent", "http://fs:8070", transport=svc)
    with caplog.at_level(logging.CRITICAL, logger="lib.flag_service_writer"):
        assert writer.update_default_variant("interventions_allowed", "none") is False
    assert [m for m, _u, _b in svc.requests] == ["PUT", "GET"]
    messages = [r.getMessage() for r in caplog.records if r.levelname == "CRITICAL"]
    assert any("UNKNOWN" in m for m in messages), messages
    assert not any("NOT applied" in m for m in messages), messages


@pytest.mark.uc("UC-053", flow="A8")
def test_connection_refused_is_not_reread(svc):
    """A refused connection is a certain failure: no re-read, nothing retried."""
    svc.down = True
    writer = FlagServiceWriter("agent", "http://fs:8070", transport=svc)
    assert writer.update_default_variant("interventions_allowed", "none") is False
    assert [m for m, _u, _b in svc.requests] == ["PUT"]


# --- clear_persisted (UC-015 BR-018, #2215) ---------------------------------------------------


@pytest.mark.uc("UC-015", br=["BR-018"], steps=[9])
@pytest.mark.parametrize(("status", "expected"), [(204, True), (200, True), (503, False), (400, False)])
def test_clear_persisted_is_one_delete_with_persist(status, expected):
    calls = []

    def transport(method, url, body):
        calls.append((method, url, body))
        return status, {}

    w = FlagServiceWriter("game", "http://fs:8070", transport=transport)
    assert w.clear_persisted("shield.activation") is expected
    assert calls == [("DELETE", "http://fs:8070/game/shield.activation?persist=true", None)]


@pytest.mark.uc("UC-015", br=["BR-018"], flow="A3", steps=[9])
@pytest.mark.parametrize(("admin_after", "expected"), [(None, True), ({"variant": "hold"}, False)])
def test_clear_persisted_without_a_clear_answer_is_settled_by_a_re_read(admin_after, expected):
    def transport(method, _url, _body):
        if method == "DELETE":
            raise TimeoutError("timed out")
        view = {"variants": ["instant"], "effective_default": "instant"}
        if admin_after is not None:
            view["admin_value"] = admin_after
        return 200, view

    w = FlagServiceWriter("game", "http://fs:8070", transport=transport)
    assert w.clear_persisted("shield.activation") is expected


@pytest.mark.uc("UC-015", br=["BR-018"], flow="A3", steps=[9])
def test_clear_persisted_with_the_flag_service_down_fails():
    def transport(_method, _url, _body):
        raise urllib.error.URLError("refused")

    w = FlagServiceWriter("game", "http://fs:8070", transport=transport)
    assert w.clear_persisted("shield.activation") is False
