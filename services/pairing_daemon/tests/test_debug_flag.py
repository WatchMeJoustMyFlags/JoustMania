"""debug flag: flag wins, env fallback, default off (#1788, FR-150, FR-151)."""

from unittest.mock import MagicMock, patch

import pytest

from psmove_pairing.config import is_debug


def _client(value):
    client = MagicMock()
    client.get_boolean_value.side_effect = lambda _key, default, _ctx: default if value is None else value
    return client


@pytest.mark.uc("UC-035", flow="main")
def test_flag_on_wins_over_env_off(monkeypatch):
    monkeypatch.setenv("DEBUG", "0")
    with patch("psmove_pairing.config._observability_client", _client(True)):
        assert is_debug() is True


@pytest.mark.uc("UC-035", flow="main")
def test_flag_off_wins_over_env_on(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    with patch("psmove_pairing.config._observability_client", _client(False)):
        assert is_debug() is False


@pytest.mark.uc("UC-035", flow="main")
def test_env_used_when_flag_unresolved(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    with patch("psmove_pairing.config._observability_client", _client(None)):
        assert is_debug() is True
    with patch("psmove_pairing.config._observability_client", None):
        assert is_debug() is True


@pytest.mark.uc("UC-035", flow="main")
def test_default_off(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    with patch("psmove_pairing.config._observability_client", None):
        assert is_debug() is False


@pytest.mark.uc("UC-035", flow="main")
def test_runtime_change_takes_effect(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    client = _client(False)
    with patch("psmove_pairing.config._observability_client", client):
        assert is_debug() is False
        client.get_boolean_value.side_effect = lambda *_a: True
        assert is_debug() is True
