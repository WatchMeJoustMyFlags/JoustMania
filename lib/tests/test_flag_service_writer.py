"""FlagServiceWriter: the menu's admin writes through the flag service (UC-053 A2, FR-018)."""

import urllib.error

import pytest

from lib.flag_service_writer import FlagServiceWriter, match_variant, variant_value


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
def test_refusals_and_outage_fail_closed(svc, caplog):
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.update_default_variant("sensitivity", "warp") is False
    assert w.update_default_variant("nope", "x") is False
    assert w.get_current_variant("nope") is None
    assert w.get_variant_names("nope") == []
    svc.down = True
    assert w.update_default_variant("sensitivity", "fast") is False
    assert w.cycle_variant("sensitivity") is None
    assert "NOT applied" in caplog.text


@pytest.mark.uc("UC-053", flow="A2")
def test_unpersisted_write_is_logged(svc, caplog):
    svc.persist_ok = False
    w = FlagServiceWriter("game", "http://fs:8070", transport=svc)
    assert w.update_default_variant("sensitivity", "slow") is True
    assert "NOT persisted" in caplog.text


def test_base_url_from_env(monkeypatch):
    monkeypatch.setenv("FLAG_SERVICE_URL", "http://elsewhere:9/")
    assert FlagServiceWriter("user").base_url == "http://elsewhere:9"
    monkeypatch.delenv("FLAG_SERVICE_URL")
    assert FlagServiceWriter("user").base_url == "http://flag-service:8070"
