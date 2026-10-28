#!/usr/bin/env bash
# UC: none (CI tooling, #1787)
# Tests for scripts/ci/prune-collector-tags.sh: only hash-only otel-collector
# versions beyond the keep window and not referenced are deleted. Uses a fake
# `gh` (GH=...) serving a canned version list and logging DELETE calls; the real
# GHCR API is never called.
#
#   ./scripts/tests/test-prune-collector-tags.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRUNE="${SCRIPT_DIR}/../ci/prune-collector-tags.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

cat >"${WORK}/gh" <<'FAKE'
#!/usr/bin/env bash
echo "$*" >>"${FAKE_LOG}"
if [[ "$1 $2" == "api --paginate" ]]; then
  [[ "$3" == /orgs/o/packages/container/r%2Fotel-collector/versions* ]] || { echo "bad path $3" >&2; exit 1; }
  cat "${FAKE_VERSIONS}"   # two pages, as --paginate prints them
  [[ -z "${FAKE_LIST_FAIL:-}" ]] || exit 1
fi
FAKE
chmod +x "${WORK}/gh"

# Versions (id: tags, created): 11-17 hash-only, oldest = 11. Page 1 / page 2.
cat >"${WORK}/versions" <<'JSON'
[
 {"id":1,"created_at":"2026-01-01T00:00:00Z","metadata":{"container":{"tags":["0.7.0"]}}},
 {"id":2,"created_at":"2026-01-02T00:00:00Z","metadata":{"container":{"tags":["main","latest"]}}},
 {"id":3,"created_at":"2026-01-03T00:00:00Z","metadata":{"container":{"tags":["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}}},
 {"id":4,"created_at":"2026-01-04T00:00:00Z","metadata":{"container":{"tags":[]}}},
 {"id":5,"created_at":"2026-01-05T00:00:00Z","metadata":{"container":{"tags":["0123456789abcdef","main"]}}},
 {"id":17,"created_at":"2026-02-17T00:00:00Z","metadata":{"container":{"tags":["1700000000000017"]}}},
 {"id":16,"created_at":"2026-02-16T00:00:00Z","metadata":{"container":{"tags":["1600000000000016"]}}}
]
[
 {"id":15,"created_at":"2026-02-15T00:00:00Z","metadata":{"container":{"tags":["1500000000000015"]}}},
 {"id":14,"created_at":"2026-02-14T00:00:00Z","metadata":{"container":{"tags":["1400000000000014"]}}},
 {"id":13,"created_at":"2026-02-13T00:00:00Z","metadata":{"container":{"tags":["1300000000000013"]}}},
 {"id":12,"created_at":"2026-02-12T00:00:00Z","metadata":{"container":{"tags":["1200000000000012"]}}},
 {"id":11,"created_at":"2026-02-11T00:00:00Z","metadata":{"container":{"tags":["1100000000000011"]}}},
 {"id":10,"created_at":"2026-02-10T00:00:00Z","metadata":{"container":{"tags":["1000000000000010"]}}}
]
JSON

# Repo whose latest release tag references hash 1100000000000011.
REPO_DIR="${WORK}/repo"
git init -q "${REPO_DIR}"
(
  cd "${REPO_DIR}"
  echo "OTEL_COLLECTOR_TAG=1100000000000011" >.env
  git add .env
  git -c user.name=t -c user.email=t@t commit -qm init
  git tag joustmania-v0.7.0
)

run() { # run <desc> [env...]; leaves output in ${WORK}/out, deletes in ${WORK}/log
  : >"${WORK}/log"
  (cd "${REPO_DIR}" && env GH="${WORK}/gh" FAKE_LOG="${WORK}/log" FAKE_VERSIONS="${WORK}/versions" \
    REPO=O/R CURRENT_HASH=1500000000000015 "$@" bash "$PRUNE") >"${WORK}/out" 2>&1 \
    || { cat "${WORK}/out" >&2; fail "script failed"; }
}
deletes() { grep -c -- '-X DELETE' "${WORK}/log" || true; }

# Dry run: nothing deleted, but the would-delete list is printed.
run KEEP=3 DRY_RUN=1
[[ "$(deletes)" == 0 ]] || fail "dry run called DELETE"
grep -q 'would delete 10 ' "${WORK}/out" || fail "dry run did not list id 10"

# Real run: keep newest 3 (17,16,15), 11 is referenced by the release, 14 not newest... 
run KEEP=3 DRY_RUN=0
mapfile -t gone < <(grep -- '-X DELETE' "${WORK}/log" | sed 's|.*/versions/||' | sort -n)
[[ "${gone[*]}" == "10 12 13 14" ]] || fail "deleted: ${gone[*]} (want 10 12 13 14)"
# Never: release / main / latest / 40-hex / untagged / mixed-tag versions.
for id in 1 2 3 4 5 11 15 16 17; do
  ! grep -q -- "/versions/${id}\$" "${WORK}/log" || fail "version ${id} must not be deleted"
done

# The HEAD hash survives even outside the keep window.
run KEEP=1 DRY_RUN=0 CURRENT_HASH=1000000000000010
! grep -q -- '/versions/10$' "${WORK}/log" || fail "current hash version deleted"
grep -q -- '/versions/16$' "${WORK}/log" || fail "KEEP=1 should delete 16"

# Bad input is refused before any API call.
: >"${WORK}/log"
if (cd "${REPO_DIR}" && env GH="${WORK}/gh" FAKE_LOG="${WORK}/log" REPO=O/R KEEP=x bash "$PRUNE") >/dev/null 2>&1; then
  fail "KEEP=x accepted"
fi
[[ ! -s "${WORK}/log" ]] || fail "API called on bad input"

# KEEP=0 is refused too.
if (cd "${REPO_DIR}" && env GH="${WORK}/gh" FAKE_LOG="${WORK}/log" REPO=O/R KEEP=0 bash "$PRUNE") >/dev/null 2>&1; then
  fail "KEEP=0 accepted"
fi

# Listing failure after page 1: abort, non-zero, no DELETE.
: >"${WORK}/log"
if (cd "${REPO_DIR}" && env GH="${WORK}/gh" FAKE_LOG="${WORK}/log" FAKE_VERSIONS="${WORK}/versions" \
    FAKE_LIST_FAIL=1 REPO=O/R CURRENT_HASH=1500000000000015 KEEP=3 DRY_RUN=0 bash "$PRUNE") >/dev/null 2>&1; then
  fail "listing failure did not fail the script"
fi
[[ "$(deletes)" == 0 ]] || fail "DELETE called after listing failure"

echo "PASS: test-prune-collector-tags"
