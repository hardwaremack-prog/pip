#!/usr/bin/env bash
# Pip — one-time setup for Raspberry Pi 500 (Raspberry Pi OS 64-bit)
# Needs internet now; Pip runs fully offline afterwards.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
HERE="$(pwd)"

g() { printf "\033[32m%s\033[0m\n" "$*"; }
r() { printf "\033[31m%s\033[0m\n" "$*"; }

echo
g "  =========================================="
g "    PIP  -  one-time setup"
g "  =========================================="
echo

if [ "$(uname -m)" != "aarch64" ]; then
  r "This needs 64-bit Raspberry Pi OS (yours reports $(uname -m))."
  exit 1
fi

g "[1/5] System packages ..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip curl >/dev/null
if ! command -v chromium-browser >/dev/null && ! command -v chromium >/dev/null; then
  sudo apt-get install -y -qq chromium-browser >/dev/null 2>&1 || sudo apt-get install -y -qq chromium >/dev/null
fi

g "[2/5] Ollama (runs the AI model) ..."
if ! command -v ollama >/dev/null; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
sudo systemctl enable --now ollama >/dev/null 2>&1 || true
for i in $(seq 1 20); do curl -s http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done

g "[3/5] Pip's Python environment ..."
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements.txt

g "[4/5] Voices, speech recognizer and AI models (a few GB) ..."
.venv/bin/python setup_models.py || true

g "[5/5] Desktop and menu shortcuts ..."
chmod +x "$HERE"/*.sh
"$HERE/shortcuts.sh"
mkdir -p data
sha1sum requirements.txt | cut -d' ' -f1 > data/requirements.sha1
cp config.json data/config.defaults.json

echo
g "Done! Double-click Pip on your desktop (or find it in the menu under Accessories)."
g "To get new versions later: click Update now in Pip's gear menu, or the Update Pip icon."
echo
