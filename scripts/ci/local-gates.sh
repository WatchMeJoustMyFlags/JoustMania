#!/usr/bin/env bash
# Local PR gates: run the checks that matter for what this branch changed.
#
#   make pr-gates                          # auto-detect from git diff
#   make pr-gates SERVICE=menu,agent       # explicit (comma or space separated)
#   scripts/ci/local-gates.sh menu agent   # same, positional
#   scripts/ci/local-gates.sh --plan       # print the selected gates, run nothing
#   scripts/ci/local-gates.sh --print-cmd service-touched   # print one gate's command (tests)
#
# Always runs `make lint` (ruff + scripts/tests) and the spec lint gate.
# Everything else is selected from the changed files: the union of
# `git diff --name-only $BASE...HEAD`, uncommitted changes and untracked files.
# Except for the service tier, the mapping mirrors the `changes` job's path
# filters in .github/workflows/ci.yml (each case below names its filter); keep
# those in step. The service tier (T2, minutes, flaky under load) is
# deliberately a SUBSET of the CI filter: PR CI "Service Tests" runs the full
# tier for the CI filter's paths, and the merge queue always runs it. Do not
# widen the local lists back to the CI filter.
# grafana-json, compose and yaml-dupkeys are local-only extras.
#
# Service tier, two lanes:
#   service          full `make test-service`. Selected by harness / stack
#                    changes: any non-test_*.py file under tests/service/
#                    (except README.md), tests/contract/{flagd_harness,infra_config}.py,
#                    tests/integration/{helpers,flag_service}.py,
#                    tests/integration/fake_openai/**, docker-compose.yml,
#                    services/flagd/**, services/flag-service/**, proto/**,
#                    pyproject.toml, uv.lock, and the game_coordinator,
#                    menu and controller_manager server.py files.
#   service-touched `make test-service SERVICE_TESTS="<files>"`: only the
#                    tests/service/**/test_*.py files the diff adds or edits
#                    (existing files only). Dropped when `service` is selected.
#   Everything else (lib/**, other service code, agent, rust-hid, Makefile,
#   ci.yml) runs no service tier locally; the summary says so.
#
# Services / gate keys accepted by SERVICE:
#   lib game_coordinator menu controller_manager audio pairing_daemon python_hid operator_pad
#   agent flag-service connect-proxy agent-protos dashboard rust-hid
#   contract contract-flagd service service-touched grafana compose voice
#   (audio also selects voice-check)
#   (hyphens and underscores are interchangeable)
#
# Env:
#   BASE            diff base (default origin/main)
#   SERVICE         explicit gate keys; disables auto-detection of gates
#   PR_LABELS       comma list for the spec-touched gate (default: read from
#                   the branch's PR with gh, empty if there is none)
#   PR_GATES_FILES  file with a newline list of changed files (tests; skips git)
#   NODE24_DIR      Node 24 install dir (default: newest ~/.nvm/versions/node/v24.*)
#
# Output: one summary line per gate (PASS/FAIL/SKIP + duration), then the full
# output of failing gates only. Exit status 1 if any gate failed.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT" || exit 2
BASE="${BASE:-origin/main}"

# Per-package pytest invocation, verified against each pyproject (all seven
# declare a `test` extra). Change a row here if a package's setup changes.
declare -A PYTEST_CMD=(
  [lib]="uv run --extra test pytest -q --tb=short -p no:cacheprovider"
  [game_coordinator]="uv run --extra test pytest -q --tb=short -p no:cacheprovider"
  [menu]="uv run --extra test pytest -q --tb=short -p no:cacheprovider"
  [controller_manager]="uv run --extra test pytest -q --tb=short -p no:cacheprovider"
  [audio]="uv run --extra test pytest -q --tb=short -p no:cacheprovider"
  [pairing_daemon]="uv run --extra test pytest -q --tb=short -p no:cacheprovider"
  [python_hid]="uv run --extra test pytest -q --tb=short -p no:cacheprovider"
  [operator_pad]="uv run --extra test pytest -q --tb=short -p no:cacheprovider"
)
PY_SERVICES=(game_coordinator menu controller_manager audio pairing_daemon python_hid operator_pad)

