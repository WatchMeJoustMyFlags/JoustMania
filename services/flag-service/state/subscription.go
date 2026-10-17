package state

import "fmt"

// Subscription is one open SyncFlags stream. C receives a value whenever the
// snapshot changed since the stream last read it (latest wins: a slow stream
// skips intermediate versions and sends the newest configuration).
type Subscription struct {
	// SelectorSet is the stream's flag set ("" = every served set).
	SelectorSet string
	C           chan struct{}
	state       *State
}

func (sub *Subscription) wake() {
	select {
	case sub.C <- struct{}{}:
	default: // a wake-up is already pending
	}
}

// Close removes the subscription; the stream no longer counts as open.
func (sub *Subscription) Close() {
	s := sub.state
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.subs, sub)
}

// Subscribe opens a subscription for a selector set ("" = every served set).
// The channel starts with one pending wake-up so the stream sends the current
// configuration first. An unserved selector set is an error.
func (s *State) Subscribe(selectorSet string) (*Subscription, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if selectorSet != "" && !s.served[selectorSet] {
		return nil, fmt.Errorf("flag set %q is not served", selectorSet)
	}
	sub := &Subscription{SelectorSet: selectorSet, C: make(chan struct{}, 1), state: s}
	sub.C <- struct{}{}
	s.subs[sub] = struct{}{}
	return sub, nil
}

// hasStreamForLocked reports whether an open stream would carry set: a write
// flagd cannot see is rejected with 503 no_sync_stream (UC-053 A7).
func (s *State) hasStreamForLocked(set string) bool {
	for sub := range s.subs {
		if sub.SelectorSet == "" || sub.SelectorSet == set {
			return true
		}
	}
	return false
}

// SyncStreams returns the number of open SyncFlags streams.
func (s *State) SyncStreams() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.subs)
}
