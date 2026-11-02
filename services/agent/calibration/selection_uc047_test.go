package calibration

import (
	"fmt"
	"math"
	"testing"

	"github.com/joustmania/agent/gamejournal"
)

func fp(v float64) *float64 { return &v }
func bp(v bool) *bool       { return &v }

// game is an eligible 20 s FFA game of two moving players (variance 0.04 and
// 0.01) and one still player, marked non-test.
func game(id string) gamejournal.Record {
	return gamejournal.Record{
		SchemaVersion:   gamejournal.SchemaVersion,
		GameID:          id,
		Mode:            FFAMode,
		DurationSeconds: fp(20),
		TestSession:     bp(false),
		Eliminations:    2,
		Players: []gamejournal.Player{
			{MeanMovementVariance: fp(0.04)},
			{MeanMovementVariance: fp(0.01)},
			{MeanMovementVariance: fp(0.0001)},
		},
		Metrics: gamejournal.Metrics{EliminationPace: fp(6)},
	}
}

func games(n int) []gamejournal.Record {
	out := make([]gamejournal.Record, n)
	for i := range out {
		out[i] = game(fmt.Sprintf("g%03d", i))
	}
	return out
}

func ids(recs []gamejournal.Record) []string {
	out := make([]string, len(recs))
	for i, r := range recs {
		out[i] = r.GameID
	}
	return out
}

func sameIDs(t *testing.T, got []gamejournal.Record, want ...string) {
	t.Helper()
	g := ids(got)
	if fmt.Sprint(g) != fmt.Sprint(want) {
		t.Fatalf("game ids = %v, want %v", g, want)
	}
}

// UC: UC-047 BR-002 BR-003
func TestUC047_Eligible(t *testing.T) {
	t.Run("UC-047/BR-002 a two-mover FFA game of at least 10 s is eligible", func(t *testing.T) {
		if r, ok := Eligible(game("a")); !ok {
			t.Fatalf("eligible = false (%s), want true", r)
		}
		g := game("a")
		g.DurationSeconds = fp(MinDurationSeconds)
		if r, ok := Eligible(g); !ok {
			t.Fatalf("exactly 10 s: eligible = false (%s), want true", r)
		}
	})

	cases := []struct {
		name   string
		mutate func(*gamejournal.Record)
		want   Reason
	}{
		{"UC-047/BR-002 another mode is excluded", func(r *gamejournal.Record) { r.Mode = "Teams" }, ReasonOtherMode},
		{"UC-047/BR-002 an empty mode is excluded", func(r *gamejournal.Record) { r.Mode = "" }, ReasonOtherMode},
		{"UC-047/BR-003 a game marked test is excluded", func(r *gamejournal.Record) { r.TestSession = bp(true) }, ReasonTestSession},
		{"UC-047/BR-003 a null test mark counts as test", func(r *gamejournal.Record) { r.TestSession = nil }, ReasonTestSession},
		{"UC-047/BR-002 a game shorter than 10 s is excluded", func(r *gamejournal.Record) { r.DurationSeconds = fp(9.99) }, ReasonTooShort},
		{"UC-047/BR-002 a missing duration is metrics_missing", func(r *gamejournal.Record) { r.DurationSeconds = nil }, ReasonMetricsMissing},
		{"UC-047/BR-002 a non-finite duration is metrics_missing", func(r *gamejournal.Record) { r.DurationSeconds = fp(math.Inf(1)) }, ReasonMetricsMissing},
		{"UC-047/BR-002 a player without a mean variance is metrics_missing", func(r *gamejournal.Record) { r.Players[2].MeanMovementVariance = nil }, ReasonMetricsMissing},
		{"UC-047/BR-002 a NaN mean variance is metrics_missing", func(r *gamejournal.Record) { r.Players[0].MeanMovementVariance = fp(math.NaN()) }, ReasonMetricsMissing},
		{"UC-047/BR-002 one mover is too few", func(r *gamejournal.Record) { r.Players[1].MeanMovementVariance = fp(0.0049) }, ReasonTooFewMoved},
		{"UC-047/BR-002 no players is too few", func(r *gamejournal.Record) { r.Players = nil }, ReasonTooFewMoved},
		{"UC-047/BR-002 one shaker at 10 times the second dominates", func(r *gamejournal.Record) { r.Players[0].MeanMovementVariance = fp(0.1) }, ReasonDominant},
		{"UC-047/BR-002 the order of the checks names the first failing reason", func(r *gamejournal.Record) {
			r.TestSession = nil
			r.DurationSeconds = fp(3)
		}, ReasonTestSession},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			g := game("a")
			tc.mutate(&g)
			r, ok := Eligible(g)
			if ok || r != tc.want {
				t.Fatalf("Eligible = (%q, %v), want (%q, false)", r, ok, tc.want)
			}
		})
	}

	t.Run("UC-047/BR-002 just under 10 times the second does not dominate", func(t *testing.T) {
		g := game("a")
		g.Players[0].MeanMovementVariance = fp(0.0999)
		if r, ok := Eligible(g); !ok {
			t.Fatalf("eligible = false (%s), want true", r)
		}
	})

	t.Run("UC-047/BR-002 the movement floor is inclusive", func(t *testing.T) {
		g := game("a")
		g.Players[0].MeanMovementVariance = fp(0.04)
		g.Players[1].MeanMovementVariance = fp(MovementFloor)
		if r, ok := Eligible(g); !ok {
			t.Fatalf("eligible = false (%s), want true", r)
		}
	})
}

