import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import lib.feature_flags as ff_module
from lib.feature_flags import (
    get_flag_client,
    init_flag_domain,
    read_float_flag,
    read_int_flag,
    read_string_flag,
)


def _mock_flag_value(value):
    """Patch init/get_flag_client so the domain client returns ``value``."""
    client = MagicMock()
    client.get_object_value.return_value = value
    return (
        patch("lib.feature_flags.init_flag_domain"),
        patch("lib.feature_flags.get_flag_client", return_value=client),
    )


def test_read_float_flag_returns_numeric():
    init_p, get_p = _mock_flag_value(3.5)
    with init_p, get_p:
        assert read_float_flag("game", "k", 1.0) == 3.5
    # ints are coerced to float
    init_p, get_p = _mock_flag_value(7)
    with init_p, get_p:
        assert read_float_flag("game", "k", 1.0) == 7.0


def test_read_float_flag_rejects_bool_and_nonnumeric():
    for bad in (True, "x", None, [1.0]):
        init_p, get_p = _mock_flag_value(bad)
        with init_p, get_p:
            assert read_float_flag("game", "k", 1.0) == 1.0


def test_read_float_flag_fails_safe_on_exception():
    with patch("lib.feature_flags.init_flag_domain", side_effect=RuntimeError("boom")):
        assert read_float_flag("game", "k", 2.5) == 2.5


def test_read_int_flag_accepts_int_and_integral_float():
    init_p, get_p = _mock_flag_value(4)
    with init_p, get_p:
        assert read_int_flag("game", "k", 1) == 4
    # integral float coerced; bool subclass rejected
    init_p, get_p = _mock_flag_value(6.0)
    with init_p, get_p:
        assert read_int_flag("game", "k", 1) == 6


def test_read_int_flag_rejects_nonintegral_bool_nonnumeric():
    for bad in (2.5, True, "x", None):
        init_p, get_p = _mock_flag_value(bad)
        with init_p, get_p:
            assert read_int_flag("game", "k", 9) == 9


def test_read_int_flag_fails_safe_on_exception():
    with patch("lib.feature_flags.init_flag_domain", side_effect=RuntimeError("boom")):
        assert read_int_flag("game", "k", 3) == 3


def _mock_string_flag_value(value):
    """Patch init/get_flag_client so the client's get_string_value returns ``value``."""
    client = MagicMock()
    client.get_string_value.return_value = value
    return (
        patch("lib.feature_flags.init_flag_domain"),
        patch("lib.feature_flags.get_flag_client", return_value=client),
    )


def test_read_string_flag_returns_string():
    init_p, get_p = _mock_string_flag_value("allow")
    with init_p, get_p:
        assert read_string_flag("game", "shadow_policy", "block") == "allow"


def test_read_string_flag_rejects_nonstring():
    for bad in (1, True, None, ["allow"]):
        init_p, get_p = _mock_string_flag_value(bad)
        with init_p, get_p:
            assert read_string_flag("game", "shadow_policy", "block") == "block"


def test_read_string_flag_fails_safe_on_exception():
    with patch("lib.feature_flags.init_flag_domain", side_effect=RuntimeError("boom")):
        assert read_string_flag("game", "shadow_policy", "block") == "block"


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

    client = get_flag_client("game")

    mock_api.get_client.assert_called_with(domain="game")
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
            "OTEL_SERVICE_NAME": "test-service",
            "OTEL_SERVICE_NAMESPACE": "joustmania",
            "ENVIRONMENT": "staging",
        },
    ):
        init_flag_domain("test_env")

    # Verify set_evaluation_context was called with correct attributes
    call_args = mock_api.set_evaluation_context.call_args
    ctx = call_args[0][0]
    assert ctx.attributes["service_name"] == "test-service"
    assert ctx.attributes["service_namespace"] == "joustmania"
    assert ctx.attributes["language"] == "python"
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


# --------------------------------------------------------------------------- #
# gameId calibration context (#838)
# --------------------------------------------------------------------------- #
from lib.feature_flags import _calibration_context, read_object_flag  # noqa: E402


class _GameIdAwareClient:
    """Resolves a calibration flag by the gameId in the EvaluationContext.

    Mimics flagd targeting: gameId == experiment_id -> the experiment value,
    else the baseline value. Records the contexts it saw for assertions.
    """

    def __init__(self, baseline, experiment_id, experiment_value):
        self.baseline = baseline
        self.experiment_id = experiment_id
        self.experiment_value = experiment_value
        self.seen_contexts = []

    def get_object_value(self, _key, default, ctx):
        self.seen_contexts.append(ctx)
        attrs = getattr(ctx, "attributes", None) or {}
        if attrs.get("gameId") == self.experiment_id:
            return self.experiment_value
        return self.baseline


def _mock_client(client):
    return (
        patch("lib.feature_flags.init_flag_domain"),
        patch("lib.feature_flags.get_flag_client", return_value=client),
    )


def test_calibration_context_adds_gameid_when_present():
    ctx = _calibration_context("game_abc")
    assert ctx.attributes["gameId"] == "game_abc"


def test_calibration_context_empty_when_no_gameid():
    for empty in (None, ""):
        ctx = _calibration_context(empty)
        assert "gameId" not in (ctx.attributes or {})


def test_read_object_flag_targeted_variant_for_matching_game():
    """A gameId-targeted calibration flag resolves the experiment variant for the
    matching game and the baseline for every other game / no game."""
    client = _GameIdAwareClient(
        baseline={"slow_warning": [1, 2]},
        experiment_id="game_shadow",
        experiment_value={"slow_warning": [9, 9]},
    )
    init_p, get_p = _mock_client(client)
    with init_p, get_p:
        # Matching shadow game -> experiment variant.
        assert read_object_flag("game", "thresholds", {}, game_id="game_shadow") == {"slow_warning": [9, 9]}
        # A different game -> baseline.
        assert read_object_flag("game", "thresholds", {}, game_id="game_real") == {"slow_warning": [1, 2]}
        # No gameId (un-targeted) -> baseline, and NO gameId leaked into context.
        assert read_object_flag("game", "thresholds", {}) == {"slow_warning": [1, 2]}
    last_ctx = client.seen_contexts[-1]
    assert "gameId" not in (last_ctx.attributes or {})


def test_read_float_flag_threads_gameid_context():
    client = MagicMock()
    client.get_object_value.return_value = 4.0
    init_p, get_p = _mock_client(client)
    with init_p, get_p:
        assert read_float_flag("game", "death_grace_period_seconds", 1.0, game_id="game_x") == 4.0
    ctx = client.get_object_value.call_args[0][2]
    assert ctx.attributes["gameId"] == "game_x"


def test_read_int_flag_threads_gameid_context():
    client = MagicMock()
    client.get_object_value.return_value = 3
    init_p, get_p = _mock_client(client)
    with init_p, get_p:
        assert read_int_flag("game", "zombie.initial_count", 1, game_id="game_y") == 3
    ctx = client.get_object_value.call_args[0][2]
    assert ctx.attributes["gameId"] == "game_y"
