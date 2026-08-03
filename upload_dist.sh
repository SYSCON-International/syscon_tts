#!/bin/bash
#
# Upload everything in dist/ to PyPI.
#
# Mirrors django-search-filter-sort's upload_dist.sh. Credentials come from
# ~/.pypirc; no token is read or written by this script.
#
# Do NOT use this for the very first upload of a new project. Run this instead,
# so the token for this specific project is selected explicitly:
#
#     twine upload --repository syscon_tts dist/*
#
# where `syscon_tts` is the section name in ~/.pypirc (see CONTRIBUTING.md).
# After the project exists on PyPI, this script is the normal path.
#
# Usage: ./upload_dist.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [ ! -d dist ] || [ -z "$(ls -A dist 2>/dev/null)" ]; then
  echo "error: dist/ is empty. Run ./create_dist.sh first." >&2
  exit 1
fi

PYTHON="${PYTHON:-python}"

# --skip-existing matches the convention used by the other Syscon package: it
# lets a re-run finish cleanly when some files are already uploaded. Be aware
# it also means forgetting to bump the version fails silently rather than
# loudly -- check `syscon-tts --version` against PyPI if an upload seems to
# have done nothing.
"$PYTHON" -m twine upload --skip-existing dist/*
