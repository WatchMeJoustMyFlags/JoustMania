import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import lib.feature_flags as ff_module
from lib.feature_flags import get_flag_client, init_flag_domain


@patch("lib.feature_flags.FlagdProvider")
@patch("lib.feature_flags.api")
def test_init_flag_domain(mock_api, mock_provider):
    """Test that init_flag_domain creates a provider with the correct selector."""
    ff_module._initialized_domains.discard("test_domain")
    ff_module._hooks_registered = False

    init_flag_domain("test_domain")

    from openfeature.contrib.provider.flagd.config import ResolverType

    mock_provider.assert_called_once()
    _args, kwargs = mock_provider.call_args
    assert kwargs["port"] == 8015
    assert kwargs["resolver_type"] == ResolverType.IN_PROCESS
    assert kwargs["selector"] == "flagSetId=test_domain"

    mock_api.set_provider.assert_called_once()
    _args, kwargs = mock_api.set_provider.call_args
    assert kwargs["domain"] == "test_domain"

    # Verify TracingHook was registered
    mock_api.add_hooks.assert_called_once()

    # Cleanup
    ff_module._initialized_domains.discard("test_domain")
    ff_module._hooks_registered = False


@patch("lib.feature_flags.FlagdProvider")
@patch("lib.feature_flags.api")
def test_init_flag_domain_idempotent(_mock_api, mock_provider):
    """Test that init_flag_domain only initializes once per domain."""
    ff_module._initialized_domains.discard("test_domain")
    ff_module._hooks_registered = False

    init_flag_domain("test_domain")
    init_flag_domain("test_domain")

    # Provider should only be created once
    mock_provider.assert_called_once()

    # Cleanup
    ff_module._initialized_domains.discard("test_domain")
    ff_module._hooks_registered = False


@patch("lib.feature_flags.FlagdProvider")
@patch("lib.feature_flags.api")
def test_tracing_hook_registered_once(mock_api, _mock_provider):
    """Test that TracingHook is registered only once across multiple domains."""
    ff_module._initialized_domains.discard("domain_a")
    ff_module._initialized_domains.discard("domain_b")
    ff_module._hooks_registered = False

    init_flag_domain("domain_a")
    init_flag_domain("domain_b")

    # Hook should be registered exactly once, even though two domains were initialized
    mock_api.add_hooks.assert_called_once()

    # Cleanup
    ff_module._initialized_domains.discard("domain_a")
    ff_module._initialized_domains.discard("domain_b")
    ff_module._hooks_registered = False


@patch("lib.feature_flags.api")
def test_get_flag_client(mock_api):
    """Test that get_flag_client returns a domain-scoped client."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client

    client = get_flag_client("game_settings")

    mock_api.get_client.assert_called_with(domain="game_settings")
    assert client is mock_client
