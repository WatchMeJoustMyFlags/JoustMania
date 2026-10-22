"""debug flag: flag only, default off (#1788, FR-150, FR-151)."""

from unittest.mock import create_autospec, patch

from openfeature.client import OpenFeatureClient

from psmove_pairing.config import is_debug


def _client(value):
    client = create_autospec(OpenFeatureClient, instance=True)
    client.get_boolean_value.side_effect = lambda _key, default, _ctx: default if value is None else value
    return client


def test_flag_on_wins(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    with patch("psmove_pairing.config._observability_client", _client(True)):
        assert is_debug() is True


def test_flag_off_means_off(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    with patch("psmove_pairing.config._observability_client", _client(False)):
        assert is_debug() is False


def test_removed_debug_env_is_ignored(monkeypatch):
    """DEBUG=1 no longer turns debug on (#1907): no client or an unresolved flag is off."""
    monkeypatch.setenv("DEBUG", "1")
    with patch("psmove_pairing.config._observability_client", None):
        assert is_debug() is False
    with patch("psmove_pairing.config._observability_client", _client(None)):
        assert is_debug() is False


def test_default_off(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    with patch("psmove_pairing.config._observability_client", None):
        assert is_debug() is False


def test_runtime_change_takes_effect(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    client = _client(False)
    with patch("psmove_pairing.config._observability_client", client):
        assert is_debug() is False
        client.get_boolean_value.side_effect = lambda *_a: True
        assert is_debug() is True
