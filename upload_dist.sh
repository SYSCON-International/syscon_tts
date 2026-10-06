#!/bin/bash
#
# Upload everything in dist/ to PyPI.
#
# Mirrors django-search-filter-sort's upload_dist.sh. Credentials come from
# ~/.pypirc; no token is read or written by this script.
#
# The upload targets the `syscon_tts` section of ~/.pypirc, which holds this
# project's own API token (see CONTRIBUTING.md). Naming that section matters
# once the token is project-scoped: with no repository selected, twine falls
# through to the generic [pypi] section, so a project-scoped token sitting in
# [syscon_tts] would never be read and the upload would go out under whatever
# account-wide token [pypi] happens to hold.
#
# Override to publish through a different ~/.pypirc section -- rehearsing
# against TestPyPI, say:
#
#     TWINE_REPOSITORY=testpypi ./upload_dist.sh
#
# Usage: ./upload_dist.sh [extra twine upload options]

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [ ! -d dist ] || [ -z "$(ls -A dist 2>/dev/null)" ]; then
  echo "error: dist/ is empty. Run ./create_dist.sh first." >&2
  exit 1
fi

PYTHON="${PYTHON:-python}"

# twine takes the ~/.pypirc section name from TWINE_REPOSITORY, so a default
# here is equivalent to passing --repository while still letting the caller
# override it. twine fails loudly if the section is absent, which is the right
# outcome -- silently uploading under the wrong token is the failure to avoid.
export TWINE_REPOSITORY="${TWINE_REPOSITORY:-syscon_tts}"

# No --skip-existing by default. With it, forgetting to bump the version
# "succeeds" while uploading nothing. Without it PyPI refuses a file that
# already exists and twine fails loudly, which is what you want.
# To resume an upload that was interrupted part-way, pass it explicitly:
#
#     ./upload_dist.sh --skip-existing
#
echo "Uploading $(ls dist | tr '\n' ' ')using the '${TWINE_REPOSITORY}' section of ~/.pypirc"
"$PYTHON" -m twine upload "$@" dist/*
