#!/usr/bin/env bash
# Voice catalog generator (#2107, UC-031 BR-007, C-050): runs
# scripts/tools/gen_voice.py with pinned tool dependencies.
#
#   scripts/tools/gen-voice.sh            # make voice: render the lines whose input changed
#   scripts/tools/gen-voice.sh --check    # make voice-check: catalog, manifest and files agree
#   scripts/tools/gen-voice.sh --sample   # make voice-sample: re-render the sample lines, compare
#   scripts/tools/gen-voice.sh --help
#
# Dev or CI only; nothing here runs on the stage Pi. Piper voices run through
# sherpa-onnx (Apache-2.0), never the GPL piper-tts package. The models are
# downloaded once to ~/.cache/joustmania/voice-models (override with
# JOUSTMANIA_VOICE_CACHE) and checked against the sha256 pinned in the catalog.
# ffmpeg comes from the pinned imageio-ffmpeg wheel (set FFMPEG to override).
# Python 3.12 is pinned for the tool only, to avoid wheel gaps; the services
# stay on 3.14.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
command -v uv >/dev/null || { echo "gen-voice: uv not found (https://docs.astral.sh/uv/)" >&2; exit 2; }

for arg in "$@"; do
  if [[ "$arg" == --check ]]; then
    # No TTS and no model download: PyYAML (pinned) is the only dependency.
    exec uv run --no-project --with pyyaml==6.0.3 python "$ROOT/scripts/tools/gen_voice.py" "$@"
  fi
done

exec uv run --no-project --python 3.12 \
  --with sherpa-onnx==1.13.8 \
  --with numpy==2.5.4 \
  --with soxr==1.1.0 \
  --with pyloudnorm==0.2.0 \
  --with imageio-ffmpeg==0.6.0 \
  --with pyyaml==6.0.3 \
  python "$ROOT/scripts/tools/gen_voice.py" "$@"