# Gate order in the summary.
ALL_GATES=(lint spec-lint spec-touched
  py:lib py:game_coordinator py:menu py:controller_manager py:audio py:pairing_daemon py:python_hid py:operator_pad
  go:agent go:flag-service connect-proxy agent-protos dashboard rust-hid
  voice-check contract contract-flagd service service-touched grafana-json compose yaml-dupkeys)

plan_only=false
print_cmd=""
explicit=()
for arg in "$@"; do
  if [[ "$print_cmd" == next ]]; then print_cmd="$arg"; continue; fi
  case "$arg" in
    --plan) plan_only=true ;;
    --print-cmd) print_cmd=next ;;
    -h|--help) sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) explicit+=("$arg") ;;
  esac
done
if [[ -n "${SERVICE:-}" ]]; then
  read -r -a from_env <<<"${SERVICE//,/ }"
  explicit+=("${from_env[@]}")
fi

declare -A selected=([lint]=1 [spec-lint]=1)
declare -A changed_yaml=() changed_dash=() touched_service=()
ci_service=0   # the CI service_tests filter matched (summary note only)

select_key() {  # map a SERVICE key to gates
  local key="${1//-/_}"
  case "$key" in
    lib) selected[py:lib]=1 ;;
    game_coordinator|menu|controller_manager|pairing_daemon|python_hid|operator_pad) selected[py:$key]=1 ;;
    audio) selected[py:audio]=1; selected[voice-check]=1 ;;
    voice|voice_check) selected[voice-check]=1 ;;
    agent) selected[go:agent]=1 ;;
    flag_service) selected[go:flag-service]=1 ;;
    connect_proxy) selected[connect-proxy]=1 ;;
    agent_protos) selected[agent-protos]=1 ;;
    dashboard) selected[dashboard]=1 ;;
    rust_hid|rust) selected[rust-hid]=1 ;;
    contract) selected[contract]=1 ;;
    contract_flagd|flagd) selected[contract-flagd]=1 ;;
    service|service_tests) selected[service]=1 ;;
    service_touched) selected[service-touched]=1 ;;
    grafana) selected[grafana-json]=1 ;;
    compose) selected[compose]=1 ;;
    *) echo "local-gates: unknown SERVICE key '$1'" >&2; exit 2 ;;
  esac
}

