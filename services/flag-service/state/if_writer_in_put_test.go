package state

import (
	"net/http"
	"strings"
	"sync"
	"testing"

	"github.com/joustmania/flag-service/fserr"
)

// if_writer_in_put_test.go covers the write precondition of a real overlay
// PUT (#2260, UC-043 A19): ?if_writer_in=promotion, writes only over the
// promotion's own overlay or none, else 412 writer_mismatch.

func ifWriterInPut(writer string, allowed ...string) WriteRequest {
	return WriteRequest{Set: "game", Flag: "sensitivity", Variant: "fast", Scope: realScope, Writer: writer, IfWriterIn: allowed}
}

// UC: UC-043 A19
func TestIfWriterInPut(t *testing.T) {
	t.Run("UC-043/A19 no overlay present: the empty entry matches and the write lands", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		if _, err := f.state.Put(ifWriterInPut("promotion", "promotion", "")); err != nil {
			t.Fatalf("put: %v", err)
		}
		if w, ok := f.realWriter("sensitivity"); !ok || w != "promotion" {
			t.Fatalf("writer %q present %v, want promotion", w, ok)
		}
	})

	t.Run("UC-043/A19 the promotion's own overlay matches and is replaced", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(ifWriterInPut("promotion"))
		if _, err := f.state.Put(ifWriterInPut("promotion", "promotion", "")); err != nil {
			t.Fatalf("put: %v", err)
		}
	})

	t.Run("UC-043/A19 another writer's overlay is refused 412 writer_mismatch and left in place", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(ifWriterInPut("operator"))
		v0 := f.state.Version()
		_, err := f.state.Put(ifWriterInPut("promotion", "promotion", ""))
		wantReason(t, err, http.StatusPreconditionFailed, fserr.WriterMismatch)
		if w, _ := f.realWriter("sensitivity"); w != "operator" {
			t.Fatalf("writer %q after a refused PUT, want operator", w)
		}
		if f.state.Version() != v0 {
			t.Fatal("a refused PUT bumped the snapshot version")
		}
	})

	t.Run("UC-043/A19 an overlay without a writer label matches no entry, not even the empty one", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(ifWriterInPut(""))
		_, err := f.state.Put(ifWriterInPut("promotion", "promotion", ""))
		wantReason(t, err, http.StatusPreconditionFailed, fserr.WriterMismatch)
	})

	t.Run("UC-043/A19 a list without the empty entry refuses a write where no overlay is present", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		_, err := f.state.Put(ifWriterInPut("promotion", "promotion"))
		wantReason(t, err, http.StatusPreconditionFailed, fserr.WriterMismatch)
		if _, ok := f.realWriter("sensitivity"); ok {
			t.Fatal("a refused PUT wrote an overlay")
		}
	})

	t.Run("UC-043/A19 a dry run evaluates the precondition too", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(ifWriterInPut("operator"))
		req := ifWriterInPut("promotion", "promotion", "")
		req.DryRun = true
		_, err := f.state.Put(req)
		wantReason(t, err, http.StatusPreconditionFailed, fserr.WriterMismatch)
	})

	t.Run("UC-043/A19 without the precondition a PUT writes over any overlay", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(ifWriterInPut("operator"))
		f.put(ifWriterInPut("promotion"))
		if w, _ := f.realWriter("sensitivity"); w != "promotion" {
			t.Fatalf("writer %q, want promotion", w)
		}
	})

	t.Run("UC-043/A19 outside scope real, over 16 entries or an entry over the id limit is 400 malformed", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		stack := ifWriterInPut("promotion", "promotion", "")
		stack.Scope = Scope{}
		_, err := f.state.Put(stack)
		wantReason(t, err, http.StatusBadRequest, fserr.Malformed)
		_, err = f.state.Put(ifWriterInPut("promotion", make([]string, MaxIfWriterIn+1)...))
		wantReason(t, err, http.StatusBadRequest, fserr.Malformed)
		_, err = f.state.Put(ifWriterInPut("promotion", strings.Repeat("x", MaxIDLength+1)))
		wantReason(t, err, http.StatusBadRequest, fserr.Malformed)
	})

	t.Run("UC-043/A19 409 real_game_live on a hold key takes precedence over 412", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(WriteRequest{Set: "game", Flag: "shield.energy_budget_seconds", Value: raw("20"), Scope: realScope, Writer: "operator"})
		f.registerKind("g-real", "real")
		req := WriteRequest{Set: "game", Flag: "shield.energy_budget_seconds", Value: raw("21"), Scope: realScope, Writer: "promotion", IfWriterIn: []string{"promotion", ""}}
		_, err := f.state.Put(req)
		wantReason(t, err, http.StatusConflict, fserr.RealGameLive)
		f.state.EndGame("g-real", EndReasonEnded)
		_, err = f.state.Put(req)
		wantReason(t, err, http.StatusPreconditionFailed, fserr.WriterMismatch)
		if w, _ := f.realWriter("shield.energy_budget_seconds"); w != "operator" {
			t.Fatalf("writer %q after two refused PUTs, want operator", w)
		}
	})
}

// UC: UC-043 A19
func TestIfWriterInPutRaceFree(t *testing.T) {
	t.Run("UC-043/A19 concurrent conditional and unconditional PUTs are race-free (run with -race), fail only with writer_mismatch, and the precondition still holds afterwards", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		const rounds = 300
		var wg sync.WaitGroup
		wg.Add(2)
		go func() {
			defer wg.Done()
			for i := 0; i < rounds; i++ {
				// The other writer alternates between setting and clearing its overlay.
				if i%2 == 0 {
					if _, err := f.state.Put(ifWriterInPut("operator")); err != nil {
						t.Errorf("put: %v", err)
						return
					}
				} else if _, err := f.state.Clear(ClearRequest{Set: "game", Flag: "sensitivity", Scope: realScope}); err != nil {
					t.Errorf("clear: %v", err)
					return
				}
			}
		}()
		errs := make(chan string, rounds)
		go func() {
			defer wg.Done()
			for i := 0; i < rounds; i++ {
				_, err := f.state.Put(ifWriterInPut("promotion", "promotion", ""))
				if err != nil && err.Reason != fserr.WriterMismatch {
					errs <- err.Error()
				}
			}
		}()
		wg.Wait()
		close(errs)
		for e := range errs {
			t.Errorf("unexpected rejection: %s", e)
		}
		f.put(ifWriterInPut("operator"))
		if _, err := f.state.Put(ifWriterInPut("promotion", "promotion", "")); err == nil || err.Reason != fserr.WriterMismatch {
			t.Fatalf("final put: %v, want writer_mismatch", err)
		}
	})
}
