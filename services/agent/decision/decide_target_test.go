package decision

import (
	"context"
	"encoding/json"
	"reflect"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
)

// decide_target_test.go covers #1295: decision spans name the targeted player
// (player.serial + player.name), and the rules-path agent.decide answers describe
// the decision Evaluate actually emitted, with the permission verdict on
// decide.blocked / decide.block_reason.

func answersOf(t *testing.T, span sdktrace.ReadOnlySpan) map[string]decide.Answer {
	t.Helper()
	var as []decide.Answer
	if err := json.Unmarshal([]byte(strAttr(t, span, AttrDecideAnswers)), &as); err != nil {
		t.Fatalf("decide.answers: %v", err)
	}
	out := map[string]decide.Answer{}
	for _, a := range as {
		out[a.QuestionID] = a
	}
	return out
}

// firstDecisionSpan is the agent.decision span of the cycle's first decision
// (spans end in emission order).
func firstDecisionSpan(t *testing.T, spans []sdktrace.ReadOnlySpan) sdktrace.ReadOnlySpan {
	t.Helper()
	dec := spansByName(spans, SpanDecision)
	if len(dec) == 0 {
		t.Fatal("no agent.decision span")
	}
	return dec[0]
}

// runBalancedCycle runs one rules-path cycle of the balanced fixture (a
// player-targeted top decision) with the given allow-list and returns the spans.
func runBalancedCycle(t *testing.T, allowed []string, names map[string]string) []sdktrace.ReadOnlySpan {
	t.Helper()
	fx := decideFixtures()[1] // balanced: R3 handicap / R4 shield, both player-targeted
	if fx.name != "balanced" {
		t.Fatalf("fixture order changed: got %q", fx.name)
	}
	snap := rulesSnapshot()
	snap.InterventionsAllowed = allowed
	clock := time.Unix(10_000, 0)
	l, sr := decideRecordingLoop(t, snap)
	l.Rules = newTestEngine(fx.cfg, &clock)
	l.Actions = &capturingSink{}
	if names != nil {
		l.SetPlayerNameResolver(func(serial string) string { return names[serial] })
	}
	l.OnEvaluate(context.Background(), fx.ctx, testTrigger())
	return sr.Ended()
}

