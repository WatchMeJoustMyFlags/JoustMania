// Package prediction holds the agent's prediction Book (#2479, UC-040 A44,
// BR-034): every registered decision prediction and base-rate control, scored
// exactly once against telemetry (services/agent/characteristics) at its horizon,
// earlier when the outcome is already decided, or at game end.
//
// Observation only: the Book never reaches the permission chain, the allow-list,
// the budgets or the action sink. It emits agent.prediction.outcome spans and the
// agent_prediction* metrics, nothing else.
//
// Lifecycle (the decision/effect.go pattern, #918, #923): one timer per pending
// prediction at registration + horizon + HorizonGrace, through the afterFunc seam,
// waited on by a goroutine tracked on a WaitGroup and bounded by the root context.
// Shutdown stops every timer without scoring; Await joins the goroutines. Pending
// predictions live in memory only: a restart loses them (registered - outcomes
// shows the loss) and a late game end of an unknown game is a no-op.
package prediction

import (
	"context"
	"hash/fnv"
	"sync"
	"time"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/characteristics"
	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamesummary"
)

// HorizonGrace is added to the horizon before the horizon check: one export
// interval plus margin, so an elimination observed at the horizon has arrived.
const HorizonGrace = 3 * time.Second

// Caps (bounded memory, about 200 B per entry).
const (
	// MaxPendingPerGame / MaxPendingTotal bound the pending predictions; over a
	// cap the oldest pending one is scored unscorable overflow.
	MaxPendingPerGame = 32
	MaxPendingTotal   = 256
	// finalizedGames is how many ended games keep their results for lookup.
	finalizedGames = 8
	// maxResultsPerGame bounds the results kept per game for lookup (spans and
	// metrics are emitted for every result).
	maxResultsPerGame = 256
)

// Controls (base rates): every controlInterval of play one control prediction,
// the templates in turn, at most maxControlsPerGame per game.
const (
	controlInterval    = 15 * time.Second
	maxControlsPerGame = 12
	controlP           = 0.5
	// BackendNone is the backend label of a control.
	BackendNone = "none"
)

// controlSpecs are the control templates in turn: survives and activity_rises on
// a hash-chosen alive player.
var controlSpecs = []struct {
	template string
	horizonS int
	targeted bool
}{
	{decide.TemplateSurvives, 15, true},
	{decide.TemplateNoElimination, 15, false},
	{decide.TemplateElimination, 30, false},
	{decide.TemplateActivityRises, 10, true},
	{decide.TemplateClosenessRises, 15, false},
	{decide.TemplateGameEnds, 60, false},
}

// Registration is one prediction to register.
type Registration struct {
	Backend    string
	Prediction decide.Prediction
	// Acted: the decision passed the permission chain and its write was applied.
	// false keeps it as a counterfactual (held, blocked, gated off, discarded).
	Acted bool
	// DecideSpan is the agent.decide span the outcome span links to.
	DecideSpan trace.SpanContext
	// Context is the game's context at decision time (the baseline source).
	Context gamecontext.GameContext
}

// Result is one scored prediction.
type Result struct {
	ID               string
	GameID           string
	GameKind         string
	Backend          string
	Prediction       decide.Prediction
	Acted            bool
	RegisteredAt     time.Time
	ScoredAt         time.Time
	Outcome          string
	UnscorableReason string
	DecidedEarly     bool
	Before, After    *float64
	Eliminations     *int
	EndOffsetS       *float64
	Approx           bool

	decideSC trace.SpanContext
	parent   gameParent
}

// entry is one pending prediction.
type entry struct {
	id       string
	gameID   string
	gameKind string
	backend  string
	pred     decide.Prediction
	acted    bool
	key      string
	base     baseline
	decideSC trace.SpanContext
	parent   gameParent
	resolved bool
	// done is closed when the entry is resolved, so its timer goroutine exits.
	done chan struct{}
}

// gameParent is the game trace ids the outcome span is parented under.
type gameParent struct {
	traceID, rootSpanID, phaseSpanID, cycleSpanID string
}

// gameBook is one running game's pending entries and results.
type gameBook struct {
	pending       []*entry
	results       []Result
	controls      int
	nextControlAt time.Duration
	// controlOffset is where this game's template rotation starts (a hash of the
	// game id), so a template's base rate is not always sampled at the same
	// game times.
	controlOffset int
}

