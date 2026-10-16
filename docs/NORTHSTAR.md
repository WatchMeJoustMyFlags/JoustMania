# Northstar: the dark factory

JoustMania improves itself with no human in the loop, the way a lights-out
factory runs without workers on the floor.

## The loop: Observe → Experiment → Promote

- **Observe.** The agent only ever sees the game through OpenTelemetry (spans
  and metrics), never by reading code.
- **Experiment.** Any idea it forms is tested in shadow games with mock
  controllers, so real players never touch an untested game-logic variant.
- **Promote.** Whatever survives is promoted by flipping an OpenFeature flag,
  which makes every change observable and reversible.

The same two standards govern the agent itself: **OTel is its nervous system,
OpenFeature is its will**, and there is deliberately no bespoke control plane
anywhere.

## Authority is tiered by risk

| Tier | Change | Agent authority |
|------|--------|-----------------|
| 1 | Runtime config: flags, thresholds, weights, in-game interventions | Applies directly (still a flag flip: observable, reversible) |
| 2 | Interpreted game logic | Applies once shadow validation passes |
| 3 | Anything needing a compile step | Proposes only; a human reviews |

## Data flywheel

1. **Today:** shadow games are the only data source.
2. **Later:** real game spans calibrate the mocks.
3. **Eventually:** live outcomes become ground truth, with automatic rollback.

## Hard constraints

These are what make this a northstar rather than a feature list:

- **OTel only.** The agent's view of the game is spans and metrics, nothing else.
- **Flags only.** Every agent action is an OpenFeature flag change.
- **Shadow gate always for game logic.** Tier 2 changes never reach real players
  unvalidated. Tier 1 runtime config applies directly, because it is a flag flip
  that can be observed and reverted.
- **Degrade, never stall.** Hosted inference is allowed. When it is slow or
  unreachable (bad event WiFi included), the rules engine decides, and the game
  keeps playing.
- **No control interfaces beyond OTel and OpenFeature.** Inference backends are
  dependencies behind a single decision contract, selected by a flag. They are
  not a control plane.

## Not a goal right now

A fully local or offline inference runtime. Local models are too slow for
in-game decisions and setting them up is not worth the time today. The rules
engine is the offline floor.
