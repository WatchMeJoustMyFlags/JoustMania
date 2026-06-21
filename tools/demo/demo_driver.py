#!/usr/bin/env python3
"""
Agent demo driver — scripted mock gameplay that trips the agent's rules (#794).

The agent OBSERVES a live game through OTLP telemetry (the game coordinator emits
``game_player_skill_level``, ``game_player_movement_variance``,
``game_player_movement_intensity``, ``game_duration_seconds``,
``game_player_alive``, … which the agent's GameContext store ingests) and runs its
objective-weighted RULES engine each cycle (``services/agent/decision/ruleset.go``).
On stage we don't want to *hope* the agent decides something — this driver plays
deterministic mock-controller movement patterns engineered to make the coordinator
emit metric values that trip a specific rule on cue.

It drives a REAL (primary, menu-started) game so the agent's live decision /
intervention path reacts — NOT a shadow/experiment game.

  ┌──────────┐  AddControllers / SimulateMovement / SimulateDeath  ┌───────────┐
  │  driver  │ ─────────────────────────────────────────────────▶ │ controller│
  └──────────┘  StartMenu / select_game / TRIGGER-ready (menu)     │  manager  │
       │                                                           └─────┬─────┘
       │  StreamGameEvents (watch game_id + lifecycle)                   │ 60Hz motion
       ▼                                                                 ▼
  ┌──────────┐                                                    ┌───────────┐
  │   game   │  game_player_* metrics  ── OTLP ──▶  collector ──▶ │   agent   │
  │coordinator│                                                   │  (rules)  │
  └──────────┘                                                    └───────────┘

How the movement → metric → rule mapping works
----------------------------------------------
The coordinator classifies each frame's acceleration magnitude into a zone
(``analytics.py``): STILL <1.1g, ACTIVE 1.1-1.5g, WARNING 1.5-2.0g, DANGER >2.0g.
``SimulateMovement`` sets a *persistent* accel vector (held until the next call),
so a scenario "beat" just sets each controller's magnitude. From the zone history +
a rolling variance window the coordinator derives:

  * ``skill_level``  = 0.6·playstyle_control + 0.4·motion_smoothness  (R3/R4)
      BALANCED+steady  → ~1.0   |   CALM/AGGRESSIVE or jerky → low
  * ``movement_variance`` = windowed variance of accel magnitude       (R8)
      held perfectly still after the window fills → ≈0 < 0.05 epsilon
  * ``movement_intensity`` = accel magnitude                           (R6)
  * ``duration_seconds`` / ``alive`` / elimination order               (R1/R5/R7)

Run against a RUNNING stack (see --help for the runbook). Repeatable +
parameterizable so it can be re-run for verification and fired live on stage.
"""

import argparse
import math
import sys
import time

import grpc

# Import the checked-in proto stubs (repo root on sys.path). They need protobuf +
# grpcio installed in the running interpreter (pip install grpcio protobuf).
sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), "../.."))

from proto import (  # noqa: E402
    controller_manager_mock_pb2 as mockpb,
    controller_manager_mock_pb2_grpc as mockpb_grpc,
    game_coordinator_pb2 as gcpb,
    game_coordinator_pb2_grpc as gcpb_grpc,
    menu_pb2,
    menu_pb2_grpc,
)

# ---------------------------------------------------------------------------
# Accel vectors per movement zone. Magnitude = sqrt(x^2 + y^2 + z^2); the gravity
# baseline (~1g on Z) is folded into the magnitude the coordinator sees.
# ---------------------------------------------------------------------------
TAG = "demo_driver"


def _vec(magnitude: float) -> tuple[float, float, float]:
    """An accel vector with the requested magnitude, biased onto Z (gravity-ish)."""
    # Put most of it on Z so it reads like a held controller, split a little onto
    # X/Y so it is not a perfectly axis-aligned spike.
    z = magnitude * 0.9
    xy = math.sqrt(max(0.0, magnitude**2 - z**2)) / math.sqrt(2)
    return (xy, xy, z)


ZONE_STILL = _vec(1.0)      # < 1.1g  → STILL  (also drives variance→0 when held)
ZONE_ACTIVE = _vec(1.3)     # 1.1-1.5 → ACTIVE (smooth, skilled when steady)
ZONE_WARNING = _vec(1.7)    # 1.5-2.0 → WARNING
ZONE_DANGER = _vec(2.3)     # > 2.0   → DANGER (aggressive playstyle)


def log(msg: str) -> None:
    print(f"[demo-driver] {msg}", flush=True)


