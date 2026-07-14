#!/usr/bin/env bash
#
# One-shot setup for the PlantStar APU (Linux x86-64).
# Creates a virtualenv, installs dependencies, and downloads voice models.
#
# Usage: scripts/install.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
VENV="${VENV:-$ROOT/.venv}"

echo "==> Creating virtualenv at $VENV"
"$PYTHON" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> Upgrading pip"
pip install --upgrade pip

echo "==> Installing PlantStar TTS and dependencies"
pip install -e .

echo "==> Downloading voice models (requires internet for this step only)"
bash "$ROOT/scripts/download_voices.sh"

echo
echo "Install complete. Activate with: source $VENV/bin/activate"
echo "Try:   plantstar-tts list-voices"
echo "       plantstar-tts speak -v en_us_amy -o hello.wav \"Hello from PlantStar\""
echo "       plantstar-tts serve --port 5002"
