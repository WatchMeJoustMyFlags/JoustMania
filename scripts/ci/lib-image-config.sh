#!/usr/bin/env bash
# Shared registry reader for the CI image-freshness guards
# (check-retag-source.sh, check-main-tag-order.sh). Source it, do not run it.

# read_image_config <ref>: print the amd64 image config JSON of <ref> (no layers
# pulled). An index yields a {platform: config} map, a single image the config
# itself. Returns 1 when the tag is missing.
read_image_config() {
  local config_json
  if ! config_json="$(docker buildx imagetools inspect "$1" --format '{{json .Image}}' 2>/dev/null)" ||
    [[ -z "$config_json" || "$config_json" == "null" ]]; then
    return 1
  fi
  jq -c 'if has("linux/amd64") then .["linux/amd64"] else . end' <<<"$config_json"
}
