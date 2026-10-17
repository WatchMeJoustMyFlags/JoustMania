package actions

import (
	"context"
	"fmt"
	"log/slog"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flagservice"
)

// FlagSetRollout is the flag set of the rollout flags.
const FlagSetRollout = "rollout"

// RolloutWriter is the agent's writer of the rollout stage (#734, W2): the
// progressive-rollout controller expands the Bluetooth backend
// controller-by-controller by moving current_controller_count along the stage
// ladder, and PR F rolls it back the same way. Each move is one stack overlay
// write to the flag service, PUT /rollout/current_controller_count
// {"variant": <stage>} (epic #1407, C-032); it is in memory, so a flag-service
// restart returns the rollout to its baseline stage.
type RolloutWriter struct {
	svc FlagService
	log *slog.Logger
}

// flagCurrentControllerCount is the rollout flag whose defaultVariant the agent
// flips to advance/retreat the rollout stage.
const flagCurrentControllerCount = "current_controller_count"

// Rollout stage variant names and their controller-count values. These mirror
// the current_controller_count variants in services/flagd/rollout.json. flagd
// flips by VARIANT NAME (not value), so the agent must map the ladder value it
// reasons about back to the variant name before writing — this mapping is the
// sharp edge the loop depends on.
const (
	StageNoneVariant  = "none"
	StageOneVariant   = "one"
	StageThreeVariant = "three"
	StageSixVariant   = "six"
	StageAllVariant   = "all"

	StageNoneValue  = 0
	StageOneValue   = 1
	StageThreeValue = 3
	StageSixValue   = 6
	StageAllValue   = 99
)

// rolloutLadder is the ordered expansion ladder: none(0) → one(1) → three(3) →
// six(6) → all(99). It is the single source of truth for both NextStage and
// StageVariantForValue, so the value↔variant mapping can never drift.
var rolloutLadder = []struct {
	variant string
	value   int
}{
	{StageNoneVariant, StageNoneValue},
	{StageOneVariant, StageOneValue},
	{StageThreeVariant, StageThreeValue},
	{StageSixVariant, StageSixValue},
	{StageAllVariant, StageAllValue},
}

// NewRolloutWriter builds a RolloutWriter over a flag-service client. log nil
// uses slog.Default().
func NewRolloutWriter(svc FlagService, log *slog.Logger) *RolloutWriter {
	if log == nil {
		log = slog.Default()
	}
	return &RolloutWriter{svc: svc, log: log}
}

// NewRolloutWriterFromEnv builds a RolloutWriter for the flag service at
// FLAG_SERVICE_URL.
func NewRolloutWriterFromEnv(log *slog.Logger) *RolloutWriter {
	return NewRolloutWriter(flagservice.NewFromEnv(), log)
}

// SetControllerCount moves current_controller_count to the given ladder
// variant. An unknown variant is refused before any write.
func (w *RolloutWriter) SetControllerCount(variant string) error {
	known := false
	for _, stage := range rolloutLadder {
		if stage.variant == variant {
			known = true
		}
	}
	if !known {
		return fmt.Errorf("unknown rollout stage variant %q", variant)
	}
	if _, err := w.svc.Put(context.Background(), FlagSetRollout, flagCurrentControllerCount,
		flagservice.Write{Variant: variant, Writer: writerLabel, Reason: "rollout_stage"}); err != nil {
		w.log.Error("agent.rollout_write_failed", "variant", variant, "error", err)
		return err
	}
	w.log.Info("agent.rollout_written", "flag", flagCurrentControllerCount, "variant", variant)
	return nil
}

// NextStage returns the next stage up the expansion ladder from the given
// current controller-count VALUE: the next variant name, its value, and ok=true.
// ok is false at or above the terminal stage (all/99), and also when current
// does not sit on a known ladder value (a malformed/unknown observed count never
// advances). It is the loop's "where do we expand to next?" helper, and pairs
// with StageVariantForValue so the loop always flips by the correct variant
// name.
func NextStage(current int) (variant string, value int, ok bool) {
	for i, stage := range rolloutLadder {
		if stage.value == current {
			if i+1 < len(rolloutLadder) {
				next := rolloutLadder[i+1]
				return next.variant, next.value, true
			}
			return "", 0, false // already at terminal stage (all)
		}
	}
	return "", 0, false // unknown current value: do not advance
}

