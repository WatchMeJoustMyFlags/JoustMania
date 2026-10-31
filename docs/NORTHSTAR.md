# Northstar: the dark factory

JoustMania improves itself with no human in the loop, the way a lights-out
factory runs without workers on the floor.

> This describes where the system is heading, not a snapshot of today. Where
> the current code still differs, see "Where we are today" below.

## The loop: Observe → Experiment → Promote

- **Observe.** The agent only ever sees the game through OpenTelemetry (spans
  and metrics), never by reading code.
- **Experiment.** Any persistent change it considers is tested in shadow games
  with mock controllers, so real players never touch an untested variant.
- **Promote.** Whatever survives is promoted by flipping an OpenFeature flag,
  which makes every change observable and reversible.

The same two standards govern the agent itself: **OTel is its nervous system,
OpenFeature is its will**, and there is deliberately no bespoke control plane
anywhere.

## Authority is tiered by risk

| Tier | Change | Agent authority |
|------|--------|-----------------|
| 1 | In-game interventions: bounded, scoped to the current game, reversible | Applies directly, as a flag flip |
| 2 | Persistent tuning (game flags, thresholds, weights) and interpreted game logic | Applies once shadow validation passes |
| 3 | Anything needing a compile step | Proposes only (issue or PR); a human reviews |

The agent observes only through OTel, but it may *write* code proposals at tier 3.

## We enable; the agent invents

Humans set the bounds (C-034, kill switch, allow-lists) and the goals (objectives, fitness). They do not script the agent's tactics. When we want new behaviour, we give the agent:
- **a capability** it can express through the decide contract;
- **a neutral description** of what each lever does, never what it is for;
- **a goal** the behaviour could serve.

Then we measure whether it finds the move on its own, in shadow experiments where possible and on hardware where players matter. A hard-coded rule is only a labelled, opt-in fallback for when no LLM is available. Unprompted, explainable moves within the bounds are what we want to see. Every agent decision states what it expects to happen and is scored against what did happen, so its creativity stays accountable (planned: #2479).

## Data flywheel

1. **Shadow games** are the main data source for experiments.
2. **Real game spans calibrate the mocks** and anchor experiment verdicts
   against recent real games.
3. **Live outcomes become ground truth**, with automatic rollback on
   degradation.

## Hard constraints

These are what make this a northstar rather than a feature list:

- **OTel only.** The agent's view of the game is spans and metrics, nothing else,
  except `StreamGameEvents` (lifecycle of the agent's own shadow games) and
  `ListGames` / `ListMockControllers` (controller hygiene), the named exceptions
  under "No control plane" below.
- **Flags only.** Every change the agent makes to the game is an OpenFeature
  flag change.
- **Shadow gate always for persistent changes.** Tier 2 changes never reach
  real players unvalidated. Tier 1 interventions apply directly, because they
  are scoped to one game and are observable and reversible flag flips.
- **Degrade, never stall.** Hosted inference is allowed. When it is slow or
  unreachable (bad event WiFi included), the rules engine decides, and the game
  keeps playing.
- **No control plane beyond OTel and OpenFeature.** The only other outbound
  paths are named exceptions: driving shadow games through the coordinator
  and mock-controller APIs, and tier 3 proposals as issues or PRs. Inference
  backends are dependencies behind a single decision contract, selected by a
  flag, not a control plane.

## Not a goal right now

A fully local or offline inference runtime. Local models are too slow for
in-game decisions and setting them up is not worth the time today. The rules
engine is the offline floor.

## Where we are today

- **Promotion** flips the real default flag only in the gated `autonomous`
  mode. By default (`code_improvement.mode`: `issue`, then `pr`) a human
  reviews the promotion, once `AGENT_CODE_IMPROVEMENT_ENABLED` / `GITHUB_*` are
  set (D-A6); the stock stack only records them.
- **Live rollback** exists in gated form: autonomous promotion watches real
  games and reverts on degradation.
- **Decision backends** are selected by the `decision.backend` flag (epic #1225):
  `rules` by default, `claude` / `openai` / `jev` called directly, rules on any
  failure. The post-game retro and the experiment proposer call Claude directly
  when the `inference.backend` flag is `claude` (#1810). There is no gateway (#1234).
- **Talk demo.** The live demo of that contract (observe, model switch,
  permissions, rollback, degrade to rules) is scripted in
  [talk/two-standards-runbook.md](talk/two-standards-runbook.md).
