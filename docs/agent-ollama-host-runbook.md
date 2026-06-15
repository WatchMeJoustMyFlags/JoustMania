# Runbook: reaching a host Ollama from the containerized agent (#1059)

The agent's OpenAI-compatible backend (`AGENT_INFERENCE_BACKEND=openai`, see
[`agent-inference-gateway.md`](agent-inference-gateway.md) and #1048) dials
`AGENT_INFERENCE_BASE_URL`. When inference runs on **Ollama on the host** rather
than in a sidecar, two things must be true or the agent silently degrades to
`mode=rules` (`no_backend_available`) even though Ollama is "running":

1. **Ollama must listen on `0.0.0.0`, not `127.0.0.1`.**
2. **The container must dial an address that actually routes to that host** —
   which is *not* always `host.docker.internal`.

Both are network reachability, not agent config — the agent fails **safe** to
rules, so the only symptom is "the LLM never gets called."

## 1. Ollama must bind 0.0.0.0

By default Ollama binds `127.0.0.1:11434` — reachable only from the host's own
loopback, **never** from inside a container (a container's loopback is its own).
Set `OLLAMA_HOST` so it binds all interfaces:

```bash
# Linux/macOS host (systemd): override the service env, then restart
sudo systemctl edit ollama        # add: [Service]\nEnvironment="OLLAMA_HOST=0.0.0.0"
sudo systemctl restart ollama

# Or for a foreground run:
OLLAMA_HOST=0.0.0.0 ollama serve
```

```powershell
# Windows host (Ollama as a Windows app): set a user env var, then restart Ollama
setx OLLAMA_HOST "0.0.0.0"
# quit Ollama from the tray and relaunch so it picks up the new bind address
```

Verify it is listening on all interfaces (not just loopback):

```bash
curl -s http://0.0.0.0:11434/api/tags        # on the host itself
ss -ltn | grep 11434                          # LISTEN should show 0.0.0.0:11434, not 127.0.0.1:11434
```

> Security: `0.0.0.0` exposes Ollama to your LAN. That is fine for a closed demo
> network; firewall the port otherwise.

## 2. The address the container must dial

`AGENT_INFERENCE_BASE_URL` must point at an address that routes from the
container to the host's Ollama. Which one depends on where the host is:

| Topology | `AGENT_INFERENCE_BASE_URL` host | Notes |
|----------|----------------------------------|-------|
| Docker on Linux/macOS, Ollama on the **same** host | `host.docker.internal` | resolved via `extra_hosts: ["host.docker.internal:host-gateway"]` (already set on the agent service) |
| Docker **in WSL2**, Ollama on **Windows** | the **WSL default-route gateway IP** (e.g. `172.x.x.1` / `192.168.x.1`) | `host.docker.internal` resolves to the **WSL distro**, *not* Windows — see below |
| Ollama on a **LAN box / Jetson** | that box's LAN IP | must also bind `0.0.0.0` there |

### The WSL2 + Windows-Ollama gotcha

This is the most common foot-gun. With the stack running in a WSL2 distro and
Ollama running as a **Windows** app:

- `host.docker.internal` from inside a WSL2 container resolves to the **WSL
  distro's** IP — Ollama isn't there, so the dial fails and the agent degrades
  to rules.
- The Windows host is reachable at the **default-route gateway** of the WSL
  network. Find it from *inside* WSL:

  ```bash
  ip route show default | awk '{print $3}'     # e.g. 192.168.224.1
  ```

- Point the agent at that IP:

  ```bash
  AGENT_INFERENCE_BACKEND=openai \
  AGENT_INFERENCE_BASE_URL=http://192.168.224.1:11434/v1 \
  AGENT_INFERENCE_MODEL=gemma3:4b \
    docker compose up -d agent
  ```

> The gateway IP is **assigned per WSL boot** — don't hard-code it across
> reboots; re-derive it with the `ip route` command (or use Ollama's LAN IP if
> you've given Windows a stable one). Windows Firewall must also allow inbound
> 11434 to the WSL vEthernet.

## 3. End-to-end check (from inside the agent's network)

Before blaming the agent, prove the path with a throwaway container on the same
network — if this fails, so will the agent:

```bash
# substitute the host:port you configured above
docker run --rm curlimages/curl -s http://192.168.224.1:11434/api/tags
```

A 200 with your pulled models means the agent will reach it too. Then confirm
the agent actually used it — a successful inference decision logs `mode=llm`
(not `mode=rules`) with the configured model:

```bash
docker compose logs agent | grep -E 'agent\.evaluate|no_backend_available' | tail
```

`mode=rules` + `no_backend_available` after the above curl succeeds points at a
**model** problem, not networking — confirm `AGENT_INFERENCE_MODEL` matches a
tag from `/api/tags` exactly (e.g. `gemma3:4b`, not `gemma4`).

## Why the agent doesn't just crash

The inference tier is **probed** (a TCP dial of the `AGENT_INFERENCE_BASE_URL`
host:port). An unreachable probe marks the tier unavailable and the decision
loop walks down to the rules engine — a deliberate fail-safe (a flaky Ollama
must never take the game down). The cost is that a *misconfigured* endpoint is
indistinguishable from a *down* one at a glance: both read as
`no_backend_available`. This runbook is the checklist for telling them apart.
