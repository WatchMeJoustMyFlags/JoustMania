"""The agent's trigger-mode list matches the coordinator's allow-list (UC-040 BR-019, #1837).

``shield.activation = trigger`` applies only in the game modes whose class sets
``SHIELD_TRIGGER_ALLOWED = True`` (services/game_coordinator/games/, today Free-for-All).
The agent's charge ledger (UC-040 A31 step 1) blocks a second ``grant_shield`` only in
the mode named by ``shieldTriggerGameMode`` in services/agent/decision/chargeledger.go,
compared with ``game.mode`` (the class's ``get_game_name()``). If a second mode is
allowed in Python without a matching Go change, the ledger stays inert there and the
coordinator refuses every second grant; if Go names a mode Python does not allow, the
agent refuses grants the coordinator would turn into an instant shield. Both sides are
read statically (AST and a Go const regex), so the check needs no service imports.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GAMES_DIR = REPO_ROOT / "services" / "game_coordinator" / "games"
LEDGER_GO = REPO_ROOT / "services" / "agent" / "decision" / "chargeledger.go"
_GO_MODE_CONST = re.compile(
    r'^\s*const\s+shieldTriggerGameMode\s*=\s*"([^"]+)"', re.MULTILINE
)


def _classes() -> dict[str, dict]:
    """name -> {bases, allowed (own value or None), game_name (own literal or None)}."""
    out: dict[str, dict] = {}
    for path in sorted(GAMES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            info = {
                "bases": [b.id for b in node.bases if isinstance(b, ast.Name)],
                "allowed": None,
                "game_name": None,
                "file": path.name,
            }
            for item in node.body:
                target = None
                if isinstance(item, ast.AnnAssign) and isinstance(
                    item.target, ast.Name
                ):
                    target, value = item.target.id, item.value
                elif (
                    isinstance(item, ast.Assign)
                    and len(item.targets) == 1
                    and isinstance(item.targets[0], ast.Name)
                ):
                    target, value = item.targets[0].id, item.value
                if target == "SHIELD_TRIGGER_ALLOWED" and isinstance(
                    value, ast.Constant
                ):
                    info["allowed"] = bool(value.value)
                if isinstance(item, ast.FunctionDef) and item.name == "get_game_name":
                    for stmt in ast.walk(item):
                        if isinstance(stmt, ast.Return) and isinstance(
                            stmt.value, ast.Constant
                        ):
                            info["game_name"] = stmt.value.value
            out[node.name] = info
    return out


def _resolve(classes: dict[str, dict], name: str, key: str):
    seen = set()
    stack = [name]
    while stack:
        current = stack.pop(0)
        if current in seen or current not in classes:
            continue
        seen.add(current)
        if classes[current][key] is not None:
            return classes[current][key]
        stack.extend(classes[current]["bases"])
    return None


def coordinator_trigger_modes() -> set[str]:
    classes = _classes()
    modes = set()
    for name in classes:
        if _resolve(classes, name, "allowed") and (
            mode := _resolve(classes, name, "game_name")
        ):
            modes.add(mode)
    return modes


def agent_trigger_modes() -> set[str]:
    return set(_GO_MODE_CONST.findall(LEDGER_GO.read_text()))


@pytest.mark.uc("UC-040", flow="A31", steps=[1], br=["BR-019"])
def test_agent_trigger_mode_matches_the_coordinator_allow_list():
    coordinator = coordinator_trigger_modes()
    agent = agent_trigger_modes()
    assert coordinator, (
        f"no SHIELD_TRIGGER_ALLOWED mode with a get_game_name() found in {GAMES_DIR}"
    )
    assert len(agent) == 1, (
        f"shieldTriggerGameMode not found once in {LEDGER_GO}: {agent}"
    )
    assert agent == coordinator, (
        f"agent ledger trigger modes {sorted(agent)} != coordinator allow-list {sorted(coordinator)}: "
        "change shieldTriggerGameMode (services/agent/decision/chargeledger.go) together with "
        "SHIELD_TRIGGER_ALLOWED (UC-040 BR-019)"
    )


def test_the_scan_sees_inheritance_and_ignores_disallowed_modes(tmp_path, monkeypatch):
    """The scanner itself: an inherited True counts, an explicit False override does not."""
    games = tmp_path / "games"
    games.mkdir()
    (games / "m.py").write_text(
        "class Base:\n    SHIELD_TRIGGER_ALLOWED: bool = False\n"
        "class A(Base):\n    SHIELD_TRIGGER_ALLOWED = True\n    def get_game_name(self):\n        return 'AAA'\n"
        "class B(A):\n    def get_game_name(self):\n        return 'BBB'\n"
        "class C(A):\n    SHIELD_TRIGGER_ALLOWED = False\n    def get_game_name(self):\n        return 'CCC'\n"
        "class D(Base):\n    def get_game_name(self):\n        return 'DDD'\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "GAMES_DIR", games)
    assert coordinator_trigger_modes() == {"AAA", "BBB"}
