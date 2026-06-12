package gamewindow

import (
	"strconv"
	"sync"
	"testing"

	"github.com/joustmania/agent/gamesummary"
)

// sum builds a small distinguishable Summary for ordering assertions: GameID is
// the only field the tests need to tell summaries apart.
func sum(id string) gamesummary.Summary {
	return gamesummary.Summary{GameID: id}
}

// ids extracts the GameIDs of a summary slice so order/content is asserted
// compactly.
func ids(ss []gamesummary.Summary) []string {
	out := make([]string, len(ss))
	for i, s := range ss {
		out[i] = s.GameID
	}
	return out
}

func equalIDs(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestRecordAppendsOldestFirst(t *testing.T) {
	s := NewStore()
	if s.Len() != 0 {
		t.Fatalf("fresh store Len = %d, want 0", s.Len())
	}
	for _, id := range []string{"g1", "g2", "g3"} {
		s.Record(sum(id))
	}
	if s.Len() != 3 {
		t.Fatalf("Len = %d, want 3", s.Len())
	}
	// Recent(all) returns oldest-first (arrival order), newest last.
	got := ids(s.Recent(3))
	if want := []string{"g1", "g2", "g3"}; !equalIDs(got, want) {
		t.Errorf("Recent(3) = %v, want %v (oldest-first)", got, want)
	}
}

func TestRecentReturnsLastNNewestLast(t *testing.T) {
	s := NewStore()
	for _, id := range []string{"g1", "g2", "g3", "g4"} {
		s.Record(sum(id))
	}
	got := ids(s.Recent(2))
	if want := []string{"g3", "g4"}; !equalIDs(got, want) {
		t.Errorf("Recent(2) = %v, want %v (the 2 newest, newest last)", got, want)
	}
}

func TestRecentReturnsFreshCopy(t *testing.T) {
	s := NewStore()
	s.Record(sum("g1"))
	s.Record(sum("g2"))

	out := s.Recent(2)
	// Mutating the returned slice must NOT affect the store.
	out[0].GameID = "MUTATED"
	out[1] = sum("ALSO_MUTATED")

	again := ids(s.Recent(2))
	if want := []string{"g1", "g2"}; !equalIDs(again, want) {
		t.Errorf("store leaked its backing array: Recent(2) = %v, want %v", again, want)
	}
}

func TestEvictionPastRetentionCap(t *testing.T) {
	s := NewStore()
	// Record one past the cap: the oldest must be evicted and the length capped.
	for i := 0; i < RetentionCap+1; i++ {
		s.Record(sum("g" + strconv.Itoa(i)))
	}
	if s.Len() != RetentionCap {
		t.Fatalf("Len after %d records = %d, want %d (capped)", RetentionCap+1, s.Len(), RetentionCap)
	}
	all := ids(s.Recent(RetentionCap))
	// Oldest ("g0") evicted; window holds g1..gRetentionCap, oldest-first.
	if all[0] != "g1" {
		t.Errorf("oldest retained = %q, want g1 (g0 evicted)", all[0])
	}
	if last := all[len(all)-1]; last != "g"+strconv.Itoa(RetentionCap) {
		t.Errorf("newest retained = %q, want g%d", last, RetentionCap)
	}
}

func TestEvictionKeepsEvictingAsMoreArrive(t *testing.T) {
	s := NewStore()
	for i := 0; i < RetentionCap*2; i++ {
		s.Record(sum("g" + strconv.Itoa(i)))
	}
	if s.Len() != RetentionCap {
		t.Fatalf("Len = %d, want %d", s.Len(), RetentionCap)
	}
	all := ids(s.Recent(RetentionCap))
	// Window holds the last RetentionCap: g(RetentionCap)..g(2*RetentionCap-1).
	if all[0] != "g"+strconv.Itoa(RetentionCap) {
		t.Errorf("oldest retained = %q, want g%d", all[0], RetentionCap)
	}
}

func TestRecentZeroAndNegativeEmptyNonNil(t *testing.T) {
	s := NewStore()
	s.Record(sum("g1"))
	for _, n := range []int{0, -1, -100} {
		got := s.Recent(n)
		if got == nil {
			t.Errorf("Recent(%d) returned nil, want empty non-nil slice", n)
		}
		if len(got) != 0 {
			t.Errorf("Recent(%d) len = %d, want 0", n, len(got))
		}
	}
}

func TestRecentMoreThanLenReturnsAll(t *testing.T) {
	s := NewStore()
	s.Record(sum("g1"))
	s.Record(sum("g2"))
	got := ids(s.Recent(50))
	if want := []string{"g1", "g2"}; !equalIDs(got, want) {
		t.Errorf("Recent(50) = %v, want all held %v", got, want)
	}
}

func TestRecentOnEmptyStore(t *testing.T) {
	s := NewStore()
	got := s.Recent(3)
	if got == nil {
		t.Fatalf("Recent on empty store returned nil, want empty non-nil")
	}
	if len(got) != 0 {
		t.Errorf("Recent on empty store len = %d, want 0", len(got))
	}
}

// TestConcurrentRecordAndRecent exercises the mutex under -race: many goroutines
// Record while others read Recent/Len. The window is GLOBAL (one Store shared
// across concurrent games), so this is the production access pattern.
func TestConcurrentRecordAndRecent(t *testing.T) {
	s := NewStore()
	var wg sync.WaitGroup
	const writers, readers, iters = 4, 4, 200

	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < iters; i++ {
				s.Record(sum("w" + strconv.Itoa(w) + "_" + strconv.Itoa(i)))
			}
		}(w)
	}
	for r := 0; r < readers; r++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < iters; i++ {
				_ = s.Recent(5)
				_ = s.Len()
			}
		}()
	}
	wg.Wait()

	if s.Len() != RetentionCap {
		t.Fatalf("after %d records Len = %d, want %d (capped)", writers*iters, s.Len(), RetentionCap)
	}
}
