package decision

import (
	"context"
	"sync"

	"github.com/joustmania/agent/flags"
)

// BluetoothFitnessSource provides the infrastructure (Bluetooth) fitness
// thresholds (the fitness.bluetooth.* flags, #735). It is the infra-domain
// parallel to FitnessSource and is deliberately SEPARATE from it: game-objective
// and Bluetooth thresholds are distinct concerns with distinct flags, so the
// game FitnessThresholds struct is not extended.
//
// The source is re-read on every evaluation, so changing a fitness.bluetooth.*
// flag takes effect on the next infra evaluation with no restart.
type BluetoothFitnessSource interface {
	BluetoothThresholds() BluetoothThresholds
}

// LiveBluetoothFitness is a goroutine-safe BluetoothFitnessSource the infra loop
// refreshes each cycle from the evaluated fitness.bluetooth.* flags. Until the
// loop publishes the first value it serves the flagd-schema defaults, so the
// evaluator is never thresholdless.
type LiveBluetoothFitness struct {
	mu        sync.RWMutex
	threshold BluetoothThresholds
}

// NewLiveBluetoothFitness builds a source seeded with the flagd-schema default
// thresholds (50 / 0.02 / 10).
func NewLiveBluetoothFitness() *LiveBluetoothFitness {
	return &LiveBluetoothFitness{threshold: DefaultBluetoothThresholds()}
}

// Set replaces the published Bluetooth fitness thresholds.
func (f *LiveBluetoothFitness) Set(t BluetoothThresholds) {
	f.mu.Lock()
	f.threshold = t
	f.mu.Unlock()
}

// BluetoothThresholds implements BluetoothFitnessSource, returning the current
// thresholds.
func (f *LiveBluetoothFitness) BluetoothThresholds() BluetoothThresholds {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.threshold
}

// bluetoothFitnessEvaluator is the narrow flags seam FlagBluetoothFitness reads
// each infra cycle: ONLY the three fitness.bluetooth.* thresholds (not the full
// four-layer agent Snapshot the game loop evaluates). *flags.Flags satisfies it
// via its BluetoothFitness accessor; tests supply a fake. Keeping the seam this
// narrow means the ~1Hz infra path never pays for the unrelated game/agent flag
// layers, and the source can be unit-tested without a live flagd.
type bluetoothFitnessEvaluator interface {
	BluetoothFitness(ctx context.Context) flags.BluetoothFitness
}

// FlagBluetoothFitness is a BluetoothFitnessSource backed DIRECTLY by the flags
// client (#735): it re-evaluates the fitness.bluetooth.* flags on EVERY infra
// observe cycle, so a threshold flipped on stage takes effect on the very next
// evaluation with no restart — and it does so in the lobby too, independent of
// whether a GAME is active (the game decision loop only publishes thresholds
// while a session runs, so it cannot keep the infra loop live on its own).
//
// This is the infra-domain parallel to how the game loop threads live
// fitness.* thresholds (flags.Evaluate per cycle → Loop.SetFitness → LiveFitness),
// collapsed into one read-through source because the infra loop already reads
// its BluetoothFitnessSource every cycle.
//
// On any evaluation error the underlying flags accessor falls back to the
// flagd-schema defaults (last-known-good is the safe default), so a down flagd
// never makes the infra loop thresholdless.
type FlagBluetoothFitness struct {
	flags bluetoothFitnessEvaluator
}

// NewFlagBluetoothFitness builds a flag-backed Bluetooth fitness source over the
// given flags client. A nil client falls back to serving the flagd-schema
// defaults each cycle, so the infra loop is never thresholdless.
func NewFlagBluetoothFitness(f bluetoothFitnessEvaluator) *FlagBluetoothFitness {
	return &FlagBluetoothFitness{flags: f}
}

// BluetoothThresholds implements BluetoothFitnessSource by evaluating the
// fitness.bluetooth.* flags now. The infra loop calls this once per observe
// cycle, so the thresholds are always live.
func (s *FlagBluetoothFitness) BluetoothThresholds() BluetoothThresholds {
	if s.flags == nil {
		return DefaultBluetoothThresholds()
	}
	bf := s.flags.BluetoothFitness(context.Background())
	return BluetoothThresholds{
		MaxEventGapMs:       bf.MaxEventGapMs,
		MaxDroppedEventsPct: bf.MaxDroppedEventsPct,
		MinMovementUpdateHz: bf.MinMovementUpdateHz,
	}
}
