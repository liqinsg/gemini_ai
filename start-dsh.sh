#!/usr/bin/env bash
# Usage: ./start-dsh.sh [ollama|dsh|all]   (default: all)
set -euo pipefail

WHAT="${1:-all}"
WORKDIR="${DSH_WORKDIR:-$HOME/dsh-sandbox}"
SESSION="dsh"

# --- guard: never allow a public bind ---
case "${OLLAMA_HOST:-}" in
  0.0.0.0*|:*|"*"*) echo "ABORT: OLLAMA_HOST=$OLLAMA_HOST exposes the API"; exit 1;;
esac

# --- load node (nvm) ---
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

start_ollama() {
  if curl -fsS -m 3 http://127.0.0.1:11434 >/dev/null 2>&1; then
    echo "[ollama] already running"
  elif systemctl list-unit-files ollama.service >/dev/null 2>&1; then
    sudo systemctl start ollama
    sleep 3
    echo "[ollama] started via systemd"
  else
    nohup ollama serve > "$HOME/ollama.log" 2>&1 &
    sleep 3
    echo "[ollama] started via nohup (log: ~/ollama.log)"
  fi
  ss -ltn | grep ':11434' | grep -q '127.0.0.1' \
    && echo "[ollama] bound to 127.0.0.1 OK" \
    || { echo "WARN: 11434 is not localhost-only"; exit 1; }
}

start_dsh() {
  mkdir -p "$WORKDIR"
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "[dsh] tmux session '$SESSION' already exists"
  else
    tmux new-session -d -s "$SESSION" -c "$WORKDIR" \
      "source $NVM_DIR/nvm.sh; ollama launch dsh; bash"
    echo "[dsh] started in tmux (workspace: $WORKDIR)"
  fi
  sleep 8
  ss -ltn | grep ':3080' || echo "[dsh] 3080 not up yet (first run may wait at the model picker)"
}

case "$WHAT" in
  ollama) start_ollama;;
  dsh)    start_dsh;;
  all)    start_ollama; start_dsh;;
  *)      echo "usage: $0 [ollama|dsh|all]"; exit 2;;
esac
