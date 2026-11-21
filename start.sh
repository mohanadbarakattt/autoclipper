#!/usr/bin/env bash
# AutoClipper for macOS / Linux:  ./start.sh
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then
  echo "Python 3.10+ is required:  macOS: brew install python   |   Ubuntu/Debian: sudo apt install python3 python3-venv"; exit 1
fi
if ! command -v ffmpeg >/dev/null; then
  echo "FFmpeg is required:  macOS: brew install ffmpeg   |   Ubuntu/Debian: sudo apt install ffmpeg"; exit 1
fi
exec python3 scripts/easy_start.py
