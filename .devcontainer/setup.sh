#!/usr/bin/env bash
# One-time setup for the Codespace (runs automatically on creation).
set -euo pipefail

echo "==> ffmpeg + fonts"
sudo apt-get update -qq
# fonts-liberation: metric-compatible Arial stand-in, so captions measure and
# render exactly as they do on Windows (see find_font() in tools/common.py).
sudo apt-get install -y -qq ffmpeg fonts-liberation fontconfig
fc-cache -f >/dev/null
echo "    $(ffmpeg -hide_banner -version | head -1)"
echo "    Arial -> $(fc-match Arial)"

echo "==> Python packages"
pip install -q --upgrade pip
pip install -q -r tools/requirements.txt

# A place for fonts no package ships (Urdu's Jameel Noori Nastaleeq): drop
# the .ttf in here and both libass and PIL find it.
mkdir -p ~/.fonts

cat <<'EOF'

============================================================
  Ready. Start the dashboard with:

      python launch.py --no-browser

  then open the "Dashboard" port from the PORTS tab (or the
  toast that pops up). Step 2's picker appears as its own
  forwarded port (8000) when a run reaches it.

  API keys: add them once as Codespaces Secrets at
  https://github.com/settings/codespaces  (PEXELS_API_KEY,
  GEMINI_API_KEY, optional PIXABAY_API_KEY / COVERR_API_KEY /
  POLLINATIONS_TOKEN) and rebuild/restart the codespace.

  Render for free on Actions instead of in here:
      git add projects/en/<slug> && git commit -m "picks" && git push
      gh workflow run render.yml -f project=en/<slug>
============================================================
EOF
