package gate

import (
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

func bptr(b bool) *bool { return &b }

func TestShouldEvaluate(t *testing.T) {
	now := time.Unix(1000, 0)
	ttl := 5 * time.Second

	freshActive := &gamecontext.PlayerSignals{Serial: "A", Active: bptr(true), LastUpdate: now}
	staleActive := &gamecontext.PlayerSignals{Serial: "A", Active: bptr(true), LastUpdate: now.Add(-10 * time.Second)}
	inactive := &gamecontext.PlayerSignals{Serial: "A", Active: bptr(false), LastUpdate: now}

	cases := []struct {
		name    string
		active  *bool
		players map[string]*gamecontext.PlayerSignals
		want    bool
	}{
		{"nil game active", nil, map[string]*gamecontext.PlayerSignals{"A": freshActive}, false},
		{"false game active", bptr(false), map[string]*gamecontext.PlayerSignals{"A": freshActive}, false},
		{"active + fresh active player", bptr(true), map[string]*gamecontext.PlayerSignals{"A": freshActive}, true},
		{"active + stale player", bptr(true), map[string]*gamecontext.PlayerSignals{"A": staleActive}, false},
		{"active + inactive players", bptr(true), map[string]*gamecontext.PlayerSignals{"A": inactive}, false},
		{"active + no players", bptr(true), map[string]*gamecontext.PlayerSignals{}, false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c := gamecontext.GameContext{
				Session: gamecontext.SessionSignals{GameActive: tc.active},
				Players: tc.players,
			}
			if got := ShouldEvaluate(c, now, ttl); got != tc.want {
				t.Fatalf("ShouldEvaluate = %v, want %v", got, tc.want)
			}
		})
	}
}
