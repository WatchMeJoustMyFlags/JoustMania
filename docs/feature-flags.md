# Feature Flags

A short guide to how JoustMania uses flags. The flag catalog (every key, its variants, default, readers,
writers and reload behaviour) is [docs/specs/flags.md](specs/flags.md): that file is the source of truth, and a
PR that adds, removes or changes a flag updates it. The flag sets and their files are generated into
[specs/generated/flag-counts.md](specs/generated/flag-counts.md) by `make specs`; the per-set counts are
not committed (#2152): `make specs-counts` prints them.

## 1. How it works

JoustMania uses [OpenFeature](https://openfeature.dev/) with [flagd](https://flagd.dev/). Flags are the only
runtime control surface: host settings, the agent's governor, the agent's interventions and the rollout are all
flags (C-002).

```
services/flagd/<set>.json  (read-only baseline, one file per flag set)
        │ read once at start
        ▼
flag-service  (HTTP API :8070, envoy /flags/; holds overlays, game-scoped values,
        │      experiment arms; persists admin settings)
        │ flagd.sync.v1, pushed on every accepted write
        ▼
flagd  ──►  OpenFeature providers in each service (Python: in-process, one per flag set;
            agent and rust-hid: RPC)
```

- **Flag sets.** Each file declares one `flagSetId` that matches its file name (`game.json` is `game`).
  Python services select a set with `flagSetId=<set>` (`lib/feature_flags.py`).
- **Writes.** Nothing writes a flag file at runtime (C-033). Runtime changes go through the flag service:
  the menu (admin settings), the agent (interventions, experiment arms, rollout count), connect-proxy (canary)
  and the operator scripts. Only flags listed in the mutable-flag registry
  ([flags.md §13](specs/flags.md#13-mutable-flag-registry), `services/flag-service/registry.yaml`) accept writes.
- **CI baseline.** `services/flagd/ci/` replaces the baseline in the CI, dry-run and demo stacks
  (`docker-compose.ci.yml`); the differences are listed in
  [specs/generated/flags-ci-diff.md](specs/generated/flags-ci-diff.md).
- **Propagation.** A write reaches flagd within the NFR-050 bound (p95 200 ms). A baseline file change needs
  `scripts/flags.sh restart`. Whether a reader picks a change up live, per game or only at startup is the
  Reload column of the catalog.

## 2. Naming

- The flag set is the namespace; keys are bare `snake_case` (`backend` in `controller.json`, not
  `controller_backend`).
- A dotted key only for a real sub-group (`idle.timeout_minutes`, `nonstop.respawn_seconds`), never to repeat
  the set name.

## 3. Edit flag values

**Mutable flags** (with a registry row) change at runtime, without a restart:

```bash
scripts/flags.sh set agent decision.backend claude   # stack overlay, in memory
scripts/flags.sh get agent decision.backend          # baseline, overlays and the rendered flagd JSON
scripts/flags.sh clear agent decision.backend        # back to the baseline
```

An overlay lasts until it is cleared or the flag service restarts. The flag service answers `422 out_of_bounds`
for a value outside the registry bounds, `422 flag_read_only` for a flag without a registry row and
`503 no_sync_stream` while flagd is not connected.

**Every other flag** is a reviewed edit of its baseline file, then `scripts/flags.sh restart`
(flag service, then flagd).

## 4. Add or remove a flag

1. Pick the set. A value the agent changes mid-game in response to signals is an intervention and belongs in
   `interventions.json`; a value tuned per venue or experiment is calibration and belongs in `game.json`,
   `agent.json` or `system.json`
   ([722-intervention-surface.md §2b](research/722-intervention-surface.md)).
2. Add the flag to `services/flagd/<set>.json` and, when CI needs a different value, to `services/flagd/ci/`.
3. Read it at use time, not once at init, unless the reader can only apply it at startup. A safety gate falls
   back to its closed state when the flag cannot be read.
   ```python
   from lib.feature_flags import get_flag_client, init_flag_domain

   init_flag_domain("game")
   value = get_flag_client("game").get_integer_value("my_new_flag", 10)
   ```
4. If something must change it at runtime, add a registry row (`registry.yaml` and flags.md §13, same PR).
5. Add the catalog row to [specs/flags.md](specs/flags.md) and run `make specs`.

To remove a flag, delete it from both baseline files, remove every reader, writer and registry row, delete its
catalog row and run `make specs`. Do not add a flag nothing reads.

## 5. Check it

- Effective value: `scripts/flags.sh get <set> <flag>`.
- Evaluations: the OpenFeature hook metrics `feature_flag_evaluation_*` and the `feature_flag` span events
  ([specs/telemetry.md](specs/telemetry.md)); Grafana dashboard **Flags / OpenFeature** (uid `feature-flags`).
- No change seen: confirm the flag has a registry row (or that the baseline was restarted), then check the
  Reload column: a startup flag needs a restart of its reader.
- Tests: `lib/tests/` (Python client), `services/agent/flags/` (agent), `services/flag-service/` (registry and
  API), `tests/service/test_flag_propagation.py` (NFR-050).
