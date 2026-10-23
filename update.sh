#!/usr/bin/env bash
# Updates Pip to the newest version on GitHub.
# Keeps your AI models, voices, settings, chats and any config.json changes.
#
#   ./update.sh            update (restarts Pip if it's running)
#   ./update.sh --pause    same, then wait for Enter (used by the desktop icon)
set -euo pipefail
REPO="hardwaremack-prog/pip"

# Work out where Pip lives (also works when piped:  curl ... | bash)
SELF="${BASH_SOURCE[0]:-}"
if [ -n "${PIP_DIR:-}" ]; then
  DIR="$PIP_DIR"
elif [ -n "$SELF" ] && [ -f "$(dirname "$SELF")/server.py" ]; then
  DIR="$(cd "$(dirname "$SELF")" && pwd)"
elif [ -f ./server.py ]; then
  DIR="$(pwd)"
else
  DIR="$HOME/Pip"
fi
cd "$DIR"

# This script replaces itself while updating, so run from a private copy
if [ -z "${PIP_UPDATE_COPY:-}" ] && [ -n "$SELF" ] && [ -f "$SELF" ]; then
  COPY="$(mktemp /tmp/pip-update.XXXXXX)"
  cp "$SELF" "$COPY"
  PIP_UPDATE_COPY="$COPY" PIP_DIR="$DIR" exec bash "$COPY" "$@"
fi

PAUSE=0
for a in "$@"; do if [ "$a" = "--pause" ]; then PAUSE=1; fi; done
say() { echo "[update] $*"; }
finish() {
  code=$?
  if [ -n "${PIP_UPDATE_COPY:-}" ]; then rm -f "$PIP_UPDATE_COPY"; fi
  if [ $code -ne 0 ]; then say "Something went wrong (see above). Pip was not changed beyond this point."; fi
  if [ $PAUSE -eq 1 ]; then echo; read -r -p "Press Enter to close this window..." _ || true; fi
  exit $code
}
trap finish EXIT

[ -f server.py ] || { say "Can't find Pip in $DIR"; exit 1; }
mkdir -p data
OLD="$(cat VERSION 2>/dev/null || echo 1.0.0)"
say "Pip $OLD in $DIR"

# Is Pip running from this folder right now?
pip_pids() {
  # Only a Python process running exactly "server.py" from this folder
  for p in $(pgrep -f "server\.py" || true); do
    [ "$p" = "$$" ] && continue
    args="$(tr '\0' '\n' < "/proc/$p/cmdline" 2>/dev/null || true)"
    exe="$(basename "$(printf '%s\n' "$args" | sed -n 1p)")"
    arg1="$(printf '%s\n' "$args" | sed -n 2p)"
    case "$exe" in python*) ;; *) continue ;; esac
    if [ "$arg1" = "server.py" ] && [ "$(readlink "/proc/$p/cwd" 2>/dev/null)" = "$DIR" ]; then echo "$p"; fi
  done
  return 0
}
WAS_RUNNING=0
if [ -n "$(pip_pids)" ]; then WAS_RUNNING=1; fi

cp config.json data/config.before-update.json 2>/dev/null || true
REQ_BEFORE="$(sha1sum requirements.txt 2>/dev/null | cut -d' ' -f1)"

if [ -d .git ]; then
  say "Getting the newest version from GitHub ..."
  git fetch --quiet origin main
  # Your models, voices, settings and chats aren't stored in git, so they stay put.
  git reset --quiet --hard origin/main
else
  say "Downloading the newest version from GitHub ..."
  TMP="$(mktemp -d)"
  curl -fsSL --retry 2 "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar xz -C "$TMP"
  SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [ -f "$SRC/server.py" ] || { say "The download looks wrong."; rm -rf "$TMP"; exit 1; }
  # Copy the app files, never the things that belong to this Pi
  (cd "$SRC" && tar cf - --exclude=./.venv --exclude=./models --exclude=./data --exclude=./.git .) | tar xf -
  rm -rf "$TMP"
fi
NEW="$(cat VERSION 2>/dev/null || echo "$OLD")"

# Put back the config.json settings you changed, but take new defaults for the rest
if [ -f data/config.before-update.json ]; then
  python3 - <<'PY'
import json, os
mine = json.load(open("data/config.before-update.json"))
new = json.load(open("config.json"))
old_defaults = json.load(open("data/config.defaults.json")) if os.path.exists("data/config.defaults.json") else None
fresh_defaults = dict(new)
for k, v in mine.items():
    if k in ("models_to_install", "voices_to_install") or k not in new:
        continue
    if old_defaults is None or old_defaults.get(k) != v:   # you changed it
        new[k] = v
json.dump(new, open("config.json", "w"), indent=2)
json.dump(fresh_defaults, open("data/config.defaults.json", "w"), indent=2)
PY
fi

chmod +x ./*.sh

if [ -x .venv/bin/python ] && [ "$(sha1sum requirements.txt | cut -d' ' -f1)" != "$(cat data/requirements.sha1 2>/dev/null)" ]; then
  say "Updating Python packages ..."
  .venv/bin/python -m pip install --quiet -r requirements.txt
  sha1sum requirements.txt | cut -d' ' -f1 > data/requirements.sha1
fi

./shortcuts.sh >/dev/null 2>&1 || true

if [ "$OLD" = "$NEW" ]; then
  say "Pip is up to date ($NEW)."
else
  say "Updated Pip: $OLD -> $NEW"
fi

if [ $WAS_RUNNING -eq 1 ]; then
  say "Restarting Pip ..."
  for p in $(pip_pids); do kill "$p" 2>/dev/null || true; done
  for _ in $(seq 1 20); do [ -z "$(pip_pids)" ] && break; sleep 0.25; done
  setsid nohup ./pip.sh --no-browser >/dev/null 2>&1 < /dev/null &
  sleep 1
  say "Pip restarted. An open Pip window reloads by itself."
fi
say "Done."
