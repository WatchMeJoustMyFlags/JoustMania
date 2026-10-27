#!/usr/bin/env bash
# Shared registry reader for the CI image-freshness guards
# (check-retag-source.sh, check-main-tag-order.sh). Source it, do not run it.

# read_image_config <ref>: print the amd64 image config JSON of <ref> (no layers
# pulled). An index yields a {platform: config} map, a single image the config
# itself. Returns 1 when the tag is missing (registry says not found / 404) and
# 3 for any other error (auth, network, registry); the error text is printed to
# stderr in that case so a hidden outage is not mistaken for a missing tag (#2120).
# is_registry_not_found <err-file>: true when the registry said the tag/manifest
# is missing. Deliberately narrow: a bare "404" (port, IP) or "executable file
# not found in $PATH" (missing credential helper) is an error, not a missing tag.
is_registry_not_found() {
  grep -qiE 'manifest unknown|name unknown|HTTP 404|404 Not Found|: not found$' "$1" &&
    ! grep -qi 'executable file not found' "$1"
}

read_image_config() {
  local config_json err_file rc=0
  err_file="$(mktemp)"
  config_json="$(docker buildx imagetools inspect "$1" --format '{{json .Image}}' 2>"$err_file")" || rc=1
  if [[ "$rc" == 0 && ( -z "$config_json" || "$config_json" == "null" ) ]]; then rc=1; fi
  if [[ "$rc" != 0 ]]; then
    if is_registry_not_found "$err_file" || [[ ! -s "$err_file" ]]; then
      rc=1
    else
      head -c 300 "$err_file" >&2
      rc=3
    fi
    rm -f "$err_file"
    return "$rc"
  fi
  rm -f "$err_file"
  jq -c 'if has("linux/amd64") then .["linux/amd64"] else . end' <<<"$config_json"
}