// UC: UC-040 A1 A10 BR-004
func TestRulesPath_DecideMatchesEmittedDecision(t *testing.T) {
	t.Run("UC-040/A10 blocked targeted decision: answers name it, decide.blocked says not_allowed", func(t *testing.T) {
		names := map[string]string{"WEAK": "Calm Lion", "MID": "Swift Fox", "STRONG": "Bold Bear"}
		spans := runBalancedCycle(t, []string{"play_audio_cue"}, names)
		dec := firstDecisionSpan(t, spans)
		action := strAttr(t, dec, AttrDecisionAction)
		serial := strAttr(t, dec, AttrPlayerSerial)
		if v, _ := attrValue(dec, AttrDecisionBlocked); !v.AsBool() {
			t.Fatalf("%s was not blocked although the allow-list is play_audio_cue only", action)
		}

		ds := spansByName(spans, SpanDecide)
		if len(ds) != 1 {
			t.Fatalf("agent.decide spans = %d, want 1", len(ds))
		}
		as := answersOf(t, ds[0])
		if got := as[decide.QuestionFFAIntervention].Choice; got != action {
			t.Errorf("ffa.intervention = %q, agent.decision emitted %q", got, action)
		}
		if got := as[decide.QuestionFFATarget].Choice; got != serial {
			t.Errorf("ffa.target = %q, agent.decision targeted %q", got, serial)
		}
		if got := strAttr(t, ds[0], AttrDecideOutcome); got != DecideOutcomeAct {
			t.Errorf("decide.outcome = %q, want act (the routing outcome)", got)
		}
		if v, ok := attrValue(ds[0], AttrDecideBlocked); !ok || !v.AsBool() {
			t.Errorf("decide.blocked = %v (present %v), want true", v.AsBool(), ok)
		}
		if got := strAttr(t, ds[0], AttrDecideBlockReason); got != string(ReasonNotAllowed) {
			t.Errorf("decide.block_reason = %q, want not_allowed", got)
		}
	})
	t.Run("UC-040/A1 applied decision: decide.blocked=false and no block reason", func(t *testing.T) {
		spans := runBalancedCycle(t, allInterventions(), nil)
		ds := spansByName(spans, SpanDecide)
		if len(ds) != 1 {
			t.Fatalf("agent.decide spans = %d, want 1", len(ds))
		}
		dec := firstDecisionSpan(t, spans)
		as := answersOf(t, ds[0])
		if as[decide.QuestionFFAIntervention].Choice != strAttr(t, dec, AttrDecisionAction) {
			t.Error("ffa.intervention does not match the emitted decision")
		}
		if v, ok := attrValue(ds[0], AttrDecideBlocked); !ok || v.AsBool() {
			t.Errorf("decide.blocked = %v (present %v), want false", v.AsBool(), ok)
		}
		if _, ok := attrValue(ds[0], AttrDecideBlockReason); ok {
			t.Error("decide.block_reason present on an applied decision")
		}
	})
	t.Run("UC-040/A1 every fixture: the answer is the first emitted decision whatever the allow-list", func(t *testing.T) {
		for _, fx := range decideFixtures() {
			for _, allowed := range [][]string{allInterventions(), {"play_audio_cue"}, nil} {
				snap := rulesSnapshot()
				snap.InterventionsAllowed = allowed
				clock := time.Unix(10_000, 0)
				l, sr := decideRecordingLoop(t, snap)
				l.Rules = newTestEngine(fx.cfg, &clock)
				l.Actions = &capturingSink{}
				for step := 0; step < 4; step++ {
					st := l.OnEvaluate(context.Background(), fx.ctx, testTrigger())
					advance(&clock, 7*time.Second)
					if len(st.Outcomes) == 0 {
						continue
					}
					ds := spansByName(sr.Ended(), SpanDecide)
					as := answersOf(t, ds[len(ds)-1])
					first := st.Outcomes[0]
					if as[decide.QuestionFFAIntervention].Choice != first.Intervention ||
						as[decide.QuestionFFATarget].Choice != targetOption(first.TargetSerial) {
						t.Errorf("%s allowed=%v step %d: answers %s -> %s, emitted %s -> %s", fx.name, allowed, step,
							as[decide.QuestionFFAIntervention].Choice, as[decide.QuestionFFATarget].Choice,
							first.Intervention, targetOption(first.TargetSerial))
					}
					if v, _ := attrValue(ds[len(ds)-1], AttrDecideBlocked); v.AsBool() == first.Dispatched {
						t.Errorf("%s step %d: decide.blocked=%v but dispatched=%v", fx.name, step, v.AsBool(), first.Dispatched)
					}
				}
			}
		}
	})
}

// UC: UC-040 A1
func TestRulesDecide_NoopNeverReadsAct(t *testing.T) {
	t.Run("UC-040/A1 a ranking that emits nothing yields no decisions and a noop answer, never outcome act", func(t *testing.T) {
		fx := decideFixtures()[5]
		if fx.name != "no candidates" {
			t.Fatalf("fixture order changed: got %q", fx.name)
		}
		clock := time.Unix(10_000, 0)
		l, _ := decideRecordingLoop(t, rulesSnapshot())
		l.Rules = newTestEngine(fx.cfg, &clock)
		out, rec := l.rulesDecide(context.Background(), rulesSnapshot(), l.Rules, fx.ctx, flags.DecisionBackendRules, "")
		if len(out) != 0 || rec == nil {
			t.Fatalf("want a ranked cycle without decisions, got %d decisions, rec %v", len(out), rec)
		}
		if iv, _ := rec.resp.Find(decide.QuestionFFAIntervention); iv.Choice != decide.OptionNoop {
			t.Fatalf("choice = %q, want noop", iv.Choice)
		}
		if rec.outcome != DecideOutcomeNoop {
			t.Fatalf("outcome = %q, want noop (a noop choice never reads act)", rec.outcome)
		}
	})
}

