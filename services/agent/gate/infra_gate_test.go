package gate

import (
	"testing"
	"time"

	"github.com/joustmania/agent/infracontext"
)

func TestShouldEvaluateInfra(t *testing.T) {
	now := time.Unix(1000, 0)
	ttl := 5 * time.Second

	fresh := &infracontext.ControllerHealth{Serial: "AA:BB", LastUpdate: now}
	stale := &infracontext.ControllerHealth{Serial: "CC:DD", LastUpdate: now.Add(-10 * time.Second)}
	edge := &infracontext.ControllerHealth{Serial: "EE:FF", LastUpdate: now.Add(-5 * time.Second)} // exactly ttl

	cases := []struct {
		name        string
		controllers map[string]*infracontext.ControllerHealth
		want        bool
	}{
		{"no controllers", map[string]*infracontext.ControllerHealth{}, false},
		{"nil map", nil, false},
		{"one fresh", map[string]*infracontext.ControllerHealth{"AA:BB": fresh}, true},
		{"one stale", map[string]*infracontext.ControllerHealth{"CC:DD": stale}, false},
		{"fresh + stale mix", map[string]*infracontext.ControllerHealth{"AA:BB": fresh, "CC:DD": stale}, true},
		{"exactly at ttl boundary", map[string]*infracontext.ControllerHealth{"EE:FF": edge}, true},
		{"nil entry only", map[string]*infracontext.ControllerHealth{"X": nil}, false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			infra := infracontext.InfraContext{Controllers: tc.controllers}
			if got := ShouldEvaluateInfra(infra, now, ttl); got != tc.want {
				t.Fatalf("ShouldEvaluateInfra = %v, want %v", got, tc.want)
			}
		})
	}
}
