package flags

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
)

// Live flag-plane tests (#1709, #1713). They run only when tests/service/test_agent_flag_live.py
// starts a real flag service + flagd and passes their addresses (flagd's SYNC port: the
// agent uses the in-process resolver); `go test ./...` skips them.
const (
	envLiveFlagdHost       = "AGENT_LIVE_FLAGD_HOST"
	envLiveFlagdPort       = "AGENT_LIVE_FLAGD_PORT"
	envLiveFlagdContainer  = "AGENT_LIVE_FLAGD_CONTAINER"
	envLiveFlagServiceURL  = "AGENT_LIVE_FLAG_SERVICE_URL"
	liveWriter             = "integration-test"
	liveFlipBound          = 1500 * time.Millisecond // NFR-050 p99 500 ms, CI headroom (test_flag_propagation.py)
	liveSetupTimeout       = 30 * time.Second
	livePollInterval       = 10 * time.Millisecond
	liveCachedReadsPerFlip = 20
	liveLateFlagdDelay     = 2 * time.Second // > the provider's default 500 ms init deadline
)

type liveFlagPlane struct {
	flags      *Flags
	serviceURL string
}

// newLiveFlagPlane sets the agent's provider up against the live flagd. startupDelay > 0
// puts a TCP relay in front of flagd that only starts relaying after that delay, i.e.
// flagd answers only after the provider's init deadline (compose starts the agent with
// flagd merely "started", #1709).
func newLiveFlagPlane(t *testing.T, startupDelay time.Duration) *liveFlagPlane {
	t.Helper()
	host, portRaw, serviceURL := os.Getenv(envLiveFlagdHost), os.Getenv(envLiveFlagdPort), os.Getenv(envLiveFlagServiceURL)
	if portRaw == "" || serviceURL == "" {
		t.Skipf("live flag plane not configured (%s, %s): run via tests/service/test_agent_flag_live.py",
			envLiveFlagdPort, envLiveFlagServiceURL)
	}
	if host == "" {
		host = "127.0.0.1"
	}
	port, err := strconv.ParseUint(portRaw, 10, 16)
	if err != nil {
		t.Fatalf("%s=%q: %v", envLiveFlagdPort, portRaw, err)
	}
	if startupDelay > 0 {
		host, port = "127.0.0.1", uint64(startLateRelay(t, net.JoinHostPort(host, portRaw), startupDelay))
	}
	ctx, cancel := context.WithCancel(context.Background())
	f, _, shutdown, err := SetupFlagd(ctx, ProviderConfig{Host: host, Port: uint16(port)}, nil)
	if err != nil {
		cancel()
		t.Fatalf("SetupFlagd: %v", err)
	}
	t.Cleanup(func() {
		shutdown()
		cancel()
	})
	waitResolvesFromFlagd(t)
	return &liveFlagPlane{flags: f, serviceURL: serviceURL}
}

// waitResolvesFromFlagd waits until the agent's client resolves a flag from flagd (no
// error), so a test never mistakes a code default (flagd not reached yet) for a served value.
func waitResolvesFromFlagd(t *testing.T) {
	t.Helper()
	client := openfeature.GetApiInstance().GetNamedClient(Domain)
	deadline := time.Now().Add(liveSetupTimeout)
	for {
		d, err := client.BooleanValueDetails(context.Background(), keyEnabled, false, openfeature.EvaluationContext{})
		if err == nil && d.ErrorCode == "" {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("the agent's provider never resolved %q from flagd: %v", keyEnabled, err)
		}
		time.Sleep(livePollInterval)
	}
}

// startLateRelay listens on a free local port and relays every connection to target,
// but accepts nothing before delay: a dial in that window hangs like a flagd that is
// still loading its flags.
func startLateRelay(t *testing.T, target string, delay time.Duration) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("relay listen: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		time.Sleep(delay)
		for {
			client, err := ln.Accept()
			if err != nil {
				return
			}
			go relay(client, target)
		}
	}()
	return ln.Addr().(*net.TCPAddr).Port
}

func relay(client net.Conn, target string) {
	defer func() { _ = client.Close() }()
	upstream, err := net.Dial("tcp", target)
	if err != nil {
		return
	}
	defer func() { _ = upstream.Close() }()
	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(upstream, client); done <- struct{}{} }()
	go func() { _, _ = io.Copy(client, upstream); done <- struct{}{} }()
	<-done
}

