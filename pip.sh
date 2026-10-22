#!/usr/bin/env bash
# Starts Pip. Log goes to pip.log next to this file.
cd "$(dirname "$(readlink -f "$0")")"
if [ ! -x .venv/bin/python ]; then
  echo "Please run ./install.sh first."
  exit 1
fi
# Make sure Ollama is up (it's normally a system service)
if ! curl -s http://127.0.0.1:11434/api/tags >/dev/null; then
  (ollama serve >/dev/null 2>&1 &)
  sleep 2
fi
exec .venv/bin/python server.py >> pip.log 2>&1