// StageVariantForValue maps a controller-count VALUE to its ladder variant name
// (e.g. 3 → "three"). ok is false for any value not on the ladder. The loop uses
// it to translate the observed rollout count (a number) into the variant flagd
// flips by.
func StageVariantForValue(value int) (string, bool) {
	for _, stage := range rolloutLadder {
		if stage.value == value {
			return stage.variant, true
		}
	}
	return "", false
}

// NextStage / StageVariantForValue are also exposed as methods so a
// RolloutWriter can satisfy the decision package's actuator seam without that
// package depending on these free functions (actions imports decision, so the
// dependency cannot go the other way). They delegate to the package functions.
func (w *RolloutWriter) NextStage(current int) (variant string, value int, ok bool) {
	return NextStage(current)
}

func (w *RolloutWriter) StageVariantForValue(value int) (string, bool) {
	return StageVariantForValue(value)
}

// rolloutStateService is the read side of the flag service ServedStage needs.
// *flagservice.Client and the fake satisfy it; a FlagService without it makes
// ServedStage an error, so the loop keeps its state (fail safe).
type rolloutStateService interface {
	GetFlag(ctx context.Context, set, flag string) (flagservice.FlagView, error)
	Epoch(ctx context.Context) (string, error)
}

// ServedStage reads what the flag service serves for current_controller_count
// now: the stage, whether it is the agent's own stack overlay, and the boot
// epoch. It satisfies decision.RolloutStageReader, so the InfraLoop drops its
// in-memory rollout state when an operator reset the stage or the flag service
// restarted (#1384). The epoch is read first: a restart between the two reads
// then shows up as an epoch change on the next check (the safe direction).
func (w *RolloutWriter) ServedStage(ctx context.Context) (decision.ServedRolloutStage, error) {
	svc, ok := w.svc.(rolloutStateService)
	if !ok {
		return decision.ServedRolloutStage{}, fmt.Errorf("flag service client cannot read flags")
	}
	epoch, err := svc.Epoch(ctx)
	if err != nil {
		return decision.ServedRolloutStage{}, err
	}
	view, err := svc.GetFlag(ctx, FlagSetRollout, flagCurrentControllerCount)
	if err != nil {
		return decision.ServedRolloutStage{}, err
	}
	served := decision.ServedRolloutStage{
		Epoch:   epoch,
		ByAgent: view.StackOverlay != nil && view.StackOverlay.Writer == writerLabel,
	}
	for _, stage := range rolloutLadder {
		if stage.variant == view.EffectiveDefault {
			served.Value, served.OnLadder = stage.value, true
		}
	}
	return served, nil
}

var _ decision.RolloutStageReader = (*RolloutWriter)(nil)

// DryRun reports false: the real writer applies flips through the flag service. The loop
// records this on the rollout.dry_run span attribute.
func (w *RolloutWriter) DryRun() bool { return false }

// DryRunRolloutWriter satisfies the same actuator seam as RolloutWriter but
// NEVER writes the flag: SetControllerCount logs the would-be flip and
// returns nil. It is the disabled-mode actuator (the LIVE rollout_enabled flag off,
// #1213; GatedRolloutActuator delegates here when the gate is off) — the loop still
// decides and spans the expansion (remediation.action="expand"),
// it just is not applied. The ladder helpers delegate to the real package
// functions, so the value↔variant mapping stays single-sourced.
type DryRunRolloutWriter struct {
	log *slog.Logger
}

// NewDryRunRolloutWriter builds the no-op actuator. log nil → slog.Default().
func NewDryRunRolloutWriter(log *slog.Logger) *DryRunRolloutWriter {
	if log == nil {
		log = slog.Default()
	}
	return &DryRunRolloutWriter{log: log}
}

// SetControllerCount logs the intended flip and returns nil without writing.
func (w *DryRunRolloutWriter) SetControllerCount(variant string) error {
	w.log.Info("agent.rollout_dry_run",
		"flag", flagCurrentControllerCount, "would_set_variant", variant)
	return nil
}

func (w *DryRunRolloutWriter) NextStage(current int) (variant string, value int, ok bool) {
	return NextStage(current)
}

func (w *DryRunRolloutWriter) StageVariantForValue(value int) (string, bool) {
	return StageVariantForValue(value)
}

// DryRun reports true: SetControllerCount never writes the rollout file. The
// loop records this on the rollout.dry_run span attribute so a rehearsed
// expand/rollback is distinguishable from a real one in Jaeger.
func (w *DryRunRolloutWriter) DryRun() bool { return true }
