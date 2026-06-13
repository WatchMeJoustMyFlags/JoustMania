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


def _ok_details(value):
    """Build a successful FlagEvaluationDetails carrying ``value``."""
    from openfeature.flag_evaluation import FlagEvaluationDetails, Reason

    return FlagEvaluationDetails(flag_key="k", value=value, reason=Reason.STATIC)


def _mock_flag_value(value):
    """Patch init/get_flag_client so the domain client returns ``value``.

    Numeric flags are read via the typed ``get_float_details`` getter (#903:
    ``get_object_value`` is TYPE_MISMATCH for numbers under the flagd RPC
    resolver; #921 switched to the ``_details`` API to surface those reasons);
    the post-read type checks remain as defense-in-depth, so the mock still
    feeds arbitrary values through the float getter.
    """
    client = MagicMock()
    client.get_float_details.return_value = _ok_details(value)
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
    """Patch init/get_flag_client so the client's get_string_details returns ``value``."""
    client = MagicMock()
    client.get_string_details.return_value = _ok_details(value)
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
# game_kind shadow/real split (#932) — fail-safe-real-default safety property.
# The agent's experiment writer (services/agent/experiment/targeting.go) scopes
# targeting on {"!=": [{"var":"game_kind"}, "real"]}: anything NOT "real"
# resolves the experimental variant. So "real" MUST be the default everywhere,
# and only an explicit shadow session may override it. These tests prove that.
# --------------------------------------------------------------------------- #


def test_game_kind_constants_match_targeting_contract():
    """The Python constants MUST equal targeting.go's GameKindVar/GameKindReal.

    If these drift, real games stop being protected (or shadow experiments stop
    resolving). The Go side is the single source of truth; this pins the values.
    """
    from lib.feature_flags import GAME_KIND_REAL, GAME_KIND_SHADOW, GAME_KIND_VAR

    assert GAME_KIND_VAR == "game_kind"
    assert GAME_KIND_REAL == "real"
    assert GAME_KIND_SHADOW == "shadow"


@patch("lib.feature_flags.FlagdProvider")
@patch("lib.feature_flags.api")
def test_api_level_context_defaults_game_kind_to_real(mock_api, _mock_provider):
    """The always-available API-level context carries game_kind="real".

    An evaluation with NO per-game context still sees game_kind="real", so an
    unlabeled game is protected from experiments by construction (fail-safe).
    """
    ff_module._initialized_domains.discard("test_gk")
    ff_module._hooks_registered = False
    ff_module._propagator_registered = False

    init_flag_domain("test_gk")

    ctx = mock_api.set_evaluation_context.call_args[0][0]
    assert ctx.attributes["game_kind"] == "real"

    ff_module._initialized_domains.discard("test_gk")
    ff_module._hooks_registered = False
    ff_module._propagator_registered = False


@patch("lib.feature_flags.api")
def test_set_game_transaction_context_defaults_game_kind_to_real(mock_api):
    """A default (real-game) call puts game_kind="real" in the transaction."""
    from lib.feature_flags import set_game_transaction_context

    set_game_transaction_context(game_mode="FFA", controller_count=4)

    ctx = mock_api.set_transaction_context.call_args[0][0]
    assert ctx.attributes["game_kind"] == "real"


@patch("lib.feature_flags.api")
def test_set_game_transaction_context_shadow_override(mock_api):
    """A shadow session passing game_kind="shadow" overrides the real default."""
    from lib.feature_flags import set_game_transaction_context

    set_game_transaction_context(game_mode="FFA", controller_count=4, game_kind="shadow")

    ctx = mock_api.set_transaction_context.call_args[0][0]
    assert ctx.attributes["game_kind"] == "shadow"


@patch("lib.feature_flags.api")
def test_set_game_session_kind_context_defaults_real(mock_api):
    """The session-boundary setter defaults to game_kind="real" (protected)."""
    from lib.feature_flags import set_game_session_kind_context

    set_game_session_kind_context()

    ctx = mock_api.set_transaction_context.call_args[0][0]
    assert ctx.attributes["game_kind"] == "real"


@patch("lib.feature_flags.api")
def test_set_game_session_kind_context_shadow(mock_api):
    """A shadow session sets game_kind="shadow" before its init-time reads."""
    from lib.feature_flags import set_game_session_kind_context

    set_game_session_kind_context("shadow")

    ctx = mock_api.set_transaction_context.call_args[0][0]
    assert ctx.attributes["game_kind"] == "shadow"


