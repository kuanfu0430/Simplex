#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ ! -x "$ROOT/.venv/bin/python" ]] || [[ ! -x "$ROOT/.runtime/searxng-venv/bin/python" ]]; then
  echo "[Simplex] 首次啟動，開始安裝必要環境…"
  "$ROOT/simplex" install
fi

exec "$ROOT/simplex" start
