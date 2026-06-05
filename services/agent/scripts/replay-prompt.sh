#!/usr/bin/env bash
#
# replay-prompt.sh — offline replay of a captured agent.llm.prompt (#739, M4 spike).
#
# The agent does not call an LLM yet: on every llm-mode cycle it CAPTURES the
# prompt it WOULD send onto an `agent.llm.prompt` span (decision/llm_capture.go).
# This script feeds one captured prompt to the `claude` CLI in headless mode so
# you can eyeball whether a real model returns a reply that matches the RESPONSE
# CONTRACT (the single JSON object documented in services/agent/llm/prompt.go and
# docs/research/739-prompt-capture.md).
#
# Usage:
#   scripts/replay-prompt.sh <system-prompt-file> <user-prompt-file>
#
# How to get the two files, from a captured span in Jaeger:
#   1. Open http://localhost:8080/jaeger/ and find a span named `agent.llm.prompt`.
#   2. Copy the `llm.prompt.system` attribute value into a file, e.g. system.txt.
#   3. Copy the `llm.prompt.user`   attribute value into a file, e.g. user.txt.
#   4. Run:  scripts/replay-prompt.sh system.txt user.txt
#   5. Eyeball the printed response: does it parse as a single JSON object with
#      the contract fields (intervention, target_serial, value, reason,
#      objective_served)?  Pipe through `jq .` to confirm it is valid JSON.
#
# This script intentionally does NOT validate the JSON itself — the spike is a
# manual eyeball. It only invokes the CLI; #741 (resolve_backend) will wire a
# real backend and #742 settles Claude auth (Claude Max sub, no API key — the
# `claude` CLI headless path is the current candidate).
#
# It ALSO replays post-game `agent.llm.retro` spans (#844) identically: copy the
# `llm.retro.system` / `llm.retro.user` attribute values out of an
# `agent.llm.retro` span (instead of llm.prompt.system/user) into the two files
# and run the same way.

set -euo pipefail

usage() {
	cat >&2 <<'EOF'
usage: replay-prompt.sh <system-prompt-file> <user-prompt-file>

  <system-prompt-file>  file holding the captured llm.prompt.system attribute
  <user-prompt-file>    file holding the captured llm.prompt.user attribute

Copy both attribute values out of an `agent.llm.prompt` span in Jaeger.
EOF
	exit 2
}

if [ "$#" -ne 2 ]; then
	usage
fi

system_file="$1"
user_file="$2"

for f in "$system_file" "$user_file"; do
	if [ ! -r "$f" ]; then
		echo "replay-prompt.sh: cannot read file: $f" >&2
		exit 1
	fi
done

# Headless invocation: the captured system prompt is appended to the CLI's
# default system prompt, the captured user prompt is the single -p turn. Output
# is the raw model response — pipe through `jq .` yourself to confirm it parses
# as the contract. (--append-system-prompt is the headless seam the `claude` CLI
# exposes; see `claude -p --help`.)
exec claude -p \
	--append-system-prompt "$(cat "$system_file")" \
	"$(cat "$user_file")"