// UC: UC-047 main A1 A9 BR-004 BR-007
func TestUC047_Select(t *testing.T) {
	t.Run("UC-047/main step 2 counts eligible games and excluded per reason", func(t *testing.T) {
		recs := games(5)
		recs[1].TestSession = nil
		recs[3].Mode = "Teams"
		sel := Select(recs, Watermark{}, 0)
		if sel.Eligible != 3 {
			t.Fatalf("Eligible = %d, want 3", sel.Eligible)
		}
		sameIDs(t, sel.Games, "g000", "g002", "g004")
		if sel.Excluded[ReasonTestSession] != 1 || sel.Excluded[ReasonOtherMode] != 1 || len(sel.Excluded) != 2 {
			t.Fatalf("Excluded = %v", sel.Excluded)
		}
	})

	t.Run("UC-047/BR-007 only games after the watermark count", func(t *testing.T) {
		sel := Select(games(6), Watermark{GameID: "g002"}, 0)
		if sel.Eligible != 3 {
			t.Fatalf("Eligible = %d, want 3", sel.Eligible)
		}
		sameIDs(t, sel.Games, "g003", "g004", "g005")
	})

	t.Run("UC-047/A9 after a reset at the newest record nothing counts", func(t *testing.T) {
		sel := Select(games(4), Watermark{GameID: "g003"}, 0)
		if sel.Eligible != 0 || len(sel.Games) != 0 || len(sel.Excluded) != 0 {
			t.Fatalf("Select = %+v, want empty", sel)
		}
	})

	t.Run("UC-047/BR-007 an evicted watermark counts every record", func(t *testing.T) {
		sel := Select(games(3), Watermark{GameID: "gone"}, 0)
		if sel.Eligible != 3 {
			t.Fatalf("Eligible = %d, want 3", sel.Eligible)
		}
	})

	t.Run("UC-047/main step 3 the most recent n eligible games in journal order", func(t *testing.T) {
		recs := games(6)
		recs[5].DurationSeconds = fp(2)
		sel := Select(recs, Watermark{}, 3)
		if sel.Eligible != 5 {
			t.Fatalf("Eligible = %d, want 5", sel.Eligible)
		}
		sameIDs(t, sel.Games, "g002", "g003", "g004")
	})

	t.Run("UC-047/BR-004 journal order wins over end time", func(t *testing.T) {
		recs := games(3)
		// A clock jump: the newest journal record has the oldest end time.
		recs[0].EndedAt = recs[0].EndedAt.AddDate(0, 0, 2)
		recs[2].EndedAt = recs[2].EndedAt.AddDate(0, 0, -2)
		sel := Select(recs, Watermark{}, 2)
		sameIDs(t, sel.Games, "g001", "g002")
	})

	t.Run("UC-047/A1 fewer than n available gives all of them", func(t *testing.T) {
		sel := Select(games(10), Watermark{}, DefaultManualN)
		if len(sel.Games) != 10 {
			t.Fatalf("len(Games) = %d, want 10", len(sel.Games))
		}
	})
}