# ---------------------------------------------------------------------------
# gRPC clients (synchronous — one driver, no concurrency needed)
# ---------------------------------------------------------------------------
class Stack:
    """Thin holder of the three service stubs against host-published ports."""

    def __init__(self, host: str, mock_port: int, menu_port: int, coord_port: int):
        self.mock_ch = grpc.insecure_channel(f"{host}:{mock_port}")
        self.menu_ch = grpc.insecure_channel(f"{host}:{menu_port}")
        self.coord_ch = grpc.insecure_channel(f"{host}:{coord_port}")
        self.mock = mockpb_grpc.MockControllerServiceStub(self.mock_ch)
        self.menu = menu_pb2_grpc.MenuServiceStub(self.menu_ch)
        self.coord = gcpb_grpc.GameCoordinatorServiceStub(self.coord_ch)

    def close(self) -> None:
        for ch in (self.mock_ch, self.menu_ch, self.coord_ch):
            ch.close()


# ---------------------------------------------------------------------------
# Mock-controller primitives (mirror agent gamerunner/operations.go for a REAL,
# menu-visible game: controllers are NOT reserved so the menu fields them).
# ---------------------------------------------------------------------------
def add_controllers(s: Stack, count: int) -> list[str]:
    resp = s.mock.AddControllers(
        mockpb.AddControllersRequest(count=count, reserved=False, tag=TAG)
    )
    if not resp.success:
        raise RuntimeError(f"AddControllers rejected: {resp.error}")
    serials = list(resp.serials)
    log(f"reserved {len(serials)} menu-visible mock controllers: {serials}")
    return serials


def remove_demo_controllers(s: Stack) -> None:
    """Best-effort cleanup of every controller this driver tagged."""
    try:
        resp = s.mock.ListMockControllers(mockpb.ListRequest())
    except grpc.RpcError as exc:
        log(f"cleanup ListMockControllers failed: {exc}")
        return
    serials = [c.serial for c in resp.controllers if c.tag == TAG]
    for serial in serials:
        try:
            s.mock.RemoveController(mockpb.RemoveControllerRequest(serial=serial))
        except grpc.RpcError as exc:
            log(f"cleanup RemoveController({serial}) failed: {exc}")
    if serials:
        log(f"removed {len(serials)} demo controllers")


def move(s: Stack, serial: str, vec: tuple[float, float, float]) -> None:
    s.mock.SimulateMovement(
        mockpb.MovementRequest(serial=serial, accel_x=vec[0], accel_y=vec[1], accel_z=vec[2])
    )


def kill(s: Stack, serial: str) -> None:
    s.mock.SimulateDeath(mockpb.DeathRequest(serial=serial))


def press_trigger(s: Stack, serial: str) -> None:
    btn = mockpb.ButtonRequest.Button.TRIGGER
    s.mock.SimulateButton(mockpb.ButtonRequest(serial=serial, button=btn, pressed=True))
    time.sleep(0.05)
    s.mock.SimulateButton(mockpb.ButtonRequest(serial=serial, button=btn, pressed=False))
    time.sleep(0.05)


# ---------------------------------------------------------------------------
# Real-game start through the Menu (same path as the dashboard start button)
# ---------------------------------------------------------------------------
def start_real_game(s: Stack, serials: list[str], game_mode: str, timeout: float) -> str:
    """Start a primary game via the menu and return its game_id.

    Opens the coordinator event stream first so the game_started / game_id is
    captured reliably, force-ends any stale game, (re)starts the menu, selects the
    mode, and marks every controller ready (TRIGGER) — the menu auto-starts when
    all are ready.
    """
    # Clean slate: force-end any prior game and restart the menu fresh.
    try:
        s.coord.ForceEndGame(gcpb.ForceEndGameRequest(reason="demo_driver_cleanup"))
    except grpc.RpcError:
        pass
    time.sleep(0.3)
    s.menu.StopMenu(menu_pb2.StopMenuRequest())
    time.sleep(0.2)
    start = s.menu.StartMenu(menu_pb2.StartMenuRequest())
    if not start.success:
        raise RuntimeError(f"StartMenu failed: {start.error}")
    time.sleep(0.4)

    log(f"selecting game mode {game_mode}")
    s.menu.ProcessInput(
        menu_pb2.ProcessInputRequest(
            input_type="web_command",
            data={"command": "select_game", "game_name": game_mode},
        )
    )
    time.sleep(0.2)

    # Open the event stream BEFORE readying so we don't miss game_started.
    stream = s.coord.StreamGameEvents(gcpb.StreamEventsRequest())

    log(f"marking {len(serials)} controllers ready (TRIGGER) — menu auto-starts when all ready")
    for serial in serials:
        press_trigger(s, serial)

    deadline = time.time() + timeout
    for event in stream:
        if event.event_type in ("game_started", "game_starting") and event.game_id:
            log(f"game started: game_id={event.game_id} (event={event.event_type})")
            return event.game_id
        if event.game_id:
            log(f"game live: game_id={event.game_id} (event={event.event_type})")
            return event.game_id
        if time.time() > deadline:
            break
    raise TimeoutError(f"game did not start within {timeout}s")


