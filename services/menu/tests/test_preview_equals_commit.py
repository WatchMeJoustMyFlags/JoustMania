"""Preview equals commit (#2106, UC-015 BR-004, BR-005, A7).

The pad computes the value a knob release commits from `GetMenuStatus` plus the net steps
(`lib.setting_ladders.preview_value`); the menu commits it with `SettingsOps.step_setting`.
This test runs both for every start value, every net step count in -12..12 and every option,
on a real MenuServicer whose flag service is a fake loaded from the real flag files
(`services/flagd/*.json`), so the ladders are pinned to the variants the flag service serves.
The status comes from the menu's own `GetMenuStatus`, read from the same fake.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
import pytest_asyncio
from openfeature.client import OpenFeatureClient
from openfeature.flag_evaluation import FlagEvaluationDetails

from lib import setting_ladders
from lib.flag_service_writer import FlagServiceWriter
from lib.setting_ladders import preview_value
from lib.types import Games
from proto import menu_pb2
from services.menu.settings_ops import AT_LIMIT
from services.menu.tests.servicer_harness import (
    cancel_background_tasks,
    default_returning_flag_client,
    make_real_servicer,
)

FLAGD_DIR = Path(__file__).resolve().parents[3] / "services" / "flagd"
STEPS = [n for n in range(-12, 13) if n != 0]
KEYS = {
    "game": ["sensitivity", "num_teams", "force_all_start", "shield.activation"],
    "user": ["game_instructions", "audio_volume.master", "current_game"],
    "agent": ["interventions_allowed", "enabled", "interventions_enabled", "decision.backend"],
}


def _flag_file_variants() -> dict[tuple[str, str], dict[str, object]]:
    variants = {}
    for flag_set, keys in KEYS.items():
        flags = json.loads((FLAGD_DIR / f"{flag_set}.json").read_text())["flags"]
        for key in keys:
            variants[(flag_set, key)] = dict(flags[key]["variants"])
    return variants


VARIANTS = _flag_file_variants()


class FlagFileService:
    """The flag service's GET / PUT over the real flag files' variants; the default is settable."""

    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}

    def __call__(self, method, url, body):
        flag_set, key = url.split("/")[-2:]
        variants = VARIANTS[(flag_set, key)]
        if method == "GET":
            return 200, {
                "variants": list(variants),
                "variant_values": variants,
                "effective_default": self.values[(flag_set, key)],
            }
        self.values[(flag_set, key)] = body["variant"]
        return 200, {"variant": body["variant"], "persisted": bool(body.get("persist"))}


def in_process_client(flags: FlagFileService, flag_set: str):
    """An in-process OpenFeature client resolving from the fake's current defaults."""
    client = create_autospec(OpenFeatureClient, instance=True)

    def details(key, _default, *_args, **_kwargs):
        variant = flags.values[(flag_set, key)]
        return FlagEvaluationDetails(flag_key=key, value=VARIANTS[(flag_set, key)][variant], variant=variant)

    def value(key, _default, *_args, **_kwargs):
        return details(key, _default).value

    for getter in ("boolean", "string", "integer", "float"):
        getattr(client, f"get_{getter}_value").side_effect = value
        getattr(client, f"get_{getter}_details").side_effect = details
    return client


@pytest.fixture
def flags():
    return FlagFileService()


@pytest_asyncio.fixture
async def menu(flags):
    def writer_factory(flag_set, *args, **kwargs):
        return FlagServiceWriter(flag_set, "http://flag-service:8070", *args, transport=flags, **kwargs)

    servicer = make_real_servicer(writer_factory=writer_factory)
    servicer.state_manager.audio.play_sound = AsyncMock()
    servicer.game_settings_client = in_process_client(flags, "game")
    servicer.user_prefs_client = in_process_client(flags, "user")
    servicer.agent_settings_client = in_process_client(flags, "agent")
    with patch("lib.feature_flags.get_flag_client", side_effect=default_returning_flag_client):
        yield servicer
    await cancel_background_tasks(servicer)


DEFAULTS = {
    (flag_set, key): json.loads((FLAGD_DIR / f"{flag_set}.json").read_text())["flags"][key]["defaultVariant"]
    for flag_set, keys in KEYS.items()
    for key in keys
}


