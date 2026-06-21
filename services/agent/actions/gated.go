package actions

import (
	"context"
	"log/slog"

	"github.com/joustmania/agent/decision"
)

// InterventionGate resolves the LIVE interventions_enabled safety gate (#1213) at
// USE-TIME. *flags.Flags satisfies it via InterventionsEnabled; tests supply a
// fake. It is read on EVERY Apply (never cached at construction) so flipping the
// flag in flagd starts/stops actuation with no restart, and a flagd outage at the
// moment of an apply FAILS CLOSED (InterventionsEnabled returns the fail-closed
// default false) so the agent never writes interventions.json when it cannot
// confirm the gate is on (#1177/#1217).
type InterventionGate interface {
	InterventionsEnabled(ctx context.Context) bool
}

// RolloutGate resolves the LIVE rollout_enabled safety gate (#1213) at USE-TIME.
// *flags.Flags satisfies it via RolloutEnabled; tests supply a fake. It is read on
// every SetControllerCount and DryRun call (never cached) so flipping the flag in
// flagd starts/stops applying with no restart, and a flagd outage FAILS CLOSED
// (RolloutEnabled returns false ⇒ dry-run) so the agent never flips the rollout
// stage when it cannot confirm the gate is on (#1177/#1217).
type RolloutGate interface {
	RolloutEnabled(ctx context.Context) bool
}

// GatedActionSink wraps the real intervention Writer behind the LIVE
// interventions_enabled flag (#1213). It REPLACES the old startup-time
// AGENT_INTERVENTIONS_ENABLED env gate (which selected the Writer-or-nil sink ONCE
// in main.go and so could never react to a flag flip and failed open to whatever
// sink was chosen at boot). Apply re-evaluates the gate per decision against the
// live Flags: when on it delegates to the real Writer; when off (or flagd
// unreachable) it discards the decision exactly like NoopActions, so a fully-gated,
// decided intervention is still recorded/spanned by the loop but never applied.
type GatedActionSink struct {
	gate   InterventionGate
	writer decision.ActionSink
	log    *slog.Logger
}

// NewGatedActionSink wraps writer behind gate. writer is the real sink applied when
// the gate is on (typically *Writer); gate is the live interventions_enabled
// source. log nil → slog.Default().
func NewGatedActionSink(gate InterventionGate, writer decision.ActionSink, log *slog.Logger) *GatedActionSink {
	if log == nil {
		log = slog.Default()
	}
	return &GatedActionSink{gate: gate, writer: writer, log: log}
}

// Apply evaluates the LIVE interventions_enabled gate (#1213) and delegates to the
// real writer only when it is on; otherwise it is a no-op (the decision was already
// recorded/spanned by the loop). Fail-closed: a flagd error inside the gate
// resolves to false, so the agent does NOT write when the control plane is
// unreachable.
func (s *GatedActionSink) Apply(ctx context.Context, d decision.Decision) error {
	if !s.gate.InterventionsEnabled(ctx) {
		s.log.Debug("agent.intervention_gated_off",
			"intervention", d.Intervention, "flag", keyInterventionsEnabledLog)
		return nil
	}
	return s.writer.Apply(ctx, d)
}

// keyInterventionsEnabledLog is the flag key surfaced in the gated-off log line.
// Mirrors flags.keyInterventionsEnabled (kept local so actions does not need the
// flags key const exported).
const keyInterventionsEnabledLog = "interventions_enabled"

// GatedRolloutActuator wraps the real RolloutWriter behind the LIVE rollout_enabled
// flag (#1213). It REPLACES the old startup-time AGENT_ROLLOUT_ENABLED env gate
// (which selected RolloutWriter-or-DryRunRolloutWriter ONCE in main.go). On every
// SetControllerCount it re-evaluates the gate: when on it delegates the real flip to
// the wrapped RolloutWriter; when off (or flagd unreachable) it is a dry-run — it
// logs the would-be flip and writes nothing. DryRun() likewise reports the LIVE gate
// state, so the loop stamps rollout.dry_run correctly per cycle.
//
// rootCtx is the agent's long-lived context, used to evaluate the gate (rollout
// flips happen on the infra loop's own goroutine, which has no per-decision ctx like
// the ActionSink does). The ladder helpers delegate to the wrapped writer (the
// value↔variant mapping stays single-sourced).
type GatedRolloutActuator struct {
	rootCtx context.Context
	gate    RolloutGate
	writer  *RolloutWriter
	dry     *DryRunRolloutWriter
	log     *slog.Logger
}

// NewGatedRolloutActuator wraps writer behind gate, evaluating against rootCtx. log
// nil → slog.Default().
func NewGatedRolloutActuator(rootCtx context.Context, gate RolloutGate, writer *RolloutWriter, log *slog.Logger) *GatedRolloutActuator {
	if log == nil {
		log = slog.Default()
	}
	return &GatedRolloutActuator{
		rootCtx: rootCtx,
		gate:    gate,
		writer:  writer,
		dry:     NewDryRunRolloutWriter(log),
		log:     log,
	}
}

// SetControllerCount delegates to the real writer when the LIVE rollout_enabled gate
// is on; otherwise it is a dry-run (logs, writes nothing). Fail-closed: a flagd
// error inside the gate resolves to false ⇒ dry-run.
func (a *GatedRolloutActuator) SetControllerCount(variant string) error {
	if !a.gate.RolloutEnabled(a.rootCtx) {
		return a.dry.SetControllerCount(variant)
	}
	return a.writer.SetControllerCount(variant)
}

// NextStage / StageVariantForValue delegate to the wrapped writer so the
// value↔variant ladder mapping is single-sourced regardless of the gate.
func (a *GatedRolloutActuator) NextStage(current int) (variant string, value int, ok bool) {
	return a.writer.NextStage(current)
}

func (a *GatedRolloutActuator) StageVariantForValue(value int) (string, bool) {
	return a.writer.StageVariantForValue(value)
}

// DryRun reports the LIVE gate state: true (rehearsal) when rollout_enabled is off
// or flagd is unreachable, false (applied) when it is on. The loop lifts this onto
// every rollout decision span as rollout.dry_run, so a Jaeger consumer sees the
// CURRENT gate state per cycle, not a boot-time snapshot.
func (a *GatedRolloutActuator) DryRun() bool {
	return !a.gate.RolloutEnabled(a.rootCtx)
}