classify() {  # map one changed path to gates; each case mirrors a ci.yml filter
  local f="$1"
  # shared_python: every Python service job.
  case "$f" in
    pyproject.toml|uv.lock|proto/*|lib/*)
      for s in "${PY_SERVICES[@]}"; do selected[py:$s]=1; done ;;
  esac
  # controller_manager / game_coordinator / menu / audio / pairing_daemon / python_hid / operator_pad
  case "$f" in
    services/game_coordinator/*|services/menu/*|services/controller_manager/*|services/audio/*|services/pairing_daemon/*|services/python_hid/*|services/operator_pad/*)
      local s="${f#services/}"; selected[py:${s%%/*}]=1 ;;
  esac
  # lib (lib-tests)
  case "$f" in
    lib/*|proto/*|services/flagd/*|services/agent/experiment/targeting.go|tests/service/test_experiment_shadow_isolation.py|tools/demo/*)
      selected[py:lib]=1 ;;
  esac
  # voice catalog (#2107): the Voice catalog check step of the audio service
  # checks (audio filter); the generator itself is covered in CI by the
  # Telemetry Contract job (tests/contract/test_gen_voice.py)
  case "$f" in
    services/audio/voice/*|services/audio/assets/*|scripts/tools/gen_voice.py|scripts/tools/gen-voice.sh)
      selected[voice-check]=1 ;;
  esac
  # rust (rust-checks)
  case "$f" in
    services/rust-hid/*|proto/psmove_hid.proto) selected[rust-hid]=1 ;;
  esac
  # agent_tests (agent-tests)
  case "$f" in
    services/agent/*|services/flagd/*|services/flag-service/registry.yaml) selected[go:agent]=1 ;;
  esac
  # flag_service_tests (flag-service-tests)
  case "$f" in
    services/flag-service/*|services/flagd/*|docs/specs/flags.md) selected[go:flag-service]=1 ;;
  esac
  # connect_proxy (image build buf-generates from proto/)
  case "$f" in
    services/connect-proxy/*|proto/*) selected[connect-proxy]=1 ;;
  esac
  # proto_pkg, agent Go stub step only (#1801; the Python stub check has no local gate)
  case "$f" in
    proto/*|services/agent/gen/*|scripts/ci/gen-agent-protos.sh|scripts/ci/validate-agent-protos.sh)
      selected[agent-protos]=1 ;;
  esac
  # dashboard
  case "$f" in
    services/dashboard/*) selected[dashboard]=1 ;;
  esac
  # telemetry (telemetry-contract)
  case "$f" in
    services/*|lib/*|proto/*|tests/contract/*|tests/integration/setup_guard.py|docker-compose.yml|docs/specs/*|scripts/*|tools/*|Makefile)
      selected[contract]=1 ;;
  esac
  # flagd_contract (flagd-contract)
  case "$f" in
    services/flagd/*|services/agent/*|lib/*|services/game_coordinator/*|tests/contract/*|docker-compose.yml|Makefile|.github/workflows/ci.yml)
      selected[contract-flagd]=1 ;;
  esac
  # service_tests (service-tests, T2): the CI filter, kept only to word the
  # summary note. Local selection is the subset below.
  case "$f" in
    lib/*|proto/*|pyproject.toml|uv.lock|services/flagd/*|services/flag-service/*|services/game_coordinator/*|\
    services/controller_manager/*|services/menu/*|tests/service/*|tests/contract/flagd_harness.py|\
    tests/contract/flagd_rpc_retry.py|tests/contract/infra_config.py|tests/integration/helpers.py|tests/integration/flag_service.py|\
    docker-compose.yml|Makefile|.github/workflows/ci.yml)
      ci_service=1 ;;
  esac
  # Local full tier: harness / stack changes only (see the header).
  case "$f" in
    tests/service/README.md) ;;
    tests/service/*)
      # bash `*` matches `/`, so test the basename: only test_*.py files are
      # targeted; every other file here is shared harness.
      if [[ "${f##*/}" == test_*.py ]]; then
        [[ -f "$f" ]] && touched_service[$f]=1
      else
        selected[service]=1
      fi ;;
    tests/contract/flagd_harness.py|tests/contract/flagd_rpc_retry.py|tests/contract/infra_config.py|\
    tests/integration/helpers.py|tests/integration/flag_service.py|tests/integration/fake_openai/*|docker-compose.yml|\
    services/flagd/*|services/flag-service/*|proto/*|pyproject.toml|uv.lock|\
    services/game_coordinator/server.py|services/menu/server.py|services/controller_manager/server.py)
      selected[service]=1 ;;
  esac
  # Local-only extras.
  case "$f" in
    services/grafana/dashboards/*.json) selected[grafana-json]=1; changed_dash[$f]=1 ;;
  esac
  case "$f" in
    docker-compose*.yml) selected[compose]=1 ;;
  esac
  case "$f" in
    *.yml|*.yaml) [[ -f "$f" ]] && { selected[yaml-dupkeys]=1; changed_yaml[$f]=1; } ;;
  esac
}

if [[ -n "${PR_GATES_FILES:-}" ]]; then
  mapfile -t files <"$PR_GATES_FILES"
else
  mapfile -t files < <({ git diff --name-only "$BASE...HEAD" 2>/dev/null
    git diff --name-only HEAD 2>/dev/null
    git ls-files --others --exclude-standard; } | sort -u)
fi

if [[ ${#explicit[@]} -gt 0 ]]; then
  for k in "${explicit[@]}"; do select_key "$k"; done
  # service-touched needs the changed test files, but only when asked for
  if [[ -n "${selected[service-touched]:-}" ]]; then
    for f in "${files[@]}"; do
      [[ "$f" == tests/service/* && "${f##*/}" == test_*.py && -f "$f" ]] && touched_service[$f]=1
    done
  fi
