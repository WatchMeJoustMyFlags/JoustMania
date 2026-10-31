"""#2232: lib unit tests must not start a real flagd provider (see #2226)."""

import threading

from openfeature import api

from lib.otel_metrics import _get_export_interval_ms


def test_export_interval_lookup_installs_no_flagd_provider():
    assert _get_export_interval_ms("probe") == 1000
    assert api.get_provider_metadata(domain="observability").name != "FlagdProvider"
    assert [t.name for t in threading.enumerate() if t.name.startswith("Flagd")] == []
