#!/bin/sh
set -eu

# Uvicorn 僅綁定容器內的 127.0.0.1；socat 綁定明確容器 IP，
# 讓 Compose 可轉送到宿主機的 127.0.0.1，同時避免綁定萬用位址。
CONTAINER_IP="$(hostname -i | awk '{print $1}')"
socat "TCP-LISTEN:8788,bind=${CONTAINER_IP},fork,reuseaddr" TCP:127.0.0.1:8787 &
exec python -m uvicorn simplex_app.main:app --host 127.0.0.1 --port 8787
