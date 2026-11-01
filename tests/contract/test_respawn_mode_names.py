"""The agent's respawn-mode list matches the coordinator's mode names (#2532, UC-040 BR-043).

The agent recognises a respawn mode by the coordinator's get_game_name() string
(`characteristics.RespawnMode`). A rename on either side would silently turn the
gate off, so both sides are read as text here: no import of the coordinator.
"""

from __future__ import annotations

import re

import pytest
import telemetry_inventory as ti

pytestmark = pytest.mark.uc("UC-040", br=["BR-043"])

GAMES = ti.REPO_ROOT / "services" / "game_coordinator" / "games"
AGENT = ti.REPO_ROOT / "services" / "agent" / "characteristics" / "characteristics.go"

# Modes where a death keeps the player in the game (UC-040 BR-038, BR-043).
RESPAWN_MODES = ["nonstop_joust.py", "zombie.py", "swapper.py", "fight_club.py"]
# Modes where every death is final: they must stay out of the list.
FINAL_DEATH_MODES = [
    "ffa.py",
    "teams.py",
    "random_teams.py",
    "tournament.py",
    "traitor.py",
    "werewolf.py",
]


def game_name(module: str) -> str:
    src = (GAMES / module).read_text()
    m = re.search(
        r"def get_game_name\(self\)[^:]*:\s*(?:\"\"\"[^\"]*\"\"\"\s*)?return \"([^\"]+)\"",
        src,
    )
    assert m, f"{module}: no literal get_game_name() return"
    return m.group(1)


def agent_respawn_names() -> set[str]:
    src = AGENT.read_text()
    body = re.search(r"func RespawnMode\(.*?\n}\n", src, re.DOTALL)
    assert body, "characteristics.go: RespawnMode not found"
    case = re.search(r"case (\"[^\n]*(?:\n\t\t\"[^\n]*)*):", body.group(0))
    assert case, "RespawnMode: case list not found"
    return set(re.findall(r"\"([^\"]+)\"", case.group(1)))


@pytest.mark.parametrize("module", RESPAWN_MODES)
def test_respawn_mode_name_is_in_the_agent_list(module: str) -> None:
    name = game_name(module)
    assert name.lower() in agent_respawn_names(), (
        f"{module} get_game_name() {name!r} is not in characteristics.RespawnMode: "
        "a rename turns the agent's respawn-mode gate off"
    )


@pytest.mark.parametrize("module", FINAL_DEATH_MODES)
def test_final_death_mode_name_is_not_in_the_agent_list(module: str) -> None:
    assert game_name(module).lower() not in agent_respawn_names()