// UC: UC-040 main A1 A10
func TestDecisionSpans_NameTheTargetedPlayer(t *testing.T) {
	names := map[string]string{"WEAK": "Calm Lion", "MID": "Swift Fox", "STRONG": "Bold Bear"}
	for _, tc := range []struct {
		name    string
		allowed []string
	}{
		{"UC-040/A10 blocked decision carries player.serial and player.name", []string{"play_audio_cue"}},
		{"UC-040/A1 applied decision carries player.serial and player.name", allInterventions()},
	} {
		t.Run(tc.name, func(t *testing.T) {
			spans := runBalancedCycle(t, tc.allowed, names)
			dec := firstDecisionSpan(t, spans)
			serial := strAttr(t, dec, AttrPlayerSerial)
			if _, known := names[serial]; !known {
				t.Fatalf("player.serial = %q, not a fixture player", serial)
			}
			if got := strAttr(t, dec, AttrPlayerName); got != names[serial] {
				t.Errorf("agent.decision player.name = %q, want %q", got, names[serial])
			}
			acts := spansByName(spans, SpanAction)
			if len(acts) == 0 {
				t.Fatal("no agent.action span")
			}
			if got := strAttr(t, acts[0], AttrPlayerSerial); got != serial {
				t.Errorf("agent.action player.serial = %q, want %q", got, serial)
			}
			if got := strAttr(t, acts[0], AttrPlayerName); got != names[serial] {
				t.Errorf("agent.action player.name = %q, want %q", got, names[serial])
			}
		})
	}
	t.Run("UC-040/A1 unknown name: player.serial only", func(t *testing.T) {
		spans := runBalancedCycle(t, allInterventions(), nil)
		dec := firstDecisionSpan(t, spans)
		strAttr(t, dec, AttrPlayerSerial)
		if _, ok := attrValue(dec, AttrPlayerName); ok {
			t.Error("player.name present although no name is known")
		}
	})
	t.Run("UC-040/A1 shadow game: player.serial only, never player.name", func(t *testing.T) {
		fx := decideFixtures()[1] // balanced: player-targeted
		fx.ctx.GameKind = "shadow"
		clock := time.Unix(10_000, 0)
		l, sr := decideRecordingLoop(t, rulesSnapshot())
		l.Rules = newTestEngine(fx.cfg, &clock)
		l.Actions = &capturingSink{}
		l.SetPlayerNameResolver(func(string) string { return "Real Player" })
		l.OnEvaluate(context.Background(), fx.ctx, testTrigger())
		spans := append(spansByName(sr.Ended(), SpanDecision), spansByName(sr.Ended(), SpanAction)...)
		if len(spans) == 0 {
			t.Fatal("no decision spans")
		}
		for _, s := range spans {
			strAttr(t, s, AttrPlayerSerial)
			if _, ok := attrValue(s, AttrPlayerName); ok {
				t.Errorf("%s carries player.name in a shadow game (mock serials collide with real games)", s.Name())
			}
		}
	})
	t.Run("UC-040/A1 session-scoped decision: no player attributes", func(t *testing.T) {
		fx := decideFixtures()[0] // endurance: adjust_music_tempo, session-scoped
		clock := time.Unix(10_000, 0)
		l, sr := decideRecordingLoop(t, rulesSnapshot())
		l.Rules = newTestEngine(fx.cfg, &clock)
		l.Actions = &capturingSink{}
		l.SetPlayerNameResolver(func(string) string { return "should not appear" })
		l.OnEvaluate(context.Background(), fx.ctx, testTrigger())
		for _, s := range append(spansByName(sr.Ended(), SpanDecision), spansByName(sr.Ended(), SpanAction)...) {
			for _, key := range []string{AttrPlayerSerial, AttrPlayerName} {
				if _, ok := attrValue(s, key); ok {
					t.Errorf("%s carries %s on a session-scoped decision", s.Name(), key)
				}
			}
		}
	})
}

