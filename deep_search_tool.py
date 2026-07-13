#!/usr/bin/env python3
"""
Simplex 深度搜尋核心
===================
整合原生 SearXNG、可選搜尋 API、LLM Judge 與 Crawl4AI 批次深爬的搜尋管線。

根據搜尋模式自動分配不同 API 組合：
- web：SearXNG general + 已啟用的 Web 搜尋供應商
- academic：SearXNG general + science + 已啟用的學術搜尋供應商
- social：SearXNG general + social media + 已啟用的社群搜尋供應商

SerpApi 僅用於 academic 模式的 Google Scholar（引用數、出版資訊等無可替代功能）。

支援三種搜尋模式：web（網路）、academic（學術）、social（社交）。

使用方式：
    import asyncio
    from deep_search_tool import deep_search

    result = asyncio.run(deep_search(
        question="什麼是量子計算？",
        search_queries=["量子計算原理", "quantum computing applications", "量子電腦最新進展"],
        search_mode="web",
    ))
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import sys
import time
import unicodedata
from contextlib import suppress
from contextvars import ContextVar
from collections.abc import Callable
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import httpx

# ── 強制 stdout/stderr 使用 UTF-8（解決 Windows CP950 問題）──────────
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 載入 .env ──────────────────────────────────────────────
try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass


VALID_SEARCH_MODES = frozenset({"web", "academic", "social"})
VALID_EXECUTION_MODES = frozenset({"instant", "fast", "full"})
VALID_MODEL_ROUTES = frozenset({"d", "g"})
VALID_RENDER_MODES = frozenset({"auto", "never", "always"})
DEFAULT_MODEL_ROUTE = "d"
研究進度回報器 = Callable[[dict[str, Any]], None]


def _回報研究進度(回報器: 研究進度回報器 | None, 事件: dict[str, Any]) -> None:
    """進度只用於 UI，不能影響既有研究管線。"""
    if not 回報器:
        return
    try:
        回報器(事件)
    except Exception:
        pass


def _追蹤來源(項目: dict[str, Any]) -> dict[str, str]:
    return {
        "title": str(項目.get("title") or 項目.get("url") or "Untitled"),
        "url": str(項目.get("url") or ""),
    }


def _依查詢分組來源(項目清單: list[dict[str, Any]]) -> list[dict[str, Any]]:
    分組: dict[str, list[dict[str, str]]] = {}
    for 項目 in 項目清單:
        查詢 = str(項目.get("from_query") or 項目.get("query") or "")
        if not 查詢:
            continue
        分組.setdefault(查詢, []).append(_追蹤來源(項目))
    return [
        {"query": 查詢, "sources": 來源}
        for 查詢, 來源 in 分組.items()
    ]


def _追蹤區塊(區塊: dict[str, Any]) -> dict[str, str]:
    預覽 = re.sub(r"\s+", " ", str(區塊.get("text") or "")).strip()
    if len(預覽) > 280:
        預覽 = f"{預覽[:277].rstrip()}…"
    return {
        "chunk_id": str(區塊.get("chunk_id") or ""),
        "title": str(區塊.get("title") or ""),
        "source_url": str(區塊.get("source_url") or ""),
        "from_query": str(區塊.get("from_query") or ""),
        "preview": 預覽,
    }


def _回報回答證據(
    回報器: 研究進度回報器 | None,
    審核結果: dict[str, Any],
    輪次: int,
) -> None:
    區塊 = [_追蹤區塊(項目) for 項目 in 審核結果.get("selected_chunks", []) if isinstance(項目, dict)]
    if not 區塊:
        return
    _回報研究進度(
        回報器,
        {
            "type": "final_evidence",
            "stage": "chunk_judge",
            "round": 輪次,
            "chunks": 區塊,
        },
    )


def normalize_search_mode(search_mode: Any) -> str:
    normalized = search_mode.strip().lower() if isinstance(search_mode, str) else "web"
    if normalized not in VALID_SEARCH_MODES:
        return "web"
    return normalized


def normalize_execution_mode(mode: Any) -> str:
    normalized = mode.strip().lower() if isinstance(mode, str) else "fast"
    if normalized not in VALID_EXECUTION_MODES:
        return "fast"
    return normalized


def normalize_model_route(model: Any) -> str:
    normalized = model.strip().lower() if isinstance(model, str) else DEFAULT_MODEL_ROUTE
    if normalized not in VALID_MODEL_ROUTES:
        return DEFAULT_MODEL_ROUTE
    return normalized


def validate_search_inputs(
    question: Any,
    search_queries: Any,
    *,
    exact_query_count: int | None = None,
) -> tuple[str, list[str]]:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question 不能為空")

    if not isinstance(search_queries, list) or not search_queries:
        if exact_query_count == 3:
            raise ValueError("search_queries 必須是包含 3 組字詞的列表")
        raise ValueError("search_queries 必須是非空字詞列表")

    normalized_queries: list[str] = []
    for item in search_queries:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("search_queries 必須全部是非空字串")
        normalized_queries.append(item.strip())

    if exact_query_count is not None and len(normalized_queries) != exact_query_count:
        if exact_query_count == 3:
            raise ValueError("search_queries 必須是包含 3 組字詞的列表")
        raise ValueError(f"search_queries 必須恰好包含 {exact_query_count} 組字詞")

    return question.strip(), normalized_queries


def _validate_search_options(
    *,
    search_mode: Any,
    mode: Any,
    model: Any,
    results_per_query: Any,
    filter_model: Any,
    judge_model_config: Any = None,
    search_provider_config: Any = None,
    min_select_per_group: Any,
    max_select_per_group: Any,
    max_chars_per_page: Any,
    crawl_concurrency: Any,
    render: Any,
    verbose: Any,
) -> str:
    """驗證 Python 直調選項，並回傳正規化後的 render。"""

    for name, value in (
        ("search_mode", search_mode),
        ("mode", mode),
        ("model", model),
    ):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{name} 必須是字串或 None")

    if filter_model is not None and not isinstance(filter_model, str):
        raise ValueError("filter_model 必須是字串或 None")
    if judge_model_config is not None and not isinstance(judge_model_config, dict):
        raise ValueError("judge_model_config 必須是字典或 None")
    if search_provider_config is not None and not isinstance(search_provider_config, dict):
        raise ValueError("search_provider_config 必須是字典或 None")

    numeric_options = (
        ("min_select_per_group", min_select_per_group),
        ("max_select_per_group", max_select_per_group),
        ("max_chars_per_page", max_chars_per_page),
        ("crawl_concurrency", crawl_concurrency),
    )
    for name, value in numeric_options:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} 必須是正整數")

    if results_per_query is not None and (
        isinstance(results_per_query, bool)
        or not isinstance(results_per_query, int)
        or results_per_query <= 0
    ):
        raise ValueError("results_per_query 必須是正整數或 None")

    if min_select_per_group > max_select_per_group:
        raise ValueError("min_select_per_group 不可大於 max_select_per_group")

    if not isinstance(render, str):
        raise ValueError("render 必須是字串")
    normalized_render = render.strip().lower()
    if normalized_render not in VALID_RENDER_MODES:
        raise ValueError("render 必須是 auto、never 或 always")

    if not isinstance(verbose, bool):
        raise ValueError("verbose 必須是布林值")

    return normalized_render


def _empty_search_summary(
    *,
    judge_verdict: str | None = None,
) -> dict[str, Any]:
    summary = {
        "raw_total_found": 0,
        "total_found": 0,
        "total_selected": 0,
        "total_selected_raw": 0,
        "total_deduped_before_crawl": 0,
        "total_budget_trimmed_before_crawl": 0,
        "total_crawl_attempted": 0,
        "total_crawled_success": 0,
        "total_crawled_failed": 0,
        "total_chunks_seen": 0,
        "total_chunks_prompted": 0,
        "total_chunks_selected": 0,
        "loops_executed": 0,
        "judge_verdict": judge_verdict,
    }
    return summary


# ── 文字解碼與清洗（內嵌版，避免依賴外部 shared 模組）───────────────
try:
    from charset_normalizer import from_bytes as charset_from_bytes
except Exception:
    charset_from_bytes = None


_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")
_ZERO_WIDTH_RE = re.compile(r"[\u200B-\u200F\u2060\uFEFF]")
_MOJIBAKE_SEQ_RE = re.compile(
    r"(?:Ã.|Â.|â(?:€|€™|€œ|€|€”|€¦|€˜|„¢)|ðŸ|ï¼|ï½|ï¾|ï»|ï¿|�)"
)


def _extract_declared_charset(content_type: str) -> str | None:
    match = _CHARSET_RE.search(content_type or "")
    if not match:
        return None
    charset = (match.group(1) or "").strip().strip("'\"")
    return charset or None


def _iter_candidate_texts(
    data: bytes,
    content_type: str,
    fallback_text: str | None,
) -> Iterable[str]:
    seen: set[str] = set()

    def _add(text: str) -> str | None:
        if not text or text in seen:
            return None
        seen.add(text)
        return text

    declared = _extract_declared_charset(content_type)
    encodings: list[str] = []
    if declared:
        encodings.append(declared)
    encodings.extend(
        [
            "utf-8",
            "utf-8-sig",
            "big5",
            "cp950",
            "gb18030",
            "gbk",
            "shift_jis",
            "euc-jp",
            "cp1252",
            "latin-1",
        ]
    )

    for encoding in encodings:
        try:
            candidate = data.decode(encoding, errors="strict")
        except Exception:
            continue
        added = _add(candidate)
        if added is not None:
            yield added

    if charset_from_bytes is not None:
        try:
            matches = charset_from_bytes(data)
        except Exception:
            matches = None
        if matches is not None:
            for match in matches:
                try:
                    candidate = str(match)
                except Exception:
                    continue
                added = _add(candidate)
                if added is not None:
                    yield added

    if fallback_text:
        added = _add(fallback_text)
        if added is not None:
            yield added

    try:
        added = _add(data.decode("utf-8", errors="replace"))
        if added is not None:
            yield added
    except Exception:
        pass


def _character_mix_score(text: str) -> float:
    if not text:
        return -10_000.0

    score = 0.0
    letters = 0
    digits = 0
    whitespace = 0
    punctuation = 0
    strange = 0

    for ch in text:
        codepoint = ord(ch)
        category = unicodedata.category(ch)
        if 0x4E00 <= codepoint <= 0x9FFF or 0x3400 <= codepoint <= 0x4DBF:
            letters += 1
            score += 3.0
        elif 0x3040 <= codepoint <= 0x30FF or 0xAC00 <= codepoint <= 0xD7AF:
            letters += 1
            score += 2.5
        elif 0xFF61 <= codepoint <= 0xFF9F:
            strange += 1
            score -= 3.5
        elif 0xE000 <= codepoint <= 0xF8FF:
            strange += 1
            score -= 4.0
        elif category.startswith("L"):
            letters += 1
            score += 2.0
        elif category == "Nd":
            digits += 1
            score += 1.2
        elif ch in "\r\n\t ":
            whitespace += 1
            score += 0.15
        elif category.startswith("P"):
            punctuation += 1
            score += 0.2
        elif category.startswith("S"):
            punctuation += 1
            score -= 0.1
        else:
            strange += 1
            score -= 0.8

    suspicious = len(_MOJIBAKE_SEQ_RE.findall(text))
    replacements = text.count("\uFFFD")
    score -= suspicious * 14.0
    score -= replacements * 28.0
    score -= strange * 0.8

    if letters + digits == 0 and len(text.strip()) > 12:
        score -= 25.0

    if punctuation > max(letters + digits, 1) * 1.35:
        score -= 15.0

    if len(text) > 32:
        score += min(len(text), 4000) * 0.01

    return score


def _repair_mojibake_round(text: str) -> str:
    # 純 ASCII（且沒有 HTML entity）不可能透過 latin-1/cp1252 round-trip
    # 產生不同的有效 UTF-8 文字，可直接跳過昂貴的全文評分。
    if text.isascii() and "&" not in text:
        return text

    # 含 CJK 等無法以 latin-1/cp1252 編碼的正常文字，在沒有 HTML entity
    # 與已知亂碼訊號時也不會產生其他候選；此快路徑不改變輸出。
    if (
        "&" not in text
        and "\uFFFD" not in text
        and not _MOJIBAKE_SEQ_RE.search(text)
    ):
        encodable = False
        for encoding in ("latin-1", "cp1252"):
            try:
                text.encode(encoding, errors="strict")
                encodable = True
                break
            except (UnicodeEncodeError, LookupError):
                continue
        if not encodable:
            return text

    best = text
    best_score = _character_mix_score(text)

    candidates: list[str] = []
    html_once = html.unescape(text)
    html_twice = html.unescape(html_once)
    candidates.extend([html_once, html_twice])

    for source in (text, html_once, html_twice):
        for encoding in ("latin-1", "cp1252"):
            try:
                candidates.append(
                    source.encode(encoding, errors="strict").decode(
                        "utf-8", errors="strict"
                    )
                )
            except Exception:
                continue

    seen_candidates: set[str] = set()
    for candidate in candidates:
        if candidate in seen_candidates:
            continue
        seen_candidates.add(candidate)
        score = _character_mix_score(candidate)
        if score > best_score + 4.0:
            best = candidate
            best_score = score

    return best


def repair_mojibake_text(text: str, max_rounds: int = 2) -> str:
    current = text or ""
    for _ in range(max(1, max_rounds)):
        repaired = _repair_mojibake_round(current)
        if repaired == current:
            break
        current = repaired
    return current


def decode_response_text(
    data: bytes,
    content_type: str = "",
    fallback_text: str | None = None,
) -> str:
    if not data:
        return ""

    # ASCII bytes 在所有候選編碼下內容相同；fallback 也相同時可直接完成。
    if data.isascii():
        ascii_text = data.decode("ascii")
        if fallback_text in (None, ascii_text):
            return repair_mojibake_text(ascii_text)

    best = ""
    best_score = -10_000.0
    seen_repaired: set[str] = set()
    for candidate in _iter_candidate_texts(data, content_type, fallback_text):
        repaired = repair_mojibake_text(candidate)
        if repaired in seen_repaired:
            continue
        seen_repaired.add(repaired)
        score = _character_mix_score(repaired)
        if score > best_score:
            best = repaired
            best_score = score

    return best or repair_mojibake_text(fallback_text or "")


def _looks_garbled_line(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False

    suspicious = len(_MOJIBAKE_SEQ_RE.findall(stripped)) + stripped.count("\uFFFD")
    if suspicious == 0:
        return False

    natural = sum(
        1
        for ch in stripped
        if unicodedata.category(ch).startswith("L")
        or unicodedata.category(ch) == "Nd"
    )
    if stripped.count("\uFFFD") >= 2:
        return True
    if suspicious >= 3 and natural < max(10, int(len(stripped) * 0.45)):
        return True
    return False


def sanitize_text(
    text: str,
    *,
    preserve_newlines: bool = True,
    aggressive: bool = False,
) -> str:
    if text is None:
        return ""

    cleaned = str(text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = repair_mojibake_text(cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = _ZERO_WIDTH_RE.sub("", cleaned)
    cleaned = _CONTROL_RE.sub(" ", cleaned)
    cleaned = unicodedata.normalize("NFKC", cleaned)

    if preserve_newlines:
        cleaned = re.sub(r"[ \t\f\v]+", " ", cleaned)
        cleaned = re.sub(r" *\n *", "\n", cleaned)
        if aggressive:
            lines = [
                line for line in cleaned.splitlines() if not _looks_garbled_line(line)
            ]
            cleaned = "\n".join(lines)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    else:
        cleaned = re.sub(r"\s+", " ", cleaned)

    return cleaned.strip()

# ── 載入內嵌版新版爬蟲內核（專案內部自帶，不依賴外部資料夾）──────────
_PROJECT_ROOT = Path(__file__).parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_crawl4ai_plus = None
_crawl4ai_plus_load_error: Exception | None = None
try:
    import pro_search_crawl_backend as _crawl4ai_plus
except Exception as exc:
    _crawl4ai_plus = None
    _crawl4ai_plus_load_error = exc

# 將本模組的 decode_response_text 注入 crawl backend，
# 讓 HTTP 抓取走更精確的 bytes→str 解碼（修復 mojibake）。
if _crawl4ai_plus is not None and hasattr(_crawl4ai_plus, "set_decode_html_hook"):
    _crawl4ai_plus.set_decode_html_hook(decode_response_text)


# ============================================================
# 常數
# ============================================================

# ── 搜尋 API Keys（從 .env 載入）──
BRAVE_SEARCH_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "") or os.environ.get("BRAVE_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
EXA_API_KEY = os.environ.get("EXA_API_KEY", "")
SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY", "") or os.environ.get("SERP_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_BASE_URL = (
    os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    .strip()
    .rstrip("/")
)

# 搜尋 API 端點
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
TAVILY_API_URL = "https://api.tavily.com/search"
EXA_API_URL = "https://api.exa.ai/search"
SERPAPI_API_URL = "https://serpapi.com/search.json"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").strip().rstrip("/")

# 搜尋模式配置（Brave/Tavily 每組查詢預設取 20 筆，SerpApi Scholar 取 20 筆）
DEFAULT_RESULTS_PER_QUERY = 20
DEFAULT_MIN_SELECT_PER_GROUP = 3
DEFAULT_MAX_SELECT_PER_GROUP = 8
DEFAULT_SEARCH_COUNTRY = "US"
SOCIAL_PLATFORM_DOMAINS = ["reddit.com", "x.com", "twitter.com"]
EXA_SNIPPET_CONTENTS: dict[str, Any] = {
    "highlights": {"maxCharacters": 1200},
    "text": {"maxCharacters": 800, "verbosity": "compact"},
}
SEARCH_MODE_CONFIG: dict[str, dict] = {
    "web": {
        "sources": [
            {
                "api": "searxng",
                "per_query": 1,
                "page_native": True,
                "params": {"category": "general", "search_lane": "general"},
            },
            {
                "api": "brave",
                "per_query": DEFAULT_RESULTS_PER_QUERY,
                "params": {"extra_snippets": True},
            },
            {
                "api": "tavily",
                "per_query": DEFAULT_RESULTS_PER_QUERY,
                "params": {
                    "search_depth": "fast",
                },
            },
        ],
        "min_select_per_group": 4,
        "max_select_per_group": 9,
        "fallback_min_pages": 6,
    },
    "academic": {
        "sources": [
            {
                "api": "searxng",
                "per_query": 1,
                "page_native": True,
                "params": {"category": "general", "search_lane": "general"},
            },
            {
                "api": "searxng",
                "per_query": 1,
                "page_native": True,
                "params": {"category": "science", "search_lane": "academic"},
            },
            {
                "api": "exa",
                "per_query": DEFAULT_RESULTS_PER_QUERY,
                "params": {
                    "category": "research paper",
                    "search_type": "auto",
                    "contents": dict(EXA_SNIPPET_CONTENTS),
                },
            },
            {
                "api": "serpapi_google_scholar",
                "per_query": 20,
                "params": {"engine": "google_scholar"},
            },
            {
                "api": "brave",
                "per_query": 20,
                "params": {"extra_snippets": True},
            },
        ],
        "min_select_per_group": 5,
        "max_select_per_group": 10,
        "fallback_min_pages": 6,
    },
    "social": {
        "sources": [
            {
                "api": "searxng",
                "per_query": 1,
                "page_native": True,
                "params": {"category": "general", "search_lane": "general"},
            },
            {
                "api": "searxng",
                "per_query": 1,
                "page_native": True,
                "params": {"category": "social media", "search_lane": "social"},
            },
            {
                "api": "brave",
                "per_query": DEFAULT_RESULTS_PER_QUERY,
                "params": {
                    "result_filter": "discussions",
                    "extra_snippets": True,
                    "query_suffix": "site:reddit.com",
                },
            },
            {
                "api": "tavily",
                "per_query": DEFAULT_RESULTS_PER_QUERY,
                "params": {
                    "search_depth": "fast",
                    "exclude_domains": SOCIAL_PLATFORM_DOMAINS,
                },
            },
            {
                "api": "exa",
                "per_query": 10,
                "params": {
                    "search_type": "auto",
                    "contents": dict(EXA_SNIPPET_CONTENTS),
                },
            },
        ],
        "engine_quota_per_query": DEFAULT_RESULTS_PER_QUERY,
        "domain_quota_per_query": 3,
        "min_select_per_group": 6,
        "max_select_per_group": 11,
        "fallback_min_pages": 6,
    },
}

SOCIAL_FALLBACK_SITE_GROUPS: list[list[str]] = [
    ["ptt.cc", "dcard.tw", "mobile01.com", "forum.gamer.com.tw"],
    ["reddit.com", "news.ycombinator.com", "lemmy.world", "mastodon.social"],
]
SOCIAL_FALLBACK_MAX_QUERIES = 3

HTTP_TIMEOUT_SECONDS = 15.0
OPENROUTER_TIMEOUT_SECONDS = 90.0
MAX_CRAWL_CONCURRENCY = 10
MAX_JS_CONCURRENCY = 3
MAX_CHARS_PER_PAGE = 500000
DIRECT_URL_LIMIT = 5
DIRECT_CRAWL_CONCURRENCY = 5
DIRECT_PLANNER_CONTEXT_TOKEN_LIMIT = 6000
REFRESH_URL_LIMIT = 2
SPECULATIVE_JS_WAIT_SECONDS = 1.5
SPECULATIVE_JS_PATH_RE = re.compile(
    r"(?:^|[/_.-])(?:"
    r"topic|thread|discussion|discuss|forum|forums|comment|comments|reply|replies|"
    r"post|posts|question|questions|answer|answers|talk|community"
    r")(?:$|[/_.-])",
    re.IGNORECASE,
)
SPECULATIVE_JS_QUERY_RE = re.compile(
    r"(?:^|&)(?:comment|comments|reply|thread|topic|discussion|post|question)=",
    re.IGNORECASE,
)
EXPLICIT_HTTP_URL_RE = re.compile(r"https?://[^\s<>{}\[\]\"'，。；：！？、】【（）《》「」『』]+", re.IGNORECASE)

INSTANT_RESULTS_PER_QUERY = 10
INSTANT_LOOP_CRAWL_BUDGET = {"min_total": 3, "target_total": 4, "max_total": 5}
FAST_LOOP_CRAWL_BUDGET = {"min_total": 6, "target_total": 9, "max_total": 11}
FAST_SECOND_LOOP_CRAWL_BUDGET = {"min_total": 3, "target_total": 4, "max_total": 5}
FAST_SECOND_LOOP_RESULTS_PER_QUERY = 8
FAST_SECOND_LOOP_MIN_SELECT_PER_GROUP = 1
FAST_SECOND_LOOP_MAX_SELECT_PER_GROUP = 3
FULL_LOOP_CRAWL_BUDGET = {"min_total": 6, "target_total": 8, "max_total": 9}
LOOP_MIN_QUERY_COVERAGE = 3
LOOP_SOFT_DOMAIN_CAP = 2

HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": HTTP_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7",
}

SEARCH_API_MAX_CONNECTIONS = 32
SEARCH_API_MAX_KEEPALIVE_CONNECTIONS = 16
SEARCH_API_KEEPALIVE_EXPIRY = 20.0
LLM_API_MAX_CONNECTIONS = 8
LLM_API_MAX_KEEPALIVE_CONNECTIONS = 4
LLM_API_KEEPALIVE_EXPIRY = 60.0

CHUNK_MIN_CHARS = 80
CHUNK_TARGET_CHARS = 900
CHUNK_MAX_CHARS = 1400
CHUNK_REVIEW_MAX_RETRIES = 2
CHUNK_REVIEW_FALLBACK_CHUNKS = 12
SEARCH_RESULTS_PER_QUERY_LIMIT = 30
DEFAULT_FILTER_RESULTS_PER_GROUP = SEARCH_RESULTS_PER_QUERY_LIMIT
FINAL_NARROW_CRAWL_BUDGET = {"min_total": 2, "target_total": 3, "max_total": 4}
TRACKING_QUERY_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref_src",
    "spm",
}

_SEARCH_API_CLIENT: httpx.AsyncClient | None = None
_SEARCH_API_CLIENT_LOCK: asyncio.Lock | None = None
_LLM_API_CLIENT: httpx.AsyncClient | None = None
_LLM_API_CLIENT_LOCK: asyncio.Lock | None = None


def _get_search_api_client_lock() -> asyncio.Lock:
    global _SEARCH_API_CLIENT_LOCK
    if _SEARCH_API_CLIENT_LOCK is None:
        _SEARCH_API_CLIENT_LOCK = asyncio.Lock()
    return _SEARCH_API_CLIENT_LOCK


async def _get_search_api_client() -> httpx.AsyncClient:
    global _SEARCH_API_CLIENT

    client = _SEARCH_API_CLIENT
    if client is not None and not getattr(client, "is_closed", False):
        return client

    async with _get_search_api_client_lock():
        client = _SEARCH_API_CLIENT
        if client is not None and not getattr(client, "is_closed", False):
            return client

        limits = httpx.Limits(
            max_connections=SEARCH_API_MAX_CONNECTIONS,
            max_keepalive_connections=SEARCH_API_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=SEARCH_API_KEEPALIVE_EXPIRY,
        )
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS),
            follow_redirects=True,
            http2=True,
            limits=limits,
        )
        _SEARCH_API_CLIENT = client
        return client


async def _close_search_api_client() -> None:
    global _SEARCH_API_CLIENT

    async with _get_search_api_client_lock():
        client = _SEARCH_API_CLIENT
        _SEARCH_API_CLIENT = None

    if client is not None and not getattr(client, "is_closed", False):
        await client.aclose()


def _get_llm_api_client_lock() -> asyncio.Lock:
    global _LLM_API_CLIENT_LOCK
    if _LLM_API_CLIENT_LOCK is None:
        _LLM_API_CLIENT_LOCK = asyncio.Lock()
    return _LLM_API_CLIENT_LOCK


async def _get_llm_api_client() -> httpx.AsyncClient:
    """取得 URL/chunk reviewer 共用的 HTTP client，避免每次 LLM 呼叫重做 TLS。"""
    global _LLM_API_CLIENT

    client = _LLM_API_CLIENT
    if client is not None and not getattr(client, "is_closed", False):
        return client

    async with _get_llm_api_client_lock():
        client = _LLM_API_CLIENT
        if client is not None and not getattr(client, "is_closed", False):
            return client

        limits = httpx.Limits(
            max_connections=LLM_API_MAX_CONNECTIONS,
            max_keepalive_connections=LLM_API_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=LLM_API_KEEPALIVE_EXPIRY,
        )
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(OPENROUTER_TIMEOUT_SECONDS),
            follow_redirects=True,
            http2=True,
            limits=limits,
        )
        _LLM_API_CLIENT = client
        return client


async def _close_llm_api_client() -> None:
    global _LLM_API_CLIENT

    async with _get_llm_api_client_lock():
        client = _LLM_API_CLIENT
        _LLM_API_CLIENT = None

    if client is not None and not getattr(client, "is_closed", False):
        await client.aclose()

_BRAVE_SEARCH_LANG_BY_COUNTRY = {
    "TW": "zh-hant",
    "HK": "zh-hant",
    "MO": "zh-hant",
    "CN": "zh-hans",
    "SG": "zh-hans",
    "JP": "ja",
    "KR": "ko",
    "US": "en",
    "GB": "en",
    "CA": "en",
    "AU": "en",
}
_BRAVE_API_SEARCH_LANG_CODES = {
    "ar",
    "eu",
    "bn",
    "bg",
    "ca",
    "zh-hans",
    "zh-hant",
    "hr",
    "cs",
    "da",
    "nl",
    "en",
    "en-gb",
    "et",
    "fi",
    "fr",
    "gl",
    "de",
    "el",
    "gu",
    "he",
    "hi",
    "hu",
    "is",
    "it",
    "jp",
    "kn",
    "ko",
    "lv",
    "lt",
    "ms",
    "ml",
    "mr",
    "nb",
    "pl",
    "pt-br",
    "pt-pt",
    "pa",
    "ro",
    "ru",
    "sr",
    "sk",
    "sl",
    "es",
    "sv",
    "ta",
    "te",
    "th",
    "tr",
    "uk",
    "vi",
}
_BRAVE_API_SEARCH_LANG_ALIASES = {
    "ja": "jp",
    "ja-jp": "jp",
    "jp-jp": "jp",
    "ko-kr": "ko",
    "zh": "zh-hans",
    "zh-cn": "zh-hans",
    "zh-sg": "zh-hans",
    "zh-tw": "zh-hant",
    "zh-hk": "zh-hant",
    "zh-mo": "zh-hant",
}
_TAVILY_COUNTRY_NAMES = {
    "TW": "taiwan",
    "CN": "china",
    "US": "united states",
    "JP": "japan",
    "KR": "south korea",
    "GB": "united kingdom",
    "CA": "canada",
    "AU": "australia",
    "SG": "singapore",
    "HK": "hong kong",
}
_GOOGLE_HL_BY_SEARCH_LANG = {
    "zh-hant": "zh-TW",
    "zh-hans": "zh-CN",
    "ja": "ja",
    "ko": "ko",
    "en": "en",
}
_TRADITIONAL_ONLY_MARKERS = set("體臺網頁資處這為與發應從點請設啟總層價門會學術業實機")
_SIMPLIFIED_ONLY_MARKERS = set("体台网页资处这为与发应从点请设启总层价门会学术业实机")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._:+/#-]{1,}")
_COUNTRY_HINTS: dict[str, tuple[str, ...]] = {
    "TW": ("台灣", "臺灣", "taiwan", "taipei", "台北", "臺北", "高雄", "taichung"),
    "CN": ("中國", "中国", "大陸", "大陆", "china", "beijing", "shanghai", "深圳"),
    "US": ("美國", "美国", "usa", "u.s.", "united states", "new york", "california", "矽谷"),
    "JP": ("日本", "japan", "tokyo", "東京", "大阪", "osaka", "京都", "kyoto"),
    "KR": ("韓國", "韩国", "south korea", "korea", "seoul", "首爾", "首尔"),
    "GB": ("英國", "英国", "uk", "u.k.", "united kingdom", "britain", "london", "倫敦"),
}
_LOCAL_INTENT_HINTS = (
    "票價", "票价", "房價", "房价", "門市", "门市", "地址", "交通", "捷運", "捷运", "餐廳", "餐厅",
    "政府", "法規", "法规", "報稅", "报税", "學區", "学区", "附近", "營業時間", "营业时间", "門診",
)
_GLOBAL_TOPIC_HINTS = (
    "api", "sdk", "github", "gitlab", "python", "javascript", "typescript", "rust", "go", "java",
    "react", "next.js", "node", "docker", "kubernetes", "postgres", "mysql", "redis", "openai",
    "gemini", "claude", "llm", "ai", "saas", "benchmark", "pricing", "release notes", "changelog",
)
_MIRROR_TRANSLATIONS = (
    ("官方文件", "official documentation"), ("最佳實踐", "best practices"), ("最佳实践", "best practices"),
    ("使用方法", "usage"), ("如何", "how to"), ("怎麼", "how to"), ("怎么", "how to"),
    ("教學", "tutorial"), ("教程", "tutorial"), ("指南", "guide"), ("範例", "examples"), ("示例", "examples"),
    ("比較", "comparison"), ("差異", "differences"), ("優缺點", "pros cons"), ("优缺点", "pros cons"),
    ("原理", "principles"), ("架構", "architecture"), ("架构", "architecture"), ("功能", "features"),
    ("限制", "limitations"), ("價格", "pricing"), ("价格", "pricing"), ("費用", "cost"), ("费用", "cost"),
    ("效能", "performance"), ("性能", "performance"), ("速度", "speed"), ("測試", "benchmark"), ("测试", "benchmark"),
    ("更新", "update"), ("最新", "latest"), ("版本", "version"), ("發佈", "release"), ("发布", "release"),
    ("錯誤", "error"), ("错误", "error"), ("問題", "issue"), ("无法", "cannot"), ("無法", "cannot"),
    ("失敗", "failed"), ("失败", "failed"), ("修復", "fix"), ("修复", "fix"), ("原因", "cause"),
    ("支援", "support"), ("支持", "support"), ("相容", "compatibility"), ("兼容", "compatibility"),
)


def _contains_codepoint_in_ranges(text: str, ranges: tuple[tuple[int, int], ...]) -> bool:
    for char in text:
        code = ord(char)
        for start, end in ranges:
            if start <= code <= end:
                return True
    return False


def _contains_han(text: str) -> bool:
    return _contains_codepoint_in_ranges(text, ((0x3400, 0x4DBF), (0x4E00, 0x9FFF)))


def _contains_hiragana_or_katakana(text: str) -> bool:
    return _contains_codepoint_in_ranges(text, ((0x3040, 0x30FF),))


def _contains_hangul(text: str) -> bool:
    return _contains_codepoint_in_ranges(text, ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF)))


def _contains_bopomofo(text: str) -> bool:
    return _contains_codepoint_in_ranges(text, ((0x3100, 0x312F), (0x31A0, 0x31BF)))


def _han_char_count(text: str) -> int:
    return sum(1 for char in text if _contains_han(char))


def _detect_country_hint(text: str) -> str | None:
    lowered = (text or "").strip().lower()
    if not lowered:
        return None
    for code, hints in _COUNTRY_HINTS.items():
        for hint in hints:
            marker = hint.lower()
            if _LATIN_TOKEN_RE.fullmatch(marker):
                if re.search(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", lowered):
                    return code
            elif marker in lowered:
                return code
    return None


def _detect_brave_search_lang(text: str, country: str | None) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if _contains_hiragana_or_katakana(raw):
        return "ja"
    if _contains_hangul(raw):
        return "ko"
    if _contains_bopomofo(raw):
        return "zh-hant"
    if _contains_han(raw):
        region_lang = _BRAVE_SEARCH_LANG_BY_COUNTRY.get((country or "").upper())
        if region_lang and region_lang.startswith("zh"):
            return region_lang
        trad_score = sum(1 for char in raw if char in _TRADITIONAL_ONLY_MARKERS)
        simp_score = sum(1 for char in raw if char in _SIMPLIFIED_ONLY_MARKERS)
        if trad_score > simp_score:
            return "zh-hant"
        if simp_score > trad_score:
            return "zh-hans"
        return "zh-hant" if country in {"TW", "HK", "MO"} else "zh-hans" if country == "CN" else None
    if _LATIN_TOKEN_RE.search(raw):
        return "en"
    return None


def _normalize_brave_api_search_lang(search_lang: str | None) -> str | None:
    raw = (search_lang or "").strip()
    if not raw:
        return None

    normalized = raw.lower().replace("_", "-")
    aliased = _BRAVE_API_SEARCH_LANG_ALIASES.get(normalized)
    if aliased:
        return aliased
    if normalized in _BRAVE_API_SEARCH_LANG_CODES:
        return normalized

    base = normalized.split("-", 1)[0]
    if base in _BRAVE_API_SEARCH_LANG_CODES:
        return base
    return None


def _extract_brave_error_details(data: dict[str, Any]) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return ""

    detail = str(error.get("detail") or "").strip()
    meta = error.get("meta") if isinstance(error.get("meta"), dict) else {}
    raw_errors = meta.get("errors") if isinstance(meta, dict) else None

    parts: list[str] = []
    if isinstance(raw_errors, list):
        for item in raw_errors[:3]:
            if not isinstance(item, dict):
                continue
            loc = ".".join(str(x) for x in item.get("loc", []))
            message = str(item.get("msg") or item.get("message") or "").strip()
            input_value = item.get("input")
            segment = message
            if loc:
                segment = f"{loc}: {segment}" if segment else loc
            if input_value not in (None, ""):
                segment = f"{segment} (input={input_value})" if segment else f"input={input_value}"
            if segment:
                parts.append(segment)

    if detail and parts:
        return f"{detail}; {' | '.join(parts)}"
    if parts:
        return " | ".join(parts)
    return detail


def _brave_has_search_lang_validation_error(data: dict[str, Any]) -> bool:
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return False

    meta = error.get("meta") if isinstance(error.get("meta"), dict) else {}
    raw_errors = meta.get("errors") if isinstance(meta, dict) else None
    if not isinstance(raw_errors, list):
        return False

    for item in raw_errors:
        if not isinstance(item, dict):
            continue
        loc = item.get("loc", [])
        if isinstance(loc, list) and loc and loc[-1] == "search_lang":
            return True
    return False


def _to_tavily_country(country: str | None) -> str | None:
    if not country:
        return None
    code = (country or "").strip().upper()
    return _TAVILY_COUNTRY_NAMES.get(code)


def _tavily_search_depth_allows_country(search_depth: str | None) -> bool:
    normalized = (search_depth or "basic").strip().lower()
    return normalized not in {"fast", "ultra-fast"}


def _to_google_gl(country: str | None) -> str | None:
    code = (country or "").strip().upper()
    if len(code) != 2:
        return None
    return code.lower()


def _to_google_hl(search_lang: str | None, country: str | None) -> str | None:
    if search_lang:
        mapped = _GOOGLE_HL_BY_SEARCH_LANG.get(search_lang)
        if mapped:
            return mapped
    if country in {"TW", "HK", "MO"}:
        return "zh-TW"
    if country == "CN":
        return "zh-CN"
    if country == "JP":
        return "ja"
    if country == "KR":
        return "ko"
    if country in {"US", "GB", "CA", "AU"}:
        return "en"
    return None


def _normalize_language_hint(language: str | None) -> tuple[str | None, str | None]:
    raw = (language or "").strip().lower().replace("_", "-")
    if not raw:
        return None, None
    if raw.startswith(("zh-tw", "zh-hant")):
        return "zh-hant", "TW"
    if raw.startswith(("zh-cn", "zh-hans")):
        return "zh-hans", "CN"
    if raw.startswith("ja"):
        return "ja", "JP"
    if raw.startswith("ko"):
        return "ko", "KR"
    if raw.startswith("en"):
        return "en", "US"
    return None, None


def _looks_local_query(text: str, country: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    if country in {"TW", "CN", "JP", "KR", "GB"} and any(h.lower() in lowered for h in _LOCAL_INTENT_HINTS):
        return True
    return "near me" in lowered or "附近" in text


def _looks_global_topic(text: str) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in _GLOBAL_TOPIC_HINTS) or len(_LATIN_TOKEN_RE.findall(text or "")) >= 2


def _build_english_mirror(query: str) -> str | None:
    raw = (query or "").strip()
    if not raw:
        return None
    mirror = raw
    for source, target in _MIRROR_TRANSLATIONS:
        mirror = mirror.replace(source, f" {target} ")
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._:+/#-]*", mirror):
        cleaned = token.strip("()[]{}<>\"'`,;:|")
        lowered = cleaned.lower()
        if len(cleaned) < 2 or lowered in seen:
            continue
        seen.add(lowered)
        tokens.append(cleaned)
    if not tokens:
        return None
    return " ".join(tokens[:12])


def _build_query_profile(question: str, search_queries: list[str]) -> dict[str, Any]:
    corpus = " ".join([question or "", *search_queries]).strip()
    country = _detect_country_hint(corpus)
    latin_tokens = len(_LATIN_TOKEN_RE.findall(corpus))
    han_chars = _han_char_count(corpus)
    mixed_latin_technical = latin_tokens >= 3 and han_chars <= 10
    if not country:
        if _contains_bopomofo(corpus):
            country = "TW"
        elif _contains_hiragana_or_katakana(corpus):
            country = "JP"
        elif _contains_hangul(corpus):
            country = "KR"
        elif _contains_han(corpus):
            if mixed_latin_technical:
                country = DEFAULT_SEARCH_COUNTRY
            else:
                trad_score = sum(1 for char in corpus if char in _TRADITIONAL_ONLY_MARKERS)
                simp_score = sum(1 for char in corpus if char in _SIMPLIFIED_ONLY_MARKERS)
                if trad_score > simp_score:
                    country = "TW"
                elif simp_score > trad_score:
                    country = "CN"
    country = country or DEFAULT_SEARCH_COUNTRY
    return {
        "country": country,
        "search_lang": "en" if mixed_latin_technical else _detect_brave_search_lang(corpus, country),
        "is_global_topic": _looks_global_topic(corpus),
    }


def _plan_engine_query(
    query: str,
    *,
    api_name: str,
    search_mode: str,
    profile: dict[str, Any],
    base_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_query = (query or "").strip()
    detected_country = _detect_country_hint(raw_query)
    has_non_english_script = (
        _contains_han(raw_query)
        or _contains_hiragana_or_katakana(raw_query)
        or _contains_hangul(raw_query)
        or _contains_bopomofo(raw_query)
    )
    latin_tokens = len(_LATIN_TOKEN_RE.findall(raw_query))
    han_chars = _han_char_count(raw_query)
    if detected_country:
        country = detected_country
    elif _looks_global_topic(raw_query) and (not has_non_english_script or (latin_tokens >= 3 and han_chars <= 8)):
        country = DEFAULT_SEARCH_COUNTRY
    else:
        country = profile.get("country") or DEFAULT_SEARCH_COUNTRY
    search_lang = _detect_brave_search_lang(raw_query, country)
    if not search_lang and not has_non_english_script:
        search_lang = profile.get("search_lang")
    local_query = _looks_local_query(raw_query, country)
    needs_english_mirror = bool(
        raw_query and search_mode != "social" and not local_query and _looks_global_topic(raw_query)
        and has_non_english_script
    )
    english_mirror = _build_english_mirror(raw_query) if needs_english_mirror else None
    params = dict(base_params or {})
    effective_query = raw_query

    if api_name == "brave":
        params["country"] = country
        if search_lang:
            params["search_lang"] = search_lang
    elif api_name == "tavily":
        tavily_country = _to_tavily_country(country)
        if (
            tavily_country
            and params.get("topic", "general") == "general"
            and _tavily_search_depth_allows_country(params.get("search_depth"))
        ):
            params["country"] = tavily_country
    elif api_name in {"serpapi_google", "serpapi_google_forums"}:
        google_gl = _to_google_gl(country)
        google_hl = _to_google_hl(search_lang, country)
        if google_gl:
            params["gl"] = google_gl
        if google_hl:
            params["hl"] = google_hl
    elif api_name == "serpapi_google_scholar":
        google_hl = _to_google_hl(search_lang, country)
        if google_hl:
            params["hl"] = google_hl
    elif api_name == "exa":
        params["user_location"] = country
        if english_mirror:
            effective_query = f"{english_mirror} {raw_query}".strip()

    return {
        "query": effective_query,
        "params": params,
        "country": country,
        "search_lang": search_lang,
        "english_mirror": english_mirror,
        "needs_english_mirror": bool(english_mirror),
    }


# ── 審核 LLM 預設模型 ─────────────────────────────────────
DEFAULT_GEMINI_FILTER_MODEL = "openrouter/google/gemini-3-flash-preview"
DEFAULT_DEEPSEEK_FILTER_MODEL = "openrouter/deepseek/deepseek-v4-flash"
DEFAULT_FILTER_MODEL = DEFAULT_DEEPSEEK_FILTER_MODEL


def _openrouter_api_model_name(model: str) -> str:
    normalized = (model or "").strip()
    if normalized.startswith("openrouter/"):
        return normalized[len("openrouter/") :]
    return normalized


def _build_llm_route_config(
    model_route: str | None,
    filter_model: str | None,
    model_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if model_settings is not None and not model_settings.get("model"):
        return {
            "route": "disabled",
            "transport": "disabled",
            "model": "",
            "display_model": "未設定 Judge 模型",
        }
    if isinstance(model_settings, dict) and model_settings.get("model"):
        provider = str(model_settings.get("provider") or "custom").strip()
        base_url = str(model_settings.get("base_url") or "").strip().rstrip("/")
        endpoint = str(model_settings.get("chat_endpoint") or "/chat/completions").strip()
        if endpoint and not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        使用OpenRouter格式 = provider.lower() == "openrouter" or "openrouter.ai" in base_url.lower()
        return {
            "route": provider,
            "transport": "openai_compatible_http",
            "provider": provider,
            "model": str(model_settings.get("model") or "").strip(),
            "display_model": f"{provider}/{model_settings.get('model')}",
            "api_key": str(model_settings.get("api_key") or "").strip(),
            "base_url": f"{base_url}{endpoint}" if base_url else "",
            "headers": dict(model_settings.get("headers") or {}),
            # Judge 是篩選器而不是推理型回答器；依端點使用對應的停用格式。
            "reasoning": {"effort": "none"} if 使用OpenRouter格式 else None,
            "reasoning_effort": None if 使用OpenRouter格式 else "none",
        }

    route = normalize_model_route(model_route)

    if route == "g":
        resolved_model = (filter_model or DEFAULT_GEMINI_FILTER_MODEL).strip()
        if not resolved_model:
            resolved_model = DEFAULT_GEMINI_FILTER_MODEL
        if not resolved_model.startswith("openrouter/"):
            resolved_model = f"openrouter/{resolved_model}"
        return {
            "route": "g",
            "transport": "openrouter_http",
            "model": _openrouter_api_model_name(resolved_model),
            "display_model": resolved_model,
            "reasoning": {"enabled": False},
        }

    resolved_model = (filter_model or DEFAULT_DEEPSEEK_FILTER_MODEL).strip()
    if not resolved_model:
        resolved_model = DEFAULT_DEEPSEEK_FILTER_MODEL
    display_model = (
        resolved_model
        if resolved_model.startswith("openrouter/")
        else f"openrouter/{resolved_model}"
    )
    return {
        "route": "d",
        "transport": "openrouter_http",
        "model": _openrouter_api_model_name(resolved_model),
        "display_model": display_model,
        "reasoning": {"effort": "none"},
    }


def _extract_openrouter_text_response(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip()
    return ""


async def _openrouter_chat_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    reasoning: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    active_api_key = (api_key if api_key is not None else OPENROUTER_API_KEY).strip()
    active_base_url = (base_url or OPENROUTER_API_URL).strip()
    if not active_api_key:
        raise RuntimeError("LLM API Key 未設定，無法呼叫模型")
    if not active_base_url:
        raise RuntimeError("LLM API 地址未設定，無法呼叫模型")

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if reasoning is not None:
        payload["reasoning"] = reasoning
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    headers = {
        "Authorization": f"Bearer {active_api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update({str(k): str(v) for k, v in extra_headers.items()})

    client = await _get_llm_api_client()
    response = await client.post(
        active_base_url,
        headers=headers,
        json=payload,
    )

    try:
        data = response.json()
    except Exception:
        data = {}

    if response.is_error:
        error_payload = data.get("error") if isinstance(data, dict) else None
        error_message = None
        if isinstance(error_payload, dict):
            error_message = error_payload.get("message") or error_payload.get("code")
        raise RuntimeError(
            f"OpenRouter HTTP {response.status_code}: {error_message or response.text}"
        )

    content = _extract_openrouter_text_response(data)
    if content:
        return content
    raise RuntimeError("OpenRouter 回傳缺少有效的 assistant content")


async def _nvidia_chat_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    reasoning_effort: str | None = None,
) -> str:
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY 未設定，無法呼叫 NVIDIA NIM")
    if not NVIDIA_BASE_URL:
        raise RuntimeError("NVIDIA_BASE_URL 未設定，無法呼叫 NVIDIA NIM")

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
    }

    client = await _get_llm_api_client()
    response = await client.post(
        f"{NVIDIA_BASE_URL}/chat/completions",
        headers=headers,
        json=payload,
    )

    try:
        data = response.json()
    except Exception:
        data = {}

    if response.is_error:
        error_payload = data.get("error") if isinstance(data, dict) else None
        error_message = None
        if isinstance(error_payload, dict):
            error_message = error_payload.get("message") or error_payload.get("code")
        raise RuntimeError(
            f"NVIDIA HTTP {response.status_code}: {error_message or response.text}"
        )

    content = _extract_openrouter_text_response(data)
    if content:
        return content
    raise RuntimeError("NVIDIA 回傳缺少有效的 assistant content")


async def _call_llm_raw_content(
    *,
    llm_config: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
) -> str:
    if llm_config.get("transport") == "disabled":
        raise RuntimeError("Judge 模型未設定")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    if llm_config.get("transport") in {"openrouter_http", "openai_compatible_http"}:
        return await _openrouter_chat_completion(
            model=str(llm_config.get("model") or ""),
            messages=messages,
            reasoning=llm_config.get("reasoning"),
            reasoning_effort=llm_config.get("reasoning_effort"),
            api_key=llm_config.get("api_key"),
            base_url=llm_config.get("base_url"),
            extra_headers=llm_config.get("headers"),
        )

    if llm_config.get("transport") == "nvidia_http":
        return await _nvidia_chat_completion(
            model=str(llm_config.get("model") or ""),
            messages=messages,
            reasoning_effort=str(llm_config.get("reasoning_effort") or ""),
        )

    # 非公開擴充路線才需要 LiteLLM；延遲匯入可避免正常 d/g 路線
    # 在每次 MCP 冷啟動多付約一秒初始化成本。
    try:
        from litellm import acompletion
    except ImportError as exc:
        raise RuntimeError("litellm 未安裝，無法呼叫自訂 LLM 路線") from exc

    response = await acompletion(
        model=str(llm_config.get("model") or ""),
        messages=messages,
    )
    return response.choices[0].message.content or ""


# ============================================================
# Pipeline Monitor（即時終端顯示所有 I/O）
# ============================================================


class PipelineMonitor:
    """管線監控器：即時在終端顯示所有步驟和 LLM I/O。"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    BLUE = "\033[34m"
    WHITE = "\033[37m"
    BG_DARK = "\033[40m"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._start_time = time.time()

    def _elapsed(self) -> str:
        return f"{time.time() - self._start_time:.1f}s"

    def _print(self, *args, **kwargs):
        if self.enabled:
            print(*args, **kwargs, flush=True)

    def _separator(self, char: str = "─", width: int = 80):
        self._print(f"{self.DIM}{char * width}{self.RESET}")

    def _header(self, text: str):
        self._print(f"\n{self.BOLD}{self.CYAN}{'═' * 80}{self.RESET}")
        self._print(f"{self.BOLD}{self.CYAN}  {text}{self.RESET}")
        self._print(f"{self.BOLD}{self.CYAN}{'═' * 80}{self.RESET}")

    def pipeline_start(self, question: str, queries: list[str], mode: str):
        self._header("[>>] Deep Search Pipeline START")
        self._print(f"{self.WHITE}  問題: {self.BOLD}{question}{self.RESET}")
        self._print(f"{self.WHITE}  模式: {self.BOLD}{mode}{self.RESET}")
        self._print(f"{self.WHITE}  查詢: {self.BOLD}{queries}{self.RESET}")
        self._separator()

    def step_start(self, step: int, title: str, detail: str = ""):
        self._print(f"\n{self.BOLD}{self.GREEN}[>] Step {step}: {title}{self.RESET}")
        if detail:
            self._print(f"  {self.DIM}{detail}{self.RESET}")

    def step_done(self, step: int, summary: str):
        self._print(
            f"{self.GREEN}  [OK] Step {step} 完成 [{self._elapsed()}] -- {summary}{self.RESET}"
        )

    def search_results(self, groups: list[dict]):
        """顯示搜尋結果摘要。"""
        for gi, group in enumerate(groups):
            n = len(group.get("results", []))
            self._print(
                f"  {self.YELLOW}[組 {gi + 1}]{self.RESET} [{group['query']}]: {n} 條結果"
            )
            plan = group.get("plan") or {}
            if plan:
                locale = f"{plan.get('country', '?')}/{plan.get('search_lang') or '-'}"
                engine_queries = plan.get("engine_queries", {})
                brave_query = engine_queries.get("brave")
                exa_query = engine_queries.get("exa")
                if brave_query or exa_query:
                    self._print(
                        f"    {self.DIM}locale={locale} | Brave={brave_query or '-'} | Exa={exa_query or '-'}{self.RESET}"
                    )
            for ri, r in enumerate(group.get("results", [])[:5]):
                self._print(f"    {self.DIM}[{ri}] {r['title'][:65]}{self.RESET}")
            if n > 5:
                self._print(f"    {self.DIM}... 還有 {n - 5} 條{self.RESET}")

    def llm_input(self, system_prompt: str, user_prompt: str, model: str):
        """顯示送給 LLM 的完整輸入。"""
        self._print(
            f"\n{self.BOLD}{self.MAGENTA}+-- LLM 審核輸入 {'─' * 50}{self.RESET}"
        )
        self._print(f"{self.MAGENTA}|  模型: {model}{self.RESET}")
        self._print(f"{self.MAGENTA}|{self.RESET}")
        self._print(
            f"{self.MAGENTA}|  -- System Prompt ({len(system_prompt)} 字元) --{self.RESET}"
        )
        for line in system_prompt[:500].split("\n"):
            self._print(f"{self.DIM}{self.MAGENTA}|  {line}{self.RESET}")
        if len(system_prompt) > 500:
            self._print(
                f"{self.DIM}{self.MAGENTA}|  ... (共 {len(system_prompt)} 字元){self.RESET}"
            )
        self._print(f"{self.MAGENTA}|{self.RESET}")
        self._print(
            f"{self.MAGENTA}|  -- User Prompt ({len(user_prompt)} 字元) --{self.RESET}"
        )
        for line in user_prompt[:800].split("\n"):
            self._print(f"{self.DIM}{self.MAGENTA}|  {line}{self.RESET}")
        if len(user_prompt) > 800:
            self._print(
                f"{self.DIM}{self.MAGENTA}|  ... (共 {len(user_prompt)} 字元){self.RESET}"
            )
        self._print(f"{self.MAGENTA}+{'─' * 59}{self.RESET}")

    def llm_output(self, raw_response: str):
        """顯示 LLM 的完整回傳。"""
        self._print(f"\n{self.BOLD}{self.BLUE}+-- LLM 審核輸出 {'─' * 50}{self.RESET}")
        for line in raw_response.split("\n"):
            self._print(f"{self.BLUE}|  {line}{self.RESET}")
        self._print(f"{self.BLUE}+{'─' * 59}{self.RESET}")

    def filter_selection(self, selected: list[dict], total: int):
        """顯示篩選結果。"""
        self._print(f"  {self.YELLOW}[選中] 共 {total} 條 URL{self.RESET}")
        for item in selected:
            self._print(
                f"    組 {item['group_index'] + 1}: 選中索引 {item['result_indices']}"
            )
            if item.get("reasoning"):
                self._print(f"    {self.DIM}理由: {item['reasoning'][:80]}{self.RESET}")

    def crawl_progress(self, url: str, status: str, detail: str = ""):
        """單頁爬取進度。"""
        icon = "[OK] " if status == "ok" else "[!!] " if status == "fail" else "[..]"
        short_url = url[:70] + "..." if len(url) > 70 else url
        msg = f"  {icon} {short_url}"
        if detail:
            msg += f"  {self.DIM}({detail}){self.RESET}"
        self._print(msg)

    def dedup_notice(self, removed_count: int):
        """URL 去重通知。"""
        if removed_count > 0:
            self._print(
                f"  {self.YELLOW}[DEDUP] 移除 {removed_count} 個重複 URL{self.RESET}"
            )


    def pipeline_done(self, summary: dict, elapsed_ms: int):
        self._header("[>>] Deep Search Pipeline DONE")
        self._print(f"  搜尋結果: {summary.get('total_found', 0)} 條")
        self._print(f"  LLM 篩選: {summary.get('total_selected', 0)} 條")
        self._print(f"  爬取成功: {summary.get('total_crawled_success', 0)} 條")
        self._print(f"  爬取失敗: {summary.get('total_crawled_failed', 0)} 條")
        self._print(f"  總耗時: {elapsed_ms}ms")
        self._separator("=")


# 直接呼叫 adapter 時的預設 monitor；完整管線使用 request-local context。
_monitor = PipelineMonitor(enabled=True)
_monitor_context: ContextVar[PipelineMonitor] = ContextVar(
    "pro_search_pipeline_monitor",
    default=_monitor,
)


def _current_monitor() -> PipelineMonitor:
    """取得當前請求的 monitor，避免並行搜尋共用可變全域狀態。"""
    return _monitor_context.get()


# ============================================================
# 模組一：多源搜尋 API（Brave + Tavily + Exa + SerpApi）
# ============================================================


# ── Brave Search API 客戶端 ──────────────────────────────────
async def _brave_search(
    query: str,
    count: int = 20,
    *,
    extra_snippets: bool = True,
    result_filter: str | None = None,
    freshness: str | None = None,
    country: str = DEFAULT_SEARCH_COUNTRY,
    search_lang: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """呼叫 Brave Web Search API，返回標準化結果列表。"""
    active_api_key = (api_key if api_key is not None else BRAVE_SEARCH_API_KEY).strip()
    active_base_url = (base_url or BRAVE_API_URL).strip()
    if not active_api_key or not active_base_url:
        return []

    brave_search_lang = _normalize_brave_api_search_lang(search_lang)
    params: dict[str, Any] = {
        "q": query,
        "count": min(count, 20),
        "text_decorations": "false",
    }
    if country:
        params["country"] = country
    if brave_search_lang:
        params["search_lang"] = brave_search_lang
    if extra_snippets:
        params["extra_snippets"] = "true"
    if result_filter:
        params["result_filter"] = result_filter
    if freshness:
        params["freshness"] = freshness

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": active_api_key,
    }

    try:
        client = await _get_search_api_client()
        resp = await client.get(active_base_url, params=params, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {}

        if (
            resp.status_code == 422
            and "search_lang" in params
            and _brave_has_search_lang_validation_error(data)
        ):
            failed_lang = params.get("search_lang")
            fallback_params = dict(params)
            fallback_params.pop("search_lang", None)
            mon = _current_monitor()
            mon._print(
                f"  {mon.YELLOW}[Brave] search_lang={failed_lang} 不被接受，省略 search_lang 後重試{mon.RESET}"
            )
            resp = await client.get(active_base_url, params=fallback_params, headers=headers)
            try:
                data = resp.json()
            except Exception:
                data = {}

        if resp.is_error:
            details = _extract_brave_error_details(data)
            raise RuntimeError(
                f"HTTP {resp.status_code}: {details or resp.text}"
            )
    except Exception as e:
        mon = _current_monitor()
        mon._print(f"  {mon.RED}[Brave] 搜尋失敗: {e}{mon.RESET}")
        return []

    results: list[dict[str, Any]] = []
    for r in data.get("web", {}).get("results", []):
        url = sanitize_text(r.get("url", ""), preserve_newlines=False)
        if not url:
            continue

        # 組合 description + extra_snippets 作為豐富摘要
        content_parts = []
        desc = r.get("description", "")
        if desc:
            content_parts.append(sanitize_text(desc, preserve_newlines=True, aggressive=True))
        for snippet in r.get("extra_snippets", []):
            if snippet:
                content_parts.append(sanitize_text(snippet, preserve_newlines=True, aggressive=True))

        results.append({
            "title": sanitize_text(r.get("title", ""), preserve_newlines=False),
            "url": url,
            "content": "\n\n".join(content_parts) if content_parts else "",
            "engine": "brave",
            "relevance_score": None,
            "published_date": r.get("page_age", r.get("age", None)),
        })

    return results[:count]


# ── Tavily API 客戶端 ────────────────────────────────────────
async def _tavily_search(
    query: str,
    max_results: int = 20,
    *,
    search_depth: str = "fast",
    topic: str = "general",
    time_range: str | None = None,
    country: str | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """呼叫 Tavily Search API，返回標準化結果列表。"""
    active_api_key = (api_key if api_key is not None else TAVILY_API_KEY).strip()
    active_base_url = (base_url or TAVILY_API_URL).strip()
    if not active_api_key or not active_base_url:
        return []

    payload: dict[str, Any] = {
        "query": query,
        "max_results": min(max_results, 20),
        "search_depth": search_depth,
        "topic": topic,
        "include_answer": False,
        "include_raw_content": False,
    }
    if time_range:
        payload["time_range"] = time_range
    if country and _tavily_search_depth_allows_country(search_depth):
        payload["country"] = country
    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {active_api_key}",
    }

    try:
        client = await _get_search_api_client()
        resp = await client.post(active_base_url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        mon = _current_monitor()
        mon._print(f"  {mon.RED}[Tavily] 搜尋失敗: {e}{mon.RESET}")
        return []

    results: list[dict[str, Any]] = []
    for r in data.get("results", []):
        url = sanitize_text(r.get("url", ""), preserve_newlines=False)
        if not url:
            continue

        results.append({
            "title": sanitize_text(r.get("title", ""), preserve_newlines=False),
            "url": url,
            "content": sanitize_text(r.get("content", ""), preserve_newlines=True, aggressive=True),
            "engine": "tavily",
            "relevance_score": r.get("score"),
            "published_date": r.get("published_date"),
        })

    return results[:max_results]


# ── Exa API 客戶端 ───────────────────────────────────────────
async def _exa_search(
    query: str,
    num_results: int = 10,
    *,
    search_type: str = "auto",
    category: str | None = None,
    contents: dict | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    start_published_date: str | None = None,
    end_published_date: str | None = None,
    user_location: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """呼叫 Exa Search API，返回標準化結果列表。"""
    active_api_key = (api_key if api_key is not None else EXA_API_KEY).strip()
    active_base_url = (base_url or EXA_API_URL).strip()
    if not active_api_key or not active_base_url:
        return []

    payload: dict[str, Any] = {
        "query": query,
        "numResults": min(num_results, 100),
        "type": search_type,
    }
    if category:
        payload["category"] = category
    if contents:
        payload["contents"] = contents
    if include_domains:
        payload["includeDomains"] = include_domains
    if exclude_domains:
        payload["excludeDomains"] = exclude_domains
    if start_published_date:
        payload["startPublishedDate"] = start_published_date
    if end_published_date:
        payload["endPublishedDate"] = end_published_date
    if user_location:
        payload["userLocation"] = user_location

    headers = {
        "Content-Type": "application/json",
        "x-api-key": active_api_key,
    }

    try:
        client = await _get_search_api_client()
        resp = await client.post(active_base_url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        mon = _current_monitor()
        mon._print(f"  {mon.RED}[Exa] 搜尋失敗: {e}{mon.RESET}")
        return []

    results: list[dict[str, Any]] = []
    for r in data.get("results", []):
        url = sanitize_text(r.get("url", ""), preserve_newlines=False)
        if not url:
            continue

        # 組合短摘要：highlights 優先；text 只作 compact fallback。
        content_parts = []
        if r.get("highlights"):
            for h in r["highlights"]:
                content_parts.append(sanitize_text(h, preserve_newlines=True))

        # 全文（Exa 的 text 欄位）
        raw_text = r.get("text", None)

        # 相關性分數：取 highlights 平均分數
        highlight_scores = r.get("highlightScores", [])
        avg_score = sum(highlight_scores) / len(highlight_scores) if highlight_scores else None

        results.append({
            "title": sanitize_text(r.get("title", ""), preserve_newlines=False),
            "url": url,
            "content": "\n\n".join(content_parts) if content_parts else sanitize_text((raw_text or "")[:500], preserve_newlines=True),
            "engine": "exa",
            "relevance_score": avg_score,
            "published_date": r.get("publishedDate"),
        })

    return results[:num_results]


# ── SerpApi 客戶端 ───────────────────────────────────────────
def _extract_serpapi_year(*texts: Any) -> str | None:
    for text in texts:
        value = sanitize_text(str(text or ""), preserve_newlines=False)
        match = re.search(r"(19|20)\d{2}", value)
        if match:
            return match.group(0)
    return None


async def _serpapi_request(
    params: dict[str, Any],
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    active_api_key = (api_key if api_key is not None else SERPAPI_API_KEY).strip()
    active_base_url = (base_url or SERPAPI_API_URL).strip()
    if not active_api_key or not active_base_url:
        return {}

    query = {k: v for k, v in params.items() if v not in (None, "", [], {})}
    query["api_key"] = active_api_key
    query["output"] = "json"

    try:
        client = await _get_search_api_client()
        resp = await client.get(active_base_url, params=query)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        # SerpApi key 位於 query string；不可把包含完整 URL 的例外寫入日誌。
        status = exc.response.status_code if exc.response is not None else "unknown"
        mon = _current_monitor()
        mon._print(
            f"  {mon.RED}[SerpApi] 搜尋失敗: HTTP {status}{mon.RESET}"
        )
        return {}
    except Exception as exc:
        mon = _current_monitor()
        mon._print(
            f"  {mon.RED}[SerpApi] 搜尋失敗: {type(exc).__name__}{mon.RESET}"
        )
        return {}


async def _serpapi_google_search(
    query: str,
    num_results: int = 10,
    *,
    engine: str = "google",
    gl: str | None = None,
    hl: str | None = None,
    start: int | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    data = await _serpapi_request(
        {
            "engine": engine or "google",
            "q": query,
            "num": min(max(1, num_results), 100),
            "gl": gl,
            "hl": hl,
            "start": start,
        },
        api_key=api_key,
        base_url=base_url,
    )

    results: list[dict[str, Any]] = []
    for r in data.get("organic_results", []):
        url = sanitize_text(r.get("link", ""), preserve_newlines=False)
        if not url:
            continue
        results.append(
            {
                "title": sanitize_text(r.get("title", ""), preserve_newlines=False),
                "url": url,
                "content": sanitize_text(
                    r.get("snippet", ""),
                    preserve_newlines=True,
                    aggressive=True,
                ),
                "engine": "serpapi_google",
                "relevance_score": None,
                "published_date": r.get("date"),
            }
        )
    return results[:num_results]


async def _serpapi_google_scholar_search(
    query: str,
    num_results: int = 10,
    *,
    engine: str = "google_scholar",
    hl: str | None = None,
    start: int | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    data = await _serpapi_request(
        {
            "engine": engine or "google_scholar",
            "q": query,
            "num": min(max(1, num_results), 20),
            "hl": hl,
            "start": start,
        },
        api_key=api_key,
        base_url=base_url,
    )

    results: list[dict[str, Any]] = []
    for r in data.get("organic_results", []):
        url = sanitize_text(r.get("link", ""), preserve_newlines=False)
        if not url:
            continue

        publication_summary = sanitize_text(
            (r.get("publication_info") or {}).get("summary", ""),
            preserve_newlines=False,
        )
        snippet = sanitize_text(
            r.get("snippet", ""),
            preserve_newlines=True,
            aggressive=True,
        )
        citation_total = ((r.get("inline_links") or {}).get("cited_by") or {}).get("total")
        content_parts = [part for part in (publication_summary, snippet) if part]
        if citation_total:
            content_parts.append(f"Cited by {citation_total}")

        results.append(
            {
                "title": sanitize_text(r.get("title", ""), preserve_newlines=False),
                "url": url,
                "content": "\n\n".join(content_parts),
                "engine": "serpapi_google_scholar",
                "relevance_score": None,
                "published_date": _extract_serpapi_year(publication_summary, snippet),
            }
        )
    return results[:num_results]


async def _serpapi_google_forums_search(
    query: str,
    num_results: int = 10,
    *,
    engine: str = "google_forums",
    gl: str | None = None,
    hl: str | None = None,
    start: int | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    data = await _serpapi_request(
        {
            "engine": engine or "google_forums",
            "q": query,
            "gl": gl,
            "hl": hl,
            "start": start,
        },
        api_key=api_key,
        base_url=base_url,
    )

    results: list[dict[str, Any]] = []
    for r in data.get("organic_results", []):
        url = sanitize_text(r.get("link", ""), preserve_newlines=False)
        if not url:
            continue

        source = sanitize_text(r.get("source", ""), preserve_newlines=False)
        snippet = sanitize_text(
            r.get("snippet", ""),
            preserve_newlines=True,
            aggressive=True,
        )
        content_parts = [part for part in (source, snippet) if part]

        results.append(
            {
                "title": sanitize_text(r.get("title", ""), preserve_newlines=False),
                "url": url,
                "content": "\n\n".join(content_parts),
                "engine": "serpapi_google_forums",
                "relevance_score": None,
                "published_date": r.get("date"),
            }
        )
    return results[: max(1, num_results)]


def _nested_value(payload: Any, path: str, default: Any = None) -> Any:
    """以點號路徑讀取 JSON；自定義搜尋引擎可用 `data.results` 這類路徑。"""
    current = payload
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part, default)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else default
        else:
            return default
    return current


async def _searxng_search(
    query: str,
    _page_marker: int = 1,
    *,
    category: str = "general",
    search_lane: str = "general",
    pageno: int = 1,
    language: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """呼叫 SearXNG JSON API；保留頁面排序，單一 query 最多取前 30 筆。"""
    del _page_marker, api_key
    active_base_url = (base_url or SEARXNG_URL).strip().rstrip("/")
    if not active_base_url:
        return []

    params: dict[str, Any] = {
        "q": query,
        "format": "json",
        "pageno": max(1, int(pageno)),
        "categories": category or "general",
    }
    if language:
        params["language"] = language

    try:
        client = await _get_search_api_client()
        response = await client.get(
            f"{active_base_url}/search",
            params=params,
            headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        mon = _current_monitor()
        mon._print(
            f"  {mon.RED}[SearXNG/{search_lane}] 搜尋失敗: "
            f"{type(exc).__name__}{mon.RESET}"
        )
        return []

    normalized: list[dict[str, Any]] = []
    raw_results = data.get("results", []) if isinstance(data, dict) else []
    for item in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(item, dict):
            continue
        url = sanitize_text(item.get("url", ""), preserve_newlines=False)
        if not url:
            continue
        engines = item.get("engines")
        if not isinstance(engines, list):
            engine_name = item.get("engine")
            engines = [engine_name] if engine_name else []
        normalized.append(
            {
                "title": sanitize_text(item.get("title", ""), preserve_newlines=False),
                "url": url,
                "content": sanitize_text(
                    item.get("content", ""),
                    preserve_newlines=True,
                    aggressive=True,
                ),
                "engine": "searxng",
                "source_engines": [str(value) for value in engines if value],
                "search_lane": search_lane,
                "searxng_category": category,
                "searxng_page": max(1, int(pageno)),
                "relevance_score": item.get("score"),
                "published_date": item.get("publishedDate") or item.get("published_date"),
            }
        )
    return normalized[:SEARCH_RESULTS_PER_QUERY_LIMIT]


async def _custom_search(
    query: str,
    max_results: int,
    *,
    provider: dict[str, Any],
) -> list[dict[str, Any]]:
    """呼叫可由設定頁建立的通用 JSON 搜尋 API。"""
    base_url = str(provider.get("base_url") or "").strip()
    if not base_url:
        return []

    method = str(provider.get("method") or "GET").strip().upper()
    query_param = str(provider.get("query_param") or "q").strip() or "q"
    count_param = str(provider.get("count_param") or "count").strip()
    api_key = str(provider.get("api_key") or "").strip()
    auth_mode = str(provider.get("auth_mode") or "bearer").strip().lower()
    auth_name = str(provider.get("auth_name") or "Authorization").strip()
    request_data: dict[str, Any] = {query_param: query}
    if count_param:
        request_data[count_param] = max(1, int(max_results))

    headers = {"Accept": "application/json"}
    if api_key:
        if auth_mode == "query":
            request_data[auth_name or "api_key"] = api_key
        elif auth_mode == "header":
            headers[auth_name or "X-API-Key"] = api_key
        elif auth_mode != "none":
            headers[auth_name or "Authorization"] = f"Bearer {api_key}"

    extra_params = provider.get("extra_params")
    if isinstance(extra_params, dict):
        request_data.update(extra_params)

    try:
        client = await _get_search_api_client()
        if method == "POST":
            response = await client.post(base_url, json=request_data, headers=headers)
        else:
            response = await client.get(base_url, params=request_data, headers=headers)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        mon = _current_monitor()
        display_name = str(provider.get("name") or provider.get("id") or "自定義")
        mon._print(
            f"  {mon.RED}[{display_name}] 搜尋失敗: {type(exc).__name__}{mon.RESET}"
        )
        return []

    result_path = str(provider.get("result_path") or "results")
    raw_results = _nested_value(data, result_path, [])
    if not isinstance(raw_results, list):
        return []

    fields = provider.get("fields") if isinstance(provider.get("fields"), dict) else {}
    title_field = str(fields.get("title") or "title")
    url_field = str(fields.get("url") or "url")
    content_field = str(fields.get("content") or "content")
    score_field = str(fields.get("score") or "score")
    date_field = str(fields.get("published_date") or "published_date")
    provider_id = str(provider.get("id") or "custom")

    normalized: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = sanitize_text(_nested_value(item, url_field, ""), preserve_newlines=False)
        if not url:
            continue
        normalized.append(
            {
                "title": sanitize_text(
                    _nested_value(item, title_field, ""), preserve_newlines=False
                ),
                "url": url,
                "content": sanitize_text(
                    _nested_value(item, content_field, ""),
                    preserve_newlines=True,
                    aggressive=True,
                ),
                "engine": provider_id,
                "search_lane": "custom",
                "relevance_score": _nested_value(item, score_field),
                "published_date": _nested_value(item, date_field),
            }
        )
    return normalized[: max(1, int(max_results))]


# ── API 調度器 ───────────────────────────────────────────────
_API_DISPATCH: dict[str, Any] = {
    "searxng": _searxng_search,
    "brave": _brave_search,
    "tavily": _tavily_search,
    "exa": _exa_search,
    "serpapi_google": _serpapi_google_search,
    "serpapi_google_scholar": _serpapi_google_scholar_search,
    "serpapi_google_forums": _serpapi_google_forums_search,
}

_API_KEY_CHECK: dict[str, str] = {
    "searxng": "",
    "brave": "BRAVE_SEARCH_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "exa": "EXA_API_KEY",
    "serpapi_google": "SERPAPI_API_KEY",
    "serpapi_google_scholar": "SERPAPI_API_KEY",
    "serpapi_google_forums": "SERPAPI_API_KEY",
}

_API_PER_QUERY_CAP: dict[str, int] = {
    "searxng": 1,
    "brave": 20,
    "tavily": 20,
    "exa": 100,
    "serpapi_google": 100,
    "serpapi_google_scholar": 20,
    "serpapi_google_forums": 100,
}


def _get_api_per_query_cap(api_name: str) -> int:
    return int(_API_PER_QUERY_CAP.get(api_name, 100))


def _redistribute_source_quota(
    available_sources: list[dict[str, Any]],
    total_redistribute: int,
) -> None:
    remaining = max(0, int(total_redistribute))
    while remaining > 0:
        eligible: list[tuple[int, int]] = []
        for idx, source in enumerate(available_sources):
            api_name = str(source.get("api", ""))
            per_query_cap = _get_api_per_query_cap(api_name)
            current = int(source.get("per_query", 0))
            if current < per_query_cap:
                eligible.append((idx, per_query_cap))

        if not eligible:
            break

        extra_per = remaining // len(eligible)
        remainder = remaining % len(eligible)
        distributed = 0
        for order, (idx, per_query_cap) in enumerate(eligible):
            share = extra_per + (1 if order < remainder else 0)
            if share <= 0:
                continue
            current = int(available_sources[idx].get("per_query", 0))
            granted = min(share, per_query_cap - current)
            if granted <= 0:
                continue
            available_sources[idx]["per_query"] = current + granted
            distributed += granted

        if distributed <= 0:
            break
        remaining -= distributed


async def _call_api_source(
    api_name: str,
    query: str,
    per_query: int,
    params: dict,
    seen_urls: set[str] | None = None,
    provider_options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """呼叫指定 API 並進行 URL 去重。"""
    fn = _API_DISPATCH.get(api_name)
    if api_name.startswith("custom:"):
        fn = _custom_search
    if fn is None:
        return []

    call_params = dict(params or {})
    effective_query = (query or "").strip()
    query_suffix = str(call_params.pop("query_suffix", "") or "").strip()
    if query_suffix and query_suffix.lower() not in effective_query.lower():
        effective_query = f"{effective_query} {query_suffix}".strip()

    # 過濾掉 API 客戶端函數不接受的參數
    active_provider_options = dict(provider_options or {})
    api_key = active_provider_options.get("api_key")
    base_url = active_provider_options.get("base_url")

    if api_name == "searxng":
        raw = await fn(
            effective_query,
            _page_marker=per_query,
            **{
                k: v
                for k, v in call_params.items()
                if k in ("category", "search_lane", "pageno", "language")
            },
            api_key=api_key,
            base_url=base_url,
        )
    elif api_name.startswith("custom:"):
        raw = await fn(
            effective_query,
            max_results=per_query,
            provider=active_provider_options,
        )
    elif api_name == "brave":
        raw = await fn(
            effective_query,
            count=per_query,
            **{
                k: v
                for k, v in call_params.items()
                if k
                not in (
                    "search_type",
                    "category",
                    "contents",
                    "search_depth",
                    "topic",
                )
            },
            api_key=api_key,
            base_url=base_url,
        )
    elif api_name == "tavily":
        raw = await fn(
            effective_query,
            max_results=per_query,
            **{
                k: v
                for k, v in call_params.items()
                if k not in ("search_type", "category", "contents", "result_filter")
            },
            api_key=api_key,
            base_url=base_url,
        )
    elif api_name == "exa":
        raw = await fn(
            effective_query,
            num_results=per_query,
            **{
                k: v
                for k, v in call_params.items()
                if k
                not in (
                    "search_depth",
                    "topic",
                    "result_filter",
                    "extra_snippets",
                )
            },
            api_key=api_key,
            base_url=base_url,
        )
    elif api_name in (
        "serpapi_google",
        "serpapi_google_scholar",
        "serpapi_google_forums",
    ):
        raw = await fn(
            effective_query,
            num_results=per_query,
            **{
                k: v
                for k, v in call_params.items()
                if k
                not in (
                    "search_type",
                    "category",
                    "contents",
                    "search_depth",
                    "topic",
                    "result_filter",
                    "extra_snippets",
                    "include_domains",
                    "exclude_domains",
                    "user_location",
                    "country",
                    "search_lang",
                )
            },
            api_key=api_key,
            base_url=base_url,
        )
    else:
        raw = []

    # URL 去重
    deduped: list[dict[str, Any]] = []
    for item in raw:
        url = item.get("url", "")
        url_key = _normalize_url(url)
        if url and url_key and (seen_urls is None or url_key not in seen_urls):
            if seen_urls is not None:
                seen_urls.add(url_key)
            deduped.append(item)
    return deduped


_BUILTIN_SEARCH_DEFAULTS: dict[str, dict[str, Any]] = {
    "searxng": {"enabled": True, "api_key": "", "base_url": SEARXNG_URL},
    "brave": {"enabled": bool(BRAVE_SEARCH_API_KEY), "api_key": BRAVE_SEARCH_API_KEY, "base_url": BRAVE_API_URL},
    "tavily": {"enabled": bool(TAVILY_API_KEY), "api_key": TAVILY_API_KEY, "base_url": TAVILY_API_URL},
    "exa": {"enabled": bool(EXA_API_KEY), "api_key": EXA_API_KEY, "base_url": EXA_API_URL},
    "serpapi": {"enabled": bool(SERPAPI_API_KEY), "api_key": SERPAPI_API_KEY, "base_url": SERPAPI_API_URL},
}


def _provider_options(
    api_name: str,
    search_provider_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """合併環境變數預設值與 Web 設定頁傳入的搜尋供應商設定。"""
    provider_name = "serpapi" if api_name.startswith("serpapi_") else api_name
    defaults = dict(_BUILTIN_SEARCH_DEFAULTS.get(provider_name, {}))
    providers = (
        search_provider_config.get("providers", {})
        if isinstance(search_provider_config, dict)
        else {}
    )
    override = providers.get(provider_name, {}) if isinstance(providers, dict) else {}
    if isinstance(override, dict):
        defaults.update(override)
    defaults["id"] = provider_name
    return defaults


def _custom_search_sources(
    search_provider_config: dict[str, Any] | None,
    search_mode: str,
    override_per_query: int | None,
) -> list[dict[str, Any]]:
    if not isinstance(search_provider_config, dict):
        return []
    custom = search_provider_config.get("custom", [])
    if not isinstance(custom, list):
        return []

    sources: list[dict[str, Any]] = []
    for index, item in enumerate(custom):
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        modes = item.get("modes", ["web", "academic", "social"])
        if isinstance(modes, list) and search_mode not in modes:
            continue
        provider_id = str(item.get("id") or f"custom-{index + 1}").strip()
        if not provider_id or not str(item.get("base_url") or "").strip():
            continue
        requested = override_per_query or int(item.get("per_query") or DEFAULT_RESULTS_PER_QUERY)
        sources.append(
            {
                "api": f"custom:{provider_id}",
                "per_query": min(max(1, requested), 100),
                "params": {},
                "provider_options": dict(item),
            }
        )
    return sources


def _merge_search_results(raw_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """依 canonical URL 去重，但合併不同搜尋軌與底層引擎的來源資訊。"""
    merged: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url_key = _normalize_url(str(item.get("url") or ""))
        if not url_key:
            continue
        if url_key not in positions:
            copied = dict(item)
            copied["source_engines"] = list(
                dict.fromkeys(
                    [
                        *[str(v) for v in item.get("source_engines", []) if v],
                        *([str(item.get("engine"))] if item.get("engine") else []),
                    ]
                )
            )
            copied["search_lanes"] = list(
                dict.fromkeys(
                    [str(item.get("search_lane"))]
                    if item.get("search_lane")
                    else []
                )
            )
            positions[url_key] = len(merged)
            merged.append(copied)
            continue

        target = merged[positions[url_key]]
        target["source_engines"] = list(
            dict.fromkeys(
                [
                    *[str(v) for v in target.get("source_engines", []) if v],
                    *[str(v) for v in item.get("source_engines", []) if v],
                    *([str(item.get("engine"))] if item.get("engine") else []),
                ]
            )
        )
        target["search_lanes"] = list(
            dict.fromkeys(
                [
                    *[str(v) for v in target.get("search_lanes", []) if v],
                    *([str(item.get("search_lane"))] if item.get("search_lane") else []),
                ]
            )
        )
        if not target.get("content") and item.get("content"):
            target["content"] = item["content"]
        if not target.get("published_date") and item.get("published_date"):
            target["published_date"] = item["published_date"]
    return merged


# ── 多源聯合搜尋主函數 ───────────────────────────────────────
async def multi_source_search(
    queries: list[str],
    search_mode: str = "web",
    results_per_query: int | None = None,
    question: str = "",
    language: str = "zh-TW",
    tavily_search_depth: str | None = None,
    search_provider_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    多源聯合搜尋 — 根據 search_mode 自動分配 Brave/Tavily/Exa/SerpApi API 配額。
    返回格式與原 searxng_search() 完全相容。
    """
    config = SEARCH_MODE_CONFIG.get(search_mode, SEARCH_MODE_CONFIG["web"])
    sources = [
        {**source, "params": dict(source.get("params", {}))}
        for source in config.get("sources", [])
    ]

    # 檢查可用 API（有 key 的才能用）
    override_per_query = (
        int(results_per_query)
        if results_per_query and results_per_query > 0
        else None
    )
    sources.extend(
        _custom_search_sources(
            search_provider_config,
            search_mode,
            override_per_query,
        )
    )
    available_sources: list[dict[str, Any]] = []
    unavailable_quotas: dict[str, int] = {}
    for src in sources:
        api_name = src["api"]
        per_query_cap = _get_api_per_query_cap(api_name)
        effective_per_query = int(src.get("per_query", 0))
        if override_per_query is not None and not src.get("page_native"):
            effective_per_query = override_per_query
        effective_per_query = min(max(1, effective_per_query), per_query_cap)
        provider_options = (
            dict(src.get("provider_options", {}))
            if api_name.startswith("custom:")
            else _provider_options(api_name, search_provider_config)
        )
        enabled = bool(provider_options.get("enabled", True))
        base_url = str(provider_options.get("base_url") or "").strip()
        api_key = str(provider_options.get("api_key") or "").strip()
        needs_key = api_name != "searxng" and not api_name.startswith("custom:")
        if api_name.startswith("custom:"):
            needs_key = bool(provider_options.get("requires_api_key", False))

        if enabled and base_url and (api_key or not needs_key):
            source = dict(src)
            source["params"] = dict(src.get("params", {}))
            source["per_query"] = effective_per_query
            source["provider_options"] = provider_options
            if api_name == "tavily" and tavily_search_depth:
                source["params"]["search_depth"] = tavily_search_depth
            available_sources.append(source)
        elif not src.get("page_native"):
            unavailable_quotas[api_name] = effective_per_query

    # 自動降級：如果有 API 不可用，將其配額均分給其他可用 API
    quota_sources = [source for source in available_sources if not source.get("page_native")]
    if unavailable_quotas and quota_sources:
        _redistribute_source_quota(quota_sources, sum(unavailable_quotas.values()))

    if not available_sources:
        empty_profile = _build_query_profile(question, queries)
        lang_hint, country_hint = _normalize_language_hint(language)
        if not empty_profile.get("search_lang") and lang_hint:
            empty_profile["search_lang"] = lang_hint
            if empty_profile.get("country") == DEFAULT_SEARCH_COUNTRY and country_hint:
                empty_profile["country"] = country_hint
        return {
            "search_mode": search_mode,
            "query_groups": [{"query": q, "results": [], "error": "沒有可用的搜尋服務（請檢查 SearXNG 或 API Key）"} for q in queries],
            "total_results": 0,
            "raw_total_results": 0,
            "query_profile": empty_profile,
        }

    engine_quota: int | None = None
    domain_quota: int | None = None
    if search_mode == "social":
        raw_eq = config.get("engine_quota_per_query")
        raw_dq = config.get("domain_quota_per_query")
        if isinstance(raw_eq, int) and raw_eq > 0:
            engine_quota = raw_eq
        if isinstance(raw_dq, int) and raw_dq > 0:
            domain_quota = raw_dq

    query_profile = _build_query_profile(question, queries)
    lang_hint, country_hint = _normalize_language_hint(language)
    if not query_profile.get("search_lang") and lang_hint:
        query_profile["search_lang"] = lang_hint
        if query_profile.get("country") == DEFAULT_SEARCH_COUNTRY and country_hint:
            query_profile["country"] = country_hint
    query_groups: list[dict[str, Any]] = []

    async def _search_one(query: str) -> dict[str, Any]:
        """對單一查詢字詞，向所有可用 API 並行查詢。"""
        api_tasks = []
        plan_summary = {
            "country": None,
            "search_lang": None,
            "engine_queries": {},
            "english_mirror": None,
        }
        for src in available_sources:
            planned = _plan_engine_query(
                query,
                api_name=src["api"],
                search_mode=search_mode,
                profile=query_profile,
                base_params=src.get("params", {}),
            )
            if planned.get("country") and not plan_summary["country"]:
                plan_summary["country"] = planned["country"]
            if planned.get("search_lang") and not plan_summary["search_lang"]:
                plan_summary["search_lang"] = planned["search_lang"]
            plan_key = src["api"]
            lane = str(src.get("params", {}).get("search_lane") or "")
            if lane:
                plan_key = f"{plan_key}:{lane}"
            plan_summary["engine_queries"][plan_key] = planned["query"]
            if planned.get("english_mirror"):
                plan_summary["english_mirror"] = planned["english_mirror"]
            api_tasks.append(
                _call_api_source(
                    api_name=src["api"],
                    query=planned["query"],
                    per_query=src["per_query"],
                    params=planned["params"],
                    seen_urls=None,
                    provider_options=src.get("provider_options", {}),
                )
            )

        api_results = await asyncio.gather(*api_tasks, return_exceptions=True)

        # 合併所有 API 的結果
        raw_combined: list[dict[str, Any]] = []
        for ar in api_results:
            if isinstance(ar, list):
                raw_combined.extend(ar)
            elif isinstance(ar, Exception):
                pass  # 單一 API 失敗不影響整體
        combined = _merge_search_results(raw_combined)

        # Social 模式套用來源多樣性配額
        if search_mode == "social" and (engine_quota or domain_quota):
            combined = _apply_source_diversity_quota(
                combined,
                limit=sum(s["per_query"] for s in available_sources),
                engine_quota=engine_quota,
                domain_quota=domain_quota,
            )

        # SearXNG 已在每頁內完成相關性排序。不同搜尋軌合併、去重後仍以
        # query 為單位保留前 30 筆，避免三組查詢送入超過 90 條 snippets。
        combined = combined[:SEARCH_RESULTS_PER_QUERY_LIMIT]

        if not plan_summary["country"]:
            plan_summary["country"] = query_profile.get("country")
        return {
            "query": query,
            "results": combined,
            "raw_result_count": len(raw_combined),
            "deduplicated_result_count": len(combined),
            "plan": plan_summary,
        }

    tasks = [_search_one(q) for q in queries]
    groups = await asyncio.gather(*tasks, return_exceptions=True)

    total = 0
    raw_total = 0
    for g in groups:
        if isinstance(g, Exception):
            query_groups.append({"query": "(error)", "results": [], "error": str(g)})
        elif isinstance(g, dict):
            query_groups.append(g)
            total += len(g.get("results", []))
            raw_total += int(g.get("raw_result_count", len(g.get("results", []))))
        else:
            query_groups.append({"query": "(error)", "results": [], "error": f"unexpected: {type(g).__name__}"})

    return {
        "search_mode": search_mode,
        "query_groups": query_groups,
        "total_results": total,
        "raw_total_results": raw_total,
        "query_profile": query_profile,
    }


# ============================================================
# 模組二：LLM 審核篩選
# ============================================================

OPEN_EVIDENCE_DIVERSITY_INSTRUCTION = """## 開放性與證據多元性原則
你應該保持開放性的態度看待問題，不偏好任何觀點，且應盡力維持證據的多元性，若在查詢過程中發現相互矛盾的不同觀點，應將各種觀點都予以保留，不得擅自進行判斷與挑選。"""

OPEN_VIEWPOINT_DIVERSITY_INSTRUCTION = """## 開放性與論述多元性原則
你應該保持開放性的態度看待問題，不偏好任何觀點，且應盡力維持論述的多元性，若你發現搜尋資料存在相互矛盾的不同觀點，應將各種觀點予以保留，不得擅自進行判斷與挑選。"""

CHUNK_MINIMAL_EVIDENCE_SET_INSTRUCTION = "請選出最小充分 evidence set。若多個 chunks 支持同一事實且內容高度重複，優先保留最直接、最新、來源權威或資訊密度最高者；但不同來源的交叉驗證、補充必要細節、保留相互矛盾資訊，不得因表面相似而刪除。"


def _build_filter_system_prompt(
    search_mode: str,
    min_per_group: int,
    max_per_group: int,
) -> str:
    """根據搜尋模式生成審核 LLM 的 system prompt。"""

    mode_criteria = {
        "web": """## 審核標準（網路搜尋）
1. **相關性**（最重要）：結果是否直接回應用戶的問題？斷開的關聯不算。
2. **來源權威性**：官方文件 > 知名媒體/技術部落格 > 個人部落格 > 不明來源。
3. **摘要資訊密度**：摘要中是否包含具體事實、數據、步驟或深度分析？空泛描述價值低。
4. **時效性**：若問題涉及最新狀態，優先選擇近期內容。
5. **排除以下類型**：
   - 登入牆 / 付費牆（URL 中含 login、signin、paywall）
   - 純影片 / 純圖片頁面（YouTube 觀看頁除外，那些可能有字幕）
   - 論壇灌水、一行回覆的帖子
   - SEO 垃圾頁（摘要充滿關鍵字堆砌、無實質內容）
   - 內容農場（標題黨、摘要與標題不符）""",
        "academic": """## 審核標準（學術搜尋）
1. **學術相關性**（最重要）：論文/報告是否直接研究用戶問的主題？
2. **論文品質指標**：
   - 發表在知名期刊或頂級會議的優先
   - arXiv 預印本若摘要具體且方法論清晰也可選
   - 綜述型論文（review/survey）對概覽性問題特別有價值
3. **摘要資訊密度**：摘要是否包含具體方法、結果或結論？
4. **時效性**：若問題涉及「最新進展」，優先選近 2 年的論文。
5. **排除以下類型**：
   - 摘要完全缺失或只有標題
   - 與主題僅有邊緣關係的論文
   - 付費全文且無開放獲取版本（但 arXiv 版本可選）""",
        "social": """## 審核標準（社交搜尋）
1. **討論深度**（最重要）：帖子是否包含有實質的經驗分享、技術討論或深度分析？
2. **社群共識**：高互動量的帖子通常代表社群關注度和資訊價值。
3. **實用性**：是否包含實際操作經驗、踩坑記錄、比較評測？
4. **時效性**：社交媒體內容時效性強，優先選近期討論。
5. **排除以下類型**：
   - 只有幾個字的短回覆帖
   - 純抱怨/情緒發洩而無實質內容的帖子
   - 廣告帖或自我推廣帖
   - 已刪除或已鎖定的帖子""",
    }

    criteria = mode_criteria.get(search_mode, mode_criteria["web"])

    crawlability_instruction = """
## 來源可爬取性降權（非硬性禁止）
深爬階段會優先抓取公開、穩定、可直接讀取正文的頁面。請在資訊價值相近時，降低以下來源的優先級：
- 常見登入牆、付費牆、Cloudflare / captcha / human verification 反爬牆頁面
- 社群 app shell 或登入後才完整顯示的頁面，例如 Facebook、X / Twitter 個別貼文
- 常見對自動抓取不穩定的內容平台，例如 Medium、GitConnected、Plain English、Newline、TechRxiv、ACM DL
- URL 或摘要已顯示 login、signin、subscribe、paywall、captcha、verify、challenge、access denied 的結果

這不是硬性封鎖：若該來源是問題必需的一手資料、沒有合理替代來源，或摘要已提供足夠明確且不可替代的資訊，仍可選入。
但若存在同等或更好的替代來源，優先選官方文件、開放論文、GitHub、arXiv、Read the Docs、公司工程部落格、政府/標準組織頁面、可公開訪問的新聞或技術文章。"""

    return f"""你是一位高精度的搜尋結果審核專家。你將收到用戶的原始問題以及從搜尋引擎返回的多組結果（每組包含標題、URL、摘要）。

## 你的核心任務
從每組搜尋結果中，精準篩選出最值得進行全文深度爬取的網頁。你的篩選品質直接決定最終回答的資料基礎。

{OPEN_EVIDENCE_DIVERSITY_INSTRUCTION}

{OPEN_VIEWPOINT_DIVERSITY_INSTRUCTION}

{criteria}

{crawlability_instruction}

## 篩選數量
- 從每組中選出 **{min_per_group} 到 {max_per_group} 條**
- 如果某組高品質結果不足 {min_per_group} 條，可以少選但必須至少選 1 條
- 不要為了湊數而選入低品質結果


## 輸出格式
你必須嚴格輸出以下 JSON 格式，不要輸出任何其他文字：

```json
{{
  "selected": [
    {{
      "group_index": 0,
      "result_indices": [0, 3, 5],
      "reasoning": "選擇理由的一句話簡述"
    }},
    {{
      "group_index": 1,
      "result_indices": [1, 2, 7],
      "reasoning": "選擇理由的一句話簡述"
    }},
    {{
      "group_index": 2,
      "result_indices": [0, 4, 6, 8],
      "reasoning": "選擇理由的一句話簡述"
    }}
  ]
}}
```

其中 `result_indices` 是該組 results 數組中的索引（從 0 開始）。
嚴禁輸出 JSON 以外的任何內容（不要解釋、不要前言後語）。"""


# 審核 prompt 中每條結果的摘要截斷字數（防止多源 API 的長摘要炸上下文）
_FILTER_SNIPPET_MAX_CHARS = 300


def _build_filter_user_prompt(question: str, query_groups: list[dict]) -> str:
    """建構送給審核 LLM 的 user prompt。

    多源 API 返回的摘要可能很長（Brave extra_snippets、Tavily snippets、Exa highlights、SerpApi snippets），
    在此截斷到 300 字以控制 prompt 大小；relevance_score 和 published_date 以極低 token 成本提供高價值訊號。
    完整全文留給深爬階段使用。
    """
    parts = [f"## 用戶原始問題\n{question}\n"]

    for gi, group in enumerate(query_groups):
        parts.append(f"## 第 {gi + 1} 組搜尋結果（搜尋字詞：{group['query']}）")
        for ri, r in enumerate(group.get("results", [])):
            # 摘要截斷到 _FILTER_SNIPPET_MAX_CHARS 字
            raw_content = r.get("content", "(無摘要)")
            snippet = raw_content[:_FILTER_SNIPPET_MAX_CHARS]
            if len(raw_content) > _FILTER_SNIPPET_MAX_CHARS:
                snippet += "…"

            # 額外訊號行（低 token 成本，高判斷價值）
            meta_parts: list[str] = []
            engine = r.get("engine", "")
            if engine:
                meta_parts.append(f"來源={engine}")
            score = r.get("relevance_score")
            if score is not None:
                meta_parts.append(f"相關性={score:.2f}" if isinstance(score, float) else f"相關性={score}")
            pub_date = r.get("published_date")
            if pub_date:
                meta_parts.append(f"日期={pub_date}")
            meta_line = f"    元資料: {', '.join(meta_parts)}" if meta_parts else ""

            entry = (
                f"[{ri}] 標題: {r['title']}\n"
                f"    URL: {r['url']}\n"
                f"    摘要: {snippet}"
            )
            if meta_line:
                entry += f"\n{meta_line}"
            parts.append(entry)
        parts.append("")

    return "\n".join(parts)


def _limit_filter_query_groups(
    query_groups: list[dict[str, Any]],
    *,
    max_results_per_group: int,
) -> list[dict[str, Any]]:
    """保留完整搜尋結果，只限制送進 URL Judge 的候選數量。"""
    if max_results_per_group <= 0:
        return list(query_groups)
    limited: list[dict[str, Any]] = []
    for group in query_groups:
        copied = dict(group)
        copied["results"] = list(group.get("results", []))[:max_results_per_group]
        limited.append(copied)
    return limited


def _parse_filter_response(
    raw: str, query_groups: list[dict], max_per: int
) -> list[dict]:
    """解析 LLM 回傳的 JSON，並做容錯處理。"""
    # 嘗試提取 JSON 區塊
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if not json_match:
        raise ValueError("LLM 回傳中找不到 JSON 區塊")

    data = json.loads(json_match.group())
    selected = data.get("selected", [])

    # 驗證並修正
    validated: list[dict] = []
    for item in selected:
        gi = item.get("group_index", 0)
        if gi < 0 or gi >= len(query_groups):
            continue
        max_idx = len(query_groups[gi].get("results", []))
        indices = [i for i in item.get("result_indices", []) if 0 <= i < max_idx]
        if not indices:
            continue
        # 限制數量
        indices = indices[:max_per]
        validated.append(
            {
                "group_index": gi,
                "result_indices": indices,
                "reasoning": item.get("reasoning", ""),
            }
        )

    return validated


def _補足每組URL選取(
    選取結果: list[dict[str, Any]],
    查詢群組: list[dict[str, Any]],
    *,
    min_per_group: int,
    max_per_group: int,
) -> list[dict[str, Any]]:
    """即使 URL Judge 成功解析，也確保每組搜尋字詞都有可深爬候選。"""
    每組選取: dict[int, dict[str, Any]] = {}
    排序: list[int] = []
    上限 = max(0, max_per_group)
    最少數 = max(0, min(min_per_group, 上限))

    for 項目 in 選取結果:
        組別 = 項目.get("group_index")
        if not isinstance(組別, int) or not 0 <= 組別 < len(查詢群組):
            continue
        結果數 = len(查詢群組[組別].get("results", []))
        索引 = [
            值 for 值 in 項目.get("result_indices", [])
            if isinstance(值, int) and 0 <= 值 < 結果數
        ]
        if not 索引:
            continue
        if 組別 not in 每組選取:
            每組選取[組別] = {
                "group_index": 組別,
                "result_indices": [],
                "reasoning": str(項目.get("reasoning") or ""),
            }
            排序.append(組別)
        既有索引 = 每組選取[組別]["result_indices"]
        for 值 in 索引:
            if 值 not in 既有索引 and len(既有索引) < 上限:
                既有索引.append(值)

    for 組別, 群組 in enumerate(查詢群組):
        結果數 = len(群組.get("results", []))
        需要數 = min(最少數, 結果數)
        if 需要數 <= 0:
            continue
        if 組別 not in 每組選取:
            每組選取[組別] = {
                "group_index": 組別,
                "result_indices": [],
                "reasoning": "coverage fallback",
            }
            排序.append(組別)
        既有索引 = 每組選取[組別]["result_indices"]
        for 索引 in range(結果數):
            if len(既有索引) >= 需要數:
                break
            if 索引 not in 既有索引:
                既有索引.append(索引)

    return [每組選取[組別] for 組別 in 排序]


async def llm_filter_results(
    original_question: str,
    query_groups: list[dict],
    search_mode: str = "web",
    llm_config: dict[str, Any] | None = None,
    min_per_group: int = 3,
    max_per_group: int = 8,
    max_prompt_results_per_group: int = DEFAULT_FILTER_RESULTS_PER_GROUP,
    monitor: PipelineMonitor | None = None,
) -> dict[str, Any]:
    """
    呼叫 LLM 審核搜尋結果，篩選出值得深爬的 URL。

    Returns:
        包含 selected_urls、selection_details、total_selected 的字典
    """
    mon = monitor or _current_monitor()
    active_llm_config = llm_config or _build_llm_route_config(DEFAULT_MODEL_ROUTE, None)
    display_model = str(
        active_llm_config.get("display_model")
        or active_llm_config.get("model")
        or DEFAULT_FILTER_MODEL
    )

    prompt_query_groups = _limit_filter_query_groups(
        query_groups,
        max_results_per_group=max_prompt_results_per_group,
    )
    system_prompt = _build_filter_system_prompt(
        search_mode,
        min_per_group,
        max_per_group,
    )
    user_prompt = _build_filter_user_prompt(original_question, prompt_query_groups)

    # ── 顯示 LLM 輸入 ──
    mon.llm_input(system_prompt, user_prompt, display_model)

    try:
        raw_content = await _call_llm_raw_content(
            llm_config=active_llm_config,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    except Exception as exc:
        # 與 JSON 解析失敗共用原有 fallback，保留已取得的搜尋結果。
        mon._print(
            f"{mon.RED}  ⚠ LLM 審核請求失敗: {type(exc).__name__}，使用 fallback 策略{mon.RESET}"
        )
        raw_content = ""

    # ── 顯示 LLM 輸出 ──
    mon.llm_output(raw_content)

    try:
        validated = _parse_filter_response(raw_content, prompt_query_groups, max_per_group)
    except Exception as e:
        # 容錯：如果解析完全失敗，每組取前 min_per_group 條
        mon._print(
            f"{mon.RED}  ⚠ LLM 審核回傳解析失敗: {e}，使用 fallback 策略{mon.RESET}"
        )
        validated = []
        for gi, group in enumerate(prompt_query_groups):
            n = min(min_per_group, len(group.get("results", [])))
            if n > 0:
                validated.append(
                    {
                        "group_index": gi,
                        "result_indices": list(range(n)),
                        "reasoning": "fallback: LLM 回傳解析失敗",
                    }
                )

    # Judge 即使回傳合法 JSON，也可能漏掉某組；不可讓其中一組失去深爬與證據機會。
    validated = _補足每組URL選取(
        validated,
        prompt_query_groups,
        min_per_group=min_per_group,
        max_per_group=max_per_group,
    )

    # 提取 URL 並附帶來源資訊（含 URL 去重）
    selected_urls: list[dict[str, Any]] = []
    seen_selected_urls: set[str] = set()
    for item in validated:
        gi = item["group_index"]
        group = query_groups[gi]
        for ri in item["result_indices"]:
            result = group["results"][ri]
            url = result["url"]
            url_key = _normalize_url(url)
            if not url_key or url_key in seen_selected_urls:
                continue  # 跨組去重：同 URL 不重複選取
            seen_selected_urls.add(url_key)
            selected_urls.append(
                {
                    "url": url,
                    "title": result["title"],
                    "from_query": group["query"],
                    "group_index": gi,
                    "result_index": ri,
                    "engine": result.get("engine", ""),
                }
            )

    # ── 顯示篩選結果 ──
    mon.filter_selection(validated, len(selected_urls))

    return {
        "selected_urls": selected_urls,
        "selection_details": validated,
        "total_selected": len(selected_urls),
        "raw_llm_response": raw_content,
    }


# ============================================================
# 模組三：URL 處理與深爬前置
# ============================================================


def _normalize_url(url: str) -> str:
    """正規化 URL。"""
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.strip().lower()
        if normalized_key.startswith("utm_") or normalized_key in TRACKING_QUERY_PARAMS:
            continue
        query_pairs.append((key, value))

    normalized_path = parsed.path or "/"
    if normalized_path != "/" and normalized_path.endswith("/"):
        normalized_path = normalized_path.rstrip("/")

    return urlunparse(
        (
            (parsed.scheme or "https").lower(),
            parsed.netloc.lower(),
            normalized_path,
            "",
            urlencode(sorted(query_pairs)),
            "",
        )
    )


def _request_url_for_crawl(url: Any) -> str:
    """保留搜尋引擎回傳的 HTTP(S) URL 作實際請求。

    canonical URL 只用於去重與引用，避免重排 query 或移除追蹤參數時
    連帶破壞簽名、參數順序敏感的頁面。
    """
    raw_url = str(url or "").strip()
    parsed = urlparse(raw_url)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return raw_url
    return _normalize_url(raw_url)


def _extract_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().strip()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _should_speculative_js_race(url: str) -> bool:
    """用泛用互動頁訊號判斷是否提前啟動短等待 JS 競速。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    path = parsed.path or ""
    query = parsed.query or ""
    if path.lower().endswith(".pdf"):
        return False
    if SPECULATIVE_JS_PATH_RE.search(path):
        return True
    return bool(SPECULATIVE_JS_QUERY_RE.search(query))


def _allocate_loop_crawl_budget(
    candidates: list[dict[str, Any]],
    *,
    min_total: int,
    target_total: int,
    max_total: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not candidates or max_total <= 0:
        return [], {
            "candidate_count": len(candidates),
            "selected_count": 0,
            "target_total": max(0, target_total),
            "max_total": max(0, max_total),
            "budget_trimmed": len(candidates),
        }

    available = len(candidates)
    desired_total = min(available, max_total)
    if available >= min_total:
        desired_total = min(desired_total, max(min_total, target_total))

    query_groups: list[int] = []
    for item in candidates:
        gi = item.get("group_index")
        if isinstance(gi, int) and gi not in query_groups:
            query_groups.append(gi)
    desired_group_coverage = min(
        len(query_groups),
        desired_total,
        max(1, LOOP_MIN_QUERY_COVERAGE),
    )

    selected: list[dict[str, Any]] = []
    selected_urls: set[str] = set()
    selected_groups: set[int] = set()
    domain_counts: dict[str, int] = {}

    def _take(item: dict[str, Any]) -> bool:
        normalized = _normalize_url(item.get("url", ""))
        if not normalized or normalized in selected_urls:
            return False
        selected_urls.add(normalized)
        gi = item.get("group_index")
        if isinstance(gi, int):
            selected_groups.add(gi)
        domain = _extract_domain(item.get("url", ""))
        if domain:
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
        selected.append(item)
        return True

    if desired_group_coverage > 0:
        for item in candidates:
            if len(selected) >= desired_group_coverage:
                break
            gi = item.get("group_index")
            if not isinstance(gi, int) or gi in selected_groups:
                continue
            _take(item)

    for item in candidates:
        if len(selected) >= desired_total:
            break
        normalized = _normalize_url(item.get("url", ""))
        if not normalized or normalized in selected_urls:
            continue
        domain = _extract_domain(item.get("url", ""))
        if domain and domain_counts.get(domain, 0) >= LOOP_SOFT_DOMAIN_CAP:
            continue
        _take(item)

    for item in candidates:
        if len(selected) >= desired_total:
            break
        _take(item)

    return selected, {
        "candidate_count": available,
        "selected_count": len(selected),
        "target_total": max(0, target_total),
        "max_total": max(0, max_total),
        "budget_trimmed": max(0, available - len(selected)),
    }


def _apply_source_diversity_quota(
    results: list[dict[str, Any]],
    *,
    limit: int,
    engine_quota: int | None,
    domain_quota: int | None,
) -> list[dict[str, Any]]:
    if limit <= 0 or not results:
        return []

    if not engine_quota and not domain_quota:
        return results[:limit]

    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    engine_count: dict[str, int] = {}
    domain_count: dict[str, int] = {}

    def _can_take(item: dict[str, Any], *, strict_engine: bool = True) -> bool:
        engine = (item.get("engine") or "unknown").strip().lower()
        domain = _extract_domain(item.get("url", ""))
        if (
            strict_engine
            and engine_quota
            and engine_count.get(engine, 0) >= engine_quota
        ):
            return False
        if domain_quota and domain and domain_count.get(domain, 0) >= domain_quota:
            return False
        return True

    def _accept(item: dict[str, Any]):
        engine = (item.get("engine") or "unknown").strip().lower()
        domain = _extract_domain(item.get("url", ""))
        engine_count[engine] = engine_count.get(engine, 0) + 1
        if domain:
            domain_count[domain] = domain_count.get(domain, 0) + 1
        selected.append(item)

    for item in results:
        if _can_take(item, strict_engine=True):
            _accept(item)
            if len(selected) >= limit:
                return selected
        else:
            deferred.append(item)

    for item in deferred:
        if _can_take(item, strict_engine=False):
            _accept(item)
            if len(selected) >= limit:
                break

    return selected[:limit]


def _build_social_site_fallback_queries(
    search_queries: list[str],
    *,
    max_queries: int = SOCIAL_FALLBACK_MAX_QUERIES,
) -> list[str]:
    if max_queries <= 0:
        return []

    def _build_clause(sites: list[str]) -> str:
        return " OR ".join(f"site:{s}" for s in sites)

    site_clauses = [
        f"({_build_clause(group)})" for group in SOCIAL_FALLBACK_SITE_GROUPS if group
    ]
    if not site_clauses:
        return []

    built: list[str] = []
    seen: set[str] = set()
    for raw_q in search_queries:
        q = (raw_q or "").strip()
        if not q:
            continue
        for clause in site_clauses:
            candidate = f"{q} {clause}"
            if candidate in seen:
                continue
            seen.add(candidate)
            built.append(candidate)
            if len(built) >= max_queries:
                return built
    return built


def _should_trigger_social_site_fallback(loop_result: dict[str, Any]) -> bool:
    social_cfg = SEARCH_MODE_CONFIG.get("social", {})
    min_pages = int(social_cfg.get("fallback_min_pages", 6))
    pages = len(loop_result.get("pages", []))
    total_selected = int(loop_result.get("total_selected", 0))

    if pages < min_pages:
        return True
    if total_selected >= min_pages and pages < max(2, total_selected // 2):
        return True
    return False


# ============================================================
# 模組三（續）：社群快取與深爬橋接層
# ============================================================


def _smart_truncate(text: str, limit: int) -> str:
    """智能截斷，優先在自然邊界處截斷。"""
    if len(text) <= limit:
        return text
    for delimiter in ["\n\n", "。", ". ", "\n", "，", ", "]:
        idx = text.rfind(delimiter, limit - 2000, limit)
        if idx > 0:
            return (
                text[: idx + len(delimiter)]
                + f"\n\n[... 已截斷，原始長度 {len(text)} 字元]"
            )
    return text[:limit] + f"\n\n[... 已截斷，原始長度 {len(text)} 字元]"



# ── Reddit .json 快速取得 ──────────────────────────────────

_REDDIT_URL_PATTERN = re.compile(
    r"^https?://(?:www\.|old\.|new\.)?reddit\.com/r/([^/]+)/comments/([^/]+)",
    re.IGNORECASE,
)

REDDIT_USER_AGENT = "deep_search_pipeline:v1.0 (research tool)"
REDDIT_MAX_COMMENTS = 50  # 最多取前 N 條頂層留言


def _is_reddit_url(url: str) -> bool:
    """判斷 URL 是否為 Reddit 帖子。"""
    return bool(_REDDIT_URL_PATTERN.match(url))


def _reddit_json_url(url: str) -> str:
    """將 Reddit 帖子 URL 轉換為 .json 端點。"""
    # 移除查詢參數和片段
    clean = url.split("?")[0].split("#")[0]
    # 確保結尾沒有 .json（避免重複）
    if clean.endswith(".json"):
        return clean
    # 移除尾部斜線
    clean = clean.rstrip("/")
    return clean + ".json"


def _reddit_json_to_text(
    data: list | dict, max_comments: int = REDDIT_MAX_COMMENTS
) -> tuple[str, str]:
    """
    將 Reddit .json 回傳的結構化資料轉為可讀文字。

    Returns:
        (formatted_text, title)
    """
    parts: list[str] = []
    title = ""

    # Reddit .json 回傳的是一個包含兩個 Listing 的陣列：
    # [0] = 帖子本身, [1] = 留言
    if isinstance(data, list) and len(data) >= 1:
        # ── 帖子資料 ──
        post_listing = data[0]
        post_children = post_listing.get("data", {}).get("children", [])
        if post_children:
            post = post_children[0].get("data", {})
            title = post.get("title", "")
            author = post.get("author", "[deleted]")
            score = post.get("score", 0)
            upvote_ratio = post.get("upvote_ratio", 0)
            num_comments = post.get("num_comments", 0)
            subreddit = post.get("subreddit", "")
            selftext = post.get("selftext", "")

            parts.append(f"# {title}")
            parts.append(
                f"**r/{subreddit}** | u/{author} | Score: {score} ({upvote_ratio:.0%} upvoted) | {num_comments} comments"
            )
            parts.append("")
            if selftext:
                parts.append(selftext)
                parts.append("")

        # ── 留言資料 ──
        if len(data) >= 2:
            comment_listing = data[1]
            comments = comment_listing.get("data", {}).get("children", [])

            if comments:
                parts.append("---")
                parts.append("## Comments")
                parts.append("")

                count = 0
                for child in comments:
                    if child.get("kind") != "t1":  # 只取留言，跳過 "more"
                        continue
                    if count >= max_comments:
                        break
                    cdata = child.get("data", {})
                    c_author = cdata.get("author", "[deleted]")
                    c_body = cdata.get("body", "")
                    c_score = cdata.get("score", 0)

                    if not c_body or c_body == "[deleted]" or c_body == "[removed]":
                        continue

                    parts.append(f"**u/{c_author}** (Score: {c_score}):")
                    parts.append(c_body)
                    parts.append("")
                    count += 1

    formatted = "\n".join(parts)
    return (
        sanitize_text(formatted, preserve_newlines=True, aggressive=True),
        sanitize_text(title, preserve_newlines=False),
    )


async def _fetch_reddit_json(
    url: str,
    max_chars: int = MAX_CHARS_PER_PAGE,
) -> dict[str, Any]:
    """
    通過 Reddit .json 端點直接取得帖子內容（繞過 HTML 爬取）。
    速度約 0.3~0.5 秒，比 JS fallback 快 10 倍。
    """
    started = time.time()
    json_url = _reddit_json_url(url)
    normalized = _normalize_url(url)

    try:
        client = await _get_search_api_client()
        resp = await client.get(
            json_url,
            timeout=httpx.Timeout(15.0),
            headers={
                "User-Agent": REDDIT_USER_AGENT,
                "Accept": "application/json",
            },
        )

        if resp.status_code == 429:
            # Rate limited — fallback to None so caller uses crawl4ai
            return {"success": False, "error": "REDDIT_RATE_LIMITED", "fallback": True}

        if resp.status_code >= 400:
            return {
                "success": False,
                "error": f"REDDIT_HTTP_{resp.status_code}",
                "fallback": True,
            }

        data = resp.json()
        content, title = _reddit_json_to_text(data)

        if not content or len(content) < 50:
            return {"success": False, "error": "REDDIT_EMPTY_CONTENT", "fallback": True}

        elapsed = int((time.time() - started) * 1000)

        return {
            "url": normalized,
            "success": True,
            "title": title,
            "content": _smart_truncate(content, max_chars),
            "content_length": len(content),
            "used_render": "reddit_json",
            "fallback_reason": None,
            "elapsed_ms": elapsed,
        }

    except Exception as exc:
        return {
            "success": False,
            "error": f"REDDIT_ERROR: {type(exc).__name__}: {exc}",
            "fallback": True,
        }


_SOCIAL_FAST_FETCH_HANDLERS = [
    (_is_reddit_url, _fetch_reddit_json),
]


async def _try_social_fast_fetch(url: str, *, max_chars: int) -> dict[str, Any] | None:
    for matcher, fetcher in _SOCIAL_FAST_FETCH_HANDLERS:
        try:
            if not matcher(url):
                continue
            result = await fetcher(url, max_chars=max_chars)
        except Exception:
            continue
        if result.get("success"):
            return result
    return None


# ── 新版爬蟲內核相容層 ───────────────────────────────────────


def _crawl_backend_ready() -> bool:
    return _crawl4ai_plus is not None


def _crawl_metrics_to_dict(metrics: Any) -> dict[str, Any]:
    if metrics is None:
        return {}
    to_dict = getattr(metrics, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return dict(metrics)


def _cancel_child_task_when_parent_finishes(child: asyncio.Task[Any]) -> None:
    """父 worker 異常結束時取消未完成的投機 JS 子任務。"""
    parent = asyncio.current_task()
    if parent is None:
        return

    def _consume_child_result(task: asyncio.Task[Any]) -> None:
        # done callback 讀取例外不會影響後續 await，但可避免瀏覽器關閉競態
        # 產生「Future exception was never retrieved」。
        if task.cancelled():
            return
        with suppress(asyncio.CancelledError, Exception):
            task.exception()

    def _cleanup(_: asyncio.Task[Any]) -> None:
        if not child.done():
            child.cancel()

    child.add_done_callback(_consume_child_result)
    parent.add_done_callback(_cleanup)


def _crawl_error_label(code: str | None, fallback: str = "EXTRACT_EMPTY") -> str:
    label = sanitize_text(str(code or fallback), preserve_newlines=False)
    label = re.sub(r"[^A-Za-z0-9_]+", "_", label).strip("_")
    return (label or fallback).upper()


_PIPELINE_UI_LINE_PATTERN = re.compile(
    r"(?:^跳到主要內容區塊$|^搜尋$|^進階搜尋$|^分享$|^分享至\b|^首頁$|^上方連結$|"
    r"^下方連結$|^網站導覽$|^回首頁$|^熱門關鍵字$|^最新消息$|^網頁功能$|^列印內容$|"
    r"^注音$|^回上一頁$|^English$|^常見問答$|^隱私權政策$|^網站安全政策$|"
    r"^著作權聲明$|^政府網站資料開放宣告$|^維基百科，自由的百科全書$|^相關圖片$|"
    r"^點閱數[:：]?$|^資料更新[:：]?$|^資料檢視[:：]?$|^資料維護[:：]?$|"
    r"^更多.+報導$|^##\s*相關內容$|^相關內容$|^延伸閱讀$|^參考資料$|^注釋$|"
    r"^註釋$|^外部連結$|^分類$|^隱藏分類[:：]?$|^取自「$)",
    re.IGNORECASE,
)
_PIPELINE_TRAILING_CUT_PATTERN = re.compile(
    r"(?:^##\s*相關內容$|^相關內容$|^更多.+報導$|^延伸閱讀$|^相關文章$|"
    r"^相關圖片$|^注釋$|^註釋$|^參考資料$|^外部連結$|^分類$|^隱藏分類[:：]?$|"
    r"^回上一頁$|^點閱數[:：]?$|^資料更新[:：]?$|^資料檢視[:：]?$|^資料維護[:：]?$|"
    r"^地址：|^總機：|^傳真：|^服務時間：|^本網站內容版權屬|^本網站建議最佳瀏覽解析度|"
    r"^瀏覽人次$|^下方連結$|^最新消息$)",
    re.IGNORECASE,
)
_PIPELINE_FOOTNOTE_LINE_PATTERN = re.compile(
    r"^(?:\[\s*編輯\s*\]|\[\s*\d+(?:\.\d+)?\s*\]|\^+|[<>|/:：\-_=]{2,}|:::+)$"
)
_PIPELINE_INLINE_FOOTNOTE_PATTERN = re.compile(
    r"\[\s*(?:編輯|\d+(?:\.\d+)?)\s*\]"
)
_PIPELINE_INLINE_TRAILING_MARKER_PATTERN = re.compile(
    r"\n(?:##\s*相關內容|相關內容|更多[^。\n]{0,40}報導)"
)


def _pipeline_title_anchor_candidates(title: str) -> list[str]:
    normalized = sanitize_text(title or "", preserve_newlines=False)
    if not normalized:
        return []

    candidates: list[str] = []
    parts = re.split(r"\s*[|｜/\-–—:：]+\s*", normalized)
    for candidate in [normalized, *parts, parts[-1] if parts else "", parts[0] if parts else ""]:
        cleaned = candidate.strip(" -|｜/:：")
        if len(cleaned) >= 4 and cleaned not in candidates:
            candidates.append(cleaned)
    return candidates


def _is_heading_like_line(line: str) -> bool:
    stripped = line.strip().lstrip("#").strip()
    if not stripped or len(stripped) > 36:
        return False
    if re.search(r"https?://", stripped):
        return False
    if re.search(r"[。！？.!?]", stripped):
        return False
    cjk_count = len(re.findall(r"[\u4E00-\u9FFF]", stripped))
    latin_words = len(re.findall(r"[A-Za-z]{2,}", stripped))
    return cjk_count >= 2 or latin_words >= 1


def _looks_like_prose_line(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 32 or _PIPELINE_UI_LINE_PATTERN.search(stripped):
        return False
    if re.fullmatch(r"[#\[\](){}<>|/:：\-_=*.\s]+", stripped):
        return False
    if re.search(r"https?://", stripped):
        return len(stripped) >= 80

    cjk_count = len(re.findall(r"[\u4E00-\u9FFF]", stripped))
    latin_words = len(re.findall(r"[A-Za-z]{3,}", stripped))
    has_sentence_punct = bool(re.search(r"[。！？.!?；;：:]", stripped))
    connective_hits = len(re.findall(r"[的了在於是為有與及因從並將則而]", stripped))
    if cjk_count >= 16 and (has_sentence_punct or connective_hits >= 4):
        return True
    if latin_words >= 10 and has_sentence_punct:
        return True
    return False


def _is_probable_ui_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _PIPELINE_FOOTNOTE_LINE_PATTERN.fullmatch(stripped):
        return True
    if _PIPELINE_UI_LINE_PATTERN.search(stripped):
        return True
    if "另開新視窗" in stripped:
        return True
    if stripped in {"小", "中", "大"}:
        return True
    if len(stripped) <= 18 and not re.search(r"[。！？.!?]", stripped):
        if re.search(r"https?://", stripped):
            return False
        if _crawl4ai_plus is not None and len(_crawl4ai_plus.NAV_PATTERN.findall(stripped)) >= 1:
            return True
        cjk_count = len(re.findall(r"[\u4E00-\u9FFF]", stripped))
        latin_words = len(re.findall(r"[A-Za-z]{2,}", stripped))
        if (cjk_count >= 2 or latin_words >= 1) and not re.search(r"\d{2,}", stripped):
            return True
    return False


def _trim_pipeline_leading_shell(text: str) -> str:
    lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 12:
        return (text or "").strip()

    head: list[str] = []
    body = lines
    if body and body[0].lstrip().startswith("#"):
        head = [body[0].strip()]
        body = body[1:]

    probe = body[:80]
    noise_hits = sum(1 for line in probe if _is_probable_ui_line(line))
    if noise_hits < 6:
        return "\n".join(head + body).strip()

    anchor_idx: int | None = None
    for idx, line in enumerate(body[:180]):
        if not _looks_like_prose_line(line):
            continue
        anchor_idx = idx
        while anchor_idx > 0 and idx - anchor_idx < 3:
            prev = body[anchor_idx - 1]
            if _is_heading_like_line(prev):
                anchor_idx -= 1
                continue
            break
        break

    if anchor_idx is None or anchor_idx < 8:
        return "\n".join(head + body).strip()
    return "\n".join(head + body[anchor_idx:]).strip()


def _trim_pipeline_title_anchor(text: str, title: str) -> str:
    sample = (text or "").strip()
    if not sample or not title:
        return sample

    max_anchor_offset = min(3200, max(900, int(len(sample) * 0.45)))
    best_idx = 0
    best_score = 0
    for candidate in _pipeline_title_anchor_candidates(title):
        start = 0
        while True:
            idx = sample.find(candidate, start)
            if idx < 0 or idx > max_anchor_offset:
                break
            start = idx + max(len(candidate), 1)
            if idx <= 0:
                continue
            prefix_lines = [
                line.strip() for line in sample[:idx].splitlines() if line.strip()
            ]
            prefix_tail = prefix_lines[-18:]
            noise_hits = sum(1 for line in prefix_tail if _is_probable_ui_line(line))
            score = idx + noise_hits * 140
            if len(candidate) <= 8:
                score += 80
            if noise_hits >= 6:
                score += 180
            if score > best_score:
                best_idx = idx
                best_score = score

    if best_idx > 0:
        return sample[best_idx:].lstrip()
    return sample


def _trim_pipeline_trailing_sections(text: str) -> str:
    lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 12:
        return (text or "").strip()

    min_idx = max(4, int(len(lines) * 0.18))
    cut_idx: int | None = None
    for idx, line in enumerate(lines):
        if idx < min_idx:
            continue
        stripped = line.strip()
        if _PIPELINE_TRAILING_CUT_PATTERN.search(stripped):
            cut_idx = idx
            break

    if cut_idx is None:
        return "\n".join(lines).strip()
    return "\n".join(lines[:cut_idx]).strip()


def _clean_pipeline_content(text: str, *, title: str = "", url: str = "") -> str:
    current = sanitize_text(text, preserve_newlines=True, aggressive=True)
    if not current:
        return ""

    current = _PIPELINE_INLINE_FOOTNOTE_PATTERN.sub("", current)
    current = re.sub(r"\n(?:\[\s*編輯\s*\]|\[\s*\d+(?:\.\d+)?\s*\]|\^+)\s*\n", "\n", current)
    current = re.sub(r"(?m)^(?:維基百科，自由的百科全書|取自「)\s*$", "", current)
    current = _trim_pipeline_title_anchor(current, title)
    current = _trim_pipeline_leading_shell(current)
    current = _trim_pipeline_trailing_sections(current)

    inline_trailing = _PIPELINE_INLINE_TRAILING_MARKER_PATTERN.search(current)
    if inline_trailing and inline_trailing.start() >= 240:
        current = current[: inline_trailing.start()].rstrip()

    host = (urlparse(url).netloc or "").lower()
    if "wikipedia.org" in host:
        current = re.sub(r"(?m)^\s*維基百科，自由的百科全書\s*$", "", current)
        current = re.sub(r"(?m)^\s*[\[\]編輯\s\d.]+\s*$", "", current)
        current = _trim_pipeline_trailing_sections(current)

    current = sanitize_text(current, preserve_newlines=True, aggressive=True)
    current = re.sub(r"\n{3,}", "\n\n", current).strip()

    if title:
        first_line = current.splitlines()[0].strip() if current else ""
        if current and not first_line.startswith("#") and title[:18] not in first_line:
            current = f"# {sanitize_text(title, preserve_newlines=False)}\n\n{current}"
    return current


def _clean_pdf_content(text: str, *, title: str = "") -> str:
    """保留 PDF 頁面與段落邊界，避免套用 HTML 導覽清洗規則。"""
    current = sanitize_text(text, preserve_newlines=True, aggressive=True)
    if not current:
        return ""

    current = re.sub(r"\n{3,}", "\n\n", current).strip()
    if title:
        first_line = current.splitlines()[0].strip() if current else ""
        if current and not first_line.startswith("#") and title[:18] not in first_line:
            current = f"# {sanitize_text(title, preserve_newlines=False)}\n\n{current}"
    return current


def _refresh_attempt_after_clean(attempt: Any, url: str) -> Any:
    content = getattr(attempt, "content", "") or ""
    if not content:
        return attempt

    resource_type = str(getattr(attempt, "resource_type", "html") or "html").lower()
    if resource_type == "pdf":
        cleaned = _clean_pdf_content(
            content,
            title=getattr(attempt, "title", "") or "",
        )
        clean_step = "pdf_boundary_preserving_clean"
    else:
        cleaned = _clean_pipeline_content(
            content,
            title=getattr(attempt, "title", "") or "",
            url=url,
        )
        clean_step = "pipeline_noise_trim"
    if cleaned != content:
        steps = list(getattr(attempt, "postprocess_steps", []) or [])
        steps.append(
            {
                "step": clean_step,
                "changed": True,
                "before_len": len(content),
                "after_len": len(cleaned),
                "delta": len(cleaned) - len(content),
            }
        )
        attempt.postprocess_steps = steps
    attempt.content = cleaned
    attempt.metrics = _crawl4ai_plus._analyze_content_quality(
        attempt.content,
        getattr(attempt, "html", "") or "",
    )
    attempt.content_scope = _crawl4ai_plus._content_scope(attempt.content)
    attempt.quality = _crawl4ai_plus._assess_content_quality(
        attempt.content,
        attempt.metrics,
    )
    return attempt


def _build_attempt_from_fetch(
    fetch: Any,
    extraction_mode: str,
    prepared_candidates: Any | None = None,
) -> Any:
    attempt = _crawl4ai_plus.AttemptResult(attempted=True)
    attempt.fetch_success = bool(getattr(fetch, "success", False))
    attempt.status_code = getattr(fetch, "status_code", None)
    attempt.resource_type = getattr(fetch, "resource_type", "html")
    attempt.html = getattr(fetch, "html", "") or ""
    attempt.title = getattr(fetch, "title", "") or ""
    attempt.error_code = getattr(fetch, "error_code", None)
    attempt.error_message = getattr(fetch, "error_message", None)
    attempt.retryable = bool(getattr(fetch, "retryable", False))
    attempt.resource_diagnostics = dict(getattr(fetch, "diagnostics", {}) or {})

    if attempt.html:
        if prepared_candidates is None:
            content, source, title, steps = _crawl4ai_plus._extract_http_content_bundle(
                attempt.html,
                getattr(fetch, "final_url", ""),
                extraction_mode=extraction_mode,
            )
        else:
            content, source, title, steps = (
                _crawl4ai_plus._evaluate_http_content_candidates(
                    prepared_candidates,
                    extraction_mode=extraction_mode,
                )
            )
        attempt.content = content
        attempt.content_source = source
        attempt.title = title
        attempt.postprocess_steps = steps
        attempt = _refresh_attempt_after_clean(
            attempt,
            getattr(fetch, "final_url", "") or "",
        )

    return attempt


def _select_http_attempt(fetch: Any) -> tuple[Any, str]:
    prepared_candidates = None
    if getattr(fetch, "html", ""):
        prepared_candidates = _crawl4ai_plus._prepare_http_content_candidates(
            getattr(fetch, "html", "") or "",
            getattr(fetch, "final_url", "") or "",
        )
    strict_attempt = _build_attempt_from_fetch(
        fetch,
        _crawl4ai_plus.EXTRACTION_MODE_STRICT,
        prepared_candidates,
    )
    if not strict_attempt.html or strict_attempt.quality.acceptable:
        return strict_attempt, "strict"

    general_attempt = _build_attempt_from_fetch(
        fetch,
        _crawl4ai_plus.EXTRACTION_MODE_GENERAL,
        prepared_candidates,
    )
    if general_attempt.quality.acceptable and not strict_attempt.quality.acceptable:
        return general_attempt, "general"
    if general_attempt.quality.usable and not strict_attempt.quality.usable:
        return general_attempt, "general"
    if (
        general_attempt.quality.usable
        and strict_attempt.quality.reason == "BELOW_ACCEPT_THRESHOLD"
        and general_attempt.metrics.text_len
        >= max(240, int(strict_attempt.metrics.text_len * 1.45))
    ):
        return general_attempt, "general"
    return strict_attempt, "strict"


async def _attempt_http_best(
    url: str,
    *,
    render: str,
    http_semaphore: asyncio.Semaphore | None,
    pdf_semaphore: asyncio.Semaphore | None,
) -> tuple[Any, dict[str, int], str, str]:
    timings = {
        "http_queue_wait": 0,
        "http_fetch": 0,
        "http_total": 0,
        "http_extract": 0,
    }

    if render == "always":
        return (
            _crawl4ai_plus.AttemptResult(),
            timings,
            "render_always_skip_http",
            "strict",
        )

    if http_semaphore is not None:
        queue_started = time.perf_counter()
        async with http_semaphore:
            timings["http_queue_wait"] = int(
                (time.perf_counter() - queue_started) * 1000
            )
            fetch_started = time.perf_counter()
            fetch = await _crawl4ai_plus._fetch_http(url, require_public_url=True)
            timings["http_fetch"] = int((time.perf_counter() - fetch_started) * 1000)
    else:
        fetch_started = time.perf_counter()
        fetch = await _crawl4ai_plus._fetch_http(url, require_public_url=True)
        timings["http_fetch"] = int((time.perf_counter() - fetch_started) * 1000)

    if (
        getattr(fetch, "success", False)
        and getattr(fetch, "resource_type", "html") == "pdf"
        and getattr(fetch, "body_bytes", None)
    ):
        attempt = _build_attempt_from_fetch(fetch, _crawl4ai_plus.EXTRACTION_MODE_STRICT)
        extract_started = time.perf_counter()

        async def _run_pdf_extract() -> Any:
            return await asyncio.to_thread(
                _crawl4ai_plus.extract_pdf_content,
                fetch.body_bytes,
                source_url=getattr(fetch, "final_url", "") or url,
            )

        try:
            if pdf_semaphore is not None:
                async with pdf_semaphore:
                    pdf_result = await _run_pdf_extract()
            else:
                pdf_result = await _run_pdf_extract()
            attempt.content = pdf_result.text
            attempt.title = pdf_result.title
            attempt.content_source = pdf_result.content_source
            attempt.resource_diagnostics["pdf"] = pdf_result.diagnostics
            attempt = _refresh_attempt_after_clean(
                attempt,
                getattr(fetch, "final_url", "") or url,
            )
        except Exception as exc:
            attempt.error_code = "PDF_EXTRACT_ERROR"
            attempt.error_message = f"{type(exc).__name__}: {exc}"
            attempt.retryable = False
        timings["http_extract"] = int((time.perf_counter() - extract_started) * 1000)
        timings["http_total"] = timings["http_queue_wait"] + timings["http_fetch"]
        trace = "http_fetch_success" if getattr(fetch, "success", False) else "http_fetch_failed"
        return attempt, timings, trace, "pdf"

    extract_started = time.perf_counter()
    # HTML 抽取包含 trafilatura 與 BeautifulSoup CPU 工作；放到工作執行緒
    # 才不會讓單一頁面堵住整個 async 批次。
    selected_attempt, selected_mode = await asyncio.to_thread(
        _select_http_attempt,
        fetch,
    )
    timings["http_extract"] = int((time.perf_counter() - extract_started) * 1000)
    timings["http_total"] = timings["http_queue_wait"] + timings["http_fetch"]

    trace = "http_fetch_success" if getattr(fetch, "success", False) else "http_fetch_failed"
    return selected_attempt, timings, trace, selected_mode


async def _crawl_single_url(
    url: str,
    *,
    render: str,
    http_semaphore: asyncio.Semaphore | None,
    js_semaphore: asyncio.Semaphore,
    pdf_semaphore: asyncio.Semaphore | None,
    include_debug: bool = False,
    max_chars: int = MAX_CHARS_PER_PAGE,
) -> dict[str, Any]:
    normalized = _normalize_url(url)
    request_url = _request_url_for_crawl(url)
    if not normalized or not normalized.startswith(("http://", "https://")):
        return {
            "url": url,
            "success": False,
            "content": "",
            "error": "BAD_URL",
            "used_render": "none",
        }

    started = time.perf_counter()

    if not _crawl_backend_ready():
        error_message = "新版爬蟲內核不可用"
        if _crawl4ai_plus_load_error is not None:
            error_message = (
                f"{error_message}: {type(_crawl4ai_plus_load_error).__name__}: "
                f"{_crawl4ai_plus_load_error}"
            )
        return {
            "url": normalized,
            "success": False,
            "title": "",
            "content": "",
            "content_length": 0,
            "used_render": "none",
            "error": "CRAWLER_BACKEND_UNAVAILABLE",
            "error_code": "CRAWLER_BACKEND_UNAVAILABLE",
            "error_message": error_message,
            "retryable": False,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    public_url, public_url_reason = await _crawl4ai_plus._public_url_status(request_url)
    if not public_url:
        return {
            "url": normalized,
            "success": False,
            "content": "",
            "error": "UNSAFE_URL",
            "error_code": "UNSAFE_URL",
            "error_message": public_url_reason,
            "retryable": False,
            "used_render": "none",
        }

    fast_result = await _try_social_fast_fetch(normalized, max_chars=max_chars)
    if fast_result:
        return fast_result

    http_attempt = _crawl4ai_plus.AttemptResult()
    http_trace = "http_not_attempted"
    http_mode = "strict"
    http_timings = {
        "http_queue_wait": 0,
        "http_fetch": 0,
        "http_total": 0,
        "http_extract": 0,
    }
    speculative_js_task: asyncio.Task | None = None
    speculative_js_enabled = (
        render == "auto"
        and _should_speculative_js_race(normalized)
        and not getattr(_crawl4ai_plus, "_looks_like_pdf_url", lambda _: False)(normalized)
    )
    if speculative_js_enabled:
        speculative_js_task = asyncio.create_task(
            _crawl4ai_plus._attempt_js(
                request_url,
                extraction_mode=_crawl4ai_plus.EXTRACTION_MODE_STRICT,
                js_semaphore=js_semaphore,
                wait_time=SPECULATIVE_JS_WAIT_SECONDS,
                js_code=_crawl4ai_plus.GENERIC_JS_ENHANCE_SNIPPET,
                require_public_url=True,
            )
        )
        _cancel_child_task_when_parent_finishes(speculative_js_task)

    if render != "always":
        http_attempt, http_timings, http_trace, http_mode = await _attempt_http_best(
            request_url,
            render=render,
            http_semaphore=http_semaphore,
            pdf_semaphore=pdf_semaphore,
        )
    else:
        preflight_hit_pdf = False
        if getattr(_crawl4ai_plus, "PDF_AUTO_EXTRACT_ENABLED", False):
            preflight = await _crawl4ai_plus._probe_resource_type(
                request_url,
                require_public_url=True,
            )
            if preflight.get("resource_type") == "pdf":
                preflight_hit_pdf = True
                http_attempt, http_timings, http_trace, http_mode = await _attempt_http_best(
                    request_url,
                    render="never",
                    http_semaphore=http_semaphore,
                    pdf_semaphore=pdf_semaphore,
                )
        if not preflight_hit_pdf:
            http_trace, http_mode = "render_always_skip_http", "strict"

    if getattr(http_attempt, "resource_type", "html") == "pdf":
        if speculative_js_task is not None:
            speculative_js_task.cancel()
            with suppress(asyncio.CancelledError):
                await speculative_js_task
        elapsed = int((time.perf_counter() - started) * 1000)
        debug_payload: dict[str, Any] = {}
        if include_debug:
            debug_payload = {
                "metrics": _crawl_metrics_to_dict(http_attempt.metrics),
                "content_scope": http_attempt.content_scope,
                "debug": {
                    "http_trace": http_trace,
                    "http_mode": http_mode,
                    "fallback_trigger": None,
                    "timings_ms": {
                        **http_timings,
                        "js_queue_wait": 0,
                        "js_exec": 0,
                        "js_crawl": 0,
                        "js_total": 0,
                    },
                },
            }
            pdf_diag = getattr(http_attempt, "resource_diagnostics", {}).get("pdf")
            if pdf_diag:
                debug_payload["debug"]["pdf"] = pdf_diag

        if http_attempt.content and http_attempt.quality.usable:
            result = {
                "url": normalized,
                "success": True,
                "title": sanitize_text(http_attempt.title or "", preserve_newlines=False),
                "content": _smart_truncate(http_attempt.content, max_chars),
                "content_length": len(http_attempt.content),
                "used_render": "http",
                "resource_type": "pdf",
                "content_source": http_attempt.content_source,
                "fallback_reason": None,
                "elapsed_ms": elapsed,
            }
            result.update(debug_payload)
            return result

        result = {
            "url": normalized,
            "success": False,
            "title": sanitize_text(http_attempt.title or "", preserve_newlines=False),
            "content": "",
            "content_length": 0,
            "used_render": "http",
            "resource_type": "pdf",
            "content_source": http_attempt.content_source,
            "error": _crawl_error_label(http_attempt.error_code or http_attempt.quality.reason),
            "error_code": http_attempt.error_code or http_attempt.quality.reason or "PDF_EXTRACT_ERROR",
            "error_message": http_attempt.error_message or "unable to extract usable pdf content",
            "retryable": bool(http_attempt.retryable),
            "fallback_reason": None,
            "elapsed_ms": elapsed,
        }
        result.update(debug_payload)
        return result

    need_js, trigger = _crawl4ai_plus._need_js_fallback(
        status_code=http_attempt.status_code,
        html_len=len(http_attempt.html or ""),
        content=http_attempt.content,
        metrics=http_attempt.metrics,
        quality=http_attempt.quality,
        render=render,
    )

    js_attempt = _crawl4ai_plus.AttemptResult()
    js_timings = {
        "js_queue_wait": 0,
        "js_exec": 0,
        "js_crawl": 0,
        "js_total": 0,
    }
    js_attempt_refreshed = False
    if need_js:
        js_wait = (
            4.5
            if trigger in {"comments_likely_dynamic", "BELOW_ACCEPT_THRESHOLD", "below_accept_threshold"}
            else 3.5
        )
        if speculative_js_task is not None:
            js_attempt, js_timings, _ = await speculative_js_task
            js_attempt = _refresh_attempt_after_clean(js_attempt, normalized)
            js_attempt_refreshed = True
        if (
            speculative_js_task is None
            or not js_attempt.fetch_success
            or not js_attempt.quality.usable
        ):
            js_attempt, js_timings, _ = await _crawl4ai_plus._attempt_js(
                request_url,
                extraction_mode=_crawl4ai_plus.EXTRACTION_MODE_STRICT,
                js_semaphore=js_semaphore,
                wait_time=js_wait,
                js_code=_crawl4ai_plus.GENERIC_JS_ENHANCE_SNIPPET,
                require_public_url=True,
            )
            js_attempt_refreshed = False
        if not js_attempt_refreshed:
            js_attempt = _refresh_attempt_after_clean(js_attempt, normalized)
    elif speculative_js_task is not None:
        speculative_js_task.cancel()
        with suppress(asyncio.CancelledError):
            await speculative_js_task

    selected_attempt = http_attempt
    used_render = "http"
    if need_js and (
        render == "always"
        or _crawl4ai_plus._should_select_js(http_attempt, js_attempt, render)
    ):
        selected_attempt = js_attempt
        used_render = "js"

    elapsed = int((time.perf_counter() - started) * 1000)
    debug_payload: dict[str, Any] = {}
    if include_debug:
        debug_payload = {
            "metrics": _crawl_metrics_to_dict(selected_attempt.metrics),
            "content_scope": selected_attempt.content_scope,
                "debug": {
                    "http_trace": http_trace,
                    "http_mode": http_mode,
                    "fallback_trigger": trigger,
                    "speculative_js_race": speculative_js_enabled,
                    "timings_ms": {**http_timings, **js_timings},
                },
            }
        if selected_attempt.postprocess_steps:
            debug_payload["debug"]["postprocess_steps"] = selected_attempt.postprocess_steps

    if selected_attempt.content and selected_attempt.quality.usable:
        content = selected_attempt.content
        result = {
            "url": normalized,
            "success": True,
            "title": sanitize_text(selected_attempt.title or "", preserve_newlines=False),
            "content": _smart_truncate(content, max_chars),
            "content_length": len(content),
            "used_render": used_render,
            "resource_type": getattr(selected_attempt, "resource_type", "html"),
            "content_source": selected_attempt.content_source,
            "fallback_reason": trigger if need_js else None,
            "elapsed_ms": elapsed,
        }
        result.update(debug_payload)
        return result

    error_code = selected_attempt.error_code or selected_attempt.quality.reason
    result = {
        "url": normalized,
        "success": False,
        "title": sanitize_text(selected_attempt.title or "", preserve_newlines=False),
        "content": "",
        "content_length": 0,
        "used_render": used_render,
        "resource_type": getattr(selected_attempt, "resource_type", "html"),
        "content_source": selected_attempt.content_source,
        "error": _crawl_error_label(error_code),
        "error_code": error_code,
        "error_message": selected_attempt.error_message
        or "unable to extract usable content",
        "retryable": bool(selected_attempt.retryable),
        "fallback_reason": trigger if need_js else None,
        "elapsed_ms": elapsed,
    }
    result.update(debug_payload)
    return result


async def batch_deep_crawl(
    urls: list[str],
    max_chars_per_page: int = MAX_CHARS_PER_PAGE,
    max_concurrency: int = MAX_CRAWL_CONCURRENCY,
    render: str = "auto",
    monitor: PipelineMonitor | None = None,
) -> list[dict[str, Any]]:
    mon = monitor or _current_monitor()

    seen: set[str] = set()
    unique_urls: list[str] = []
    for u in urls:
        norm = _normalize_url(u)
        if norm not in seen:
            seen.add(norm)
            unique_urls.append(u)
    dedup_removed = len(urls) - len(unique_urls)
    mon.dedup_notice(dedup_removed)

    http_sem = asyncio.Semaphore(max_concurrency)
    js_sem = asyncio.Semaphore(min(MAX_JS_CONCURRENCY, max_concurrency))
    pdf_sem = asyncio.Semaphore(getattr(_crawl4ai_plus, "MAX_PDF_CONCURRENCY", 2))

    async def _worker(url: str) -> dict[str, Any]:
        try:
            result = await _crawl_single_url(
                url,
                render=render,
                http_semaphore=http_sem,
                js_semaphore=js_sem,
                pdf_semaphore=pdf_sem,
                include_debug=mon.enabled,
                max_chars=max_chars_per_page,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = {
                "url": url,
                "success": False,
                "title": "",
                "content": "",
                "content_length": 0,
                "used_render": "none",
                "resource_type": "unknown",
                "error": "CRAWL_WORKER_ERROR",
                "error_code": "CRAWL_WORKER_ERROR",
                "error_message": f"{type(exc).__name__}: {exc}",
                "retryable": False,
                "elapsed_ms": 0,
            }
        if result.get("success"):
            detail = f"{result.get('content_length', 0)} 字元, {result.get('used_render', '?')}"
            mon.crawl_progress(url, "ok", detail)
        else:
            mon.crawl_progress(url, "fail", result.get("error", "?"))
        return result

    results = await asyncio.gather(*[_worker(u) for u in unique_urls])
    return list(results)


def _trim_explicit_url_token(value: str) -> str:
    """移除訊息排版帶入的尾端標點，不改動 URL 內部 query。"""
    trimmed = str(value or "").strip().rstrip(".,;:!?，。；：！？")
    while trimmed.endswith(")") and trimmed.count(")") > trimmed.count("("):
        trimmed = trimmed[:-1]
    return trimmed


def extract_explicit_urls(
    text: str,
    *,
    max_urls: int = DIRECT_URL_LIMIT,
) -> dict[str, Any]:
    """從目前訊息擷取 HTTP(S) URL；保留首個原始 URL 以避免破壞簽名 query。"""
    unique_urls: list[str] = []
    seen: set[str] = set()
    for match in EXPLICIT_HTTP_URL_RE.finditer(str(text or "")):
        candidate = _trim_explicit_url_token(match.group(0))
        normalized = _normalize_url(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_urls.append(candidate)
    limit = max(1, int(max_urls or DIRECT_URL_LIMIT))
    return {
        "urls": unique_urls[:limit],
        "overflow": len(unique_urls) > limit,
        "total": len(unique_urls),
    }


def _bounded_unique_urls(values: Iterable[Any] | None, *, limit: int) -> list[str]:
    """以 canonical URL 去重，但保留原 URL 供帶簽名 query 的實際請求使用。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        url = str(value or "").strip()
        normalized = _normalize_url(url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(url)
        if len(result) >= limit:
            break
    return result


async def crawl_explicit_urls(
    urls: list[str],
    *,
    max_chars_per_page: int = MAX_CHARS_PER_PAGE,
    render: str = "auto",
) -> dict[str, Any]:
    """爬取使用者在本輪明確貼上的 URL，沿用正式 HTTP／JS／PDF 安全管線。"""
    started = time.perf_counter()
    normalized_urls = [str(url).strip() for url in urls if str(url).strip()]
    if not normalized_urls:
        return {"pages": [], "failed": [], "elapsed_ms": 0}

    monitor = PipelineMonitor(enabled=False)
    crawled = await batch_deep_crawl(
        normalized_urls,
        max_chars_per_page=max_chars_per_page,
        max_concurrency=min(DIRECT_CRAWL_CONCURRENCY, len(normalized_urls)),
        render=render,
        monitor=monitor,
    )
    pages: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for page in crawled:
        item = dict(page)
        item["from_query"] = "Provided URL"
        if item.get("success"):
            pages.append(item)
        else:
            failed.append(item)
    return {
        "pages": pages,
        "failed": failed,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


async def shutdown_shared_resources() -> None:
    """關閉搜尋 API 與新版爬蟲內核共用資源。"""
    await _close_llm_api_client()
    await _close_search_api_client()
    if _crawl4ai_plus is not None and hasattr(
        _crawl4ai_plus, "_shutdown_shared_resources"
    ):
        await _crawl4ai_plus._shutdown_shared_resources()


# ============================================================
# 模組四：位置策略與輔助函數
# ============================================================


def _apply_position_strategy(
    crawled_pages: list[dict],
    selection_details: list[dict],
) -> list[dict]:
    """
    位置策略排序。
    利用 LLM 的 U 型注意力偏好（開頭和結尾的文本利用率最高），
    將最重要的結果排在首尾，次要的放中間。

    按 group_index 中的 result_indices 順序推定重要性。
    """
    if len(crawled_pages) <= 2:
        return crawled_pages

    # 按 LLM selection order 排序
    importance_order: list[str] = []
    for detail in selection_details:
        gi = detail["group_index"]
        for ri in detail["result_indices"]:
            for page in crawled_pages:
                if (
                    page.get("_group_index") == gi
                    and page.get("_result_index") == ri
                ):
                    if page["url"] not in importance_order:
                        importance_order.append(page["url"])

    if not importance_order:
        return crawled_pages

    def sort_key(page):
        try:
            return importance_order.index(page["url"])
        except ValueError:
            return len(importance_order)

    sorted_pages = sorted(crawled_pages, key=sort_key)

    # 交錯排列：奇數位放前半（開頭），偶數位放後半（結尾）
    n = len(sorted_pages)
    result = [None] * n
    front = 0
    back = n - 1

    for i, page in enumerate(sorted_pages):
        if i % 2 == 0:
            result[front] = page
            front += 1
        else:
            result[back] = page
            back -= 1

    return [p for p in result if p is not None]


# ============================================================
# 模組五：Chunk reviewer 共用詞彙處理
# ============================================================

_REVIEW_TERM_RE = re.compile(r"[A-Za-z0-9_+-]{3,}|[\u4E00-\u9FFF]{2,}")


def _extract_review_terms(*texts: str) -> list[str]:
    seen: list[str] = []
    seen_keys: set[str] = set()
    for text in texts:
        for term in _REVIEW_TERM_RE.findall((text or "").lower()):
            if term in seen_keys:
                continue
            seen_keys.add(term)
            seen.append(term)
            if len(seen) >= 24:
                return seen
    return seen


def _chunk_id(round_number: int, source_ordinal: int, chunk_ordinal: int) -> str:
    return f"L{max(1, round_number)}-S{max(1, source_ordinal)}-C{max(1, chunk_ordinal):03d}"


def _split_overlong_text(
    text: str,
    *,
    max_chars: int = CHUNK_MAX_CHARS,
) -> list[str]:
    sample = sanitize_text(text or "", preserve_newlines=True, aggressive=True).strip()
    if len(sample) <= max_chars:
        return [sample] if sample else []

    pieces = [
        part.strip()
        for part in re.split(r"(?<=[。！？.!?])\s+|\n+", sample)
        if part.strip()
    ]
    if len(pieces) <= 1:
        return [
            sample[i : i + max_chars].strip()
            for i in range(0, len(sample), max_chars)
            if sample[i : i + max_chars].strip()
        ]

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
            continue
        if len(current) + 1 + len(piece) <= max_chars:
            current = f"{current} {piece}".strip()
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def _chunk_terms(question: str, *extra: str) -> list[str]:
    return _extract_review_terms(question, *extra)


def _score_chunk_for_question(
    text: str,
    *,
    question: str,
    from_query: str = "",
    position: int = 0,
    terms: list[str] | None = None,
) -> float:
    lowered = (text or "").lower()
    score = 0.0
    if position == 0:
        score += 2.0
    elif position == 1:
        score += 1.0
    active_terms = terms if terms is not None else _chunk_terms(question, from_query)
    for term in active_terms:
        if term and term in lowered:
            score += 1.25
    score += min(len(text or ""), CHUNK_TARGET_CHARS) / CHUNK_TARGET_CHARS
    return score


_PDF_PAGE_MARKER_PATTERN = re.compile(r"(?m)^##\s*第\s*\d+\s*頁\s*$")


def _extract_pdf_review_paragraphs(sample: str) -> list[str]:
    """依頁面合併 PDF 的物理短行，再交給既有長度門檻。"""
    paragraphs: list[str] = []
    page_sections = _PDF_PAGE_MARKER_PATTERN.split(sample)
    for section in page_sections:
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        if not lines:
            continue

        current_lines: list[str] = []
        current_length = 0
        for line in lines:
            additional_length = len(line) + (1 if current_lines else 0)
            if current_lines and current_length + additional_length > CHUNK_TARGET_CHARS:
                paragraphs.append(" ".join(current_lines))
                current_lines = [line]
                current_length = len(line)
            else:
                current_lines.append(line)
                current_length += additional_length

        if current_lines:
            remainder = " ".join(current_lines)
            if len(remainder) < CHUNK_MIN_CHARS and paragraphs:
                paragraphs[-1] = f"{paragraphs[-1]} {remainder}".strip()
            else:
                paragraphs.append(remainder)
    return paragraphs


def _extract_review_paragraphs(
    text: str,
    *,
    resource_type: str = "html",
) -> list[str]:
    sample = sanitize_text(text or "", preserve_newlines=True, aggressive=True)
    if not sample.strip():
        return []

    if resource_type.lower() == "pdf":
        paragraphs = _extract_pdf_review_paragraphs(sample)
    else:
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", sample) if p.strip()]
        if len(paragraphs) <= 1:
            paragraphs = [p.strip() for p in sample.splitlines() if p.strip()]

    cleaned: list[str] = []
    seen: set[str] = set()
    for paragraph in paragraphs:
        paragraph = re.sub(r"[ \t]+", " ", paragraph).strip()
        if len(paragraph) < CHUNK_MIN_CHARS:
            continue
        key = paragraph[:220].casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(paragraph)
    return cleaned


def _page_to_review_chunks(
    page: dict[str, Any],
    *,
    round_number: int,
    source_ordinal: int,
    question: str,
) -> list[dict[str, Any]]:
    paragraphs = _extract_review_paragraphs(
        page.get("content", ""),
        resource_type=str(page.get("resource_type", "html") or "html"),
    )
    if not paragraphs:
        return []

    raw_chunks: list[str] = []
    for paragraph in paragraphs:
        parts = _split_overlong_text(paragraph)
        for part in parts:
            if not part:
                continue
            raw_chunks.append(part)

    if not raw_chunks:
        return []

    review_chunks: list[dict[str, Any]] = []
    score_terms = _chunk_terms(question, page.get("from_query", ""))
    for chunk_ordinal, text in enumerate(raw_chunks, start=1):
        idx = chunk_ordinal - 1
        review_chunks.append(
            {
                "chunk_id": _chunk_id(round_number, source_ordinal, chunk_ordinal),
                "round": round_number,
                "source_ref": f"L{round_number}-S{source_ordinal}",
                "source_ordinal": source_ordinal,
                "source_url": page.get("url", ""),
                "title": page.get("title", ""),
                "from_query": page.get("from_query", ""),
                "text": text,
                "_score": _score_chunk_for_question(
                    text,
                    question=question,
                    from_query=page.get("from_query", ""),
                    position=idx,
                    terms=score_terms,
                ),
            }
        )
    return review_chunks


def _build_review_chunks_for_pages(
    pages: list[dict[str, Any]],
    *,
    round_number: int,
    question: str,
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for source_ordinal, page in enumerate(pages, start=1):
        chunks.extend(
            _page_to_review_chunks(
                page,
                round_number=round_number,
                source_ordinal=source_ordinal,
                question=question,
            )
        )
    return chunks


def _estimate_context_tokens(text: str) -> int:
    """避免新增 tokenizer 依賴，保守估算中英文混合的規劃提示詞成本。"""
    value = str(text or "")
    han_count = sum(1 for char in value if "\u3400" <= char <= "\u9fff")
    return han_count + (max(0, len(value) - han_count) + 3) // 4


def build_direct_planner_context(
    question: str,
    pages: list[dict[str, Any]],
    *,
    token_limit: int = DIRECT_PLANNER_CONTEXT_TOKEN_LIMIT,
) -> list[dict[str, str]]:
    """將指定連結的正文壓成公平、相關且受限的規劃摘要，不送整頁全文給分詞模型。"""
    chunks = _build_review_chunks_for_pages(
        pages,
        round_number=0,
        question=question,
    )
    by_source: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        source_key = str(chunk.get("source_url") or chunk.get("source_ref") or "")
        if source_key:
            by_source.setdefault(source_key, []).append(chunk)
    for source_chunks in by_source.values():
        source_chunks.sort(key=lambda item: float(item.get("_score", 0.0)), reverse=True)

    selected: list[dict[str, str]] = []
    cursors = {key: 0 for key in by_source}
    used_tokens = 0
    while by_source and used_tokens < token_limit:
        added = False
        for source_key, source_chunks in by_source.items():
            cursor = cursors[source_key]
            if cursor >= len(source_chunks):
                continue
            chunk = source_chunks[cursor]
            cursors[source_key] += 1
            text = str(chunk.get("text") or "").strip()
            if not text:
                continue
            remaining = token_limit - used_tokens
            if remaining <= 0:
                break
            cost = _estimate_context_tokens(text)
            if cost > remaining:
                # 預留可辨識的一小段，不讓單一長文吞沒其他指定來源。
                char_limit = max(240, remaining * 3)
                text = _smart_truncate(text, char_limit)
                cost = _estimate_context_tokens(text)
            if not text or cost > remaining:
                continue
            selected.append(
                {
                    "chunk_id": str(chunk.get("chunk_id") or ""),
                    "title": str(chunk.get("title") or source_key),
                    "url": str(chunk.get("source_url") or source_key),
                    "text": text,
                }
            )
            used_tokens += cost
            added = True
        if not added:
            break
    return selected


def _select_chunks_for_reviewer_prompt(
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """沿用 V3：清理後的所有有效 chunk 都交給 LLM Judge。"""
    return list(chunks)


def _public_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": chunk.get("chunk_id", ""),
        "round": chunk.get("round"),
        "source_ref": chunk.get("source_ref", ""),
        "source_url": chunk.get("source_url", ""),
        "title": chunk.get("title", ""),
        "from_query": chunk.get("from_query", ""),
        "text": chunk.get("text", ""),
    }


def _build_chunk_reviewer_system_prompt(
    *,
    final_round: bool = False,
    execution_mode: str = "fast",
) -> str:
    final_note = (
        "\n- 這是最後補洞輪；即使仍有缺口，也不要要求更多輪搜尋，只需如實標記 missing。"
        if final_round
        else "\n- 若證據不足，請產生下一輪搜尋字詞，用於補足缺口。"
    )
    instant_note = (
        "\n## Instant 快速模式規則\n"
        "- 這是 instant 快速模式，只執行單輪搜尋與深爬；你不需要規劃下一輪。\n"
        "- 若證據只足以回答部分面向，仍選出可用 chunk，並把不足之處清楚寫入 missing。"
        if execution_mode == "instant"
        else ""
    )
    return f"""你是一位研究流程 reviewer。你不負責寫最終答案，只負責根據用戶問題審核本輪爬蟲 chunk，並判斷證據是否足夠。

{OPEN_EVIDENCE_DIVERSITY_INSTRUCTION}
{OPEN_VIEWPOINT_DIVERSITY_INSTRUCTION}
{instant_note}

## 任務
1. 從本輪 chunk 清單中挑選能直接支持回答的 chunk_id。
2. 若有上一輪已選證據，請把上一輪證據與本輪新證據一起納入 sufficiency judge。
3. 判斷目前證據是否足夠回答用戶問題。
4. 若不足，指出缺口並輸出下一輪搜尋 query。

## 規則
- 只輸出 JSON，不要 markdown，不要解釋。
- {CHUNK_MINIMAL_EVIDENCE_SET_INSTRUCTION}
- selected_chunk_ids 只能包含輸入中真實存在的 chunk_id。
- 不要輸出摘要，不要改寫 chunk 內容。
- 搜尋結果 snippet 只用於選爬蟲；最終回答只能依據被選中的 chunk 原文。
- verdict 只能是 "sufficient" 或 "insufficient"。{final_note}

## 輸出格式
{{
  "selected_chunk_ids": ["L1-S1-C001", "L1-S2-C003"],
  "verdict": "sufficient",
  "gap_analysis": "若不足，簡述缺少哪些證據；若足夠，簡述已覆蓋哪些面向。",
  "coverage": {{
    "answered": ["已覆蓋的面向"],
    "missing": ["仍缺少的面向"]
  }},
  "next_search_queries": ["query1", "query2", "query3"],
  "search_mode": "web"
}}"""


def _format_chunks_for_prompt(
    chunks: list[dict[str, Any]],
    *,
    selectable: bool = True,
) -> str:
    """每個來源只宣告一次 metadata，其下列出 chunk ID 與原文。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        source_ref = str(chunk.get("source_ref") or chunk.get("source_url") or "unknown")
        grouped.setdefault(source_ref, []).append(chunk)

    parts: list[str] = []
    for source_ref, source_chunks in grouped.items():
        first = source_chunks[0]
        chunk_parts: list[str] = []
        for chunk in source_chunks:
            label = (
                f"[{chunk.get('chunk_id')}]"
                if selectable
                else f"prior chunk {chunk.get('chunk_id')}"
            )
            chunk_parts.append(f"{label}\n{chunk.get('text', '')}")
        parts.append(
            f"來源 {source_ref}: {first.get('title') or '(untitled)'}\n"
            f"URL: {first.get('source_url', '')}\n"
            f"From query: {first.get('from_query', '')}\n"
            + "\n\n".join(chunk_parts)
        )
    return "\n\n---\n\n".join(parts)


def _build_chunk_reviewer_user_prompt(
    *,
    question: str,
    search_queries: list[str],
    search_mode: str,
    execution_mode: str,
    round_number: int,
    chunks_for_prompt: list[dict[str, Any]],
    total_chunk_count: int,
    prior_evidence_chunks: list[dict[str, Any]] | None = None,
    missing_focus: list[str] | None = None,
) -> str:
    prior = prior_evidence_chunks or []
    missing = missing_focus or []
    parts = [
        f"## 用戶問題\n{question}\n",
        f"## 本輪\n第 {round_number} 輪，搜尋模式：{search_mode}，執行模式：{execution_mode}",
        "## 本輪搜尋字詞\n" + "\n".join(f"- {q}" for q in search_queries),
    ]
    if missing:
        parts.append("## 上輪指出的缺口\n" + "\n".join(f"- {m}" for m in missing))
    if prior:
        parts.append(
            "## 上輪已選證據（只供 sufficiency judge，不需要重新輸出這些 ID）\n"
            + _format_chunks_for_prompt(prior, selectable=False)
        )
    parts.append(
        f"## 本輪可選 chunk（共 {len(chunks_for_prompt)} 個，已放入全部 {total_chunk_count} 個有效 chunk）\n"
        + _format_chunks_for_prompt(chunks_for_prompt)
    )
    return "\n\n".join(parts)


def _extract_json_payload(raw: str) -> Any:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty response")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    object_match = re.search(r"\{[\s\S]*\}", text)
    if object_match:
        return json.loads(object_match.group())
    array_match = re.search(r"\[[\s\S]*\]", text)
    if array_match:
        return json.loads(array_match.group())
    raise ValueError("response does not contain JSON")


def _normalize_reviewer_verdict(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {
        "insufficient",
        "needs_more",
        "need_more",
        "needs more",
        "not_sufficient",
        "not sufficient",
        "no",
        "not_enough",
        "not enough",
        "不夠",
        "不足",
    }:
        return "insufficient"
    if raw in {"sufficient", "enough", "ok", "complete", "yes", "足夠", "充分"}:
        return "sufficient"
    return None


def _parse_chunk_reviewer_response(
    raw: str,
    chunk_map: dict[str, dict[str, Any]],
    *,
    default_search_mode: str,
) -> dict[str, Any]:
    data = _extract_json_payload(raw)
    if isinstance(data, list):
        selected_raw = data
        data = {}
    elif isinstance(data, dict):
        selected_raw = (
            data.get("selected_chunk_ids")
            or data.get("chunk_ids")
            or data.get("selected_ids")
            or []
        )
    else:
        raise ValueError("reviewer JSON must be object or array")

    if not isinstance(selected_raw, list):
        selected_raw = []
    selected_ids: list[str] = []
    seen: set[str] = set()
    for item in selected_raw:
        cid = str(item).strip()
        if cid in chunk_map and cid not in seen:
            seen.add(cid)
            selected_ids.append(cid)

    verdict = _normalize_reviewer_verdict(data.get("verdict") if isinstance(data, dict) else None)
    if verdict is None:
        raise ValueError("reviewer verdict missing or invalid")
    coverage = data.get("coverage", {}) if isinstance(data, dict) else {}
    if not isinstance(coverage, dict):
        coverage = {}
    next_queries = data.get("next_search_queries", []) if isinstance(data, dict) else []
    if not isinstance(next_queries, list):
        next_queries = []
    next_queries = [
        q.strip()
        for q in next_queries
        if isinstance(q, str) and q.strip()
    ][:3]
    next_mode = normalize_search_mode(
        (data.get("search_mode") if isinstance(data, dict) else None)
        or default_search_mode
    )

    return {
        "selected_chunk_ids": selected_ids,
        "selected_chunks": [_public_chunk(chunk_map[cid]) for cid in selected_ids],
        "verdict": verdict,
        "gap_analysis": str(data.get("gap_analysis", "") if isinstance(data, dict) else "").strip(),
        "coverage": {
            "answered": [
                str(x).strip()
                for x in coverage.get("answered", [])
                if isinstance(x, (str, int, float)) and str(x).strip()
            ]
            if isinstance(coverage.get("answered", []), list)
            else [],
            "missing": [
                str(x).strip()
                for x in coverage.get("missing", [])
                if isinstance(x, (str, int, float)) and str(x).strip()
            ]
            if isinstance(coverage.get("missing", []), list)
            else [],
        },
        "next_search_queries": next_queries,
        "search_mode": next_mode,
    }


def _fallback_select_review_chunks(
    chunks: list[dict[str, Any]],
    *,
    limit: int = CHUNK_REVIEW_FALLBACK_CHUNKS,
) -> list[dict[str, Any]]:
    if not chunks or limit <= 0:
        return []
    selected: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for chunk in chunks:
        source_ref = str(chunk.get("source_ref", ""))
        if source_ref in seen_sources:
            continue
        seen_sources.add(source_ref)
        selected.append(chunk)
        if len(selected) >= limit:
            return selected
    for chunk in sorted(chunks, key=lambda c: float(c.get("_score", 0.0)), reverse=True):
        if chunk in selected:
            continue
        selected.append(chunk)
        if len(selected) >= limit:
            break
    return selected


async def review_crawled_chunks(
    *,
    question: str,
    search_queries: list[str],
    search_mode: str,
    execution_mode: str = "fast",
    pages: list[dict[str, Any]],
    round_number: int,
    llm_config: dict[str, Any],
    mon: PipelineMonitor,
    prior_evidence_chunks: list[dict[str, Any]] | None = None,
    missing_focus: list[str] | None = None,
    final_round: bool = False,
) -> dict[str, Any]:
    all_chunks = _build_review_chunks_for_pages(
        pages,
        round_number=round_number,
        question=question,
    )
    prompt_chunks = _select_chunks_for_reviewer_prompt(all_chunks)
    chunk_map = {str(chunk.get("chunk_id")): chunk for chunk in prompt_chunks}
    display_model = str(
        llm_config.get("display_model")
        or llm_config.get("model")
        or DEFAULT_FILTER_MODEL
    )

    if not prompt_chunks:
        return {
            "success": False,
            "verdict": "insufficient",
            "gap_analysis": "本輪沒有可審核的 chunk。",
            "coverage": {"answered": [], "missing": ["沒有可用爬蟲內容"]},
            "next_search_queries": [],
            "search_mode": search_mode,
            "selected_chunk_ids": [],
            "selected_chunks": [],
            "total_chunks": len(all_chunks),
            "prompted_chunks": 0,
            "parse_retries": 0,
            "raw_llm_response": "",
        }

    system_prompt = _build_chunk_reviewer_system_prompt(
        final_round=final_round,
        execution_mode=execution_mode,
    )
    user_prompt = _build_chunk_reviewer_user_prompt(
        question=question,
        search_queries=search_queries,
        search_mode=search_mode,
        execution_mode=execution_mode,
        round_number=round_number,
        chunks_for_prompt=prompt_chunks,
        total_chunk_count=len(all_chunks),
        prior_evidence_chunks=prior_evidence_chunks,
        missing_focus=missing_focus,
    )

    last_raw = ""
    last_error: Exception | None = None
    for attempt in range(CHUNK_REVIEW_MAX_RETRIES):
        attempt_system = system_prompt
        if attempt > 0:
            attempt_system += "\n\n上一次輸出無法解析或沒有選到有效 chunk_id；這一次只能輸出合法 JSON，且 ID 必須來自輸入。"
        mon.llm_input(attempt_system, user_prompt, display_model)
        try:
            raw = await _call_llm_raw_content(
                llm_config=llm_config,
                system_prompt=attempt_system,
                user_prompt=user_prompt,
            )
        except Exception as exc:
            last_error = exc
            mon._print(
                f"{mon.RED}  [!!] Chunk reviewer 第 {attempt + 1} 次請求失敗: {type(exc).__name__}{mon.RESET}"
            )
            continue
        last_raw = raw
        mon.llm_output(raw)
        try:
            parsed = _parse_chunk_reviewer_response(
                raw,
                chunk_map,
                default_search_mode=search_mode,
            )
            if not parsed["selected_chunks"]:
                raise ValueError("reviewer did not select valid chunks")
            parsed.update(
                {
                    "success": True,
                    "total_chunks": len(all_chunks),
                    "prompted_chunks": len(prompt_chunks),
                    "discarded_chunks": max(0, len(prompt_chunks) - len(parsed["selected_chunks"])),
                    "parse_retries": attempt,
                    "raw_llm_response": raw,
                }
            )
            return parsed
        except Exception as exc:
            last_error = exc
            mon._print(
                f"{mon.RED}  [!!] Chunk reviewer 第 {attempt + 1} 次解析失敗: {exc}{mon.RESET}"
            )

    regex_ids = [
        cid
        for cid in re.findall(r"L\d+-S\d+-C\d{3}", last_raw or "")
        if cid in chunk_map
    ]
    if regex_ids:
        selected_chunks = [_public_chunk(chunk_map[cid]) for cid in dict.fromkeys(regex_ids)]
    else:
        selected_chunks = [_public_chunk(chunk) for chunk in _fallback_select_review_chunks(prompt_chunks)]
    selected_ids = [chunk["chunk_id"] for chunk in selected_chunks]

    return {
        "success": False,
        "verdict": "insufficient",
        "gap_analysis": f"Chunk reviewer parse failed: {last_error}",
        "coverage": {
            "answered": [],
            "missing": ["reviewer 輸出格式失敗，已使用 fallback chunk"],
        },
        "next_search_queries": [],
        "search_mode": search_mode,
        "selected_chunk_ids": selected_ids,
        "selected_chunks": selected_chunks,
        "total_chunks": len(all_chunks),
        "prompted_chunks": len(prompt_chunks),
        "discarded_chunks": max(0, len(prompt_chunks) - len(selected_chunks)),
        "parse_retries": CHUNK_REVIEW_MAX_RETRIES,
        "raw_llm_response": last_raw,
    }


def _dedupe_selected_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for chunk in chunks:
        cid = str(chunk.get("chunk_id", ""))
        if not cid or cid in seen:
            continue
        seen.add(cid)
        deduped.append(chunk)
    return deduped


def _build_evidence_outputs(
    raw_pages: list[dict[str, Any]],
    selected_chunks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    selected_chunks = _dedupe_selected_chunks(selected_chunks)
    chunks_by_url: dict[str, list[dict[str, Any]]] = {}
    for chunk in selected_chunks:
        url_key = _normalize_url(str(chunk.get("source_url", "")))
        if not url_key:
            continue
        chunks_by_url.setdefault(url_key, []).append(dict(chunk))

    evidence_pages: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for page in raw_pages:
        url_key = _normalize_url(str(page.get("url", "")))
        if not url_key or url_key in seen_urls or url_key not in chunks_by_url:
            continue
        seen_urls.add(url_key)
        chunks = chunks_by_url[url_key]
        selected_text = "\n\n".join(
            f"[{chunk.get('chunk_id')}]\n{chunk.get('text', '')}".strip()
            for chunk in chunks
            if chunk.get("text")
        )
        page_copy = {
            key: value
            for key, value in page.items()
            if key not in {"content", "html", "debug", "metrics"}
        }
        page_copy["content"] = selected_text
        page_copy["content_length"] = len(selected_text)
        page_copy["chunks"] = chunks
        evidence_pages.append(page_copy)

    source_registry = _attach_citation_metadata(evidence_pages)

    citation_by_url: dict[str, dict[str, Any]] = {}
    for page in evidence_pages:
        citation_by_url[_normalize_url(str(page.get("url", "")))] = {
            "source_index": page.get("source_index"),
            "citation_id": page.get("citation_id"),
            "citation_marker": page.get("citation_marker"),
            "title": page.get("title", ""),
            "url": page.get("url", ""),
        }

    flat_selected: list[dict[str, Any]] = []
    for page in evidence_pages:
        page_url_key = _normalize_url(str(page.get("url", "")))
        citation = citation_by_url.get(page_url_key, {})
        for chunk in page.get("chunks", []):
            chunk.update(citation)
            flat_selected.append(chunk)

    evidence_bundle: list[dict[str, Any]] = []
    for page in evidence_pages:
        evidence_bundle.append(
            {
                "source_index": page.get("source_index"),
                "title": page.get("title", ""),
                "url": page.get("url", ""),
                "citation_id": page.get("citation_id"),
                "citation_marker": page.get("citation_marker"),
                "chunks": [
                    {
                        "chunk_id": chunk.get("chunk_id", ""),
                        "text": chunk.get("text", ""),
                    }
                    for chunk in page.get("chunks", [])
                ],
            }
        )

    return evidence_pages, source_registry, evidence_bundle, flat_selected


async def review_explicit_pages(
    *,
    question: str,
    pages: list[dict[str, Any]],
    search_mode: str,
    execution_mode: str,
    judge_model_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """審核本輪直接爬得的頁面，產生可引用證據與是否可直接回答的判定。"""
    started = time.perf_counter()
    monitor = PipelineMonitor(enabled=False)
    llm_config = _build_llm_route_config(
        DEFAULT_MODEL_ROUTE,
        None,
        judge_model_config or {},
    )
    review = await review_crawled_chunks(
        question=question,
        search_queries=["Provided URL"],
        search_mode=normalize_search_mode(search_mode),
        execution_mode=normalize_execution_mode(execution_mode),
        pages=pages,
        round_number=0,
        llm_config=llm_config,
        mon=monitor,
        final_round=False,
    )
    evidence_pages, source_registry, evidence_bundle, selected_chunks = _build_evidence_outputs(
        pages,
        list(review.get("selected_chunks", [])),
    )
    return {
        "review": review,
        "evidence_pages": evidence_pages,
        "source_registry": source_registry,
        "evidence_bundle": evidence_bundle,
        "selected_chunks": selected_chunks,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def _chunk_filter_summary(review_results: list[dict[str, Any]]) -> dict[str, Any]:
    total_chunks = sum(int(r.get("total_chunks", 0)) for r in review_results)
    prompted_chunks = sum(int(r.get("prompted_chunks", 0)) for r in review_results)
    selected_chunks = sum(len(r.get("selected_chunks", [])) for r in review_results)
    return {
        "model_stage": "round_reviewer",
        "total_chunks": total_chunks,
        "prompted_chunks": prompted_chunks,
        "selected_chunks": selected_chunks,
        "discarded_chunks": max(0, prompted_chunks - selected_chunks),
        "rounds": [
            {
                "round": idx + 1,
                "success": bool(r.get("success")),
                "verdict": r.get("verdict"),
                "total_chunks": int(r.get("total_chunks", 0)),
                "prompted_chunks": int(r.get("prompted_chunks", 0)),
                "selected_chunks": len(r.get("selected_chunks", [])),
                "gap_analysis": r.get("gap_analysis", ""),
                "coverage": r.get("coverage", {}),
                "next_search_queries": r.get("next_search_queries", []),
                "search_mode": r.get("search_mode"),
            }
            for idx, r in enumerate(review_results)
        ],
    }


def _review_missing_focus(review_result: dict[str, Any]) -> list[str]:
    coverage = review_result.get("coverage", {})
    if isinstance(coverage, dict):
        missing = coverage.get("missing", [])
        if isinstance(missing, list):
            cleaned = [str(item).strip() for item in missing if str(item).strip()]
            if cleaned:
                return cleaned[:6]
    gap = str(review_result.get("gap_analysis", "") or "").strip()
    return [gap] if gap else []


def _resolve_reviewer_next_queries(
    review_result: dict[str, Any],
    default_queries: list[str],
    *,
    max_queries: int = 3,
) -> list[str]:
    queries = [
        q.strip()
        for q in review_result.get("next_search_queries", [])
        if isinstance(q, str) and q.strip()
    ]
    resolved: list[str] = []
    for query in queries + [q for q in default_queries if isinstance(q, str)]:
        query = query.strip()
        if query and query not in resolved:
            resolved.append(query)
        if len(resolved) >= max_queries:
            break
    return resolved


def _build_carryover_query_groups(
    query_groups: list[dict[str, Any]],
    *,
    exclude_urls: set[str] | None = None,
    label: str = "上一輪未爬候選",
    max_results_per_group: int | None = None,
) -> list[dict[str, Any]]:
    """把上一輪搜尋 snippets 轉成可供下一輪 URL reviewer 一併挑選的候選群組。"""
    if not query_groups or (max_results_per_group is not None and max_results_per_group <= 0):
        return []

    excluded = exclude_urls or set()
    carryover: list[dict[str, Any]] = []
    for group in query_groups:
        results: list[dict[str, Any]] = []
        for result in group.get("results", []):
            normalized = _normalize_url(result.get("url", ""))
            if not normalized or normalized in excluded:
                continue
            results.append(dict(result))
            if max_results_per_group is not None and len(results) >= max_results_per_group:
                break
        if not results:
            continue
        query = str(group.get("query", "") or "").strip()
        carryover.append(
            {
                "query": f"{label}: {query}" if query else label,
                "results": results,
            }
        )
    return carryover


_SEARCH_LOOP_COUNT_FIELDS = (
    "raw_total_found",
    "total_found",
    "total_selected",
    "total_selected_raw",
    "total_deduped_before_crawl",
    "total_budget_trimmed_before_crawl",
    "total_crawl_attempted",
)
_SEARCH_LOOP_LIST_FIELDS = (
    "filter_details",
    "query_plans",
    "search_query_groups",
)


def _merge_search_loop_attempts(
    base: dict[str, Any],
    addition: dict[str, Any],
    *,
    mark_added_pages_as_social_fallback: bool = False,
) -> dict[str, Any]:
    """合併兩次實際搜尋嘗試，完整保留統計、成功頁與失敗紀錄。"""
    merged = dict(base)
    base_pages = list(base.get("pages") or [])
    added_pages = [dict(page) for page in addition.get("pages") or []]
    if mark_added_pages_as_social_fallback:
        for page in added_pages:
            page["from_social_fallback"] = True
    merged["pages"] = [*base_pages, *added_pages]
    merged["failed"] = [
        *(base.get("failed") or []),
        *(addition.get("failed") or []),
    ]

    for field in _SEARCH_LOOP_COUNT_FIELDS:
        merged[field] = int(base.get(field, 0)) + int(addition.get(field, 0))
    for field in _SEARCH_LOOP_LIST_FIELDS:
        merged[field] = [
            *(base.get(field) or []),
            *(addition.get(field) or []),
        ]

    merged["success"] = bool(merged["pages"]) and (
        bool(base.get("success")) or bool(addition.get("success"))
    )
    errors = [
        str(error).strip()
        for error in (base.get("error"), addition.get("error"))
        if str(error or "").strip()
    ]
    merged["error"] = None if merged["success"] else "; ".join(dict.fromkeys(errors))
    if not merged.get("query_profile"):
        merged["query_profile"] = addition.get("query_profile")
    return merged


# ============================================================
# 模組六：搜尋迴圈與主入口
# ============================================================


async def _run_search_loop(
    *,
    question: str,
    search_queries: list[str],
    search_mode: str,
    loop_label: str,
    step_offset: int,
    results_per_query: int | None,
    llm_config: dict[str, Any],
    search_provider_config: dict[str, Any] | None,
    min_select_per_group: int,
    max_select_per_group: int,
    max_chars_per_page: int,
    crawl_concurrency: int,
    render: str,
    loop_crawl_budget: dict[str, int],
    mon: PipelineMonitor,
    existing_urls: set[str] | None = None,
    tavily_search_depth: str | None = None,
    carryover_query_groups: list[dict[str, Any]] | None = None,
    language: str = "en",
    progress_callback: 研究進度回報器 | None = None,
) -> dict[str, Any]:
    """
    執行一次完整搜尋迴圈（Steps 1-5），返回結構化結果。
    existing_urls: 已有的 URL 集合，用於跨 loop 去重。
    """
    s = step_offset  # step numbering offset

    # ── Step s+1: 多源 API 搜尋 ──
    cfg = SEARCH_MODE_CONFIG.get(search_mode, SEARCH_MODE_CONFIG["web"])
    sources = cfg.get("sources", [])
    effective_min_select = min_select_per_group
    effective_max_select = max_select_per_group
    if (
        min_select_per_group == DEFAULT_MIN_SELECT_PER_GROUP
        and max_select_per_group == DEFAULT_MAX_SELECT_PER_GROUP
    ):
        effective_min_select = int(cfg.get("min_select_per_group", min_select_per_group))
        effective_max_select = int(cfg.get("max_select_per_group", max_select_per_group))
    api_names = ", ".join(s["api"] for s in sources) if sources else "(無)"
    tavily_detail = f", Tavily={tavily_search_depth}" if tavily_search_depth else ""
    mon.step_start(
        s + 1,
        f"{loop_label} 多源 API 搜尋",
        f"模式={search_mode}, API={api_names}{tavily_detail}",
    )

    search_result = await multi_source_search(
        queries=search_queries,
        search_mode=search_mode,
        results_per_query=results_per_query,
        question=question,
        language=language,
        tavily_search_depth=tavily_search_depth,
        search_provider_config=search_provider_config,
    )

    _回報研究進度(
        progress_callback,
        {
            "type": "search_results",
            "stage": "url_judge",
            "queries": [
                {
                    "query": str(group.get("query") or ""),
                    "sources": [_追蹤來源(item) for item in group.get("results", []) if isinstance(item, dict)],
                }
                for group in search_result.get("query_groups", [])
                if isinstance(group, dict)
            ],
        },
    )

    mon.search_results(search_result["query_groups"])
    mon.step_done(s + 1, f"{search_result['total_results']} 條結果")

    if search_result["total_results"] == 0:
        return {
            "success": False,
            "error": "搜尋無結果",
            "pages": [],
            "failed": [],
            "total_found": 0,
            "raw_total_found": int(search_result.get("raw_total_results", 0)),
            "total_selected": 0,
            "total_selected_raw": 0,
            "total_deduped_before_crawl": 0,
            "total_budget_trimmed_before_crawl": 0,
            "total_crawl_attempted": 0,
            "queries": search_queries,
            "search_mode": search_mode,
            "filter_details": [],
            "search_query_groups": [],
            "query_profile": search_result.get("query_profile"),
            "query_plans": [
                {"query": g.get("query", ""), "plan": g.get("plan", {})}
                for g in search_result.get("query_groups", [])
            ],
        }

    # ── Step s+2: LLM 審核篩選 ──
    mon.step_start(
        s + 2,
        f"{loop_label} LLM 審核篩選",
        f"model={llm_config.get('display_model', DEFAULT_FILTER_MODEL)}",
    )
    filter_query_groups = list(search_result["query_groups"])
    if carryover_query_groups:
        filter_query_groups.extend(carryover_query_groups)
        carryover_results = sum(
            len(group.get("results", [])) for group in carryover_query_groups
        )
        mon._print(
            f"  {mon.DIM}{loop_label} URL reviewer 額外讀取上一輪未爬 snippets: "
            f"{len(carryover_query_groups)} 組 / {carryover_results} 條{mon.RESET}"
        )

    filter_result = await llm_filter_results(
        original_question=question,
        query_groups=filter_query_groups,
        search_mode=search_mode,
        llm_config=llm_config,
        min_per_group=effective_min_select,
        max_per_group=effective_max_select,
        max_prompt_results_per_group=min(
            DEFAULT_FILTER_RESULTS_PER_GROUP,
            int(results_per_query or DEFAULT_FILTER_RESULTS_PER_GROUP),
        ),
        monitor=mon,
    )

    mon.step_done(s + 2, f"選出 {filter_result['total_selected']} 條 URL")

    if filter_result["total_selected"] == 0:
        return {
            "success": False,
            "error": "LLM 審核未選出任何結果",
            "pages": [],
            "failed": [],
            "total_found": search_result["total_results"],
            "raw_total_found": int(search_result.get("raw_total_results", search_result["total_results"])),
            "total_selected": 0,
            "total_selected_raw": 0,
            "total_deduped_before_crawl": 0,
            "total_budget_trimmed_before_crawl": 0,
            "total_crawl_attempted": 0,
            "queries": search_queries,
            "search_mode": search_mode,
            "filter_details": [],
            "search_query_groups": search_result.get("query_groups", []),
            "query_profile": search_result.get("query_profile"),
            "query_plans": [
                {"query": g.get("query", ""), "plan": g.get("plan", {})}
                for g in search_result.get("query_groups", [])
            ],
        }

    # ── 跨 loop 去重 ──
    urls_to_crawl = []
    crawl_candidates: list[dict[str, Any]] = []
    for item in filter_result["selected_urls"]:
        item_url = _normalize_url(item["url"])
        if existing_urls and item_url in existing_urls:
            continue
        crawl_candidates.append(item)
    if existing_urls:
        deduped = filter_result["total_selected"] - len(crawl_candidates)
        if deduped > 0:
            mon._print(
                f"  {mon.YELLOW}[DEDUP] 跨 loop 去重: 移除 {deduped} 個已爬 URL{mon.RESET}"
            )
    total_selected_raw = int(filter_result["total_selected"])
    total_selected_after_dedup = len(crawl_candidates)
    total_deduped_before_crawl = max(
        0,
        total_selected_raw - total_selected_after_dedup,
    )
    budget_result = _allocate_loop_crawl_budget(
        crawl_candidates,
        min_total=int(loop_crawl_budget.get("min_total", 0)),
        target_total=int(loop_crawl_budget.get("target_total", 0)),
        max_total=int(loop_crawl_budget.get("max_total", 0)),
    )
    selected_candidates, budget_stats = budget_result
    urls_to_crawl = [item["url"] for item in selected_candidates]
    total_budget_trimmed_before_crawl = int(budget_stats.get("budget_trimmed", 0))

    _回報研究進度(
        progress_callback,
        {
            "type": "url_selection",
            "stage": "crawling",
            "queries": _依查詢分組來源(selected_candidates),
        },
    )

    if total_budget_trimmed_before_crawl > 0:
        mon._print(
            f"  {mon.YELLOW}[BUDGET] {loop_label} 候選 {budget_stats['candidate_count']} → "
            f"實際深爬 {budget_stats['selected_count']} "
            f"(目標 {budget_stats['target_total']}, 上限 {budget_stats['max_total']}){mon.RESET}"
        )

    if not urls_to_crawl:
        return {
            "success": True,
            "error": None,
            "pages": [],
            "failed": [],
            "total_found": search_result["total_results"],
            "raw_total_found": int(search_result.get("raw_total_results", search_result["total_results"])),
            "total_selected": len(urls_to_crawl),
            "total_selected_raw": total_selected_raw,
            "total_deduped_before_crawl": total_deduped_before_crawl,
            "total_budget_trimmed_before_crawl": total_budget_trimmed_before_crawl,
            "total_crawl_attempted": 0,
            "queries": search_queries,
            "search_mode": search_mode,
            "filter_details": filter_result.get("selection_details", []),
            "search_query_groups": search_result.get("query_groups", []),
            "query_profile": search_result.get("query_profile"),
            "query_plans": [
                {"query": g.get("query", ""), "plan": g.get("plan", {})}
                for g in search_result.get("query_groups", [])
            ],
        }


    # ── Step s+3: 批次深爬 ──
    mon.step_start(
        s + 3, f"{loop_label} 批次深爬", f"{len(urls_to_crawl)} URL, render={render}"
    )

    crawled = await batch_deep_crawl(
        urls=urls_to_crawl,
        max_chars_per_page=max_chars_per_page,
        max_concurrency=crawl_concurrency,
        render=render,
        monitor=mon,
    )

    # 附加來源資訊
    url_to_src = {
        _normalize_url(item["url"]): item for item in selected_candidates
    }
    for page in crawled:
        src = url_to_src.get(_normalize_url(page.get("url", "")))
        if src:
            page["from_query"] = src.get("from_query", "")
            page["_group_index"] = src.get("group_index")
            page["_result_index"] = src.get("result_index")
            if not page.get("title"):
                page["title"] = src.get("title", "")

    success_count = sum(1 for p in crawled if p.get("success"))
    mon.step_done(s + 3, f"成功 {success_count}/{len(crawled)} 頁")
    _回報研究進度(
        progress_callback,
        {"type": "crawl_complete", "stage": "chunk_judge"},
    )

    # ── Step s+4: 位置策略排序 ──
    successful = [p for p in crawled if p.get("success")]
    failed = [p for p in crawled if not p.get("success")]

    mon.step_start(s + 4, f"{loop_label} 位置排序")

    sorted_pages = _apply_position_strategy(
        successful,
        filter_result.get("selection_details", []),
    )
    mon.step_done(s + 4, f"{len(sorted_pages)} 頁已排序")

    # 清理內部標記
    for page in sorted_pages + failed:
        page.pop("_group_index", None)
        page.pop("_result_index", None)

    return {
        "success": True,
        "error": None,
        "pages": sorted_pages,
        "failed": failed,
        "total_found": search_result["total_results"],
        "raw_total_found": int(search_result.get("raw_total_results", search_result["total_results"])),
        "total_selected": len(urls_to_crawl),
        "total_selected_raw": total_selected_raw,
        "total_deduped_before_crawl": total_deduped_before_crawl,
        "total_budget_trimmed_before_crawl": total_budget_trimmed_before_crawl,
        "total_crawl_attempted": len(crawled),
        "queries": search_queries,
        "search_mode": search_mode,
        "filter_details": filter_result.get("selection_details", []),
        "search_query_groups": search_result.get("query_groups", []),
        "filter_query_group_count": len(filter_query_groups),
        "query_profile": search_result.get("query_profile"),
        "query_plans": [
            {"query": g.get("query", ""), "plan": g.get("plan", {})}
            for g in search_result.get("query_groups", [])
        ],
    }


def _build_content_map(
    loop1_pages: list[dict],
    loop1_queries: list[str],
    loop1_mode: str,
    loop2_pages: list[dict] | None = None,
    loop2_queries: list[str] | None = None,
    loop2_mode: str | None = None,
    loop2_reason: str | None = None,
) -> dict[str, Any]:
    """建構 content_map 導航圖（只有方向標籤，不含結論）。"""

    def _page_guide(pages: list[dict]) -> list[dict]:
        return [
            {
                "index": i,
                "url": page.get("url", ""),
                "title": page.get("title", ""),
                # 保留舊版導航契約；已移除的 Covers 管線不再生成內容。
                "covers": [],
            }
            for i, page in enumerate(pages)
        ]

    cmap: dict[str, Any] = {
        "loop_1": {
            "search_mode": loop1_mode,
            "queries": loop1_queries,
            "page_count": len(loop1_pages),
            "page_guide": _page_guide(loop1_pages),
        },
    }

    if loop2_pages is not None:
        cmap["loop_2"] = {
            "search_mode": loop2_mode or "web",
            "queries": loop2_queries or [],
            "triggered_by": loop2_reason or "",
            "page_count": len(loop2_pages),
            "page_guide": _page_guide(loop2_pages),
        }

    return cmap


def _citation_protocol() -> dict[str, Any]:
    """回傳給最終回答 LLM 的引用規則，不承載任何答案型摘要。"""
    return {
        "format": "[citation](source_index:citation_id)",
        "source_of_truth": "只以 evidence_bundle[].chunks[].text 中已篩選過的引用內容作為回答事實來源。",
        "usage": "每個關鍵事實句後緊貼使用該頁的 citation_marker，不要把引用集中在文末。",
        "answering_policy": "拿到什麼證據就說什麼；不要使用搜尋 snippets、未選全文或常識推測去補足缺口。",
        "not_fact_sources": [
            "search_results_summary",
            "query_profile",
            "query_plans",
            "content_map",
            "source_registry",
            "filter_details",
            "chunk_filter",
            "research_trace",
        ],
        "kelivo_note": "citation_id 使用 URL 型 id，供 Kelivo 現有 citation fallback 直接開啟來源。",
    }


def _answer_guidance(mode: str, verdict: str | None) -> dict[str, Any]:
    """給外側回答模型的回答策略提示，不包含任何事實摘要。"""
    guidance = {
        "policy": "拿到什麼證據就說什麼，不要使用搜尋 snippets、未選全文或推測補足缺口。",
    }
    if mode == "instant":
        guidance["mode_note"] = (
            "instant 模式只執行單輪搜尋與 3~5 篇深爬，目標是取得剛好足夠回答淺層答案的證據。"
        )
    if mode == "instant" and verdict == "insufficient":
        guidance["quality_notice"] = (
            "搜尋來源資料不足，必須提醒用戶回答質量有可議之處，應斟酌取用。"
        )
    return guidance


def _quote_citation_component(value: str, *, safe: str) -> str:
    """保守編碼 citation id 內會破壞 Markdown 或 Kelivo split 的字元。"""
    return quote(value or "", safe=safe)


def _citation_id_from_url(url: str) -> str:
    """將 URL 轉為 Kelivo 目前可 fallback 開啟的 protocol-relative citation id。"""
    normalized = _normalize_url(url)
    if not normalized:
        return ""

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        # 非 http(s) 來源直接保守編碼，避免破壞 [citation](index:id)。
        return _quote_citation_component(normalized, safe="/%?&=+;,._~-")

    netloc = _quote_citation_component(parsed.netloc, safe="[]@%._~-")
    path = _quote_citation_component(parsed.path or "/", safe="/%._~-")
    citation_id = f"//{netloc}{path}"
    if parsed.query:
        query = _quote_citation_component(parsed.query, safe="=&%/?+;,._~-")
        citation_id += f"?{query}"
    if parsed.fragment:
        fragment = _quote_citation_component(parsed.fragment, safe="=&%/?+;,._~-")
        citation_id += f"#{fragment}"
    return citation_id


def _attach_citation_metadata(
    crawled_pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """只為成功頁附加引用索引；不改寫、不裁剪 crawled_pages[].content。"""
    registry: list[dict[str, Any]] = []

    for idx, page in enumerate(crawled_pages, start=1):
        citation_id = _citation_id_from_url(page.get("url", ""))
        if not citation_id:
            continue
        marker = f"[citation]({idx}:{citation_id})"
        page["source_index"] = idx
        page["citation_id"] = citation_id
        page["citation_marker"] = marker

        registry_item = {
            "source_index": idx,
            "citation_id": citation_id,
            "citation_marker": marker,
            "url": page.get("url", ""),
            "title": page.get("title", ""),
        }
        if page.get("from_query"):
            registry_item["from_query"] = page.get("from_query")
        if page.get("loop"):
            registry_item["loop"] = page.get("loop")
        if page.get("from_social_fallback"):
            registry_item["from_social_fallback"] = True
        registry.append(registry_item)

    return registry


def _invalid_search_result(
    *,
    mode: str,
    question: Any,
    search_queries: Any,
    search_mode: str,
    error: str,
) -> dict[str, Any]:
    """建立所有 Python 入口參數錯誤共用的固定失敗契約。"""
    return {
        "success": False,
        "mode": mode,
        "question": question if isinstance(question, str) else "",
        "search_queries": search_queries if isinstance(search_queries, list) else [],
        "search_mode": search_mode,
        "error": error,
        "completion_state": "failed",
        "judge_success": None,
        "search_results_summary": _empty_search_summary(),
        "query_profile": None,
        "query_plans": {"loop_1": []},
        "elapsed_ms": 0,
        "citation_protocol": _citation_protocol(),
        "answer_guidance": _answer_guidance(mode, None),
        "source_registry": [],
        "evidence_bundle": [],
        "selected_chunks": [],
        "chunk_filter": _chunk_filter_summary([]),
        "content_map": {},
        "research_trace": {
            "raw_crawled_pages": 0,
            "evidence_pages": 0,
            "failed_pages": 0,
            "loops_executed": 0,
        },
        "crawled_pages": [],
        "failed_pages": [],
        "judge_result": {
            "success": False,
            "verdict": None,
            "gap_analysis": "",
            "coverage": {},
            "error": "invalid_input",
        },
        "social_fallback": {
            "triggered": False,
            "queries": [],
            "added_pages": 0,
        },
    }


async def deep_search(
    question: str,
    search_queries: list[str],
    search_mode: str = "web",
    mode: str = "fast",
    model: str = DEFAULT_MODEL_ROUTE,
    # 可選參數
    results_per_query: int | None = None,
    filter_model: str | None = None,
    judge_model_config: dict[str, Any] | None = None,
    search_provider_config: dict[str, Any] | None = None,
    min_select_per_group: int = DEFAULT_MIN_SELECT_PER_GROUP,
    max_select_per_group: int = DEFAULT_MAX_SELECT_PER_GROUP,
    max_chars_per_page: int = MAX_CHARS_PER_PAGE,
    crawl_concurrency: int = MAX_CRAWL_CONCURRENCY,
    render: str = "auto",
    verbose: bool = True,
    language: str = "en",
    progress_callback: 研究進度回報器 | None = None,
    prior_evidence_chunks: list[dict[str, Any]] | None = None,
    refresh_urls: list[str] | None = None,
    excluded_urls: list[str] | None = None,
) -> dict[str, Any]:
    """
    深度搜尋管線 V3 — 使用 Brave/Tavily/Exa/SerpApi 多源搜尋。

    Args:
        question: 用戶原始問題
        search_queries: 搜尋字詞列表（建議 3 組）
        search_mode: "web" | "academic" | "social"
        mode: "instant"（單輪快速取證）| "fast"（最多兩輪，第二輪輕量補洞）| "full"（reviewer 審核 + 可選補搜迴圈）
        model: LLM 路線選擇，"d"=DeepSeek V4 Flash（預設，OpenRouter reasoning 關閉）、"g"=Gemini 3 Flash
        results_per_query: 若提供，會全域覆蓋每個搜尋引擎對每組查詢返回的筆數；instant 模式固定使用 10
        filter_model: 若提供，覆蓋所選路線的實際模型名稱
        judge_model_config: Web 應用提供的 Judge 模型與 API 設定
        search_provider_config: Web 應用提供的 SearXNG、內建與自定義搜尋引擎設定
        min_select_per_group: 每組最少選擇數
        max_select_per_group: 每組最多選擇數
        max_chars_per_page: 每頁深爬最大字元數
        crawl_concurrency: 爬取最大並行數
        render: 爬取渲染策略 "auto" | "never" | "always"
        verbose: 是否在終端即時顯示管線 I/O
        language: 搜尋 Provider 使用的語言提示
        progress_callback: 提供給 Web UI 的非阻塞研究進度回報
        prior_evidence_chunks: 前一輪已驗證 chunks；只提供 Judge 辨識既有覆蓋與缺口
        refresh_urls: 已驗證舊來源中需要重新爬取的 URL；最多兩個，與首輪搜尋並行
        excluded_urls: 已於本輪直接爬取的 URL；搜尋結果若命中時跳過重複深爬

    Returns:
        包含完整搜尋結果的字典
    """
    raw_question = question if isinstance(question, str) else ""
    raw_search_queries = search_queries if isinstance(search_queries, list) else []
    normalized_search_mode = normalize_search_mode(search_mode)
    normalized_mode = normalize_execution_mode(mode)
    try:
        render = _validate_search_options(
            search_mode=search_mode,
            mode=mode,
            model=model,
            results_per_query=results_per_query,
            filter_model=filter_model,
            judge_model_config=judge_model_config,
            search_provider_config=search_provider_config,
            min_select_per_group=min_select_per_group,
            max_select_per_group=max_select_per_group,
            max_chars_per_page=max_chars_per_page,
            crawl_concurrency=crawl_concurrency,
            render=render,
            verbose=verbose,
        )
        question, search_queries = validate_search_inputs(question, search_queries)
    except ValueError as exc:
        return _invalid_search_result(
            mode=normalized_mode,
            question=raw_question,
            search_queries=raw_search_queries,
            search_mode=normalized_search_mode,
            error=str(exc),
        )

    search_mode = normalized_search_mode
    mode = normalized_mode
    model = normalize_model_route(model)
    requested_refresh_urls = _bounded_unique_urls(
        refresh_urls,
        limit=REFRESH_URL_LIMIT,
    )
    crawl_excluded_urls = {
        _normalize_url(url)
        for url in _bounded_unique_urls(excluded_urls, limit=DIRECT_URL_LIMIT)
        if _normalize_url(url)
    }
    crawl_excluded_urls.update(
        _normalize_url(url)
        for url in requested_refresh_urls
        if _normalize_url(url)
    )

    mon = PipelineMonitor(enabled=verbose)
    _monitor_context.set(mon)
    pipeline_start = time.time()

    mon.pipeline_start(question, search_queries, f"{search_mode} / {mode}")
    llm_config = _build_llm_route_config(model, filter_model, judge_model_config)

    effective_results_per_query = (
        INSTANT_RESULTS_PER_QUERY
        if mode == "instant"
        else results_per_query
    )

    # 共用的 loop 參數
    loop_kwargs = dict(
        question=question,
        results_per_query=effective_results_per_query,
        llm_config=llm_config,
        search_provider_config=search_provider_config,
        min_select_per_group=min_select_per_group,
        max_select_per_group=max_select_per_group,
        max_chars_per_page=max_chars_per_page,
        crawl_concurrency=crawl_concurrency,
        render=render,
        mon=mon,
        language=language,
        progress_callback=progress_callback,
    )
    if mode == "instant":
        loop_crawl_budget = dict(INSTANT_LOOP_CRAWL_BUDGET)
    elif mode == "fast":
        loop_crawl_budget = dict(FAST_LOOP_CRAWL_BUDGET)
    else:
        loop_crawl_budget = dict(FULL_LOOP_CRAWL_BUDGET)

    # =============================================
    # Loop 1
    # =============================================
    refresh_task: asyncio.Task[list[dict[str, Any]]] | None = None
    if requested_refresh_urls:
        mon._print(
            f"  {mon.DIM}[REFRESH] 與 Loop 1 並行重讀 "
            f"{len(requested_refresh_urls)} 個已驗證來源{mon.RESET}"
        )
        refresh_task = asyncio.create_task(
            batch_deep_crawl(
                requested_refresh_urls,
                max_chars_per_page=max_chars_per_page,
                max_concurrency=min(REFRESH_URL_LIMIT, crawl_concurrency),
                render=render,
                monitor=mon,
            )
        )
    try:
        loop1 = await _run_search_loop(
            search_queries=search_queries,
            search_mode=search_mode,
            loop_label="[Loop 1]",
            step_offset=0,
            loop_crawl_budget=loop_crawl_budget,
            tavily_search_depth="fast",
            existing_urls=crawl_excluded_urls or None,
            **loop_kwargs,
        )
    except Exception:
        if refresh_task is not None:
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await refresh_task
        raise

    refreshed_pages: list[dict[str, Any]] = []
    refresh_failed: list[dict[str, Any]] = []
    if refresh_task is not None:
        try:
            refreshed = await refresh_task
        except Exception as exc:
            refreshed = []
            mon._print(f"  {mon.YELLOW}[REFRESH] 重讀失敗：{type(exc).__name__}{mon.RESET}")
        existing_page_urls = {
            _normalize_url(page.get("url", ""))
            for page in loop1.get("pages", [])
            if _normalize_url(page.get("url", ""))
        }
        for page in refreshed:
            item = dict(page)
            item["from_query"] = "Refreshed verified source"
            normalized = _normalize_url(item.get("url", ""))
            if item.get("success") and normalized and normalized not in existing_page_urls:
                existing_page_urls.add(normalized)
                refreshed_pages.append(item)
            elif not item.get("success"):
                refresh_failed.append(item)
        if refreshed_pages:
            loop1["pages"] = [*refreshed_pages, *loop1.get("pages", [])]
            loop1["success"] = True
            loop1["error"] = None
        if refresh_failed:
            loop1["failed"] = [*loop1.get("failed", []), *refresh_failed]

    social_fallback: dict[str, Any] | None = None
    social_fallback_queries: list[str] = []
    social_fallback_triggered = False
    social_fallback_executed = False
    initial_loop1_failed = not loop1.get("success") or not loop1.get("pages")

    if mode != "instant" and search_mode == "social" and (
        initial_loop1_failed or _should_trigger_social_site_fallback(loop1)
    ):
        social_fallback_queries = _build_social_site_fallback_queries(search_queries)
        if social_fallback_queries:
            social_fallback_triggered = True
            mon._print(f"  {mon.YELLOW}[SOCIAL] 啟用 site: 補搜{mon.RESET}")
            mon._print(f"  {mon.DIM}queries: {social_fallback_queries}{mon.RESET}")

            existing_urls = set(crawl_excluded_urls) | {
                _normalize_url(p["url"])
                for p in loop1.get("pages", [])
                if _normalize_url(p.get("url", ""))
            }
            social_remaining_budget = {
                "min_total": max(
                    0,
                    int(loop_crawl_budget.get("min_total", 0))
                    - int(loop1.get("total_selected", 0)),
                ),
                "target_total": max(
                    0,
                    int(loop_crawl_budget.get("target_total", 0))
                    - int(loop1.get("total_selected", 0)),
                ),
                "max_total": max(
                    0,
                    int(loop_crawl_budget.get("max_total", 0))
                    - int(loop1.get("total_selected", 0)),
                ),
            }
            if int(social_remaining_budget.get("max_total", 0)) > 0:
                social_fallback_executed = True
                social_fallback = await _run_search_loop(
                    question=question,
                    search_queries=social_fallback_queries,
                    search_mode="web",
                    loop_label="[Social Fallback]",
                    step_offset=4,
                    results_per_query=results_per_query,
                    llm_config=llm_config,
                    search_provider_config=search_provider_config,
                    min_select_per_group=min_select_per_group,
                    max_select_per_group=max_select_per_group,
                    max_chars_per_page=max_chars_per_page,
                    crawl_concurrency=crawl_concurrency,
                    render=render,
                    loop_crawl_budget=social_remaining_budget,
                    mon=mon,
                    existing_urls=existing_urls,
                    tavily_search_depth="fast",
                    language=language,
                    progress_callback=progress_callback,
                )
                loop1 = _merge_search_loop_attempts(
                    loop1,
                    social_fallback,
                    mark_added_pages_as_social_fallback=True,
                )
            else:
                mon._print(
                    f"  {mon.DIM}[SOCIAL] 目前 loop 預算已滿，跳過 site: 補搜深爬{mon.RESET}"
                )

    if not loop1.get("success") or not loop1.get("pages"):
        elapsed = int((time.time() - pipeline_start) * 1000)
        summary = _empty_search_summary()
        summary["raw_total_found"] = int(loop1.get("raw_total_found", loop1.get("total_found", 0)))
        summary["total_found"] = int(loop1.get("total_found", 0))
        summary["total_selected"] = int(loop1.get("total_selected", 0))
        summary["total_selected_raw"] = int(loop1.get("total_selected_raw", 0))
        summary["total_deduped_before_crawl"] = int(
            loop1.get("total_deduped_before_crawl", 0)
        )
        summary["total_budget_trimmed_before_crawl"] = int(
            loop1.get("total_budget_trimmed_before_crawl", 0)
        )
        summary["total_crawl_attempted"] = int(
            loop1.get("total_crawl_attempted", 0)
        )
        summary["total_crawled_success"] = len(loop1.get("pages", []))
        summary["total_crawled_failed"] = len(loop1.get("failed", []))
        summary["loops_executed"] = 1 + (1 if social_fallback_executed else 0)
        return {
            "success": False,
            "mode": mode,
            "question": question,
            "search_queries": search_queries,
            "search_mode": search_mode,
            "error": loop1.get("error", "Loop 1 failed"),
            "completion_state": "failed",
            "judge_success": None,
            "social_fallback": {
                "triggered": social_fallback_triggered,
                "queries": social_fallback_queries,
                "added_pages": len(social_fallback.get("pages", []))
                if social_fallback
                else 0,
            },
            "search_results_summary": summary,
            "query_profile": loop1.get("query_profile"),
            "query_plans": {"loop_1": loop1.get("query_plans", [])},
            "elapsed_ms": elapsed,
            "citation_protocol": _citation_protocol(),
            "answer_guidance": _answer_guidance(mode, None),
            "source_registry": [],
            "evidence_bundle": [],
            "selected_chunks": [],
            "chunk_filter": _chunk_filter_summary([]),
            "content_map": {},
            "research_trace": {
                "raw_crawled_pages": len(loop1.get("pages", [])),
                "evidence_pages": 0,
                "failed_pages": len(loop1.get("failed", [])),
                "loops_executed": 1 + (1 if social_fallback_executed else 0),
            },
            "crawled_pages": [],
            "failed_pages": loop1.get("failed", []),
            "judge_result": {
                "success": False,
                "verdict": None,
                "gap_analysis": "",
                "coverage": {},
                "error": "search_loop_failed",
            },
        }

    # =============================================
    # Loop 1 chunk reviewer：深爬全文只在內部審核，外側只拿 selected chunks
    # =============================================
    review_step = 9 if social_fallback_executed else 5
    for page in loop1["pages"]:
        page["loop"] = 1

    mon.step_start(
        review_step,
        "Loop 1 Chunk Reviewer",
        "選 evidence chunks + sufficiency judge",
    )
    loop1_review = await review_crawled_chunks(
        question=question,
        search_queries=search_queries,
        search_mode=search_mode,
        execution_mode=mode,
        pages=loop1["pages"],
        round_number=1,
        llm_config=llm_config,
        mon=mon,
        prior_evidence_chunks=prior_evidence_chunks,
        final_round=(mode == "instant"),
    )
    _回報回答證據(progress_callback, loop1_review, 1)
    mon.step_done(
        review_step,
        f"verdict={loop1_review.get('verdict')} / chunks={len(loop1_review.get('selected_chunks', []))}",
    )

    review_results: list[dict[str, Any]] = [loop1_review]
    selected_chunks: list[dict[str, Any]] = list(loop1_review.get("selected_chunks", []))
    all_pages: list[dict[str, Any]] = list(loop1.get("pages", []))
    all_failed: list[dict[str, Any]] = list(loop1.get("failed", []))
    loop2: dict[str, Any] | None = None
    loop3: dict[str, Any] | None = None
    loop2_queries: list[str] = []
    loop3_queries: list[str] = []
    loop2_mode: str | None = None
    loop3_mode: str | None = None
    loop2_executed = False
    loop3_executed = False

    final_review = loop1_review

    if mode == "fast" and loop1_review.get("verdict") == "insufficient":
        loop2_queries = _resolve_reviewer_next_queries(loop1_review, search_queries)
        loop2_mode = normalize_search_mode(loop1_review.get("search_mode") or search_mode)
        attempted_loop_urls = set(crawl_excluded_urls) | {
            _normalize_url(p.get("url", ""))
            for p in [*all_pages, *all_failed]
            if _normalize_url(p.get("url", ""))
        }
        carryover_groups = _build_carryover_query_groups(
            loop1.get("search_query_groups", []),
            exclude_urls=attempted_loop_urls,
            label="Loop 1 未爬候選",
        )
        mon._print(f"\n  {mon.CYAN}[FAST] Loop 1 不足，觸發 Fast Loop 2 輕量補洞{mon.RESET}")
        mon._print(f"  {mon.CYAN}  搜尋模式: {loop2_mode}{mon.RESET}")
        mon._print(f"  {mon.CYAN}  補搜字詞: {loop2_queries}{mon.RESET}")
        if carryover_groups:
            mon._print(
                f"  {mon.CYAN}  一併提供 Loop 1 未爬 snippets: {len(carryover_groups)} 組{mon.RESET}"
            )

        fast_loop2_kwargs = dict(loop_kwargs)
        fast_loop2_kwargs["results_per_query"] = min(
            int(results_per_query or FAST_SECOND_LOOP_RESULTS_PER_QUERY),
            FAST_SECOND_LOOP_RESULTS_PER_QUERY,
        )
        fast_loop2_kwargs["min_select_per_group"] = FAST_SECOND_LOOP_MIN_SELECT_PER_GROUP
        fast_loop2_kwargs["max_select_per_group"] = FAST_SECOND_LOOP_MAX_SELECT_PER_GROUP

        loop2_executed = True
        loop2 = await _run_search_loop(
            search_queries=loop2_queries,
            search_mode=loop2_mode,
            loop_label="[Fast Loop 2]",
            step_offset=review_step,
            existing_urls=attempted_loop_urls,
            loop_crawl_budget=FAST_SECOND_LOOP_CRAWL_BUDGET,
            tavily_search_depth="fast",
            carryover_query_groups=carryover_groups,
            **fast_loop2_kwargs,
        )
        if loop2.get("success") and loop2.get("pages"):
            for page in loop2["pages"]:
                page["loop"] = 2
            all_pages.extend(loop2.get("pages", []))
            all_failed.extend(loop2.get("failed", []))

            loop2_review_step = review_step + 5
            mon.step_start(
                loop2_review_step,
                "Fast Loop 2 Chunk Reviewer",
                "讀本輪 chunks + 上輪 selected evidence 做最後 judge",
            )
            loop2_review = await review_crawled_chunks(
                question=question,
                search_queries=loop2_queries,
                search_mode=loop2_mode,
                execution_mode=mode,
                pages=loop2["pages"],
                round_number=2,
                llm_config=llm_config,
                mon=mon,
                prior_evidence_chunks=selected_chunks,
                missing_focus=_review_missing_focus(loop1_review),
                final_round=True,
            )
            _回報回答證據(progress_callback, loop2_review, 2)
            mon.step_done(
                loop2_review_step,
                f"verdict={loop2_review.get('verdict')} / chunks={len(loop2_review.get('selected_chunks', []))}",
            )
            review_results.append(loop2_review)
            selected_chunks.extend(loop2_review.get("selected_chunks", []))
            final_review = loop2_review
        elif loop2:
            all_failed.extend(loop2.get("failed", []))

    if mode == "full" and loop1_review.get("verdict") == "insufficient":
        loop2_queries = _resolve_reviewer_next_queries(loop1_review, search_queries)
        loop2_mode = normalize_search_mode(loop1_review.get("search_mode") or search_mode)
        mon._print(f"\n  {mon.CYAN}[FULL] Loop 1 不足，觸發 Loop 2 補洞{mon.RESET}")
        mon._print(f"  {mon.CYAN}  搜尋模式: {loop2_mode}{mon.RESET}")
        mon._print(f"  {mon.CYAN}  補搜字詞: {loop2_queries}{mon.RESET}")

        loop2_executed = True
        loop2 = await _run_search_loop(
            search_queries=loop2_queries,
            search_mode=loop2_mode,
            loop_label="[Loop 2]",
            step_offset=review_step,
            existing_urls=set(crawl_excluded_urls) | {
                _normalize_url(p.get("url", ""))
                for p in all_pages
                if _normalize_url(p.get("url", ""))
            },
            loop_crawl_budget=FULL_LOOP_CRAWL_BUDGET,
            tavily_search_depth="fast",
            **loop_kwargs,
        )
        if loop2.get("success") and loop2.get("pages"):
            for page in loop2["pages"]:
                page["loop"] = 2
            all_pages.extend(loop2.get("pages", []))
            all_failed.extend(loop2.get("failed", []))

            loop2_review_step = review_step + 5
            mon.step_start(
                loop2_review_step,
                "Loop 2 Chunk Reviewer",
                "讀本輪 chunks + 上輪 selected evidence 做 judge",
            )
            loop2_review = await review_crawled_chunks(
                question=question,
                search_queries=loop2_queries,
                search_mode=loop2_mode,
                execution_mode=mode,
                pages=loop2["pages"],
                round_number=2,
                llm_config=llm_config,
                mon=mon,
                prior_evidence_chunks=selected_chunks,
                missing_focus=_review_missing_focus(loop1_review),
            )
            _回報回答證據(progress_callback, loop2_review, 2)
            mon.step_done(
                loop2_review_step,
                f"verdict={loop2_review.get('verdict')} / chunks={len(loop2_review.get('selected_chunks', []))}",
            )
            review_results.append(loop2_review)
            selected_chunks.extend(loop2_review.get("selected_chunks", []))
            final_review = loop2_review
        elif loop2:
            all_failed.extend(loop2.get("failed", []))

    if mode == "full" and final_review.get("verdict") == "insufficient":
        loop3_queries = _resolve_reviewer_next_queries(
            final_review,
            loop2_queries or search_queries,
            max_queries=2,
        )
        loop3_mode = normalize_search_mode(final_review.get("search_mode") or loop2_mode or search_mode)
        mon._print(f"\n  {mon.CYAN}[FULL] Loop 2 仍不足，觸發最後窄補洞 Loop 3{mon.RESET}")
        mon._print(f"  {mon.CYAN}  搜尋模式: {loop3_mode}{mon.RESET}")
        mon._print(f"  {mon.CYAN}  補搜字詞: {loop3_queries}{mon.RESET}")

        loop3_kwargs = dict(loop_kwargs)
        loop3_kwargs["results_per_query"] = min(int(results_per_query or 8), 8)
        loop3_executed = True
        loop3 = await _run_search_loop(
            search_queries=loop3_queries,
            search_mode=loop3_mode,
            loop_label="[Loop 3 Narrow]",
            step_offset=review_step + 10,
            existing_urls=set(crawl_excluded_urls) | {
                _normalize_url(p.get("url", ""))
                for p in all_pages
                if _normalize_url(p.get("url", ""))
            },
            loop_crawl_budget=FINAL_NARROW_CRAWL_BUDGET,
            tavily_search_depth="fast",
            **loop3_kwargs,
        )
        if loop3.get("success") and loop3.get("pages"):
            for page in loop3["pages"]:
                page["loop"] = 3
            all_pages.extend(loop3.get("pages", []))
            all_failed.extend(loop3.get("failed", []))

            loop3_review_step = review_step + 15
            mon.step_start(
                loop3_review_step,
                "Loop 3 Chunk Reviewer",
                "最後窄補洞，不再要求下一輪搜尋",
            )
            loop3_review = await review_crawled_chunks(
                question=question,
                search_queries=loop3_queries,
                search_mode=loop3_mode,
                execution_mode=mode,
                pages=loop3["pages"],
                round_number=3,
                llm_config=llm_config,
                mon=mon,
                prior_evidence_chunks=selected_chunks,
                missing_focus=_review_missing_focus(final_review),
                final_round=True,
            )
            _回報回答證據(progress_callback, loop3_review, 3)
            mon.step_done(
                loop3_review_step,
                f"verdict={loop3_review.get('verdict')} / chunks={len(loop3_review.get('selected_chunks', []))}",
            )
            review_results.append(loop3_review)
            selected_chunks.extend(loop3_review.get("selected_chunks", []))
            final_review = loop3_review
        elif loop3:
            all_failed.extend(loop3.get("failed", []))

    evidence_pages, source_registry, evidence_bundle, flat_selected_chunks = _build_evidence_outputs(
        all_pages,
        selected_chunks,
    )
    chunk_filter = _chunk_filter_summary(review_results)

    content_map = _build_content_map(
        loop1_pages=loop1["pages"],
        loop1_queries=search_queries,
        loop1_mode=search_mode,
        loop2_pages=loop2["pages"] if loop2 and loop2.get("pages") else None,
        loop2_queries=loop2_queries,
        loop2_mode=loop2_mode,
        loop2_reason=loop1_review.get("gap_analysis"),
    )
    if loop3 and loop3.get("pages"):
        content_map["loop_3"] = {
            "search_mode": loop3_mode or "web",
            "queries": loop3_queries,
            "triggered_by": final_review.get("gap_analysis", ""),
            "page_count": len(loop3.get("pages", [])),
            "page_guide": [
                {
                    "index": i,
                    "url": page.get("url", ""),
                    "title": page.get("title", ""),
                    "covers": [],
                }
                for i, page in enumerate(loop3.get("pages", []))
            ],
        }

    loop_results = [loop1]
    if loop2:
        loop_results.append(loop2)
    if loop3:
        loop_results.append(loop3)

    elapsed = int((time.time() - pipeline_start) * 1000)
    summary = {
        "raw_total_found": sum(
            int(loop.get("raw_total_found", loop.get("total_found", 0)))
            for loop in loop_results
        ),
        "total_found": sum(int(loop.get("total_found", 0)) for loop in loop_results),
        "total_selected": sum(int(loop.get("total_selected", 0)) for loop in loop_results),
        "total_selected_raw": sum(int(loop.get("total_selected_raw", 0)) for loop in loop_results),
        "total_deduped_before_crawl": sum(
            int(loop.get("total_deduped_before_crawl", 0)) for loop in loop_results
        ),
        "total_budget_trimmed_before_crawl": sum(
            int(loop.get("total_budget_trimmed_before_crawl", 0)) for loop in loop_results
        ),
        "total_crawl_attempted": sum(int(loop.get("total_crawl_attempted", 0)) for loop in loop_results),
        "total_crawled_success": len(all_pages),
        "total_crawled_failed": len(all_failed),
        "refreshed_sources_requested": len(requested_refresh_urls),
        "refreshed_sources_success": len(refreshed_pages),
        "total_chunks_seen": int(chunk_filter.get("total_chunks", 0)),
        "total_chunks_prompted": int(chunk_filter.get("prompted_chunks", 0)),
        "total_chunks_selected": int(chunk_filter.get("selected_chunks", 0)),
        "loops_executed": 1
        + (1 if social_fallback_executed else 0)
        + (1 if loop2_executed else 0)
        + (1 if loop3_executed else 0),
        "judge_verdict": final_review.get("verdict"),
    }

    mon.pipeline_done(summary, elapsed)
    judge_success = all(bool(result.get("success")) for result in review_results)
    completion_state = (
        "complete"
        if evidence_pages
        and final_review.get("verdict") == "sufficient"
        and judge_success
        else "partial"
        if evidence_pages
        else "failed"
    )

    result_payload = {
        "success": bool(evidence_pages),
        "mode": mode,
        "question": question,
        "search_queries": search_queries,
        "search_mode": search_mode,
        "completion_state": completion_state,
        "judge_success": judge_success,
        "search_results_summary": summary,
        "query_profile": loop1.get("query_profile"),
        "query_plans": {
            "loop_1": loop1.get("query_plans", []),
            "loop_2": loop2.get("query_plans", []) if loop2 else [],
            "loop_3": loop3.get("query_plans", []) if loop3 else [],
        },
        "elapsed_ms": elapsed,
        "citation_protocol": _citation_protocol(),
        "answer_guidance": _answer_guidance(mode, final_review.get("verdict")),
        "source_registry": source_registry,
        "content_map": content_map,
        "evidence_bundle": evidence_bundle,
        "selected_chunks": flat_selected_chunks,
        "chunk_filter": chunk_filter,
        "research_trace": {
            "raw_crawled_pages": len(all_pages),
            "evidence_pages": len(evidence_pages),
            "failed_pages": len(all_failed),
            "refreshed_sources": len(refreshed_pages),
            "loops_executed": summary["loops_executed"],
        },
        "crawled_pages": evidence_pages,
        "failed_pages": all_failed,
        "judge_result": {
            "success": judge_success,
            "verdict": final_review.get("verdict"),
            "gap_analysis": final_review.get("gap_analysis", ""),
            "coverage": final_review.get("coverage", {}),
            "error": None if judge_success else "chunk_reviewer_fallback_or_parse_error",
        },
        "social_fallback": {
            "triggered": social_fallback_triggered,
            "queries": social_fallback_queries,
            "added_pages": len(social_fallback.get("pages", []))
            if social_fallback
            else 0,
        },
    }
    if mode == "fast":
        result_payload["filter_details"] = {
            "loop_1": loop1.get("filter_details", []),
            "loop_2": loop2.get("filter_details", []) if loop2 else [],
        }
    return result_payload


# ============================================================
# 測試入口
# ============================================================


async def _test_search_only():
    """測試多源 API 搜尋模組。"""
    print("=" * 60)
    print("測試多源 API 搜尋模組")
    print("=" * 60)

    for mode in ["web", "academic", "social"]:
        print(f"\n--- 模式: {mode} ---")
        result = await multi_source_search(
            queries=["Python asyncio"],
            search_mode=mode,
        )
        print(f"  結果數: {result['total_results']}")
        for group in result["query_groups"]:
            print(f"  查詢: {group['query']}")
            for r in group["results"][:3]:
                print(f"    - {r['title'][:60]}  [{r.get('engine', '?')}]")


async def _test_full_pipeline():
    """測試完整管線。"""
    print("=" * 60)
    print("測試完整 Deep Search V3 管線")
    print("=" * 60)

    result = await deep_search(
        question="什麼是 Python 的 asyncio，有哪些最佳實踐？",
        search_queries=[
            "Python asyncio tutorial",
            "Python async await best practices",
            "asyncio event loop 原理",
        ],
        search_mode="web",
        mode="full",
        results_per_query=10,
        max_chars_per_page=20000,
        render="never",
    )

    print(f"\n成功: {result['success']}")
    print(
        f"搜尋摘要: {json.dumps(result['search_results_summary'], ensure_ascii=False)}"
    )
    print(f"耗時: {result.get('elapsed_ms', 0)}ms")

    if result.get("crawled_pages"):
        print(f"\n爬取結果 ({len(result['crawled_pages'])} 頁):")
        for p in result["crawled_pages"]:
            content_preview = (
                (p.get("content", "")[:100] + "...") if p.get("content") else "(空)"
            )
            print(f"  ✅ {p.get('title', '?')[:50]}")
            print(f"     URL: {p['url']}")
            print(
                f"     長度: {p.get('content_length', 0)} 字元 | 渲染: {p.get('used_render', '?')}"
            )
            print(f"     預覽: {content_preview}")

    if result.get("failed_pages"):
        print(f"\n失敗頁面 ({len(result['failed_pages'])} 頁):")
        for p in result["failed_pages"]:
            print(f"  ❌ {p['url']} — {p.get('error', '?')}")


async def _test_instant_pipeline():
    """測試 instant 單輪快速取證管線。"""
    print("=" * 60)
    print("測試 Instant Deep Search V3 管線")
    print("=" * 60)

    result = await deep_search(
        question="什麼是 Python 的 asyncio？",
        search_queries=[
            "Python asyncio overview",
            "Python async await event loop",
            "asyncio official documentation",
        ],
        search_mode="web",
        mode="instant",
        max_chars_per_page=12000,
        render="never",
    )

    print(f"\n成功: {result['success']}")
    print(
        f"搜尋摘要: {json.dumps(result['search_results_summary'], ensure_ascii=False)}"
    )
    print(f"回答提示: {json.dumps(result.get('answer_guidance', {}), ensure_ascii=False)}")
    print(f"耗時: {result.get('elapsed_ms', 0)}ms")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deep Search Pipeline Tool V3")
    parser.add_argument(
        "--test",
        choices=["search", "instant", "full"],
        default="search",
        help="測試模式: search=僅搜尋, instant=單輪快速管線, full=完整管線",
    )
    args = parser.parse_args()

    async def _main() -> None:
        try:
            if args.test == "search":
                await _test_search_only()
            elif args.test == "instant":
                await _test_instant_pipeline()
            else:
                await _test_full_pipeline()
        finally:
            await shutdown_shared_resources()

    asyncio.run(_main())