def test_experiment_targeting_protects_real_resolves_shadow():
    """End-to-end intent: the agent's "!= real" targeting resolves the baseline
    for a real/unlabeled context and the experimental value for a shadow context.

    Mirrors flagd JSONLogic {"if":[{"!=":[{"var":"game_kind"},"real"]}, "X", <else>]}
    so we prove the wiring's effect without the docker stack: it is the merged
    eval-context attributes that decide the branch.
    """
    from lib.feature_flags import GAME_KIND_REAL, GAME_KIND_SHADOW, GAME_KIND_VAR

    def resolve(attrs: dict) -> str:
        # The fake provider models flagd's "!= real" branch on game_kind.
        return "X" if attrs.get(GAME_KIND_VAR) != GAME_KIND_REAL else "baseline"

    # Real (explicit) and unlabeled-but-default-real → protected baseline.
    assert resolve({GAME_KIND_VAR: GAME_KIND_REAL}) == "baseline"
    assert resolve({GAME_KIND_VAR: GAME_KIND_REAL, "game_mode": "FFA"}) == "baseline"
    # Shadow → experimental value.
    assert resolve({GAME_KIND_VAR: GAME_KIND_SHADOW}) == "X"
    # SAFETY: a context that somehow MISSES game_kind would resolve the
    # experiment — which is exactly why "real" is the fail-safe default the
    # wiring guarantees is always present.
    assert resolve({}) == "X"


# --------------------------------------------------------------------------- #
# experiment_id / arm attribution (#975, epic #982) — the foundation of the
# experiment/cohort framework. These mirror the game_kind dual-path: the eval
# context carries experiment_id + arm WHEN PROVIDED, and a call WITHOUT them
# leaves a real (or non-experiment) game unaffected — absent experiment_id means
# "not in any experiment", so a non-experiment game's experiment targeting is
# false by construction (real-by-default carries over for free).
# --------------------------------------------------------------------------- #


def test_experiment_constants_match_targeting_contract():
    """The experiment-identity constants pin the values targeting.go (#977) keys on."""
    from lib.feature_flags import ARM_CONTROL, ARM_EXPERIMENTAL, ARM_VAR, EXPERIMENT_ID_VAR

    assert EXPERIMENT_ID_VAR == "experiment_id"
    assert ARM_VAR == "arm"
    assert ARM_EXPERIMENTAL == "experimental"
    assert ARM_CONTROL == "control"


@patch("lib.feature_flags.api")
def test_set_game_transaction_context_carries_experiment_identity(mock_api):
    """A shadow game bound to an experiment lands experiment_id + arm in context."""
    from lib.feature_flags import set_game_transaction_context

    set_game_transaction_context(
        game_mode="FFA",
        controller_count=4,
        game_kind="shadow",
        experiment_id="exp_abc123",
        arm="experimental",
    )

    ctx = mock_api.set_transaction_context.call_args[0][0]
    assert ctx.attributes["game_kind"] == "shadow"
    assert ctx.attributes["experiment_id"] == "exp_abc123"
    assert ctx.attributes["arm"] == "experimental"


@patch("lib.feature_flags.api")
def test_set_game_transaction_context_omits_experiment_identity_when_absent(mock_api):
    """A real/non-experiment game leaves experiment_id + arm ABSENT.

    An absent experiment_id is the real-by-default fail-safe: the experiment
    targeting condition (== exp_X) is false, so the game resolves the existing
    default exactly as it did before any experiment framework existed.
    """
    from lib.feature_flags import set_game_transaction_context

    set_game_transaction_context(game_mode="FFA", controller_count=4)

    ctx = mock_api.set_transaction_context.call_args[0][0]
    assert ctx.attributes["game_kind"] == "real"
    assert "experiment_id" not in ctx.attributes
    assert "arm" not in ctx.attributes


@patch("lib.feature_flags.api")
def test_set_game_session_kind_context_carries_experiment_identity(mock_api):
    """The session-boundary setter carries experiment_id + arm before init reads."""
    from lib.feature_flags import set_game_session_kind_context

    set_game_session_kind_context("shadow", experiment_id="exp_abc123", arm="control")

    ctx = mock_api.set_transaction_context.call_args[0][0]
    assert ctx.attributes["game_kind"] == "shadow"
    assert ctx.attributes["experiment_id"] == "exp_abc123"
    assert ctx.attributes["arm"] == "control"


