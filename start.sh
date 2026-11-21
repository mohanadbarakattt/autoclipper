#!/usr/bin/env bash
# AutoClipper for macOS / Linux:  ./start.sh
set -e
cd "$(dirname "$0")"
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  echo "Python 3.10+ is required:  macOS: brew install python   |   Ubuntu/Debian: sudo apt install python3 python3-venv"; exit 1
fi
if ! command -v ffmpeg >/dev/null; then
  echo "FFmpeg is required:  macOS: brew install ffmpeg   |   Ubuntu/Debian: sudo apt install ffmpeg"; exit 1
fi
exec python3 scripts/easy_start.py
