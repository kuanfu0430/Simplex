"""PDF helper 必須可從專案根目錄獨立匯入。"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


專案根目錄 = Path(__file__).resolve().parents[1]


class PDFHelper本地匯入測試(unittest.TestCase):
    """用乾淨子行程排除工作區外 fallback 與既有模組快取。"""

    def test_專案根可直接匯入PDFHelper並辨識基本資源(self) -> None:
        helper路徑 = 專案根目錄 / "crawl4ai_pdf.py"
        self.assertTrue(helper路徑.is_file(), "專案根目錄缺少 crawl4ai_pdf.py")

        程式 = """
from pathlib import Path
import crawl4ai_pdf as helper

root = Path.cwd().resolve()
assert Path(helper.__file__).resolve().parent == root
assert helper.looks_like_pdf_bytes(b"%PDF-1.7\\n")
assert helper.looks_like_pdf_url("https://example.com/report.PDF?download=1")
assert helper.is_pdf_content_type("application/pdf; charset=binary")
assert helper.detect_resource_type("https://example.com/x", "text/html", b"<html>") == "html"
assert helper.PDF_OCR_LANGUAGES == "eng+chi_tra+chi_sim+jpn"
"""
        結果 = subprocess.run(
            [sys.executable, "-c", 程式],
            cwd=專案根目錄,
            env={
                **os.environ,
                "PDF_OCR_LANGUAGES": "",
            },
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )

        self.assertEqual(
            結果.returncode,
            0,
            msg=f"PDF helper 本地匯入失敗：\n{結果.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