// Book is the process-wide prediction book. The zero value is not usable; use New.
type Book struct {
	mu        sync.Mutex
	games     map[string]*gameBook
	total     int
	finalized map[string][]Result
	order     []string // finalized games, oldest first

	now       func() time.Time
	afterFunc func(d time.Duration, fn func()) (stop func() bool)
	current   func(gameID string) (gamecontext.GameContext, bool)
	root      context.Context
	wg        sync.WaitGroup
	tel       telemetry
}

// New builds a Book. root bounds every timer goroutine (nil = never cancelled);
// current re-reads a game's live context at the horizon (nil = predictions wait
// for game end). Spans and metrics go to the global providers unless SetTelemetry
// overrides them.
func New(root context.Context, current func(gameID string) (gamecontext.GameContext, bool)) *Book {
	if root == nil {
		root = context.Background()
	}
	return &Book{
		games:     map[string]*gameBook{},
		finalized: map[string][]Result{},
		now:       time.Now,
		afterFunc: realAfterFunc,
		current:   current,
		root:      root,
		tel:       defaultTelemetry(),
	}
}

// SetClock overrides the clock (tests). Set before use.
func (b *Book) SetClock(now func() time.Time) { b.now = now }

// SetAfterFunc overrides the horizon timer (tests). Set before use.
func (b *Book) SetAfterFunc(f func(d time.Duration, fn func()) (stop func() bool)) { b.afterFunc = f }

// realAfterFunc is the production timer: a stopped timer never runs fn and leaves
// no goroutine behind.
func realAfterFunc(d time.Duration, fn func()) func() bool {
	return time.AfterFunc(d, fn).Stop
}

// Await blocks until every timer goroutine has returned (shutdown joins it after
// the root context is cancelled; tests use it to make a scoring land).
func (b *Book) Await() { b.wg.Wait() }

// dedupeKey identifies "the same prediction again" within a game.
func dedupeKey(r Registration) string {
	p := r.Prediction
	acted := "0"
	if r.Acted {
		acted = "1"
	}
	return r.Backend + "|" + p.Source + "|" + p.Rule + "|" + p.Template + "|" + p.Target + "|" + acted
}

// Register adds a prediction of the game in r.Context. It returns the
// prediction id; merged is true when an identical prediction (same backend,
// source, rule or lever, template, target and acted) was pending, in which case
// the id is that one's and nothing new is registered. ok is false when nothing
// was registered or merged: an invalid prediction, or a game that already ended.
// A prediction already settled at registration (no target, target not alive) is
// scored at once.
func (b *Book) Register(r Registration) (id string, merged, ok bool) {
	probe := r.Prediction
	if probe.Scope == decide.ScopeTarget && probe.Target == "" {
		probe.Target = "?" // validated as targeted; scored no_target below
	}
	if !decide.ValidPrediction(probe) {
		return "", false, false
	}
	gameID := r.Context.SessionID
	if gameID == "" {
		return "", false, false
	}
	now := b.now()
	key := dedupeKey(r)

	b.mu.Lock()
	if _, ended := b.finalized[gameID]; ended {
		b.mu.Unlock()
		return "", false, false
	}
	gb := b.gameLocked(gameID)
	for _, e := range gb.pending {
		if e.key == key {
			id := e.id
			b.mu.Unlock()
			b.tel.merged(r.Backend, r.Prediction, r.Context.GameKind)
			return id, true, true
		}
	}
	e := &entry{
		id:       uuid.NewString(),
		gameID:   gameID,
		gameKind: r.Context.GameKind,
		backend:  r.Backend,
		pred:     r.Prediction,
		acted:    r.Acted,
		key:      key,
		base:     takeBaseline(r.Prediction, r.Context, now),
		decideSC: r.DecideSpan,
		parent: gameParent{r.Context.GameTraceID, r.Context.GameTraceSpanID,
			r.Context.GameplayPhaseSpanID, r.Context.GameCycleSpanID},
		done: make(chan struct{}),
	}
	var out []Result
	if v := registrationVerdict(r.Prediction, r.Context); v.decided {
		e.resolved = true
		close(e.done)
		res := b.resultLocked(e, v, now, nil)
		out = append(out, res)
	} else {
		out = append(out, b.admitLocked(gb, e, now)...)
	}
	b.mu.Unlock()

	b.tel.registered(r.Backend, r.Prediction, r.Context.GameKind)
	b.emit(out)
	b.schedule(e)
	return e.id, false, true
}

