#!/usr/bin/env python3
"""以單一前景程序管理 Simplex 與原生 SearXNG。"""

from __future__ import annotations

import os
import json
import secrets
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.request import Request, urlopen


根目錄 = Path(__file__).resolve().parent.parent
執行目錄 = 根目錄 / ".runtime"
SearXNG原始碼 = 執行目錄 / "searxng-src"
SearXNGPython = 執行目錄 / "searxng-venv" / "bin" / "python"
SimplexPython = 根目錄 / ".venv" / "bin" / "python"
設定檔 = 根目錄 / "searxng" / "settings.yml"
SearXNG密鑰檔 = 執行目錄 / "searxng.secret"
前端網址 = "http://127.0.0.1:8787/"
停止中 = False


def 連接埠使用中(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def 是SearXNG服務() -> bool:
    """確認 8888 上的程序真的提供 SearXNG JSON 搜尋介面。"""
    try:
        請求 = Request(
            "http://127.0.0.1:8888/search?q=simplex-health&format=json&pageno=1",
            headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
        )
        with urlopen(請求, timeout=2) as 回應:
            資料 = json.loads(回應.read())
        return isinstance(資料, dict) and isinstance(資料.get("results"), list)
    except Exception:
        return False


def 是Simplex服務() -> bool:
    """確認 8787 上是目前的 Simplex，而不是其他或舊版服務。"""
    try:
        with urlopen(f"{前端網址}api/ready", timeout=2) as 回應:
            資料 = json.loads(回應.read())
        return isinstance(資料, dict) and 資料.get("status") == "ready"
    except Exception:
        return False


def 開啟前端() -> None:
    """用新分頁載入乾淨根網址，避免 Safari 只聚焦記憶體中的舊頁面。"""
    webbrowser.open_new_tab(前端網址)


def 終止程序(程序: subprocess.Popen | None) -> None:
    if 程序 is None or 程序.poll() is not None:
        return
    程序.terminate()
    try:
        程序.wait(timeout=8)
    except subprocess.TimeoutExpired:
        程序.kill()


def 取得SearXNG密鑰() -> str:
    執行目錄.mkdir(parents=True, exist_ok=True)
    if SearXNG密鑰檔.is_file():
        現有 = SearXNG密鑰檔.read_text(encoding="utf-8").strip()
        if 現有:
            return 現有
    新密鑰 = secrets.token_urlsafe(48)
    SearXNG密鑰檔.write_text(新密鑰 + "\n", encoding="utf-8")
    try:
        SearXNG密鑰檔.chmod(0o600)
    except OSError:
        pass
    return 新密鑰


def 處理停止(_訊號: int, _frame: object) -> None:
    global 停止中
    停止中 = True


def 啟動SearXNG() -> subprocess.Popen | None:
    if 連接埠使用中(8888):
        if 是SearXNG服務():
            print("[Simplex] 偵測到 127.0.0.1:8888 已有 SearXNG，直接沿用。")
            return None
        raise RuntimeError("127.0.0.1:8888 已被非 SearXNG 程序占用")
    if not SearXNGPython.is_file() or not SearXNG原始碼.is_dir():
        print("[Simplex] 尚未安裝原生 SearXNG；Simplex 將以降級模式啟動。")
        return None
    環境 = os.environ.copy()
    環境.update(
        {
            "SEARXNG_SETTINGS_PATH": str(設定檔),
            "SEARXNG_PORT": "8888",
            "SEARXNG_BIND_ADDRESS": "127.0.0.1",
            "SEARXNG_BASE_URL": "http://127.0.0.1:8888/",
            "SEARXNG_LIMITER": "false",
            "SEARXNG_PUBLIC_INSTANCE": "false",
            "SEARXNG_SECRET": 取得SearXNG密鑰(),
        }
    )
    print("[Simplex] 啟動 SearXNG：127.0.0.1:8888")
    return subprocess.Popen(
        [str(SearXNGPython), "-m", "searx.webapp"],
        cwd=SearXNG原始碼,
        env=環境,
    )


def 啟動Simplex() -> subprocess.Popen:
    python = SimplexPython if SimplexPython.is_file() else Path(sys.executable)
    環境 = os.environ.copy()
    環境.setdefault("SIMPLEX_DATA_DIR", str(根目錄 / "data"))
    環境.setdefault("CRAWL4_AI_BASE_DIRECTORY", str(根目錄 / "data"))
    環境.setdefault("SEARXNG_URL", "http://127.0.0.1:8888")
    print("[Simplex] 啟動前後端：127.0.0.1:8787")
    return subprocess.Popen(
        [
            str(python),
            "-m",
            "uvicorn",
            "simplex_app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8787",
        ],
        cwd=根目錄,
        env=環境,
    )


def main() -> int:
    signal.signal(signal.SIGINT, 處理停止)
    signal.signal(signal.SIGTERM, 處理停止)
    if 連接埠使用中(8787):
        if 是Simplex服務():
            print("[Simplex] 偵測到服務已啟動，直接開啟目前前端。")
            if os.environ.get("SIMPLEX_NO_BROWSER", "0") != "1":
                開啟前端()
            return 0
        raise RuntimeError("127.0.0.1:8787 已被非 Simplex 程序占用")

    searxng = 啟動SearXNG()
    try:
        simplex = 啟動Simplex()
    except Exception:
        終止程序(searxng)
        raise

    已開啟瀏覽器 = False
    重新啟動次數 = 0
    try:
        while not 停止中:
            if simplex.poll() is not None:
                print(f"[Simplex] 主程序已結束，代碼 {simplex.returncode}")
                return int(simplex.returncode or 1)

            if not 已開啟瀏覽器 and 連接埠使用中(8787):
                已開啟瀏覽器 = True
                if os.environ.get("SIMPLEX_NO_BROWSER", "0") != "1":
                    開啟前端()

            if searxng is not None and searxng.poll() is not None:
                重新啟動次數 += 1
                等待秒數 = min(2 ** min(重新啟動次數, 4), 16)
                print(f"[Simplex] SearXNG 意外停止，{等待秒數} 秒後重啟。")
                time.sleep(等待秒數)
                if not 停止中:
                    searxng = 啟動SearXNG()
            time.sleep(0.35)
    finally:
        print("[Simplex] 正在安全關閉服務…")
        終止程序(simplex)
        終止程序(searxng)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"[Simplex] 啟動失敗：{exc}", file=sys.stderr)
        raise SystemExit(1)
