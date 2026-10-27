"""#2226: unit tests must not start a real flagd provider.

Building ``AudioServiceServicer`` reads ``menu_voice`` through ``init_flag_domain("user")``.
Without a guard that installed a real in-process ``FlagdProvider`` whose gRPC sync worker
kept retrying ``flagd:8015`` for the whole session, next to the ``grpc.aio`` servers of
other tests, and the CI run died with a segfault in a gRPC channel thread.
"""

import threading

from openfeature import api


def _flagd_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("Flagd")]


def test_servicer_construction_starts_no_flagd_provider(mock_audio_servicer):
    assert mock_audio_servicer.menu_voice == "gen_female"
    assert api.get_provider_metadata(domain="user").name != "FlagdProvider"
    assert _flagd_threads() == []
