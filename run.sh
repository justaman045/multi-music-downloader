#!/usr/bin/env bash
# YT Music Downloader - launcher
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x venv/bin/python ]; then
    echo "No virtualenv found. Run ./install.sh first." >&2
    exit 1
fi

exec venv/bin/python ytm-ll-dl-modern.py "$@"