@patch("lib.feature_flags.api")
def test_set_game_session_kind_context_omits_experiment_identity_when_absent(mock_api):
    """The default (real-game) session boundary leaves experiment_id + arm absent."""
    from lib.feature_flags import set_game_session_kind_context

    set_game_session_kind_context()

    ctx = mock_api.set_transaction_context.call_args[0][0]
    assert ctx.attributes["game_kind"] == "real"
    assert "experiment_id" not in ctx.attributes
    assert "arm" not in ctx.attributes


def test_experiment_targeting_leaves_non_experiment_game_unaffected():
    """End-to-end intent (#977): an experiment-scoped flag resolves the experimental
    value ONLY for the matching (experiment_id, arm); a real/control/unlabeled game
    falls through to the existing default.

    Mirrors flagd JSONLogic
      {"if":[{"and":[{"==":[{"var":"experiment_id"},"exp_X"]},
                     {"==":[{"var":"arm"},"experimental"]}]}, "V", <else>]}
    so we prove the wiring's effect (real-by-default for free) without the stack.
    """
    from lib.feature_flags import ARM_CONTROL, ARM_EXPERIMENTAL, ARM_VAR, EXPERIMENT_ID_VAR

    def resolve(attrs: dict) -> str:
        in_arm = attrs.get(EXPERIMENT_ID_VAR) == "exp_X" and attrs.get(ARM_VAR) == ARM_EXPERIMENTAL
        return "V" if in_arm else "default"

    # Experimental arm of exp_X → the experiment value.
    assert resolve({EXPERIMENT_ID_VAR: "exp_X", ARM_VAR: ARM_EXPERIMENTAL}) == "V"
    # Control arm of exp_X → falls through to the existing default (control IS
    # the else-branch; no separate control variant).
    assert resolve({EXPERIMENT_ID_VAR: "exp_X", ARM_VAR: ARM_CONTROL}) == "default"
    # A DIFFERENT experiment's game → default (no cross-experiment interaction).
    assert resolve({EXPERIMENT_ID_VAR: "exp_Y", ARM_VAR: ARM_EXPERIMENTAL}) == "default"
    # A real / non-experiment game (no experiment_id at all) → default. This is
    # the real-by-default fail-safe that comes for free from an absent label.
    assert resolve({"game_kind": "real"}) == "default"
    assert resolve({}) == "default"


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

    def get_object_details(self, _key, default, ctx):
        from openfeature.flag_evaluation import FlagEvaluationDetails, Reason

        self.seen_contexts.append(ctx)
        attrs = getattr(ctx, "attributes", None) or {}
        value = self.experiment_value if attrs.get("gameId") == self.experiment_id else self.baseline
        return FlagEvaluationDetails(flag_key=_key, value=value, reason=Reason.STATIC)


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
    client.get_float_details.return_value = _ok_details(4.0)
    init_p, get_p = _mock_client(client)
    with init_p, get_p:
        assert read_float_flag("game", "death_grace_period_seconds", 1.0, game_id="game_x") == 4.0
    ctx = client.get_float_details.call_args[0][2]
    assert ctx.attributes["gameId"] == "game_x"


def test_read_int_flag_threads_gameid_context():
    client = MagicMock()
    client.get_float_details.return_value = _ok_details(3)
    init_p, get_p = _mock_client(client)
    with init_p, get_p:
        assert read_int_flag("game", "zombie.initial_count", 1, game_id="game_y") == 3
    ctx = client.get_float_details.call_args[0][2]
    assert ctx.attributes["gameId"] == "game_y"


# --------------------------------------------------------------------------- #
# Flag-evaluation fallback visibility (#921): WARN + flag_eval_errors_total
# --------------------------------------------------------------------------- #
import pytest  # noqa: E402

import lib.flag_eval_visibility as vis  # noqa: E402


@pytest.fixture
def fresh_visibility():
    """Reset the once-per-key warn memory and force us out of startup grace."""
    vis.reset_for_tests()
    # Push process-start far enough back that the grace window has elapsed, so
    # provider-not-ready style reasons are NOT suppressed by default.
    original = vis._PROCESS_START
    vis._PROCESS_START = original - (vis._STARTUP_GRACE_SECONDS + 100)
    yield
    vis._PROCESS_START = original
    vis.reset_for_tests()


def _error_details(error_code, value):
    """Build a FlagEvaluationDetails carrying a non-raising error resolution."""
    from openfeature.flag_evaluation import FlagEvaluationDetails, Reason

    return FlagEvaluationDetails(
        flag_key="k",
        value=value,
        reason=Reason.ERROR,
        error_code=error_code,
        error_message="boom",
    )


def _mock_float_details(details):
    client = MagicMock()
    client.get_float_details.return_value = details
    return (
        patch("lib.feature_flags.init_flag_domain"),
        patch("lib.feature_flags.get_flag_client", return_value=client),
    )