def reset(flags: FlagFileService, menu) -> None:
    """Every flag at its flag-file default, no saved agent level."""
    flags.values = dict(DEFAULTS)
    menu.settings_ops.saved_agent_level = None


async def assert_preview_equals_commit(menu, option: str, steps: int, committed) -> None:
    status = await menu.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), None)
    preview = preview_value(option, status, steps)
    result = await menu.settings_ops.step_setting(option, "up" if steps > 0 else "down", abs(steps))
    case = f"{option} from {result.old!r} by {steps}"
    assert result.error in (None, AT_LIMIT), case
    assert preview.at_limit == (result.error == AT_LIMIT), case
    assert preview.value == result.new == committed(), case


FLAG_OPTIONS = {
    "sensitivity": ("game", "sensitivity"),
    "num_teams": ("game", "num_teams"),
    "force_all_start": ("game", "force_all_start"),
    "game_instructions": ("user", "game_instructions"),
    "audio_volume.master": ("user", "audio_volume.master"),
    "shield.activation": ("game", "shield.activation"),  # UC-015 BR-018 (#2215)
}


@pytest.mark.uc("UC-015", br=["BR-004", "BR-005"], flow="A7")
@pytest.mark.asyncio
@pytest.mark.parametrize("option", sorted(FLAG_OPTIONS))
async def test_flag_ladders_preview_equals_commit(menu, flags, option):
    key = FLAG_OPTIONS[option]
    assert set(VARIANTS[key]) == set(setting_ladders.FLAG_LADDERS[option]), "ladder differs from the flag file"
    for start in VARIANTS[key]:
        for steps in STEPS:
            reset(flags, menu)
            flags.values[key] = start
            await assert_preview_equals_commit(menu, option, steps, lambda: flags.values[key])


@pytest.mark.uc("UC-015", br=["BR-004", "BR-005"], flow="A7")
@pytest.mark.asyncio
async def test_mode_ladder_preview_equals_commit(menu, flags):
    assert menu.get_game_options() == setting_ladders.MODE_LADDER
    for start in setting_ladders.MODE_LADDER:
        for steps in STEPS:
            reset(flags, menu)
            await menu.select_game_mode(start, source="test")
            await assert_preview_equals_commit(menu, "current_game", steps, lambda: menu.current_selection.name)


AGENT_LEVEL_STARTS = [
    # (enabled, level, saved level): running at every rung and off the ladder (probe), and stopped
    # by the kill switch or by level none, with and without a saved level (BR-002, BR-003).
    *[("on", level, None) for level in ("none", "ambient", "standard", "full", "probe")],
    *[("off", level, saved) for level in ("none", "standard") for saved in (None, "ambient", "standard", "full")],
    *[("on", "none", saved) for saved in ("ambient", "full")],
]


@pytest.mark.uc("UC-015", br=["BR-002", "BR-003", "BR-004"], flow="A7")
@pytest.mark.asyncio
@pytest.mark.parametrize(("enabled", "level", "saved"), AGENT_LEVEL_STARTS)
async def test_agent_level_preview_equals_commit(menu, flags, enabled, level, saved):
    def committed():
        live = flags.values[("agent", "interventions_allowed")]
        if enabled == "on" and level != "none":
            return live
        return menu.settings_ops.saved_agent_level or setting_ladders.AGENT_RESUME_FALLBACK_LEVEL

    for steps in STEPS:
        reset(flags, menu)
        flags.values[("agent", "enabled")] = enabled
        flags.values[("agent", "interventions_allowed")] = level
        menu.settings_ops.saved_agent_level = saved
        await assert_preview_equals_commit(menu, "agent_level", steps, committed)


@pytest.mark.uc("UC-015", br=["BR-004"])
def test_backend_ladder_is_the_flag_file_and_the_menu_list():
    from services.menu.settings_ops import DECISION_BACKENDS

    assert list(VARIANTS[("agent", "decision.backend")]) == setting_ladders.BACKENDS
    assert list(DECISION_BACKENDS) == setting_ladders.BACKENDS


@pytest.mark.uc("UC-015", br=["BR-004"])
def test_every_selectable_mode_is_a_games_member():
    assert all(Games.from_name(name) is not None for name in setting_ladders.MODE_LADDER)