// UC: UC-040 main A10
func TestRemote_BlockedDecisionNamesTargetAndDecideBlocked(t *testing.T) {
	t.Run("UC-040/A10 remote act blocked at landing: decide.blocked + player.serial", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
		h, span := fireAndMutate(t, be, time.Hour, func(h remoteHarness) {
			h.flags.mutate(func(s *flags.Snapshot) { s.InterventionsAllowed = []string{"noop"} })
		})
		if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeAct {
			t.Fatalf("outcome = %q, want act", g)
		}
		if v, ok := attrValue(span, AttrDecideBlocked); !ok || !v.AsBool() {
			t.Fatal("decide.blocked must be true when the permission chain blocked the routed answer")
		}
		if g := strAttr(t, span, AttrDecideBlockReason); g != string(ReasonNotAllowed) {
			t.Fatalf("decide.block_reason = %q, want not_allowed", g)
		}
		dec := firstDecisionSpan(t, h.sr.Ended())
		if g := strAttr(t, dec, AttrPlayerSerial); g != "A" {
			t.Fatalf("agent.decision player.serial = %q, want A", g)
		}
	})
	t.Run("UC-040/main remote noop: no decide.blocked", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer(decide.OptionNoop, 0.95, decide.TargetNone, 0, "x"))
		_, span := fireAndMutate(t, be, time.Hour, func(remoteHarness) {})
		if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeNoop {
			t.Fatalf("outcome = %q, want noop", g)
		}
		if _, ok := attrValue(span, AttrDecideBlocked); ok {
			t.Fatal("decide.blocked present although nothing reached the permission chain")
		}
	})
}

// UC: UC-040 A1
func TestEmissionRequest_WidensWithoutMutating(t *testing.T) {
	t.Run("UC-040/A1 ranked interventions and targets join the options; input untouched", func(t *testing.T) {
		fx := decideFixtures()[1]
		req := ffaRequest(fx.ctx, []string{"play_audio_cue"})
		orig := make([][]string, len(req.Questions))
		for i, q := range req.Questions {
			orig[i] = append([]string(nil), q.Options...)
		}
		ranked := []scoredCandidate{
			{candidate: candidate{decision: Decision{Intervention: InterventionGrantShield, TargetSerial: "GONE"}}, score: 1},
			{candidate: candidate{decision: Decision{Intervention: InterventionAdjustMusicTempo}}, score: 0.5},
		}
		wide := emissionRequest(req, ranked)
		for i, q := range req.Questions {
			if !reflect.DeepEqual(q.Options, orig[i]) {
				t.Fatalf("question %s mutated: %v -> %v", q.ID, orig[i], q.Options)
			}
		}
		ivOpts := optionSet(wide.Questions[0])
		tgtOpts := optionSet(wide.Questions[1])
		if !ivOpts[InterventionGrantShield] || !ivOpts[InterventionAdjustMusicTempo] || !ivOpts["play_audio_cue"] {
			t.Errorf("intervention options = %v", wide.Questions[0].Options)
		}
		if !tgtOpts["GONE"] || !tgtOpts[decide.TargetNone] {
			t.Errorf("target options = %v", wide.Questions[1].Options)
		}
		resp := answerFromRanked(ranked, wide, true)
		if iv, _ := resp.Find(decide.QuestionFFAIntervention); iv.Choice != InterventionGrantShield {
			t.Errorf("choice = %q, want the top ranked grant_shield", iv.Choice)
		}
		if tg, _ := resp.Find(decide.QuestionFFATarget); tg.Choice != "GONE" {
			t.Errorf("target = %q, want GONE (a non-alive target still answers)", tg.Choice)
		}
	})
}
