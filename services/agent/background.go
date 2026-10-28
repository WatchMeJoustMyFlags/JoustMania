package main

import "sync"

// backgroundGroup tracks fire-and-forget goroutines that main() starts outside
// run() (the game-end proposer, #2323) so run()'s shutdown path can join them
// before main's deferred tracer and metrics shutdown flushes. Without the join, a
// proposer still running at shutdown could end its spans or record metrics after
// the exporters were shut down.
//
// Close is final: Go after Close starts nothing, so a late game-end hook cannot
// race the join (sync.WaitGroup forbids an Add that races a Wait from zero). The
// goroutines must be bounded by the root context, which is already cancelled when
// Close runs, so the join returns promptly. A nil group runs nothing and joins
// nothing.
type backgroundGroup struct {
	mu     sync.Mutex
	closed bool
	wg     sync.WaitGroup
}

// Go runs f in a tracked goroutine and reports whether it started (false after Close).
func (g *backgroundGroup) Go(f func()) bool {
	if g == nil {
		return false
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.closed {
		return false
	}
	g.wg.Go(f)
	return true
}

// Close stops new goroutines from starting and waits for the running ones.
func (g *backgroundGroup) Close() {
	if g == nil {
		return
	}
	g.mu.Lock()
	g.closed = true
	g.mu.Unlock()
	g.wg.Wait()
}
