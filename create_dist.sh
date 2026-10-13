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

  echo "==> Checking CI is green on HEAD"
  # release.yml enforces this structurally (publish needs build needs the CI
  # matrix); the local path must not be weaker. Best-effort via gh: if the
  # latest CI run for this commit did not succeed, refuse. Skippable only by
  # the same SKIP_RELEASE_CHECKS escape hatch above.
  if command -v gh >/dev/null 2>&1; then
    HEAD_SHA="$(git rev-parse HEAD)"
    CONCLUSION="$(gh run list --branch main --commit "$HEAD_SHA" --limit 1 \
      --json conclusion --jq '.[0].conclusion' 2>/dev/null || true)"
    if [ "$CONCLUSION" != "success" ]; then
      echo "error: CI for HEAD ($HEAD_SHA) is '${CONCLUSION:-missing}', not 'success'." >&2
      echo "       Push, wait for green CI on main, then build." >&2
      exit 1
    fi
    echo "    CI is green on HEAD"
  else
    echo "warning: gh not found; cannot confirm CI is green on HEAD." >&2
  fi

  echo "==> Running the test suite"
  "$PYTHON" -m pytest -q

  echo "==> Checking the version is strictly newer than the newest PyPI release"
  # Single-sourced from _version.py (pyproject uses dynamic version, so there is
  # no [project].version to read). "Strictly newer than anything live" -- not
  # just "not this exact version" -- so a stale tree cannot republish an older
  # line, which is how 0.1.0 (carrying __version__ 0.0.4) reached PyPI.
  "$PYTHON" - <<'PY'
import sys, json, urllib.error, urllib.request
sys.path.insert(0, "src")
from syscon_tts._version import __version__ as me

def parse(v):
    return tuple(int(p) for p in v.split(".") if p.isdigit())

try:
    with urllib.request.urlopen("https://pypi.org/pypi/syscon-tts/json", timeout=30) as r:
        data = json.load(r)
except urllib.error.HTTPError as exc:
    if exc.code == 404:
        print(f"    syscon-tts has no releases yet; {me} is newer")
        sys.exit(0)
    raise
live = [v for v, files in data["releases"].items()
        if files and not all(f.get("yanked") for f in files)]
newest = max(live, key=parse) if live else None
print(f"    this={me} newest_live={newest}")
if newest is not None and parse(me) <= parse(newest):
    sys.exit(f"error: {me} is not newer than the newest live release {newest}. Bump the version.")
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
