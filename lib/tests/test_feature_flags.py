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
    ff_module._propagator_registered = False

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
    ff_module._propagator_registered = False


@patch("lib.feature_flags.FlagdProvider")
@patch("lib.feature_flags.api")
def test_init_flag_domain_idempotent(_mock_api, mock_provider):
    """Test that init_flag_domain only initializes once per domain."""
    ff_module._initialized_domains.discard("test_domain")
    ff_module._hooks_registered = False
    ff_module._propagator_registered = False

    init_flag_domain("test_domain")
    init_flag_domain("test_domain")

    # Provider should only be created once
    mock_provider.assert_called_once()

    # Cleanup
    ff_module._initialized_domains.discard("test_domain")
    ff_module._hooks_registered = False
    ff_module._propagator_registered = False


@patch("lib.feature_flags.FlagdProvider")
@patch("lib.feature_flags.api")
def test_tracing_hook_registered_once(mock_api, _mock_provider):
    """Test that TracingHook is registered only once across multiple domains."""
    ff_module._initialized_domains.discard("domain_a")
    ff_module._initialized_domains.discard("domain_b")
    ff_module._hooks_registered = False
    ff_module._propagator_registered = False

    init_flag_domain("domain_a")
    init_flag_domain("domain_b")

    # Hook should be registered exactly once, even though two domains were initialized
    mock_api.add_hooks.assert_called_once()

    # Cleanup
    ff_module._initialized_domains.discard("domain_a")
    ff_module._initialized_domains.discard("domain_b")
    ff_module._hooks_registered = False
    ff_module._propagator_registered = False


@patch("lib.feature_flags.api")
def test_get_flag_client(mock_api):
    """Test that get_flag_client returns a domain-scoped client."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client

    client = get_flag_client("game_settings")

    mock_api.get_client.assert_called_with(domain="game_settings")
    assert client is mock_client


@patch("lib.feature_flags.FlagdProvider")
@patch("lib.feature_flags.api")
def test_init_flag_domain_registers_propagator(mock_api, _mock_provider):
    """Test that init_flag_domain registers transaction context propagator on first call."""
    ff_module._initialized_domains.discard("test_propagator")
    ff_module._hooks_registered = False
    ff_module._propagator_registered = False

    init_flag_domain("test_propagator")

    # Should register propagator
    mock_api.set_transaction_context_propagator.assert_called_once()

    # Should set API-level evaluation context
    mock_api.set_evaluation_context.assert_called_once()

    # Cleanup
    ff_module._initialized_domains.discard("test_propagator")
    ff_module._hooks_registered = False
    ff_module._propagator_registered = False


@patch("lib.feature_flags.FlagdProvider")
@patch("lib.feature_flags.api")
def test_propagator_registered_only_once(mock_api, _mock_provider):
    """Test that propagator registration is idempotent across multiple domains."""
    ff_module._initialized_domains.discard("domain_a")
    ff_module._initialized_domains.discard("domain_b")
    ff_module._hooks_registered = False
    ff_module._propagator_registered = False

    init_flag_domain("domain_a")
    init_flag_domain("domain_b")

    # Propagator should be registered only once
    mock_api.set_transaction_context_propagator.assert_called_once()
    mock_api.set_evaluation_context.assert_called_once()

    # But both domains should be initialized
    assert "domain_a" in ff_module._initialized_domains
    assert "domain_b" in ff_module._initialized_domains

    # Cleanup
    ff_module._initialized_domains.discard("domain_a")
    ff_module._initialized_domains.discard("domain_b")
    ff_module._hooks_registered = False
    ff_module._propagator_registered = False


@patch("lib.feature_flags.FlagdProvider")
@patch("lib.feature_flags.api")
def test_api_level_context_uses_env_vars(mock_api, _mock_provider):
    """Test that API-level context reads from environment variables."""
    import os

    ff_module._initialized_domains.discard("test_env")
    ff_module._hooks_registered = False
    ff_module._propagator_registered = False

    with patch.dict(
        os.environ,
        {
            "SERVICE_NAME": "test-service",
            "ENVIRONMENT": "staging",
        },
    ):
        init_flag_domain("test_env")

    # Verify set_evaluation_context was called with correct attributes
    call_args = mock_api.set_evaluation_context.call_args
    ctx = call_args[0][0]
    assert ctx.attributes["service_name"] == "test-service"
    assert ctx.attributes["environment"] == "staging"
    assert "hostname" in ctx.attributes

    # Cleanup
    ff_module._initialized_domains.discard("test_env")
    ff_module._hooks_registered = False
    ff_module._propagator_registered = False


@patch("lib.feature_flags.api")
def test_set_game_transaction_context(mock_api):
    """Test that set_game_transaction_context sets correct context."""
    from lib.feature_flags import set_game_transaction_context

    set_game_transaction_context(
        game_mode="FFA",
        controller_count=4,
        sensitivity=2,
    )

    mock_api.set_transaction_context.assert_called_once()
    call_args = mock_api.set_transaction_context.call_args
    ctx = call_args[0][0]
    assert ctx.targeting_key == "FFA"
    assert ctx.attributes["game_mode"] == "FFA"
    assert ctx.attributes["controller_count"] == 4
    assert ctx.attributes["sensitivity"] == 2


@patch("lib.feature_flags.api")
def test_set_game_transaction_context_without_sensitivity(mock_api):
    """Test that set_game_transaction_context works without optional sensitivity."""
    from lib.feature_flags import set_game_transaction_context

    set_game_transaction_context(
        game_mode="Werewolf",
        controller_count=6,
    )

    call_args = mock_api.set_transaction_context.call_args
    ctx = call_args[0][0]
    assert ctx.targeting_key == "Werewolf"
    assert ctx.attributes["game_mode"] == "Werewolf"
    assert ctx.attributes["controller_count"] == 6
    assert "sensitivity" not in ctx.attributes
