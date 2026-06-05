package decision

import "sync"

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