// gameLocked returns gameID's book, creating it.
func (b *Book) gameLocked(gameID string) *gameBook {
	gb := b.games[gameID]
	if gb == nil {
		h := fnv.New32a()
		_, _ = h.Write([]byte(gameID))
		gb = &gameBook{nextControlAt: controlInterval, controlOffset: int(h.Sum32() % uint32(len(controlSpecs)))}
		b.games[gameID] = gb
	}
	return gb
}

// admitLocked adds e to the pending set, evicting the game's oldest (over the
// per-game cap) and then the globally oldest (over the total cap) as overflow.
func (b *Book) admitLocked(gb *gameBook, e *entry, now time.Time) []Result {
	var out []Result
	if len(gb.pending) >= MaxPendingPerGame {
		out = append(out, b.resolveLocked(gb.pending[0], unscorable(ReasonOverflow), now, nil))
	}
	if b.total >= MaxPendingTotal {
		var oldest *entry
		for _, g := range b.games {
			if len(g.pending) > 0 && (oldest == nil || g.pending[0].base.at.Before(oldest.base.at)) {
				oldest = g.pending[0]
			}
		}
		if oldest != nil {
			out = append(out, b.resolveLocked(oldest, unscorable(ReasonOverflow), now, nil))
		}
	}
	gb.pending = append(gb.pending, e)
	b.total++
	return out
}

// schedule starts e's horizon timer goroutine (not for an entry already scored).
func (b *Book) schedule(e *entry) {
	b.mu.Lock()
	resolved := e.resolved
	b.mu.Unlock()
	if resolved {
		return
	}
	b.wg.Add(1)
	fired := make(chan struct{})
	stop := b.afterFunc(e.base.horizon+HorizonGrace, func() { close(fired) })
	go func() {
		defer b.wg.Done()
		select {
		case <-b.root.Done():
			stop()
		case <-e.done:
			stop()
		case <-fired:
			b.onHorizon(e)
		}
	}()
}

// onHorizon scores e from the game's live context. A game whose partition is gone
// or that is no longer active leaves e pending for its game end.
func (b *Book) onHorizon(e *entry) {
	if b.current == nil || b.root.Err() != nil {
		return
	}
	c, ok := b.current(e.gameID)
	if !ok || c.Session.GameActive == nil || !*c.Session.GameActive {
		return
	}
	now := b.now()
	b.mu.Lock()
	var out []Result
	if !e.resolved {
		out = append(out, b.resolveLocked(e, horizonVerdict(e.pred, e.base, c, now), now, nil))
	}
	b.mu.Unlock()
	b.emit(out)
}

// resolveLocked claims e (exactly once), removes it from its game's pending set
// and returns its result. The caller emits outside the lock.
func (b *Book) resolveLocked(e *entry, v verdict, now time.Time, endAt *time.Time) Result {
	e.resolved = true
	close(e.done)
	if gb := b.games[e.gameID]; gb != nil {
		for i, p := range gb.pending {
			if p == e {
				gb.pending = append(gb.pending[:i], gb.pending[i+1:]...)
				b.total--
				break
			}
		}
	}
	return b.resultLocked(e, v, now, endAt)
}

// resultLocked builds e's result and stores it with its game.
func (b *Book) resultLocked(e *entry, v verdict, now time.Time, endAt *time.Time) Result {
	res := Result{
		ID: e.id, GameID: e.gameID, GameKind: e.gameKind, Backend: e.backend,
		Prediction: e.pred, Acted: e.acted, RegisteredAt: e.base.at, ScoredAt: now,
		Outcome: v.outcome, UnscorableReason: v.reason,
		Before: v.before, After: v.after, Eliminations: v.eliminations, Approx: v.approx,
		DecidedEarly: v.outcome != OutcomeUnscorable && now.Before(e.base.horizonEnd()),
		decideSC:     e.decideSC,
		parent:       e.parent,
	}
	if endAt != nil {
		off := endAt.Sub(e.base.at).Seconds()
		res.EndOffsetS = &off
	}
	if gb := b.games[e.gameID]; gb != nil && len(gb.results) < maxResultsPerGame {
		gb.results = append(gb.results, res)
	}
	return res
}

