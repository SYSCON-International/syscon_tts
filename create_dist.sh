#!/bin/bash
#
# Build the distributions into dist/.
#
# Mirrors django-search-filter-sort's create_dist.sh, with one difference:
# that project runs `python setup.py sdist`, which this project cannot use
# because it has no setup.py -- packaging is declared in pyproject.toml.
#
# `python -m build` is the pyproject-native equivalent. It also produces a
# wheel alongside the sdist, which matters for the APU: installing from an
# sdist makes pip fetch setuptools and run a build step on the target machine,
# while a wheel installs with no build at all. Both belong on PyPI; pip picks
# the wheel automatically.
#
# Before building it refuses to run unless the build would be one release.yml
# could have produced: a clean tree, HEAD exactly origin/main, the test suite
# passing, and a version PyPI does not already have. Without them 0.1.0
# reached PyPI from an untagged tree, carrying `__version__ = "0.0.4"`.
# SKIP_RELEASE_CHECKS=1 skips them for a local rehearsal build -- never upload
# what that produces.
#
# Usage: ./create_dist.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python}"

if [ "${SKIP_RELEASE_CHECKS:-}" = "1" ]; then
  echo "==> SKIP_RELEASE_CHECKS=1: building without release checks. Do not upload this."
else
  echo "==> Checking the tree is clean"
  if [ -n "$(git status --porcelain)" ]; then
    echo "error: uncommitted or untracked changes. Commit or stash them first." >&2
    git status --short >&2
    exit 1
  fi

  echo "==> Checking HEAD is origin/main"
  git fetch --quiet origin main
  if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
    echo "error: HEAD is not origin/main. Release only what is merged and pushed." >&2
    exit 1
  fi

  echo "==> Running the test suite"
  "$PYTHON" -m pytest -q

  echo "==> Checking the version is new to PyPI"
  # The version test in the suite has already proved pyproject and
  # __version__ agree.
  "$PYTHON" - <<'PY'
import sys, urllib.error, urllib.request
try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib
with open("pyproject.toml", "rb") as handle:
    version = tomllib.load(handle)["project"]["version"]
url = f"https://pypi.org/pypi/syscon-tts/{version}/json"
try:
    urllib.request.urlopen(url, timeout=30)
except urllib.error.HTTPError as exc:
    if exc.code == 404:
        print(f"    {version} is not on PyPI yet")
        sys.exit(0)
    raise
sys.exit(f"error: syscon-tts {version} is already on PyPI. Bump the version.")
PY
fi

echo "==> Cleaning previous builds"
rm -rf dist build src/*.egg-info

echo "==> Installing build tooling"
"$PYTHON" -m pip install --quiet --upgrade build twine

echo "==> Building sdist and wheel"
"$PYTHON" -m build

echo "==> Checking metadata"
"$PYTHON" -m twine check dist/*

echo
echo "Built:"
ls -1 dist
echo
echo "Next: twine upload --repository syscon_tts dist/*      (first upload)"
echo "      ./upload_dist.sh                                 (thereafter)"
