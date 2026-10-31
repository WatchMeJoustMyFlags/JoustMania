# Vision

> AIUP stage 1 of 5: **vision** → [requirements](requirements.md) → [entity model](entity_model.md) →
> [use cases](use_cases.puml) → [technical specs](specs/). Start at the [docs index](README.md).

## 1. Product in one paragraph

JoustMania is a party game for a room full of people holding PlayStation Move
motion controllers. Every player tries to jostle everyone else's controller while
keeping their own still enough to survive; move too fast and you are out. The
music sets the pace: when it speeds up, the allowed movement goes up too. This
repository is a cloud-native rebuild of the
[original JoustMania](https://github.com/adangert/JoustMania): a set of gRPC
microservices running in Docker on a Raspberry Pi, instrumented end to end with
OpenTelemetry, configured through OpenFeature flags (flagd), and steered by an
**agent** that observes the game only through telemetry and changes it only
through flags.

## 2. Who it is for

| Stakeholder | What they want from JoustMania |
|---|---|
| **Player** | Pair a controller, join the lobby, play a fair, fast, loud game with friends without touching a screen. |
| **Game host / admin** | Pick the game mode and calibrate it (sensitivity, number of teams, force-all-start, and the agent's allowed interventions) from a controller, start games, and pull the plug on the agent from the device if needed. |
| **Operator** | Run the stack on a Pi (or a laptop with mock controllers), see what is happening in traces, metrics, logs and dashboards, and roll infrastructure changes forward and back safely. |
| **Presenter** | Demonstrate "Two Standards Walk Into an Agent" live: flip flags on stage and show the effect in the game and in Jaeger within seconds, with a safe fallback when the venue WiFi fails. |
| **Agent** (system actor) | Observe games through OTel, decide bounded in-game interventions, run shadow experiments, and propose or promote improvements, all through OpenFeature flags. |
| **Maintainer / contributor** | Change the system with a spec that says what it does, and code and tests that follow the spec. |

## 3. Product vision: the party game

- **Zero-screen play.** Everything a player or host needs happens on the
  controllers: LED colour and rumble are the UI, voice and music are the
  feedback. Pairing is plug-in-USB-once, then Bluetooth.
- **Many ways to play.** Ten game modes are implemented today: Joust
  Free-for-All, Joust Teams, Joust Random Teams, Traitor, Werewolf, Zombies,
  Swapper, Fight Club, Tournament and Non-Stop Joust (see
  [UC-020 to UC-026](use_cases.puml)). Commander, Ninja Bomb and Random exist only
  as names in the game enum and are not playable (see
  [specs-inventory.md](specs-inventory.md#4-discrepancies-found)).
- **Fair and responsive.** Motion is sampled fast enough that a death feels
  caused by the player, not by the system (see NFR-001 to NFR-004 in
  [requirements.md](requirements.md)).
- **The host stays in charge of intent.** The human admin decides what is
  played and how it is calibrated. The agent acts within the bounds and toward
  the goals the host sets, and is free to find its own moves inside them. This
  split is the [ownership model](OWNERSHIP_MODEL.md).
- **Runs at a party.** One Raspberry Pi, Bluetooth controllers, a speaker, no
  internet required to play. Hosted inference is optional; the rules engine is
  the offline floor.

## 4. Product vision: the self-improving system (northstar)

The long-term direction is the **dark factory**: JoustMania improves itself with
no human in the loop. The authoritative statement of that goal, its loop
(Observe → Experiment → Promote), its authority tiers and its hard constraints is
**[NORTHSTAR.md](NORTHSTAR.md)**.

**Why NORTHSTAR.md stays a separate file instead of becoming a section here.**
It is a short, deliberately opinionated document that agent work is required to
read first (it is linked from `CLAUDE.md`), the talk runbook links to it (PR #1256), and it changes at a different
rhythm from the product vision: the northstar moves when the maintainer re-scopes
the agent; this vision moves when the product changes. Folding it in would break
those links and mix two change rhythms. This document therefore *summarises and
links*; where they differ, NORTHSTAR.md wins for agent direction and
[requirements.md](requirements.md) wins for what is built.

Summary of the northstar, as traced into requirements:

| Northstar element | Where it lands |
|---|---|
| Observe only through OpenTelemetry | C-001, FR-040 to FR-043, UC-041 |
| Change only through OpenFeature flags | C-002, FR-018, FR-045, FR-050, FR-051, UC-044, UC-045 |
| Tier 1: bounded in-game interventions apply directly | FR-044 to FR-053, UC-040 |
| Tier 2: persistent tuning only after shadow validation | C-003, FR-060 to FR-066, UC-043 |
| Tier 3: compile-step changes are proposals reviewed by a human | C-007, FR-055, FR-068, UC-043 |
| Degrade, never stall | C-004, NFR-010 to NFR-013 |
| No control plane beyond OTel and OpenFeature | C-005 |
| No local or offline inference runtime (not a goal right now) | C-006 |

"Where we are today" in NORTHSTAR.md lists the gap between the northstar and
`main`. The requirements catalog marks every planned item with its issue number
and status `Open` / `In Progress`; nothing planned is written as shipped.

## 5. The talk: "Two Standards Walk Into an Agent"

JoustMania is also the live demo for a conference talk whose thesis is that two
open standards are enough to build, observe and govern an autonomous agent:

- **OpenTelemetry is the agent's nervous system.** The agent sees the game only
  as spans and metrics, and every agent decision is itself a span (the trace is
  the audit log).
- **OpenFeature is the agent's will.** Every change the agent makes is a flag
  change, and every limit on the agent (on/off, which backend, which
  interventions are allowed, kill switch) is a flag the presenter can flip on
  stage.

Decision backends have fixed roles: Jev decides in game, Claude reviews and proposes after the game and is
the manual in-game backup, and the rules engine is the automatic floor. The canonical statement is
[C-018](requirements.md#constraints).

The demo arc (issue #743; runbook: [docs/talk/two-standards-runbook.md](talk/two-standards-runbook.md), PR #1256): observe a live game → switch the decision
backend live (Jev as the main in-game backend, Claude as backup, OpenAI-compatible, rules) → narrow permissions and
hit the kill switch → optional infrastructure rollout and automatic rollback →
pull the network and show the rules fallback. See [UC-070](use_cases/UC-070.md).

## 6. Principles for how we build it

1. **Spec first.** Behaviour is specified in [requirements.md](requirements.md),
   [use cases](use_cases.puml) and [technical specs](specs/) before or with the
   code that implements it. See [CONTRIBUTING.md](CONTRIBUTING.md#spec-driven-development-aiup).
2. **The spec states what the code does.** Plans carry an issue number and an
   `Open` status. Disagreements between docs and code are logged in
   [specs-inventory.md](specs-inventory.md#4-discrepancies-found) and fixed in code
   or spec, never papered over.
3. **Everything observable, everything reversible.** New behaviour ships with
   spans or metrics ([telemetry spec](specs/telemetry.md)) and, where it is
   tunable, a flag ([flag catalog](specs/flags.md)).
4. **Decisions are checked against their expected result.** Our own decisions are recorded in the use cases and requirements (with their issue); the agent's decisions carry a prediction that is scored after the game (#2479).

## 7. Out of scope

- Player identity or profiles across sessions: a controller is not a person
  (decision recorded when issue #23 was closed).
- Local or offline LLM inference (see NORTHSTAR.md "Not a goal right now").
- A bespoke control API for the agent beyond flags, the coordinator / mock
  controller APIs for shadow games, and tier 3 issues or PRs.
