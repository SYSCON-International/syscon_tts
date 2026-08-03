#!/usr/bin/env bash
#
# Build and upload syscon-tts to PyPI from a local checkout.
#
# The CI workflow (.github/workflows/release.yml) is the preferred path -- it
# builds from a clean tagged checkout rather than whatever is in your working
# directory. Use this when you need to publish by hand or to TestPyPI.
#
# Credentials come from ~/.pypirc, the same file django-search-filter-sort
# uploads with. No token is read or written by this script.
#
# Usage:
#   scripts/release.sh                 # build, check, upload to PyPI
#   scripts/release.sh --test          # upload to TestPyPI instead
#   scripts/release.sh --build-only    # build and check, upload nothing
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TARGET="pypi"
UPLOAD=1
for arg in "$@"; do
  case "$arg" in
    --test)       TARGET="testpypi" ;;
    --build-only) UPLOAD=0 ;;
    -h|--help)    sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PYTHON="${PYTHON:-python}"

VERSION="$("$PYTHON" - <<'PY'
import re, pathlib
text = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
print(re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1))
PY
)"
echo "==> Version: $VERSION"

# A dirty tree means the artifact would not match any commit, so nobody could
# later reproduce what was published.
if [ -n "$(git status --porcelain)" ]; then
  echo "warning: working tree is dirty; the upload will not match any commit" >&2
  if [ "$UPLOAD" -eq 1 ]; then
    read -r -p "Continue anyway? [y/N] " reply
    [ "$reply" = "y" ] || [ "$reply" = "Y" ] || { echo "aborted"; exit 1; }
  fi
fi

echo "==> Running tests"
"$PYTHON" -m pytest -q

echo "==> Cleaning previous builds"
rm -rf dist build src/*.egg-info

echo "==> Building sdist and wheel"
"$PYTHON" -m pip install --quiet --upgrade build twine
"$PYTHON" -m build

echo "==> Checking metadata"
"$PYTHON" -m twine check dist/*

if [ "$UPLOAD" -eq 0 ]; then
  echo "Built (upload skipped):"
  ls -1 dist
  exit 0
fi

# Deliberately no --skip-existing: if this version is already on PyPI the
# upload should fail loudly, because it means the version was not bumped.
# PyPI never allows replacing a released version.
if [ "$TARGET" = "testpypi" ]; then
  echo "==> Uploading $VERSION to TestPyPI"
  "$PYTHON" -m twine upload --repository testpypi dist/*
  echo "Install with: pip install -i https://test.pypi.org/simple/ syscon-tts==$VERSION"
else
  echo "==> Uploading $VERSION to PyPI"
  "$PYTHON" -m twine upload dist/*
  echo "Install with: pip install syscon-tts==$VERSION"
fi

echo
echo "Tag the release so the repo matches what was published:"
echo "  git tag v$VERSION && git push origin v$VERSION"
