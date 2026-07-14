#!/usr/bin/env bash
#
# Download the Piper voice models listed in config/voices.json into the
# voices/ directory. Run this ONCE on a machine with internet access (or on
# the APU during initial provisioning). After this, the service runs offline.
#
# Usage:
#   scripts/download_voices.sh              # download all voices in the manifest
#   scripts/download_voices.sh en_us_amy    # download only specific voice id(s)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="${PLANTSTAR_TTS_MANIFEST:-$ROOT/config/voices.json}"
VOICES_DIR="${PLANTSTAR_TTS_VOICES_DIR:-$ROOT/voices}"

mkdir -p "$VOICES_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required to parse the manifest" >&2
  exit 1
fi

# Pick a downloader.
if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fSL --retry 3 -o "$1" "$2"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -O "$1" "$2"; }
else
  echo "error: need curl or wget to download voice models" >&2
  exit 1
fi

# Emit "id<TAB>filename<TAB>url" rows for the requested voices (or all).
rows="$(python3 - "$MANIFEST" "$@" <<'PY'
import json, sys
manifest = sys.argv[1]
wanted = set(sys.argv[2:])
data = json.load(open(manifest, encoding="utf-8"))
for v in data.get("voices", []):
    if wanted and v["id"] not in wanted:
        continue
    print(f'{v["id"]}\t{v["model"]}\t{v["model_url"]}')
    print(f'{v["id"]}\t{v["config"]}\t{v["config_url"]}')
PY
)"

if [ -z "$rows" ]; then
  echo "No matching voices found in $MANIFEST" >&2
  exit 1
fi

while IFS=$'\t' read -r vid fname url; do
  [ -z "$fname" ] && continue
  dest="$VOICES_DIR/$fname"
  if [ -s "$dest" ]; then
    echo "  ok   $fname (already present)"
    continue
  fi
  if [ -z "$url" ]; then
    echo "  skip $fname (no url in manifest for '$vid')" >&2
    continue
  fi
  echo "  get  $fname"
  fetch "$dest.part" "$url"
  mv "$dest.part" "$dest"
done <<< "$rows"

echo "Done. Models are in: $VOICES_DIR"