# ---------------------------------------------------------------------------
# Scenarios — each plays a movement pattern designed to trip a named rule.
# A scenario is (name, description, rule, observable, runner(stack, serials, dur)).
# ---------------------------------------------------------------------------
def beat(label: str) -> None:
    log(f"  beat: {label}")


def scenario_skill_gap(s: Stack, serials: list[str], duration: float) -> None:
    """R3 — skill gap exceeds fitness.balanced.max_skill_gap (0.4).

    Split the field into a high-skill cohort (BALANCED + steady ~1.3g → skill≈1.0)
    and a low-skill cohort (alternating STILL/DANGER → AGGRESSIVE + jerky → low
    skill). The coordinator's per-player ``game_player_skill_level`` gap crosses
    the threshold and the agent emits ``adjust_player_sensitivity`` on the outlier.
    """
    if len(serials) < 2:
        raise RuntimeError("skill_gap needs >= 2 controllers")
    pro = serials[: max(1, len(serials) // 2)]
    novices = serials[max(1, len(serials) // 2):]
    log(f"pros (high skill): {pro}  |  novices (low skill): {novices}")
    deadline = time.time() + duration
    flip = False
    while time.time() < deadline:
        # Pros: steady ACTIVE → BALANCED playstyle + low variance → high skill.
        for serial in pro:
            move(s, serial, ZONE_ACTIVE)
        # Novices: alternate STILL <-> DANGER → AGGRESSIVE + jerky → low skill.
        for serial in novices:
            move(s, serial, ZONE_DANGER if flip else ZONE_STILL)
        beat("pros steady-active, novices erratic still/danger (widening skill gap)")
        flip = not flip
        time.sleep(1.0)


def scenario_statue(s: Stack, serials: list[str], duration: float) -> None:
    """R8 — a player games the EMA by holding perfectly still.

    Hold one controller at a constant STILL vector so its rolling movement
    variance collapses below the statueVarianceEpsilon (0.05) once the variance
    window fills; the others jostle normally. The agent emits
    ``send_controller_effect`` to nudge the statue. (send_controller_effect is in
    the stock ``ambient`` allow-list, so this one can DISPATCH, not just decide.)
    """
    statue = serials[0]
    movers = serials[1:]
    log(f"statue (held perfectly still): {statue}  |  movers: {movers}")
    deadline = time.time() + duration
    flip = False
    while time.time() < deadline:
        move(s, statue, ZONE_STILL)  # constant vector → variance → 0
        for serial in movers:
            move(s, serial, ZONE_WARNING if flip else ZONE_ACTIVE)
        beat("statue frozen (variance→0), others jostling")
        flip = not flip
        time.sleep(1.0)


def scenario_low_variance(s: Stack, serials: list[str], duration: float) -> None:
    """Whole-field low activity → endurance/chaos pressure.

    Everyone barely moves (held near-still). Combined with early eliminations this
    feeds the endurance window (R1/R2 calming cue / slow tempo) and, per player,
    the same statue signal as R8. A gentle, ambient-friendly demo beat.
    """
    log("entire field holds low, steady motion (low activity / low variance)")
    deadline = time.time() + duration
    while time.time() < deadline:
        for serial in serials:
            move(s, serial, ZONE_STILL)
        beat("all controllers near-still (low variance across the field)")
        time.sleep(1.0)


def scenario_death_cascade(s: Stack, serials: list[str], duration: float) -> None:
    """R1/R2 — session young + players dying fast (endurance at risk).

    Keep the survivors lively, then eliminate players on a steady cadence early in
    the session so the elimination fraction climbs while ``duration_seconds`` is
    still well under ``fitness.endurance.min_session_seconds`` (120s). The agent
    slows the music (``adjust_music_tempo``) / plays a calming cue
    (``play_audio_cue``) to stretch the round.
    """
    deadline = time.time() + duration
    to_kill = serials[:-1]  # leave one survivor so the game stays live
    next_kill = 0
    last_move = 0.0
    last_kill = 0.0
    while time.time() < deadline:
        now = time.time()
        if now - last_move >= 1.0:
            for serial in serials:
                move(s, serial, ZONE_ACTIVE)
            last_move = now
        if now - last_kill >= 3.0 and next_kill < len(to_kill):
            kill(s, to_kill[next_kill])
            beat(f"eliminate {to_kill[next_kill]} early (death cascade while session young)")
            next_kill += 1
            last_kill = now
        time.sleep(0.25)


def scenario_idle_dominator(s: Stack, serials: list[str], duration: float) -> None:
    """R6-flavored — one player dominates (very active) while others idle.

    One controller stays in WARNING/DANGER (peak intensity) while the rest barely
    move (lowest ``movement_intensity``). Past the accelerate target this is the
    "eliminate the least-moving player" shape; before it, the activity spread
    still feeds the balanced/chaos signals. Pairs well with --duration long enough
    to cross fitness.accelerate.target_session_seconds (60s).
    """
    dominator = serials[0]
    idlers = serials[1:]
    log(f"dominator (high intensity): {dominator}  |  idlers (lowest intensity): {idlers}")
    deadline = time.time() + duration
    flip = False
    while time.time() < deadline:
        move(s, dominator, ZONE_DANGER if flip else ZONE_WARNING)
        for serial in idlers:
            move(s, serial, ZONE_STILL)
        beat("dominator pegged high, idlers near-still (widening activity spread)")
        flip = not flip
        time.sleep(1.0)


def scenario_blocked_battery(s: Stack, serials: list[str], duration: float) -> None:
    """Permission-chain demo — a decision that is BLOCKED, so the block shows on
    the dashboards.

    The skill-gap pattern produces an ``adjust_player_sensitivity`` decision, which
    is a *difficulty* intervention. With the stock ``interventions_allowed=ambient``
    allow-list that intervention is NOT permitted, so the agent records
    ``decision.blocked=true block_reason=not_allowed`` (and the coordinator
    increments ``game_interventions_total{blocked="true"}``) — the permission chain
    is visible end to end without anything reaching the game.

    To make this beat fire, leave ``interventions_allowed`` at its stock ``ambient``
    default (do NOT widen it to ``standard``/``full``). The driver only plays the
    pattern; the gate is the flag.
    """
    log("playing the skill-gap pattern under interventions_allowed=ambient")
    log("expected: adjust_player_sensitivity decision BLOCKED (block_reason=not_allowed)")
    log("verify the allow-list is 'ambient' (stock) — a wider variant would let it through")
    scenario_skill_gap(s, serials, duration)


SCENARIOS = {
    "skill_gap": (
        "Split field into high/low skill cohorts → skill gap > 0.4",
        "R3 adjust_player_sensitivity",
        "decision.action=adjust_player_sensitivity on the outlier (blocked under ambient)",
        scenario_skill_gap,
    ),
    "statue": (
        "Hold one controller perfectly still → variance → 0",
        "R8 send_controller_effect",
        "decision.action=send_controller_effect (DISPATCHES under stock ambient)",
        scenario_statue,
    ),
    "low_variance": (
        "Whole field near-still → low activity / low variance",
        "R1/R2/R8 (endurance + statue pressure)",
        "calming cue / slow tempo decisions; statue nudges",
        scenario_low_variance,
    ),
    "death_cascade": (
        "Eliminate players fast while the session is young",
        "R1/R2 endurance (session at risk)",
        "decision.action=adjust_music_tempo / play_audio_cue",
        scenario_death_cascade,
    ),
    "idle_dominator": (
        "One player dominates, the rest idle",
        "R6 eliminate-least-active (past accelerate target)",
        "activity spread; eliminate-player shape when past target",
        scenario_idle_dominator,
    ),
    "blocked_battery": (
        "Skill-gap pattern under ambient allow-list → a BLOCKED decision",
        "R3 blocked by permission layer",
        "decision.blocked=true block_reason=not_allowed; game_interventions_total{blocked=true}",
        scenario_blocked_battery,
    ),
}


def print_scenarios() -> None:
    print("Available scenarios:\n")
    for name, (desc, rule, observable, _fn) in SCENARIOS.items():
        print(f"  {name}")
        print(f"      what:       {desc}")
        print(f"      trips:      {rule}")
        print(f"      observe:    {observable}\n")


WATCH_NOTE = """
Where to watch the agent react
------------------------------
  * agent logs:   docker compose logs -f agent
                  look for: agent.signal_received -> agent.decision (-> agent.action)
  * Jaeger:       http://localhost:8080/jaeger/   service=agent
                  decision audit trace: agent.signal_received -> agent.decision -> agent.action
                  span attrs: decision.action, decision.objective_served, decision.reason,
                              decision.blocked / decision.block_reason
  * Prometheus:   http://localhost:8080/prometheus/
                  agent_evaluations_total (#1053), game_interventions_total{type,objective,blocked}
  * Grafana:      http://localhost:8080/grafana/   (agent / experiments dashboards)

Prerequisites for the agent to ACT (see docs/agent-act-runbook.md)
------------------------------------------------------------------
  The agent only DECIDES (and traces) unless both gates are open:
    1. agent.json `interventions_enabled` = on (flagd flag, #1213; re-read at
       use-time, no agent restart)                               — real sink
    2. agent.json `enabled` = on   (flag flip, hot-reload)        — kill switch
  And the permission layer (`interventions_allowed`) must include the rule's
  intervention. To SEE a *blocked* decision, run --scenario blocked_battery with the
  stock `ambient` allow-list. Even with the gates CLOSED, the OBSERVE/DECIDE pipeline
  still emits agent.signal_received -> agent.decision spans + agent_evaluations_total —
  enough to confirm the driver tripped a rule. LLM inference being unreachable (#1059)
  is fine: the decision loop runs in RULES mode, which is exactly what this driver targets.
"""


def run_once(s: Stack, args: argparse.Namespace) -> None:
    desc, rule, observable, fn = SCENARIOS[args.scenario]
    log(f"scenario={args.scenario} players={args.players} duration={args.duration}s mode={args.game_mode}")
    log(f"  designed to trip: {rule}")
    log(f"  observe:          {observable}")

    serials = add_controllers(s, args.players)
    try:
        game_id = start_real_game(s, serials, args.game_mode, args.start_timeout)
        log("driving movement pattern — watch the agent (see the WATCH section at the end)")
        fn(s, serials, args.duration)
        log(f"scenario beats complete for game_id={game_id}")
        if not args.keep_game:
            try:
                s.coord.ForceEndGame(
                    gcpb.ForceEndGameRequest(reason="demo_driver_scenario_done")
                )
                log("force-ended the demo game")
            except grpc.RpcError as exc:
                log(f"force-end failed: {exc}")
    finally:
        if not args.keep_controllers:
            remove_demo_controllers(s)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=WATCH_NOTE,
    )
    parser.add_argument("--scenario", default="skill_gap", help="scenario to play (see --list)")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--players", type=int, default=4, help="number of mock controllers (default 4)")
    parser.add_argument("--duration", type=float, default=30.0, help="seconds to drive the pattern (default 30)")
    parser.add_argument("--game-mode", default="JoustFFA", help="game mode to start (default JoustFFA)")
    parser.add_argument("--loop", action="store_true", help="replay the scenario until interrupted")
    parser.add_argument("--host", default="localhost", help="stack host (default localhost)")
    parser.add_argument("--mock-port", type=int, default=50062, help="mock control API port (default 50062)")
    parser.add_argument("--menu-port", type=int, default=50054, help="menu gRPC port (default 50054)")
    parser.add_argument("--coord-port", type=int, default=50053, help="game coordinator gRPC port (default 50053)")
    parser.add_argument("--start-timeout", type=float, default=30.0, help="game-start timeout seconds (default 30)")
    parser.add_argument("--keep-game", action="store_true", help="do not force-end the game when done")
    parser.add_argument("--keep-controllers", action="store_true", help="do not remove demo controllers when done")
    args = parser.parse_args()

    if args.list:
        print_scenarios()
        return 0
    if args.scenario not in SCENARIOS:
        print(f"unknown scenario {args.scenario!r}\n", file=sys.stderr)
        print_scenarios()
        return 2

    s = Stack(args.host, args.mock_port, args.menu_port, args.coord_port)
    try:
        if args.loop:
            log("loop mode — Ctrl-C to stop")
            while True:
                run_once(s, args)
        else:
            run_once(s, args)
    except KeyboardInterrupt:
        log("interrupted — cleaning up")
        if not args.keep_controllers:
            remove_demo_controllers(s)
    finally:
        s.close()
    print(WATCH_NOTE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