else
  for f in "${files[@]}"; do [[ -n "$f" ]] && classify "$f"; done
fi
# The full tier subsumes the targeted lane.
if [[ -z "${selected[service]:-}" && ${#touched_service[@]} -gt 0 ]]; then
  selected[service-touched]=1
fi
# explicit SERVICE=service-touched with no changed test file: nothing to run
[[ ${#touched_service[@]} -eq 0 ]] && unset 'selected[service-touched]'
# spec-touched needs a changed-file list; only meaningful in auto mode.
[[ ${#explicit[@]} -eq 0 && ${#files[@]} -gt 0 ]] && selected[spec-touched]=1

service_note() {  # the CI service filter matched but the full tier is not run here
  if [[ $ci_service -eq 1 && -z "${selected[service]:-}" ]]; then
    echo 'note: service tier not run locally; PR CI runs the full tier'
  fi
}

if $plan_only; then
  for g in "${ALL_GATES[@]}"; do [[ -n "${selected[$g]:-}" ]] && echo "$g"; done
  service_note >&2
  exit 0
fi

find_node24() {
  if [[ -n "${NODE24_DIR:-}" ]]; then echo "$NODE24_DIR"; return; fi
  compgen -G "$HOME/.nvm/versions/node/v24.*" >/dev/null || return 1
  printf '%s\n' "$HOME"/.nvm/versions/node/v24.* | sort -V | tail -1
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
printf '%s\n' "${files[@]}" >"$WORK/changed.txt"

gate_cmd() {  # print the shell command for a gate
  local g="$1"
  case "$g" in
    lint) echo "make lint" ;;
    spec-lint) echo "scripts/aiup/spec-lint-gate.sh" ;;
    spec-touched)
      local labels="${PR_LABELS-}"
      if [[ -z "${PR_LABELS+x}" ]]; then
        labels="$(timeout 20 gh pr view --json labels --jq '[.labels[].name] | join(",")' 2>/dev/null || true)"
      fi
      echo "CHANGED_FILES_FILE='$WORK/changed.txt' PR_LABELS='$labels' scripts/aiup/check-spec-touched.sh" ;;
    py:lib) echo "cd lib && ${PYTEST_CMD[lib]}" ;;
    py:*) local s="${g#py:}"; echo "cd services/$s && ${PYTEST_CMD[$s]}" ;;
    go:*) echo "cd services/${g#go:} && go vet ./... && go test -race ./..." ;;
    # Generated code exists only inside the image build: the builder stage runs
    # buf generate, go vet and go test (services/connect-proxy/Dockerfile).
    connect-proxy) echo "docker build -q --target builder -f services/connect-proxy/Dockerfile -t joustmania/connect-proxy-gates:local ." ;;
    # Regenerates services/agent/gen (docker + buf.build) and fails on drift (#1801).
    agent-protos) echo "make ci-validate-agent-protos" ;;
    dashboard)
      local node
      if ! node="$(find_node24)"; then
        echo "echo 'dashboard needs Node 24: install it with nvm (nvm install 24) or set NODE24_DIR' >&2; exit 1"; return
      fi
      echo "export PATH='$node/bin':\"\$PATH\" && cd services/dashboard && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npx tsc --noEmit && npx vitest run" ;;
    voice-check) echo "make voice-check" ;;
    rust-hid) echo "cd services/rust-hid && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test" ;;
    # Same steps as the Telemetry Contract job: generated specs, then the tests.
    contract) echo "make specs-check && make spec-refs-check && uv run --no-project --with pytest --with pyyaml --with pytest-rerunfailures pytest tests/contract -q --tb=short -p no:cacheprovider --ignore tests/contract/test_flagd_resolution.py --ignore tests/contract/test_flagd_rpc_retry.py --ignore tests/contract/test_fractional_bucketing.py" ;;
    contract-flagd) echo "make test-contract-flagd" ;;
    service) echo "make test-service" ;;
    service-touched)
      if [[ ${#touched_service[@]} -eq 0 ]]; then
        echo "local-gates: service-touched: no changed tests/service test files" >&2; return 1
      fi
      local sorted
      sorted="$(printf '%s\n' "${!touched_service[@]}" | sort | tr '\n' ' ')"
      echo "make test-service SERVICE_TESTS='${sorted% }'" ;;
    grafana-json)
      local list=("${!changed_dash[@]}")
      [[ ${#list[@]} -eq 0 ]] && list=(services/grafana/dashboards/*.json)
      echo "for f in ${list[*]}; do python3 -m json.tool \"\$f\" >/dev/null || { echo \"invalid JSON: \$f\"; exit 1; }; done" ;;
    compose)
      # The combinations the Makefile, CI and the runbooks bring up.
      echo "set -e; for c in 'docker-compose.yml' 'docker-compose.yml docker-compose.override.yml' \
'docker-compose.yml docker-compose.ci.yml' 'docker-compose.yml docker-compose.override.yml docker-compose.dev.yml' \
'docker-compose.yml docker-compose.override.yml docker-compose.ci.yml docker-compose.dry-run.yml' \
'docker-compose.lite.yml'; do \
a=(); for f in \$c; do a+=(-f \"\$f\"); done; echo \"compose: \$c\"; \
docker compose \"\${a[@]}\" --profile agent --profile dashboard config -q; done" ;;
    yaml-dupkeys)
      echo "uv run --no-project --with pyyaml python3 scripts/ci/yaml-dupkeys.py ${!changed_yaml[*]}" ;;
  esac
}

if [[ "$print_cmd" == next ]]; then echo "local-gates: --print-cmd needs a gate name" >&2; exit 2; fi
if [[ -n "$print_cmd" ]]; then
  [[ " ${ALL_GATES[*]} " == *" $print_cmd "* ]] || { echo "local-gates: unknown gate '$print_cmd'" >&2; exit 2; }
  gate_cmd "$print_cmd" || exit 1
  exit 0
fi

fmt_dur() { local s=$1; if (( s >= 60 )); then printf '%dm%02ds' $((s/60)) $((s%60)); else printf '%ds' "$s"; fi; }

# Gates run nested tools (make lint runs scripts/tests): do not leak our inputs.
unset SERVICE PR_GATES_FILES

declare -A status=() duration=()
failed=0
for g in "${ALL_GATES[@]}"; do
  if [[ -z "${selected[$g]:-}" ]]; then status[$g]=SKIP; continue; fi
  log="$WORK/${g//[:\/]/_}.log"
  echo ">> $g" >&2
  start=$(date +%s)
  if (cd "$ROOT" && bash -c "$(gate_cmd "$g")") >"$log" 2>&1; then
    status[$g]=PASS
  else
    status[$g]=FAIL; failed=1
  fi
  duration[$g]=$(fmt_dur $(( $(date +%s) - start )))
done

echo
echo "== pr-gates summary (base $BASE, ${#files[@]} changed file(s)) =="
for g in "${ALL_GATES[@]}"; do
  printf '%-22s %-4s %s\n' "$g" "${status[$g]}" "${duration[$g]:-}"
done
service_note
for g in "${ALL_GATES[@]}"; do
  [[ "${status[$g]}" == FAIL ]] || continue
  echo
  echo "== FAIL: $g ($(gate_cmd "$g" | head -c 200)) =="
  cat "$WORK/${g//[:\/]/_}.log"
done
exit "$failed"
