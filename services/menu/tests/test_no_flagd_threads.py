"""#2232: menu unit tests must not start a real flagd provider (see #2226).

``MenuServicer.__init__`` calls ``init_flag_domain`` for four domains through the name
imported into ``services.menu.servicer``. Tests that only patched
``lib.feature_flags.init_flag_domain`` (``test_grpc_integration``) left that reference
real, so each domain installed an in-process FlagdProvider retrying ``flagd:8015``.
"""

import threading

from openfeature import api

from services.menu import servicer as menu_servicer


def test_servicer_flag_domain_init_installs_no_flagd_provider():
    for domain in ("game", "user", "system", "agent"):
        menu_servicer.init_flag_domain(domain)
        assert api.get_provider_metadata(domain=domain).name != "FlagdProvider"
    assert [t.name for t in threading.enumerate() if t.name.startswith("Flagd")] == []
