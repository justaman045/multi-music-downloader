#!/usr/bin/env bash
# YT Music Downloader - one-time setup
# Creates ./venv and installs all dependencies.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

echo "=========================================="
echo " YT Music Downloader - setup"
echo "=========================================="
echo

if [ ! -x venv/bin/python ]; then
    echo "[1/3] Creating virtualenv in ./venv ..."
    "$PYTHON" -m venv venv
else
    echo "[1/3] Virtualenv already exists."
fi

echo "[2/3] Installing Python dependencies ..."
venv/bin/python -m pip install --disable-pip-version-check -U -r requirements.txt

echo "[3/3] Verifying runtime (downloads project-local Deno on first run) ..."
venv/bin/python ytm-ll-dl-modern.py --version

echo
echo "Setup complete. Start the app with:"
echo "    ./run.sh"
echo
