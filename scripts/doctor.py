#!/usr/bin/env python3
"""Simplex 安裝後的離線能力檢查。"""

from __future__ import annotations

import asyncio
import importlib.metadata
import shutil
import subprocess
import sys
from pathlib import Path


根目錄 = Path(__file__).resolve().parent.parent
必要OCR語言 = ("eng", "chi_tra", "chi_sim", "jpn")


def 通過(名稱: str, 詳情: str = "") -> None:
    print(f"[通過] {名稱}{f'：{詳情}' if 詳情 else ''}")


def 失敗(名稱: str, 詳情: str) -> None:
    print(f"[失敗] {名稱}：{詳情}")


async def 檢查瀏覽器() -> bool:
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as 工具:
            瀏覽器 = await 工具.chromium.launch(headless=True)
            頁面 = await 瀏覽器.new_page()
            await 頁面.set_content("<main id='ok'>Simplex JS ready</main>")
            文字 = await 頁面.locator("#ok").inner_text()
            await 瀏覽器.close()
        if 文字 == "Simplex JS ready":
            通過("Playwright Chromium", "本地 JS fixture")
            return True
    except Exception as exc:
        失敗("Playwright Chromium", type(exc).__name__)
    return False


def main() -> int:
    錯誤 = 0
    for 套件 in ("crawl4ai", "playwright", "patchright", "PyMuPDF", "pypdf"):
        try:
            通過(套件, importlib.metadata.version(套件))
        except importlib.metadata.PackageNotFoundError:
            失敗(套件, "未安裝")
            錯誤 += 1

    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig  # noqa: F401

        通過("Crawl4AI 核心匯入")
    except Exception as exc:
        失敗("Crawl4AI 核心匯入", type(exc).__name__)
        錯誤 += 1

    if not asyncio.run(檢查瀏覽器()):
        錯誤 += 1

    tesseract = shutil.which("tesseract")
    if tesseract:
        通過("Tesseract OCR", tesseract)
        try:
            結果 = subprocess.run(
                [tesseract, "--list-langs"],
                capture_output=True,
                check=True,
                text=True,
                timeout=10,
            )
            已安裝語言 = set(結果.stdout.splitlines()[1:])
            缺少語言 = [語言 for 語言 in 必要OCR語言 if 語言 not in 已安裝語言]
            if 缺少語言:
                失敗("Tesseract OCR 語言資料", f"缺少 {', '.join(缺少語言)}")
                錯誤 += 1
            else:
                通過("Tesseract OCR 語言資料", ", ".join(必要OCR語言))
        except (OSError, subprocess.SubprocessError) as exc:
            失敗("Tesseract OCR 語言資料", type(exc).__name__)
            錯誤 += 1
    else:
        失敗("Tesseract OCR", "未安裝；掃描 PDF 將無法 OCR")
        錯誤 += 1

    if (根目錄 / "frontend" / "dist" / "index.html").is_file():
        通過("Simplex production 前端")
    else:
        失敗("Simplex production 前端", "尚未執行 npm run build")
        錯誤 += 1

    searx_python = 根目錄 / ".runtime" / "searxng-venv" / "bin" / "python"
    if searx_python.is_file():
        通過("原生 SearXNG 環境")
    else:
        失敗("原生 SearXNG 環境", "尚未完成安裝")
        錯誤 += 1

    print(f"\nSimplex doctor：{'全部通過' if 錯誤 == 0 else f'{錯誤} 項需要處理'}")
    return 0 if 錯誤 == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
