#!/usr/bin/env sh
# macOS / Linux launcher (not yet tested on those systems; Windows uses "Start Clipper.cmd").
cd "$(dirname "$0")" || exit 1
command -v ffmpeg >/dev/null || { echo "ffmpeg not found: install it (macOS: brew install ffmpeg, Debian/Ubuntu: sudo apt install ffmpeg)"; exit 1; }
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up. This takes a minute."
  python3 -m venv .venv && .venv/bin/python -m pip install -q -r requirements.txt || exit 1
fi
exec .venv/bin/python -m clipper "$@"
