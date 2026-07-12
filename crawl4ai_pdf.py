# 此檔案隨專案內附，提供 PDF 偵測、文字層解析與可選 OCR。
from __future__ import annotations

import html
import io
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

try:
    import fitz
except ImportError:
    fitz = None  # type: ignore[assignment]

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None  # type: ignore[assignment]


PDF_SIGNATURE = b"%PDF-"
PDF_SAMPLE_PAGES = 5
PDF_PAGE_MIN_TEXT_CHARS = 40
PDF_PAGE_PRINTABLE_RATIO_MIN = 0.15
PDF_GOOD_TEXT_PAGE_MIN_CHARS = 80
PDF_SCAN_PAGE_MAX_TEXT = 20
PDF_SCAN_DOC_RATIO = 0.8
PDF_TEXT_DOC_RATIO = 0.6
PDF_OCR_DPI = max(72, int(os.getenv("PDF_OCR_DPI", "200")))
PDF_OCR_LANGUAGES = os.getenv("PDF_OCR_LANGUAGES", "eng").strip() or "eng"
PDF_ENABLE_OCR = os.getenv("PDF_ENABLE_OCR", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
PDF_FORCE_ENGINE = os.getenv("PDF_FORCE_ENGINE", "auto").strip().lower() or "auto"
PDF_AUTO_EXTRACT_ENABLED = os.getenv("PDF_AUTO_EXTRACT_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


@dataclass
class PDFExtractionResult:
    text: str
    title: str
    content_source: str
    diagnostics: dict[str, Any]


def pdf_capabilities() -> dict[str, Any]:
    tesseract_path = shutil.which("tesseract")
    return {
        "python_executable": sys.executable,
        "auto_extract_enabled": PDF_AUTO_EXTRACT_ENABLED,
        "pymupdf_available": fitz is not None,
        "pypdf_available": PdfReader is not None,
        "tesseract_available": bool(tesseract_path),
        "tesseract_path": tesseract_path,
        "ocr_enabled": PDF_ENABLE_OCR and fitz is not None and bool(tesseract_path),
        "ocr_languages": PDF_OCR_LANGUAGES,
        "ocr_dpi": PDF_OCR_DPI,
        "force_engine": PDF_FORCE_ENGINE,
    }


def is_pdf_content_type(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return "application/pdf" in ct or "application/x-pdf" in ct


def looks_like_pdf_url(url: str) -> bool:
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        return False
    return path.endswith(".pdf")


def looks_like_pdf_bytes(data: bytes) -> bool:
    return (data or b"").startswith(PDF_SIGNATURE)


def detect_resource_type(url: str, content_type: str, sample: bytes) -> str:
    if looks_like_pdf_bytes(sample):
        return "pdf"
    if is_pdf_content_type(content_type):
        return "pdf"
    ct = (content_type or "").lower()
    if "text/html" in ct or "application/xhtml+xml" in ct:
        return "html"
    if looks_like_pdf_url(url):
        return "pdf"
    return "other"


def normalize_pdf_text(text: str) -> str:
    current = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    current = current.replace("\u00a0", " ").replace("\ufeff", "")
    current = re.sub(r"[ \t]+", " ", current)
    current = re.sub(r"[ \t]*\n[ \t]*", "\n", current)
    current = re.sub(r"\n{3,}", "\n\n", current)
    lines = [line.strip() for line in current.splitlines()]
    current = "\n".join(lines)
    current = re.sub(r"\n{3,}", "\n\n", current)
    return current.strip()


def _printable_ratio(text: str) -> float:
    sample = text or ""
    if not sample:
        return 0.0
    printable = sum(1 for ch in sample if ch.isprintable() and not ch.isspace())
    return printable / max(len(sample), 1)


def pdf_page_stats(page: Any, text: str) -> dict[str, Any]:
    image_count = 0
    block_count = 0
    has_text_blocks = False
    try:
        image_count = len(page.get_images(full=True))
    except Exception:
        image_count = 0
    try:
        blocks = page.get_text("blocks")
        block_count = len(blocks or [])
        has_text_blocks = any(
            isinstance(block, (tuple, list))
            and len(block) >= 5
            and str(block[4] or "").strip()
            for block in (blocks or [])
        )
    except Exception:
        block_count = 0
        has_text_blocks = False
    stripped = (text or "").strip()
    return {
        "char_count": len(stripped),
        "word_count": len(stripped.split()),
        "block_count": block_count,
        "image_count": image_count,
        "has_text_blocks": has_text_blocks,
        "printable_ratio": round(_printable_ratio(stripped), 3),
    }


def pdf_page_needs_ocr(page: Any, text: str, stats: dict[str, Any]) -> bool:
    chars = int(stats.get("char_count", 0))
    image_count = int(stats.get("image_count", 0))
    printable_ratio = float(stats.get("printable_ratio", 0.0))
    has_text_blocks = bool(stats.get("has_text_blocks", False))
    if chars < PDF_PAGE_MIN_TEXT_CHARS and image_count > 0:
        return True
    if chars < PDF_SCAN_PAGE_MAX_TEXT and not has_text_blocks:
        return True
    return printable_ratio < PDF_PAGE_PRINTABLE_RATIO_MIN and image_count > 0


def _extract_pdf_page_text(page: Any) -> str:
    if fitz is None:
        return ""
    return page.get_text("text", sort=True) or ""


def _extract_pdf_page_text_with_ocr(page: Any, languages: str, dpi: int) -> str:
    if fitz is None:
        return ""
    textpage = page.get_textpage_ocr(language=languages, dpi=dpi)
    return page.get_text("text", textpage=textpage, sort=True) or ""


def _document_mode(sample_stats: list[dict[str, Any]]) -> str:
    if not sample_stats:
        return "text_dominant"
    good_pages = sum(
        1 for item in sample_stats if int(item.get("char_count", 0)) >= PDF_GOOD_TEXT_PAGE_MIN_CHARS
    )
    scan_pages = sum(
        1
        for item in sample_stats
        if int(item.get("char_count", 0)) <= PDF_SCAN_PAGE_MAX_TEXT
        and int(item.get("image_count", 0)) > 0
    )
    total = len(sample_stats)
    if total and good_pages / total >= PDF_TEXT_DOC_RATIO:
        return "text_dominant"
    if total and scan_pages / total >= PDF_SCAN_DOC_RATIO:
        return "scan_dominant"
    return "mixed"


def _join_page_texts(page_texts: list[tuple[int, str]]) -> str:
    blocks: list[str] = []
    multi_page = len(page_texts) > 1
    for page_no, page_text in page_texts:
        cleaned = normalize_pdf_text(page_text)
        if not cleaned:
            continue
        if multi_page:
            blocks.append(f"## 第 {page_no} 頁\n\n{cleaned}")
        else:
            blocks.append(cleaned)
    return normalize_pdf_text("\n\n".join(blocks))


def extract_pdf_with_pymupdf(
    pdf_bytes: bytes,
    *,
    enable_ocr: bool,
    ocr_languages: str,
    ocr_dpi: int,
) -> PDFExtractionResult:
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed")

    started = time.perf_counter()
    ocr_ms = 0
    ocr_pages: list[int] = []
    text_pages: list[int] = []
    page_texts: list[tuple[int, str]] = []
    capabilities = pdf_capabilities()
    ocr_available = bool(capabilities["ocr_enabled"]) and enable_ocr

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page_count = doc.page_count
        metadata = doc.metadata or {}
        title = (metadata.get("title") or "").strip()
        sample_stats: list[dict[str, Any]] = []
        for page_no in range(min(page_count, PDF_SAMPLE_PAGES)):
            page = doc.load_page(page_no)
            page_text = _extract_pdf_page_text(page)
            sample_stats.append(pdf_page_stats(page, page_text))
        mode = _document_mode(sample_stats)

        for page_index in range(page_count):
            page_no = page_index + 1
            page = doc.load_page(page_index)
            base_text = _extract_pdf_page_text(page)
            stats = pdf_page_stats(page, base_text)
            should_ocr = False
            if ocr_available:
                if mode == "scan_dominant":
                    should_ocr = True
                else:
                    should_ocr = pdf_page_needs_ocr(page, base_text, stats)

            if should_ocr:
                ocr_started = time.perf_counter()
                ocr_text = _extract_pdf_page_text_with_ocr(page, ocr_languages, ocr_dpi)
                ocr_ms += int((time.perf_counter() - ocr_started) * 1000)
                final_text = ocr_text or base_text
                if final_text.strip():
                    ocr_pages.append(page_no)
            else:
                final_text = base_text
            if final_text.strip():
                text_pages.append(page_no)
            page_texts.append((page_no, final_text))

        final_text = _join_page_texts(page_texts)
        if not final_text.strip():
            raise RuntimeError("PyMuPDF returned empty text")

        if ocr_pages and len(ocr_pages) == page_count:
            content_source = "pdf_ocr_full"
        elif ocr_pages:
            content_source = "pdf_ocr_hybrid"
        else:
            content_source = "pdf_text_pymupdf"

        diagnostics = {
            "page_count": page_count,
            "engine": "pymupdf",
            "mode": mode,
            "sample_pages": list(range(1, min(page_count, PDF_SAMPLE_PAGES) + 1)),
            "sample_stats": sample_stats,
            "ocr_used": bool(ocr_pages),
            "ocr_pages": ocr_pages,
            "text_pages": text_pages,
            "extract_ms": int((time.perf_counter() - started) * 1000),
            "ocr_ms": ocr_ms,
            "ocr_available": ocr_available,
            "ocr_languages": ocr_languages,
            "ocr_dpi": ocr_dpi,
        }
        return PDFExtractionResult(
            text=final_text,
            title=title,
            content_source=content_source,
            diagnostics=diagnostics,
        )
    finally:
        doc.close()


def extract_pdf_with_pypdf(pdf_bytes: bytes) -> PDFExtractionResult:
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed")

    started = time.perf_counter()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    title = ""
    metadata = reader.metadata
    if metadata is not None:
        title = str(getattr(metadata, "title", "") or metadata.get("/Title", "") or "").strip()

    page_texts: list[tuple[int, str]] = []
    for page_no, page in enumerate(reader.pages, start=1):
        page_texts.append((page_no, page.extract_text() or ""))

    final_text = _join_page_texts(page_texts)
    if not final_text.strip():
        raise RuntimeError("pypdf returned empty text")

    diagnostics = {
        "page_count": len(reader.pages),
        "engine": "pypdf",
        "mode": "text_dominant",
        "sample_pages": list(range(1, min(len(reader.pages), PDF_SAMPLE_PAGES) + 1)),
        "sample_stats": [],
        "ocr_used": False,
        "ocr_pages": [],
        "text_pages": [page_no for page_no, text in page_texts if text.strip()],
        "extract_ms": int((time.perf_counter() - started) * 1000),
        "ocr_ms": 0,
        "ocr_available": False,
        "ocr_languages": "",
        "ocr_dpi": 0,
    }
    return PDFExtractionResult(
        text=final_text,
        title=title,
        content_source="pdf_text_pypdf",
        diagnostics=diagnostics,
    )


def _should_try_pypdf_after_pymupdf(result: PDFExtractionResult) -> bool:
    if result.content_source.startswith("pdf_ocr"):
        return False
    stripped = result.text.strip()
    return len(stripped) < 80


def extract_pdf_content(
    pdf_bytes: bytes,
    *,
    source_url: str,
    enable_ocr: bool | None = None,
    ocr_languages: str | None = None,
    ocr_dpi: int | None = None,
) -> PDFExtractionResult:
    capabilities = pdf_capabilities()
    if not capabilities["auto_extract_enabled"]:
        raise RuntimeError("PDF auto extraction is disabled")

    effective_enable_ocr = capabilities["ocr_enabled"] if enable_ocr is None else bool(enable_ocr)
    effective_languages = (ocr_languages or capabilities["ocr_languages"] or "eng").strip() or "eng"
    effective_dpi = max(72, int(ocr_dpi or capabilities["ocr_dpi"] or PDF_OCR_DPI))
    force_engine = str(capabilities["force_engine"] or "auto")

    errors: list[str] = []
    engines_tried: list[str] = []

    def _decorate(result: PDFExtractionResult, *, fallback_used: bool = False) -> PDFExtractionResult:
        diagnostics = dict(result.diagnostics)
        diagnostics.update(
            {
                "source_url": source_url,
                "bytes_size": len(pdf_bytes),
                "engines_tried": engines_tried[:],
                "engine_selected": diagnostics.get("engine"),
                "fallback_used": fallback_used,
            }
        )
        return PDFExtractionResult(
            text=result.text,
            title=result.title,
            content_source=result.content_source,
            diagnostics=diagnostics,
        )

    if force_engine in {"pymupdf", "auto"} and capabilities["pymupdf_available"]:
        engines_tried.append("pymupdf")
        try:
            primary = extract_pdf_with_pymupdf(
                pdf_bytes,
                enable_ocr=effective_enable_ocr,
                ocr_languages=effective_languages,
                ocr_dpi=effective_dpi,
            )
            if capabilities["pypdf_available"] and _should_try_pypdf_after_pymupdf(primary):
                engines_tried.append("pypdf")
                try:
                    secondary = extract_pdf_with_pypdf(pdf_bytes)
                    if len(secondary.text) > len(primary.text) * 1.2:
                        secondary_diag = dict(secondary.diagnostics)
                        secondary_diag["fallback_from"] = "pymupdf"
                        return _decorate(
                            PDFExtractionResult(
                                text=secondary.text,
                                title=secondary.title or primary.title,
                                content_source=secondary.content_source,
                                diagnostics=secondary_diag,
                            ),
                            fallback_used=True,
                        )
                except Exception as exc:
                    errors.append(f"pypdf_after_pymupdf:{type(exc).__name__}:{exc}")
            return _decorate(primary, fallback_used=False)
        except Exception as exc:
            errors.append(f"pymupdf:{type(exc).__name__}:{exc}")
            if force_engine == "pymupdf":
                raise RuntimeError("; ".join(errors)) from exc

    if force_engine in {"pypdf", "auto"} and capabilities["pypdf_available"]:
        engines_tried.append("pypdf")
        try:
            return _decorate(extract_pdf_with_pypdf(pdf_bytes), fallback_used="pymupdf" in engines_tried)
        except Exception as exc:
            errors.append(f"pypdf:{type(exc).__name__}:{exc}")
            if force_engine == "pypdf":
                raise RuntimeError("; ".join(errors)) from exc

    if errors:
        message = "; ".join(errors)
    else:
        message = (
            "no PDF extraction engine is available"
            f" (python={capabilities['python_executable']},"
            f" pymupdf_available={capabilities['pymupdf_available']},"
            f" pypdf_available={capabilities['pypdf_available']})"
        )
    raise RuntimeError(message)


def render_pdf_text_as_html(text: str, title: str) -> str:
    safe_title = html.escape((title or "").strip() or "PDF 文件")
    content = normalize_pdf_text(text)
    if not content:
        return (
            '<article data-source-type="pdf"><h1>'
            f"{safe_title}</h1><section data-page=\"1\"><pre></pre></section></article>"
        )

    sections: list[str] = []
    current_page = 1
    buffer: list[str] = []
    page_heading = re.compile(r"^## 第 (\d+) 頁$")

    def _flush(page_no: int, lines: list[str]) -> None:
        body = html.escape("\n".join(lines).strip())
        sections.append(f'<section data-page="{page_no}"><pre>{body}</pre></section>')

    for raw_line in content.splitlines():
        match = page_heading.match(raw_line.strip())
        if match:
            if buffer:
                _flush(current_page, buffer)
                buffer = []
            current_page = int(match.group(1))
            continue
        buffer.append(raw_line)
    if buffer:
        _flush(current_page, buffer)

    if not sections:
        escaped = html.escape(content)
        sections.append(f'<section data-page="1"><pre>{escaped}</pre></section>')
    return (
        '<article data-source-type="pdf">'
        f"<h1>{safe_title}</h1>"
        + "".join(sections)
        + "</article>"
    )
