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
# Usage: ./create_dist.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python}"

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
