#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$ROOT/.runtime"
SEARXNG_COMMIT="62a1ab7eddc84e98e97605e0a1378e806de6185c"

cd "$ROOT"
echo "[Simplex] 安裝位置：$ROOT"

for command in python3 git npm; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "[Simplex] 缺少必要工具：$command" >&2
    exit 1
  fi
done

if ! python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
then
  echo "[Simplex] 需要 Python 3.11 或以上版本。" >&2
  exit 1
fi

MISSING_OCR_LANGUAGES=()
if command -v tesseract >/dev/null 2>&1; then
  for language in eng chi_tra chi_sim jpn; do
    if ! tesseract --list-langs 2>/dev/null | grep -Fxq "$language"; then
      MISSING_OCR_LANGUAGES+=("$language")
    fi
  done
fi

if ! command -v tesseract >/dev/null 2>&1 || [[ ${#MISSING_OCR_LANGUAGES[@]} -gt 0 ]]; then
  if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    echo "[Simplex] 安裝 Tesseract OCR…"
    if ! command -v tesseract >/dev/null 2>&1; then
      brew install tesseract
    fi
    echo "[Simplex] 安裝中日文 OCR 語言資料…"
    brew install tesseract-lang
  elif command -v apt-get >/dev/null 2>&1; then
    echo "[Simplex] 需要系統權限安裝 Tesseract 與瀏覽器依賴。"
    APT_COMMAND=(apt-get)
    if [[ "$EUID" -ne 0 ]]; then
      if ! command -v sudo >/dev/null 2>&1; then
        echo "[Simplex] 目前不是 root 且找不到 sudo，無法安裝系統依賴。" >&2
        exit 1
      fi
      APT_COMMAND=(sudo apt-get)
    fi
    "${APT_COMMAND[@]}" update
    "${APT_COMMAND[@]}" install -y \
      tesseract-ocr \
      tesseract-ocr-eng \
      tesseract-ocr-chi-tra \
      tesseract-ocr-chi-sim \
      tesseract-ocr-jpn
  else
    echo "[Simplex] 找不到可支援的套件管理器，請先安裝 Tesseract。" >&2
    exit 1
  fi
fi

MISSING_OCR_LANGUAGES=()
for language in eng chi_tra chi_sim jpn; do
  if ! tesseract --list-langs 2>/dev/null | grep -Fxq "$language"; then
    MISSING_OCR_LANGUAGES+=("$language")
  fi
done
if [[ ${#MISSING_OCR_LANGUAGES[@]} -gt 0 ]]; then
  echo "[Simplex] 缺少 Tesseract OCR 語言資料：${MISSING_OCR_LANGUAGES[*]}" >&2
  exit 1
fi

echo "[Simplex] 建立 Python 環境…"
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install --upgrade pip
REQUIREMENTS_FILE="$ROOT/requirements.lock"
if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
  REQUIREMENTS_FILE="$ROOT/requirements.txt"
fi
"$ROOT/.venv/bin/python" -m pip install -r "$REQUIREMENTS_FILE"

echo "[Simplex] 安裝 Playwright 與 Patchright Chromium…"
PLAYWRIGHT_BROWSERS_PATH="$RUNTIME/browsers" "$ROOT/.venv/bin/python" -m playwright install --with-deps chromium
PLAYWRIGHT_BROWSERS_PATH="$RUNTIME/browsers" "$ROOT/.venv/bin/python" -m patchright install --with-deps chromium

echo "[Simplex] 建置 React PWA…"
npm --prefix "$ROOT/frontend" ci
npm --prefix "$ROOT/frontend" run build

mkdir -p "$RUNTIME"
if [[ ! -d "$RUNTIME/searxng-src/.git" ]]; then
  echo "[Simplex] 下載固定版本 SearXNG…"
  rm -rf "$RUNTIME/searxng-src"
  mkdir -p "$RUNTIME/searxng-src"
  git -C "$RUNTIME/searxng-src" init
  git -C "$RUNTIME/searxng-src" remote add origin https://github.com/searxng/searxng.git
fi
git -C "$RUNTIME/searxng-src" fetch --depth 1 origin "$SEARXNG_COMMIT"
git -C "$RUNTIME/searxng-src" update-ref refs/remotes/origin/master FETCH_HEAD
git -C "$RUNTIME/searxng-src" checkout -B master FETCH_HEAD
git -C "$RUNTIME/searxng-src" branch --set-upstream-to=origin/master master

echo "[Simplex] 建立隔離的 SearXNG 環境…"
python3 -m venv "$RUNTIME/searxng-venv"
"$RUNTIME/searxng-venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$RUNTIME/searxng-venv/bin/python" -m pip install pyyaml msgspec typing-extensions pybind11
"$RUNTIME/searxng-venv/bin/python" -m pip install --use-pep517 --no-build-isolation -e "$RUNTIME/searxng-src"

echo "$SEARXNG_COMMIT" > "$RUNTIME/searxng.version"
echo "[Simplex] 執行安裝後檢查…"
PLAYWRIGHT_BROWSERS_PATH="$RUNTIME/browsers" "$ROOT/.venv/bin/python" "$ROOT/scripts/doctor.py"

echo
echo "[Simplex] 安裝完成。雙擊 Simplex Search.command 或執行 ./simplex start 即可使用。"
