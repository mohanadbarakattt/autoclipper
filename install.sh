#!/usr/bin/env bash
# AutoClipper one-line installer for macOS / Linux:
#   curl -fsSL https://raw.githubusercontent.com/mohanadbarakattt/autoclipper/main/install.sh | bash
set -e
DEST="$HOME/AutoClipper"
if [ "$(uname)" = "Darwin" ]; then
  command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
  command -v python3 >/dev/null || brew install python
  command -v ffmpeg  >/dev/null || brew install ffmpeg
elif command -v apt-get >/dev/null; then
  { command -v python3 >/dev/null && python3 -m venv --help >/dev/null 2>&1; } || sudo apt-get install -y python3 python3-venv
  command -v ffmpeg >/dev/null || sudo apt-get install -y ffmpeg
fi
TMP="$(mktemp -d)"
curl -fsSL https://github.com/mohanadbarakattt/autoclipper/archive/refs/heads/main.tar.gz | tar -xz -C "$TMP"
mkdir -p "$DEST"
# copy code, keep your own profiles/keys/clips
(cd "$TMP"/autoclipper-main && find . -type f | while read -r f; do
  case "$f" in ./config/profiles/*) [ -e "$DEST/$f" ] && continue;; esac
  mkdir -p "$DEST/$(dirname "$f")" && cp "$f" "$DEST/$f"
done)
rm -rf "$TMP"
chmod +x "$DEST/start.sh"
echo "Installed to $DEST - starting..."
exec "$DEST/start.sh"
