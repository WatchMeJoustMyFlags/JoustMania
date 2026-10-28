#!/usr/bin/env bash
# Retention for the otel-collector content-hash tags in GHCR (#1787).
#
# CI pushes ghcr.io/<repo>/otel-collector:<hash> (16 hex chars, see
# collector-image-hash.sh) for every new content of images/otel-collector/**.
# Nothing removed them. This lists the package versions and deletes the old
# hash-only ones.
#
# A version is a deletion CANDIDATE only when it has at least one tag and EVERY
# tag matches ^[0-9a-f]{16}$. Release tags (X.Y.Z), latest, main, <sha> (40 hex)
# and untagged versions (multi-arch manifests) are never candidates. Of the
# candidates, kept are: the KEEP newest (created_at), any version tagged with
# the hash HEAD produces, and any version tagged with a 16-hex token found in
# .env / docker-compose*.yml at the latest release tag. The rest is deleted.
#
# Usage: scripts/ci/prune-collector-tags.sh
# Env:   REPO          owner/repo (default $GITHUB_REPOSITORY), lower-cased
#        KEEP          newest hash versions to keep (default 5)
#        DRY_RUN       1 (default) only lists; set 0 to delete
#        GH            gh binary (default gh; tests use a fake)
#        RELEASE_REF   git ref of the latest release (default: newest
#                      joustmania-v* / v* tag; none found = HEAD only)
#        CURRENT_HASH  override of the HEAD hash (tests)
set -euo pipefail

GH="${GH:-gh}"
KEEP="${KEEP:-5}"
DRY_RUN="${DRY_RUN:-1}"
REPO="${REPO:-${GITHUB_REPOSITORY:-}}"
REPO="${REPO,,}"
[[ "$REPO" == */* ]] || { echo "ERROR: REPO must be owner/repo" >&2; exit 2; }
[[ "$KEEP" =~ ^[0-9]+$ ]] || { echo "ERROR: KEEP must be a number" >&2; exit 2; }
[[ "$DRY_RUN" == 0 || "$DRY_RUN" == 1 ]] || { echo "ERROR: DRY_RUN must be 0 or 1" >&2; exit 2; }

owner="${REPO%%/*}"
pkg_enc="${REPO#*/}%2Fotel-collector"
base="/orgs/${owner}/packages/container/${pkg_enc}/versions"

# Hashes that must survive: what main produces now ...
protect=" "
current="${CURRENT_HASH:-$(bash "$(dirname "${BASH_SOURCE[0]}")/collector-image-hash.sh")}"
protect+="${current} "
# ... and any 16-hex tag the latest release's compose files / .env reference.
release_ref="${RELEASE_REF:-$(git tag --list 'joustmania-v*' --sort=-v:refname | head -n1)}"
[[ -n "$release_ref" ]] || release_ref="$(git tag --list 'v[0-9]*' --sort=-v:refname | head -n1)"
if [[ -n "$release_ref" ]]; then
  for f in .env $(git ls-tree --name-only "$release_ref" | grep -E '^docker-compose.*\.yml$' || true); do
    for h in $(git show "${release_ref}:${f}" 2>/dev/null | grep -i collector | grep -oE '(^|[^0-9a-f])[0-9a-f]{16}([^0-9a-f]|$)' | grep -oE '[0-9a-f]{16}' || true); do
      protect+="${h} "
    done
  done
fi
echo "protected hashes:${protect}(release ref: ${release_ref:-none})"

versions="$("$GH" api --paginate "$base?per_page=100" | jq -s 'add // []')"

# id, created_at, space-joined tags of every hash-only version, newest first.
mapfile -t candidates < <(jq -r '
  map(select((.metadata.container.tags // []) as $t
             | ($t | length) > 0 and all($t[]; test("^[0-9a-f]{16}$"))))
  | sort_by(.created_at) | reverse | .[]
  | "\(.id) \((.metadata.container.tags) | join(","))"' <<<"$versions")

echo "hash-only versions: ${#candidates[@]}, keeping newest ${KEEP}"
deleted=0
idx=0
for line in "${candidates[@]}"; do
  id="${line%% *}"; tags="${line#* }"
  idx=$((idx + 1))
  if ((idx <= KEEP)); then echo "keep   ${id} ${tags} (newest ${idx})"; continue; fi
  keep_reason=""
  for t in ${tags//,/ }; do [[ "$protect" == *" ${t} "* ]] && keep_reason="referenced"; done
  if [[ -n "$keep_reason" ]]; then echo "keep   ${id} ${tags} (${keep_reason})"; continue; fi
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "would delete ${id} ${tags}"
  else
    "$GH" api -X DELETE "${base}/${id}" >/dev/null
    echo "deleted ${id} ${tags}"
  fi
  deleted=$((deleted + 1))
done
echo "summary: candidates=${#candidates[@]} $([[ $DRY_RUN == 1 ]] && echo would_delete || echo deleted)=${deleted}"