// put writes a stack overlay (variant) for an agent flag through the flag service.
func (p *liveFlagPlane) put(t *testing.T, flag, variant string) {
	t.Helper()
	body, _ := json.Marshal(map[string]string{"writer": liveWriter, "variant": variant})
	p.do(t, http.MethodPut, flag, body)
	t.Cleanup(func() { p.clear(t, flag) })
}

// clear deletes the stack overlay: the flag resolves its baseline again.
func (p *liveFlagPlane) clear(t *testing.T, flag string) {
	t.Helper()
	p.do(t, http.MethodDelete, flag, nil)
}

func (p *liveFlagPlane) do(t *testing.T, method, flag string, body []byte) {
	t.Helper()
	req, err := http.NewRequest(method, p.serviceURL+"/"+Domain+"/"+url.PathEscape(flag), bytes.NewReader(body))
	if err != nil {
		t.Fatalf("%s %s: %v", method, flag, err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, flag, err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode >= 300 && !(method == http.MethodDelete && resp.StatusCode == http.StatusNotFound) {
		t.Fatalf("%s %s: HTTP %d", method, flag, resp.StatusCode)
	}
}

// waitFor polls read until it returns want, within timeout; it returns how long that took.
func waitFor[T comparable](t *testing.T, what string, read func() T, want T, timeout time.Duration) time.Duration {
	t.Helper()
	start := time.Now()
	var got T
	for time.Since(start) <= timeout {
		if got = read(); got == want {
			return time.Since(start)
		}
		time.Sleep(livePollInterval)
	}
	t.Fatalf("%s: the agent's provider still reads %v %s after the flag-service write (want %v; flagd serves it)",
		what, got, timeout, want)
	return 0
}

// readSettled reads a flag value until the provider serves want (the first read after
// setup may still be the default), then reads it again so a caching resolver holds it.
func readSettled[T comparable](t *testing.T, what string, read func() T, want T) {
	t.Helper()
	waitFor(t, what+" (before the flip)", read, want, liveSetupTimeout)
	for i := 0; i < liveCachedReadsPerFlip; i++ {
		if got := read(); got != want {
			t.Fatalf("%s: read %v, want %v before the flip", what, got, want)
		}
	}
}

// TestLiveFlip_AlreadyEvaluatedFlagsFollowTheFlagService: a flag the agent has already
// evaluated takes a flag-service write within NFR-050 bounds (#1709: the RPC resolver
// cache served the old value until restart). One subtest per flag type the agent tunes live.
// flagd answers only after the provider's init deadline, as in compose at stack start.
//
// UC: UC-043 Pre-1 A1
// UC: UC-044 main
// UC: UC-040 main
func TestLiveFlip_AlreadyEvaluatedFlagsFollowTheFlagService(t *testing.T) {
	p := newLiveFlagPlane(t, liveLateFlagdDelay)
	ctx := context.Background()

	t.Run("UC-043/Pre-1 UC-043/A1 experiments_enabled off, on, off", func(t *testing.T) {
		read := func() bool { return p.flags.ExperimentsEnabled(ctx, false) }
		p.put(t, keyExperimentsEnabled, "off")
		readSettled(t, "experiments_enabled", read, false)
		p.put(t, keyExperimentsEnabled, "on")
		took := waitFor(t, "experiments_enabled", read, true, liveFlipBound)
		t.Logf("experiments_enabled off->on seen after %s", took)
		readSettled(t, "experiments_enabled", read, true)
		p.put(t, keyExperimentsEnabled, "off")
		took = waitFor(t, "experiments_enabled", read, false, liveFlipBound)
		t.Logf("experiments_enabled on->off seen after %s", took)
	})

	t.Run("UC-044/main decision.backend rules then openai", func(t *testing.T) {
		read := func() string { return p.flags.DecisionBackend(ctx).Backend }
		p.put(t, keyDecisionBackend, "rules")
		readSettled(t, "decision.backend", read, DecisionBackendRules)
		p.put(t, keyDecisionBackend, "openai")
		took := waitFor(t, "decision.backend", read, DecisionBackendOpenAI, liveFlipBound)
		t.Logf("decision.backend rules->openai seen after %s", took)
	})
}

// TestLiveFlip_KillSwitchWhenFlagdWasLateAtStartup: the agent started before flagd
// answered (compose: depends_on flagd service_started). The provider's init missed its
// deadline, and with the resolver cache its event stream then wedged: the kill switch
// was never seen (#1709, dry run 2026-10-20).
//
// UC: UC-045 main
func TestLiveFlip_KillSwitchWhenFlagdWasLateAtStartup(t *testing.T) {
	p := newLiveFlagPlane(t, liveLateFlagdDelay)
	ctx := context.Background()
	read := func() bool { return p.flags.Evaluate(ctx).Enabled }

	t.Run("UC-045/main flagd late at startup", func(t *testing.T) {
		p.put(t, keyEnabled, "on")
		readSettled(t, "enabled", read, true)
		// Several flips: one stale entry may still be dropped before the stream wedges.
		for i := 0; i < 3; i++ {
			p.put(t, keyEnabled, "off")
			took := waitFor(t, "enabled (kill switch)", read, false, liveFlipBound)
			t.Logf("flip %d: enabled on->off seen after %s", i, took)
			p.put(t, keyEnabled, "on")
			waitFor(t, "enabled (back on)", read, true, liveFlipBound)
		}
	})
}

// TestLiveFlip_KillSwitch: `enabled` read on, then turned off, reaches the agent's
// decision cycle within NFR-050 bounds, both by an explicit off overlay and by clearing
// the overlay back to the fail-closed baseline (scripts/agent-dryrun-enable.sh off).
//
// UC: UC-045 main
// UC: UC-040 main
func TestLiveFlip_KillSwitch(t *testing.T) {
	p := newLiveFlagPlane(t, 0)
	ctx := context.Background()
	read := func() bool { return p.flags.Evaluate(ctx).Enabled }

	for _, tc := range []struct {
		name string
		off  func(t *testing.T)
	}{
		{"UC-045/main off overlay", func(t *testing.T) { p.put(t, keyEnabled, "off") }},
		{"UC-045/main overlay cleared to the baseline", func(t *testing.T) { p.clear(t, keyEnabled) }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p.put(t, keyEnabled, "on")
			readSettled(t, "enabled", read, true)
			tc.off(t)
			took := waitFor(t, "enabled (kill switch)", read, false, liveFlipBound)
			t.Logf("enabled on->off seen after %s", took)
		})
	}
}

// TestLiveFlip_FlagdRestartMidRun: flagd restarts while the agent runs (#1713). The sync
// stream is lost: reads fail closed (the kill switch reads off) until flagd syncs again;
// then the stream is back and later flips are seen within NFR-050 bounds.
//
// UC: UC-045 main A3
func TestLiveFlip_FlagdRestartMidRun(t *testing.T) {
	container := os.Getenv(envLiveFlagdContainer)
	if container == "" {
		t.Skipf("%s not set: run via tests/service/test_agent_flag_live.py", envLiveFlagdContainer)
	}
	p := newLiveFlagPlane(t, 0)
	ctx := context.Background()
	read := func() bool { return p.flags.Evaluate(ctx).Enabled }

	t.Run("UC-045/A3 UC-045/main flagd restart: fail closed, reconnect, later flips seen", func(t *testing.T) {
		p.put(t, keyEnabled, "on")
		readSettled(t, "enabled", read, true)

		// Watch for a fail-closed read while flagd is away.
		sawOff := make(chan struct{})
		stop := make(chan struct{})
		var once sync.Once
		go func() {
			for {
				select {
				case <-stop:
					return
				default:
				}
				if !read() {
					once.Do(func() { close(sawOff) })
				}
				time.Sleep(livePollInterval)
			}
		}()
		out, err := exec.Command("docker", "restart", "-t", "5", container).CombinedOutput()
		if err != nil {
			close(stop)
			t.Fatalf("docker restart %s: %v\n%s", container, err, out)
		}
		took := waitFor(t, "enabled after the flagd restart (stream reconnected)", read, true, liveSetupTimeout)
		close(stop)
		t.Logf("enabled served again %s after docker restart returned", took)
		select {
		case <-sawOff:
		default:
			t.Fatal("enabled never read off while flagd was away: reads did not fail closed")
		}

		readSettled(t, "enabled", read, true)
		p.put(t, keyEnabled, "off")
		took = waitFor(t, "enabled (kill switch) after the restart", read, false, liveFlipBound)
		t.Logf("enabled on->off seen after %s", took)
		p.put(t, keyEnabled, "on")
		waitFor(t, "enabled back on after the restart", read, true, liveFlipBound)
	})
}