def test_type_mismatch_is_visible_and_counted(fresh_visibility):  # noqa: ARG001 - fixture dependency
    """A non-raising TYPE_MISMATCH resolution -> default, WARN, counter bump."""
    from openfeature.exception import ErrorCode

    init_p, get_p = _mock_float_details(_error_details(ErrorCode.TYPE_MISMATCH, 1.0))
    with patch("lib.feature_flags.record_flag_fallback") as rec, init_p, get_p:
        assert read_float_flag("game", "death_grace_period_seconds", 1.0) == 1.0
    rec.assert_called_once()
    assert rec.call_args[0][0] == "death_grace_period_seconds"
    assert rec.call_args[0][1] == "TYPE_MISMATCH"


def test_exception_is_reported_with_type_name(fresh_visibility):  # noqa: ARG001 - fixture dependency
    with (
        patch("lib.feature_flags.init_flag_domain", side_effect=RuntimeError("nope")),
        patch("lib.feature_flags.record_flag_fallback") as rec,
    ):
        assert read_float_flag("game", "k", 2.5) == 2.5
    rec.assert_called_once()
    assert rec.call_args[0][0] == "k"
    assert rec.call_args[0][1] == "RuntimeError"


def test_record_warns_once_per_key_but_counts_every_time(fresh_visibility):  # noqa: ARG001 - fixture dependency
    with (
        patch("lib.flag_eval_visibility.flag_eval_errors_total") as counter,
        patch.object(vis.logger, "warning") as warn,
    ):
        vis.record_flag_fallback("flagA", "TYPE_MISMATCH")
        vis.record_flag_fallback("flagA", "TYPE_MISMATCH")
        vis.record_flag_fallback("flagA", "TYPE_MISMATCH")
    # WARN exactly once for the repeated (flag, reason) pair...
    assert warn.call_count == 1
    # ...but the counter increments on every call.
    assert counter.labels.call_count == 3
    counter.labels.assert_called_with(flag_key="flagA", reason="TYPE_MISMATCH")


def test_record_warns_again_for_different_reason(fresh_visibility):  # noqa: ARG001 - fixture dependency
    with (
        patch("lib.flag_eval_visibility.flag_eval_errors_total"),
        patch.object(vis.logger, "warning") as warn,
    ):
        vis.record_flag_fallback("flagA", "TYPE_MISMATCH")
        vis.record_flag_fallback("flagA", "FLAG_NOT_FOUND")
    assert warn.call_count == 2


def test_startup_grace_suppresses_provider_not_ready():
    """Within the grace window, PROVIDER_NOT_READY must not warn or count."""
    vis.reset_for_tests()
    # Force "just started": process start = now.
    original = vis._PROCESS_START
    import time as _time

    vis._PROCESS_START = _time.monotonic()
    try:
        with (
            patch("lib.flag_eval_visibility.flag_eval_errors_total") as counter,
            patch.object(vis.logger, "warning") as warn,
        ):
            vis.record_flag_fallback("flagX", "PROVIDER_NOT_READY")
        warn.assert_not_called()
        counter.labels.assert_not_called()
    finally:
        vis._PROCESS_START = original
        vis.reset_for_tests()


def test_startup_grace_does_not_suppress_after_window(fresh_visibility):  # noqa: ARG001 - fixture dependency
    """After the grace window, PROVIDER_NOT_READY becomes a real, visible error."""
    with (
        patch("lib.flag_eval_visibility.flag_eval_errors_total") as counter,
        patch.object(vis.logger, "warning") as warn,
    ):
        vis.record_flag_fallback("flagX", "PROVIDER_NOT_READY")
    warn.assert_called_once()
    counter.labels.assert_called_once_with(flag_key="flagX", reason="PROVIDER_NOT_READY")


def test_grace_window_never_suppresses_type_mismatch():
    """TYPE_MISMATCH is a real bug even during startup; never suppressed."""
    vis.reset_for_tests()
    original = vis._PROCESS_START
    import time as _time

    vis._PROCESS_START = _time.monotonic()  # just started
    try:
        with (
            patch("lib.flag_eval_visibility.flag_eval_errors_total") as counter,
            patch.object(vis.logger, "warning") as warn,
        ):
            vis.record_flag_fallback("flagY", "TYPE_MISMATCH")
        warn.assert_called_once()
        counter.labels.assert_called_once()
    finally:
        vis._PROCESS_START = original
        vis.reset_for_tests()
