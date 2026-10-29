package telemetry

import (
	"sync"
	"time"

	"go.opentelemetry.io/otel/trace"
)

// Links remembers the span context of the write that produced a snapshot
// version, so a flagservice.sync.push span can link to it (telemetry.md §10.1).
type Links struct {
	mu    sync.Mutex
	order []uint64
	byVer map[uint64]LinkEntry
	// inflight counts writes between their commit and their Remember: a sync
	// push that finds no link while one is in flight waits for it (lookup).
	inflight int
}

// linkWait bounds how long a push waits for an in-flight write's link.
const linkWait = 50 * time.Millisecond

// LinkEntry is the write span and time behind a snapshot version.
type LinkEntry struct {
	SC trace.SpanContext
	At time.Time
}

const maxLinks = 128

// Remember records that version was produced by the write span sc at time at.
func (l *Links) Remember(version uint64, sc trace.SpanContext, at time.Time) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.byVer == nil {
		l.byVer = map[uint64]LinkEntry{}
	}
	l.byVer[version] = LinkEntry{SC: sc, At: at}
	l.order = append(l.order, version)
	if len(l.order) > maxLinks {
		delete(l.byVer, l.order[0])
		l.order = l.order[1:]
	}
}

// Begin marks a write as in flight; the write calls End after Remember (or
// when it produced no version). Without it a sync push can run between the
// commit and Remember and lose the trace link.
func (l *Links) Begin() {
	l.mu.Lock()
	l.inflight++
	l.mu.Unlock()
}

// End marks the write started with Begin as finished.
func (l *Links) End() {
	l.mu.Lock()
	if l.inflight > 0 {
		l.inflight--
	}
	l.mu.Unlock()
}

// Inflight is the number of writes between Begin and End.
func (l *Links) Inflight() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.inflight
}

// Lookup returns the link of version. When none is known and a write is in
// flight it waits (at most linkWait) for that write to finish, since the write
// that produced version may be the one still to Remember it.
func (l *Links) Lookup(version uint64) (LinkEntry, bool) {
	deadline := time.Now().Add(linkWait)
	for {
		l.mu.Lock()
		e, ok := l.byVer[version]
		busy := l.inflight > 0
		l.mu.Unlock()
		if ok || !busy || !time.Now().Before(deadline) {
			return e, ok
		}
		time.Sleep(200 * time.Microsecond)
	}
}