// UC: UC-047 BR-004 A2
func TestUC047_SplitHoldout(t *testing.T) {
	t.Run("UC-047/BR-004 every 4th game is in the holdout", func(t *testing.T) {
		fit, holdout := SplitHoldout(games(9))
		sameIDs(t, holdout, "g003", "g007")
		sameIDs(t, fit, "g000", "g001", "g002", "g004", "g005", "g006", "g008")
	})

	t.Run("UC-047/BR-004 the default window of 100 holds out 25", func(t *testing.T) {
		fit, holdout := SplitHoldout(games(100))
		if len(holdout) != 25 || len(fit) != 75 {
			t.Fatalf("fit %d holdout %d, want 75 and 25", len(fit), len(holdout))
		}
	})

	t.Run("UC-047/A2 the minimum of 32 gives 3 bands of 8 and 8 held out", func(t *testing.T) {
		if MinEligibleGames != 32 {
			t.Fatalf("MinEligibleGames = %d, want 32", MinEligibleGames)
		}
		fit, holdout := SplitHoldout(games(MinEligibleGames))
		if len(holdout) != MinHoldoutGames {
			t.Fatalf("holdout %d, want %d", len(holdout), MinHoldoutGames)
		}
		for b, band := range Bands(fit) {
			if len(band) != MinBandGames {
				t.Fatalf("band %d has %d games, want %d", b, len(band), MinBandGames)
			}
		}
	})
}

// UC: UC-047 BR-005
func TestUC047_Bands(t *testing.T) {
	withPace := func(id string, pace float64) gamejournal.Record {
		g := game(id)
		g.Metrics.EliminationPace = fp(pace)
		return g
	}

	t.Run("UC-047/BR-005 thirds by elimination pace, remainder in the middle", func(t *testing.T) {
		fit := []gamejournal.Record{
			withPace("a", 9), withPace("b", 1), withPace("c", 5), withPace("d", 7),
			withPace("e", 3), withPace("f", 8), withPace("g", 2), withPace("h", 6),
		}
		b := Bands(fit)
		sameIDs(t, b[BandLow], "b", "g")
		sameIDs(t, b[BandMiddle], "e", "c", "h", "d")
		sameIDs(t, b[BandHigh], "f", "a")
	})

	t.Run("UC-047/BR-005 ties keep journal order", func(t *testing.T) {
		fit := []gamejournal.Record{withPace("x", 4), withPace("y", 4), withPace("z", 4)}
		b := Bands(fit)
		sameIDs(t, b[BandLow], "x")
		sameIDs(t, b[BandMiddle], "y")
		sameIDs(t, b[BandHigh], "z")
	})

	t.Run("UC-047/BR-005 a missing pace metric is computed from the record", func(t *testing.T) {
		slow := game("slow")
		slow.Metrics.EliminationPace = nil
		slow.Eliminations = 1 // 1 per 20 s = 3 per minute
		fit := []gamejournal.Record{withPace("fast", 9), slow, withPace("mid", 5)}
		b := Bands(fit)
		sameIDs(t, b[BandLow], "slow")
		sameIDs(t, b[BandHigh], "fast")
	})

	t.Run("UC-047/BR-005 the input is not reordered", func(t *testing.T) {
		fit := []gamejournal.Record{withPace("a", 9), withPace("b", 1), withPace("c", 5)}
		Bands(fit)
		sameIDs(t, fit, "a", "b", "c")
	})

	t.Run("UC-047/BR-005 each archetype has a fixed band", func(t *testing.T) {
		want := map[string]Band{
			"calm_long": BandLow, "slow_cascade": BandLow,
			"jittery_novices": BandMiddle, "late_burst": BandMiddle, "some_new_name": BandMiddle,
			"close_race": BandHigh, "quick_cascade": BandHigh,
		}
		for name, band := range want {
			if got := BandFor(name); got != band {
				t.Errorf("BandFor(%q) = %s, want %s", name, got, band)
			}
		}
	})
}