// Observe runs on every decision cycle of a running game (flag on): it scores the
// game's pending predictions that are already decided (early decision) and
// registers the base-rate control that is due.
func (b *Book) Observe(c gamecontext.GameContext) {
	if c.SessionID == "" || c.Session.GameActive == nil || !*c.Session.GameActive {
		return
	}
	now := b.now()
	b.mu.Lock()
	if _, ended := b.finalized[c.SessionID]; ended {
		b.mu.Unlock()
		return
	}
	var out []Result
	if gb := b.games[c.SessionID]; gb != nil {
		for _, e := range append([]*entry(nil), gb.pending...) {
			if v := earlyVerdict(e.pred, e.base, c, now); v.decided {
				out = append(out, b.resolveLocked(e, v, now, nil))
			}
		}
	}
	control, due := b.dueControlLocked(c)
	b.mu.Unlock()
	b.emit(out)
	if due {
		b.Register(Registration{Backend: BackendNone, Prediction: control, Context: c})
	}
}

// dueControlLocked returns the control prediction due for c, claiming its slot.
func (b *Book) dueControlLocked(c gamecontext.GameContext) (decide.Prediction, bool) {
	dur, ok := characteristics.Duration(c)
	if !ok {
		return decide.Prediction{}, false
	}
	gb := b.gameLocked(c.SessionID)
	if gb.controls >= maxControlsPerGame || time.Duration(dur*float64(time.Second)) < gb.nextControlAt {
		return decide.Prediction{}, false
	}
	spec := controlSpecs[(gb.controlOffset+gb.controls)%len(controlSpecs)]
	gb.controls++
	gb.nextControlAt += controlInterval
	target := ""
	if spec.targeted {
		alive := characteristics.AlivePlayers(c)
		if len(alive) == 0 {
			return decide.Prediction{}, false
		}
		h := fnv.New32a()
		_, _ = h.Write([]byte(c.SessionID))
		_, _ = h.Write([]byte{byte(gb.controls)})
		target = alive[int(h.Sum32()%uint32(len(alive)))]
	}
	return decide.NewPrediction(spec.template, spec.horizonS, controlP, decide.SourceControl, "", target), true
}

// OnGameEnd is the game-end chain hook (inserted second, after the stale counter,
// before the experiment, retro and summary consumers): it scores every pending
// prediction of the game from the pre-reset snapshot (stale end: all unscorable
// stale_end) and moves the game's results to the finalized lookup. A game the
// Book never saw is a no-op; a second end of the same game too.
func (b *Book) OnGameEnd(c gamecontext.GameContext) {
	now := b.now()
	b.mu.Lock()
	gb, known := b.games[c.SessionID]
	if !known {
		b.mu.Unlock()
		return
	}
	var out []Result
	for _, e := range append([]*entry(nil), gb.pending...) {
		var v verdict
		if c.EndReason == gamecontext.EndReasonStale {
			v = unscorable(ReasonStaleEnd)
		} else {
			v = endVerdict(e.pred, e.base, c, now)
		}
		end := now
		out = append(out, b.resolveLocked(e, v, now, &end))
	}
	delete(b.games, c.SessionID)
	b.finalized[c.SessionID] = gb.results
	b.order = append(b.order, c.SessionID)
	if len(b.order) > finalizedGames {
		delete(b.finalized, b.order[0])
		b.order = b.order[1:]
	}
	b.mu.Unlock()
	b.emit(out)
}

// Results returns a copy of the scored predictions of an ended game (the last
// finalizedGames games), nil when unknown.
func (b *Book) Results(gameID string) []Result {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]Result(nil), b.finalized[gameID]...)
}

// Pending is the number of pending predictions (tests, diagnostics).
func (b *Book) Pending() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.total
}

// emit records spans and metrics for out (outside the lock).
func (b *Book) emit(out []Result) {
	for _, r := range out {
		b.tel.outcome(b.root, r)
	}
}

// SummaryOutcomes is Results in the shape the game summary and the retro read
// (UC-042 A12): the finalized results of an ended game, nil when unknown. The
// result carries the target serial; the cross-game window never keeps it.
func (b *Book) SummaryOutcomes(gameID string) []gamesummary.PredictionOutcome {
	results := b.Results(gameID)
	if len(results) == 0 {
		return nil
	}
	out := make([]gamesummary.PredictionOutcome, 0, len(results))
	for _, r := range results {
		p := r.Prediction
		out = append(out, gamesummary.PredictionOutcome{
			ID: r.ID, Backend: r.Backend, Source: p.Source, Template: p.Template,
			Characteristic: p.Characteristic, Scope: p.Scope, Target: p.Target,
			HorizonS: p.HorizonS, P: p.P, Rule: p.Rule, Acted: r.Acted,
			Outcome: r.Outcome, UnscorableReason: r.UnscorableReason,
			DecidedEarly: r.DecidedEarly, RegisteredAt: r.RegisteredAt,
		})
	}
	return out
}
