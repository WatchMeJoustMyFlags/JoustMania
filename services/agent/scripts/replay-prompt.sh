#!/usr/bin/env bash
#
# replay-prompt.sh — offline replay of a captured agent.llm.retro prompt (#844).
#
# The post-game retro records the prompt it sends to the analyst on an
# `agent.llm.retro` span. This script feeds one captured prompt to the `claude`
# CLI in headless mode so you can eyeball the reply against the RESPONSE CONTRACT
# (services/agent/llm/retro.go), with or without AGENT_INFERENCE_BACKEND=claude.
# (The in-game agent.llm.prompt capture it was written for was retired in #1234.)
#
# Usage:
#   scripts/replay-prompt.sh <system-prompt-file> <user-prompt-file>
#
# How to get the two files from a captured span in Jaeger (#1168: the span carries
# only a fingerprint of the system prompt, not its text — the full text is on a log):
#   1. Open http://localhost:8080/jaeger/ and find a span named `agent.llm.retro`.
#   2. Copy the `llm.retro.user` attribute value into a file, e.g. user.txt.
#   3. Read the span's `llm.retro.system_sha256` attribute (a 16-hex fingerprint)
#      and pull the full system text from the `agent.llm.system_prompt` log line:
#        docker logs joustmania-agent 2>&1 \
#          | grep agent.llm.system_prompt | grep <system_sha256> | tail -1
#      Copy that line's `system` field value into a file, e.g. system.txt.
#   4. Run:  scripts/replay-prompt.sh system.txt user.txt | jq .

set -euo pipefail

usage() {
	cat >&2 <<'EOF'
usage: replay-prompt.sh <system-prompt-file> <user-prompt-file>

  <system-prompt-file>  file holding the `system` field from the
                        `agent.llm.system_prompt` log line keyed by the span's
                        llm.retro.system_sha256 (the span has only the hash)
  <user-prompt-file>    file holding the captured llm.retro.user attribute

Copy llm.retro.user out of an `agent.llm.retro` span in Jaeger; resolve the
system text from the `agent.llm.system_prompt` log line by the span's
llm.retro.system_sha256 hash.
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
