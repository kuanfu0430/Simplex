#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import copy
import ipaddress
import inspect
import json
import logging
import re
import socket
import ssl
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import urljoin, urlparse

from crawl4ai_pdf import (
    PDF_AUTO_EXTRACT_ENABLED,
    detect_resource_type,
    extract_pdf_content,
    is_pdf_content_type,
    looks_like_pdf_bytes,
    looks_like_pdf_url,
    render_pdf_text_as_html,
)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

try:
    from trafilatura import baseline as traf_baseline
    from trafilatura import extract as traf_extract
except ImportError:
    traf_extract = None
    traf_baseline = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
except ImportError:
    AsyncWebCrawler = None
    BrowserConfig = None
    CacheMode = None
    CrawlerRunConfig = None


MAX_URLS = 20
MAX_CONCURRENCY = 20
MAX_JS_CONCURRENCY = 4
MAX_PDF_CONCURRENCY = 2

HTTP_TIMEOUT_SECONDS = 12.0
HTTP_MAX_CONNECTIONS = 64
HTTP_MAX_KEEPALIVE_CONNECTIONS = 32
HTTP_KEEPALIVE_EXPIRY = 30.0
RESOURCE_PROBE_TIMEOUT_SECONDS = 8.0
RESOURCE_PROBE_BYTES = 4096
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
MAX_HTTP_REDIRECTS = 10


class UnsafePublicURL(ValueError):
    """搜尋結果深爬試圖連向非公開網路位址。"""


async def _public_url_status(url: str) -> tuple[bool, str]:
    """解析 URL 與 DNS，僅允許所有位址都屬於公開網際網路的目標。"""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return False, "URL 必須是完整的 HTTP(S) 地址"
        if parsed.username or parsed.password:
            return False, "URL 不允許內嵌帳號密碼"
        if (
            hostname == "localhost"
            or hostname.endswith(".localhost")
            or hostname.endswith(".local")
        ):
            return False, "不允許本機或區域網域"

        try:
            addresses = {ipaddress.ip_address(hostname)}
        except ValueError:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            loop = asyncio.get_running_loop()
            records = await loop.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
            addresses = {
                ipaddress.ip_address(record[4][0].split("%", 1)[0])
                for record in records
            }
        if not addresses:
            return False, "DNS 沒有回傳可用位址"
        if any(not address.is_global for address in addresses):
            return False, "不允許私有、回環、保留或 link-local 位址"
        return True, ""
    except Exception as exc:
        return False, f"URL 或 DNS 驗證失敗：{type(exc).__name__}"


async def _get_with_public_redirect_validation(
    client: Any,
    url: str,
    *,
    timeout: Any,
    headers: dict[str, str] | None = None,
) -> Any:
    """在每次 HTTP redirect 送出前重新驗證目標，避免轉向內網。"""
    current = url
    for _ in range(MAX_HTTP_REDIRECTS + 1):
        allowed, reason = await _public_url_status(current)
        if not allowed:
            raise UnsafePublicURL(reason)
        response = await client.get(
            current,
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        current = urljoin(str(response.url), location)
    raise UnsafePublicURL("HTTP redirect 次數超過安全上限")

SUMMARY_LIMIT = 6000
NORMAL_LIMIT = 12000
HTML_SUMMARY_LIMIT = 25000
HTML_NORMAL_LIMIT = 120000

QUALITY_ACCEPT_SCORE = 900
QUALITY_FORCE_JS_SCORE = 650
QUALITY_JS_BETTER_MARGIN = 150
QUALITY_ACCEPT_SCORE_CJK = 520
QUALITY_FORCE_JS_SCORE_CJK = 360
QUALITY_ACCEPT_SCORE_THAI = 620
QUALITY_FORCE_JS_SCORE_THAI = 420

TEXT_ACCEPT_LENGTH = 450
TEXT_FORCE_JS_LENGTH = 220
HTML_LARGE_THRESHOLD = 20000
SUCCESS_MIN_TEXT_LENGTH = 120
LONG_FORM_ACCEPT_LENGTH = 650
LONG_FORM_ACCEPT_SCORE = 420
LONG_FORM_ACCEPT_MAX_NAV_HITS = 8
LONG_FORM_ACCEPT_MAX_SHORT_LINE_RATIO = 0.72
MIN_USABLE_SCORE = 90
MIN_USABLE_SCORE_CJK = 40
MIN_USABLE_SCORE_THAI = 60
EXTRACTION_MODE_GENERAL = "general"
EXTRACTION_MODE_STRICT = "strict"

JS_TEMPLATE_MAP = {
    "none": "",
    "scroll_to_bottom": "window.scrollTo(0, document.body.scrollHeight);",
    "dismiss_popups": (
        "for (const s of ['button[aria-label*=close i]','button[class*=close i]',"
        "'.close','.modal-close','.popup-close']) {"
        " const el = document.querySelector(s); if (el) { el.click(); } }"
    ),
    "wait_for_network_idle": ("await new Promise((r) => setTimeout(r, 800));"),
}

NAV_PATTERN = re.compile(
    r"(?:首頁|首页|關於|关于|聯絡|联系|登入|登录|註冊|注册|訂閱|订阅|"
    r"隱私|隐私|條款|条款|搜尋|搜索|回到頂部|回到顶部|下一頁|上一页|上一頁|上一页|"
    r"ホーム|お問い合わせ|ログイン|会員登録|利用規約|検索|홈|소개|문의|로그인|회원가입|약관|검색|"
    r"หน้าแรก|ติดต่อ|เข้าสู่ระบบ|สมัครสมาชิก|นโยบาย|ค้นหา|"
    r"\bhome\b|\babout\b|\bcontact\b|\blogin\b|\bsign\s*in\b|\bsubscribe\b|"
    r"\bprivacy\b|\bterms\b|\bcookies?\b|\bmenu\b|\bsearch\b)",
    re.IGNORECASE,
)

LEADING_UI_NOISE_PATTERN = re.compile(
    r"(?:\bedition\b|\bsign\s*(?:in|out)\b|\bshare\b|\btext\s*size\b|"
    r"\btoday'?s\s+epaper\b|\bget\s+app\b|\bdownload\s+app\b|\bnewsletter(?:s)?\b|"
    r"\bsaved\s+articles\b|\bfollowing\b|\bmy\s+reads\b|\bweather(?:\s+today)?\b|"
    r"\bprivacy\s+policy\b|\bterms\s+of\s+use\b|\bcontact\s+us\b|\babout\s+us\b|"
    r"\bprint\s+ad\s+rates\b|\bcode\s+of\s+ethics\b|\bsitemap\b|\brss\s+feeds\b|"
    r"\bimage\s+source\b|\bphoto\s+credit\b|\bcomments?\b|"
    r"隱私政策|隐私政策|使用條款|使用条款|聯絡我們|联系我们|關於我們|关于我们|"
    r"分享|字體大小|字体大小|下載App|下载App|訂閱電子報|订阅电子报|"
    r"ホーム|シェア|ログイン|利用規約|お問い合わせ|공유|로그인|문의하기|이용약관|"
    r"แชร์|เข้าสู่ระบบ|ข้อกำหนดการใช้งาน|ติดต่อเรา)",
    re.IGNORECASE,
)

TRAILING_SECTION_PATTERN = re.compile(
    r"(?:\bfollow\s+us\b|\balso\s+read\b|\bread\s+more\b|\brecommended\b|"
    r"\brelated\s+(?:articles?|stories|news)\b|\byou\s+may\s+also\s+like\b|"
    r"\blatest\s+updates\b|\blive\s+updates\b|\btop\s+stories\b|\bmost\s+read\b|"
    r"\btrending\b|\bnewsletter(?:s)?\b|\ball\s+rights\s+reserved\b|"
    r"延伸閱讀|延伸阅读|相關文章|相关文章|更多報導|更多报道|最新更新|即時更新|热门|最受歡迎|最受欢迎|"
    r"関連記事|おすすめ|続きを読む|最新情報|人気記事|관련 기사|추천 기사|더 읽기|실시간 업데이트|"
    r"อ่านเพิ่มเติม|บทความที่เกี่ยวข้อง|อัปเดตล่าสุด|กำลังมาแรง)",
    re.IGNORECASE,
)

TITLE_META_LINE_PATTERN = re.compile(
    r"(?:\b(?:updated|published|last\s+updated)\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\b.*\b\d{4}\b|"
    r"\b(?:desk|editor|staff|team)\b|"
    r"^\s*/\s*[A-Z][A-Z0-9.]+\s*/\s*$)",
    re.IGNORECASE,
)

JS_SIGNAL_PATTERNS = [
    re.compile(r"please\s+enable\s+javascript", re.IGNORECASE),
    re.compile(r"requires\s+javascript", re.IGNORECASE),
    re.compile(r"<div[^>]+id=['\"](?:root|app|__next)['\"]", re.IGNORECASE),
    re.compile(r"window\.__NUXT__", re.IGNORECASE),
    re.compile(r"window\.__NEXT_DATA__", re.IGNORECASE),
]

COMMENT_UI_PATTERN = re.compile(
    r"(?:\bcomments?\b|\bcomment\s*section\b|\brepl(?:y|ies)\b|留言|評論|评论|回覆|ความคิดเห็น|แสดงความคิดเห็น)",
    re.IGNORECASE,
)

COMMENT_ITEM_PATTERN = re.compile(
    r"(?:comment\s*#?\s*\d+|reply\s*#?\s*\d+|ความคิดเห็นที่\s*\d+|留言\s*\d+|评论\s*\d+)",
    re.IGNORECASE,
)

COMMENT_DYNAMIC_HINT_PATTERN = re.compile(
    r"(?:javascript:void\(0\)|\bload\s+more\b|\bshow\s+more\b|\bview\s+all\s+comments\b|"
    r"\bwrite\s+a\s+comment\b|\badd\s+a\s+comment\b|แสดงความคิดเห็น|留言)",
    re.IGNORECASE,
)

GENERIC_JS_ENHANCE_SNIPPET = (
    "(() => {"
    "const clickSelectors=['button[class*=comment i]','a[class*=comment i]',"
    "'[aria-label*=comment i]','button[class*=reply i]','a[class*=reply i]',"
    "'button[class*=more i]','a[class*=more i]','.load-more','.show-more'];"
    "for (const sel of clickSelectors){"
    "for (const el of document.querySelectorAll(sel)){"
    "if (el && typeof el.click === 'function') { try { el.click(); } catch (e) {} }"
    "}}"
    "window.scrollTo(0, document.body.scrollHeight);"
    "setTimeout(()=>window.scrollTo(0, document.body.scrollHeight), 800);"
    "setTimeout(()=>window.scrollTo(0, document.body.scrollHeight), 1800);"
    "})();"
)

LOW_VALUE_PATTERNS = [
    (re.compile(r"please\s+enable\s+js", re.IGNORECASE), "BLOCKER_ENABLE_JS"),
    (
        re.compile(r"please\s+enable\s+javascript", re.IGNORECASE),
        "BLOCKER_ENABLE_JS",
    ),
    (
        re.compile(r"sorry,?\s+something\s+went\s+wrong", re.IGNORECASE),
        "BLOCKER_ERROR_PAGE",
    ),
    (re.compile(r"access\s+denied", re.IGNORECASE), "BLOCKER_ACCESS_DENIED"),
    (re.compile(r"verify\s+you\s+are\s+human", re.IGNORECASE), "BLOCKER_ANTI_BOT"),
    (re.compile(r"just\s+a\s+moment", re.IGNORECASE), "BLOCKER_ANTI_BOT"),
    (re.compile(r"cloudflare", re.IGNORECASE), "BLOCKER_ANTI_BOT"),
]

ANTI_BOT_JS_ERROR_PATTERN = re.compile(
    r"(?:cloudflare|anti[-\s]?bot|verify\s+you\s+are\s+human|just\s+a\s+moment|"
    r"captcha|cf-chl|challenge-platform|access\s+denied)",
    re.IGNORECASE,
)

PLAYWRIGHT_BROWSER_MISSING_PATTERN = re.compile(
    r"(?:executable\s+doesn'?t\s+exist|playwright\s+install|download\s+new\s+browsers?|"
    r"browser(?:\s+binary)?\s+not\s+found|no\s+chromium)",
    re.IGNORECASE,
)


def _classify_js_error(message: str, default: str = "JS_CRAWL_ERROR") -> str:
    """將 JS fallback 錯誤分類成穩定錯誤碼，方便 pipeline 與測試統計。"""
    sample = message or ""
    if PLAYWRIGHT_BROWSER_MISSING_PATTERN.search(sample):
        return "JS_BROWSER_MISSING"
    if ANTI_BOT_JS_ERROR_PATTERN.search(sample):
        return "JS_ANTI_BOT_BLOCKED"
    return default

HTTP_HEADERS = {
    "User-Agent": HTTP_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7",
}

RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}

# ── 可替換的 HTML 解碼鉤子 ─────────────────────────────────
# 預設直接使用 httpx response.text；外部呼叫方可透過
# set_decode_html_hook() 注入更精確的 bytes→str 解碼器
# （如 deep_search_tool 的 decode_response_text）。
_decode_html_hook: Any = None


def set_decode_html_hook(fn: Any) -> None:
    """註冊自訂的 HTML bytes→str 解碼函數。

    簽名：fn(data: bytes, content_type: str, fallback_text: str | None) -> str
    """
    global _decode_html_hook
    _decode_html_hook = fn


def _decode_html(response: Any) -> str:
    """將 HTTP response 的 body 解碼為文字。

    若已註冊 _decode_html_hook，優先使用它（以 response.content bytes
    為輸入，搭配 content-type header 做多編碼嘗試）；否則退回 httpx 內建
    的 response.text。
    """
    if _decode_html_hook is not None:
        try:
            content_type = response.headers.get("content-type", "")
            return _decode_html_hook(
                response.content,
                content_type,
                response.text or None,
            )
        except Exception:
            pass
    return response.text or ""


logger = logging.getLogger(__name__)

_BROWSER_CONFIG_SUPPORTED_KWARGS: frozenset[str] | None = None
_RUN_CONFIG_SUPPORTED_KWARGS: frozenset[str] | None = None
_WARNED_UNSUPPORTED_BROWSER_CONFIG_KEYS: set[str] = set()
_WARNED_UNSUPPORTED_RUN_CONFIG_KEYS: set[str] = set()

_HTTP_CLIENTS: dict[bool, Any] = {}
_HTTP_CLIENTS_LOCK: asyncio.Lock | None = None

_SHARED_JS_CRAWLER: Any = None
_SHARED_JS_CRAWLER_SIGNATURE: str | None = None
_SHARED_JS_CRAWLER_LOCK: asyncio.Lock | None = None
_SHARED_BROWSER_CONFIG: Any = None
_SHARED_BROWSER_CONFIG_SIGNATURE: str | None = None


@dataclass
class ContentMetrics:
    quality_score: int
    text_len: int
    line_count: int
    short_line_ratio: float
    nav_hits: int
    js_signals: int
    language_hint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "quality_score": self.quality_score,
            "text_len": self.text_len,
            "line_count": self.line_count,
            "short_line_ratio": round(self.short_line_ratio, 3),
            "nav_hits": self.nav_hits,
            "js_signals": self.js_signals,
            "language_hint": self.language_hint,
        }


@dataclass
class FetchResult:
    success: bool
    status_code: int | None
    final_url: str
    html: str
    content_type: str
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    resource_type: str = "html"
    body_bytes: bytes | None = None
    title: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class QualityDecision:
    usable: bool
    acceptable: bool
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "usable": self.usable,
            "acceptable": self.acceptable,
            "reason": self.reason,
        }


@dataclass
class TextPostprocessResult:
    text: str
    steps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SummaryResult:
    text: str
    blocks_total: int
    blocks_kept: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks_total": self.blocks_total,
            "blocks_kept": self.blocks_kept,
        }


@dataclass
class CandidateEvaluation:
    score: int
    metrics: ContentMetrics
    cleaned: str
    quality: QualityDecision
    postprocess_steps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class HTTPContentCandidates:
    """同一份 HTML 可供 strict/general 共用的未評分候選。"""

    html: str
    fallback_content: str
    fallback_title: str
    candidates: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class AttemptResult:
    attempted: bool = False
    fetch_success: bool = False
    status_code: int | None = None
    resource_type: str = "html"
    content: str = ""
    html: str = ""
    title: str = ""
    content_source: str | None = None
    metrics: ContentMetrics = field(
        default_factory=lambda: ContentMetrics(0, 0, 0, 1.0, 0, 0, "latin")
    )
    content_scope: str = "general"
    quality: QualityDecision = field(
        default_factory=lambda: QualityDecision(False, False, "EMPTY_CONTENT")
    )
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    postprocess_steps: list[dict[str, Any]] = field(default_factory=list)
    summary: SummaryResult = field(default_factory=lambda: SummaryResult("", 0, 0))
    resource_diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_diag_dict(self, *, include_fetch_success: bool = False) -> dict[str, Any]:
        if not self.attempted:
            payload = {
                "attempted": False,
                "status_code": None,
                "error_code": None,
                "error_message": None,
                "resource_type": "html",
                "html_len": 0,
                "content_source": None,
                "text_len": 0,
                "quality_score": 0,
                "content_scope": None,
                "usable": None,
                "acceptable": None,
                "unusable_reason": None,
                "accept_reject_reason": None,
                "language_hint": None,
                "postprocess_steps": [],
            }
            if include_fetch_success:
                payload["success"] = None
            return payload

        payload = {
            "attempted": self.attempted,
            "status_code": self.status_code,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "resource_type": self.resource_type,
            "html_len": len(self.html),
            "content_source": self.content_source,
            "text_len": self.metrics.text_len,
            "quality_score": self.metrics.quality_score,
            "content_scope": self.content_scope,
            "usable": self.quality.usable,
            "acceptable": self.quality.acceptable,
            "unusable_reason": None if self.quality.usable else self.quality.reason,
            "accept_reject_reason": None if self.quality.acceptable else self.quality.reason,
            "language_hint": self.metrics.language_hint,
            "postprocess_steps": self.postprocess_steps,
        }
        if include_fetch_success:
            payload["success"] = self.fetch_success
        return payload


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return f"https://{url}"


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _normalize_content_format(content_format: str | None) -> str:
    normalized = (content_format or "markdown").strip().lower()
    if normalized in {"markdown", "html"}:
        return normalized
    return "markdown"


def _normalize_extraction_mode(extraction_mode: str | None) -> str:
    normalized = (extraction_mode or EXTRACTION_MODE_GENERAL).strip().lower()
    if normalized in {EXTRACTION_MODE_GENERAL, EXTRACTION_MODE_STRICT}:
        return normalized
    return EXTRACTION_MODE_GENERAL


def _quality_profile(language_hint: str) -> dict[str, int | float]:
    normalized = (language_hint or "").lower()
    if normalized == "cjk":
        return {
            "short_line_threshold": 16,
            "long_line_threshold": 36,
            "accept_score": QUALITY_ACCEPT_SCORE_CJK,
            "force_js_score": QUALITY_FORCE_JS_SCORE_CJK,
            "min_usable_score": MIN_USABLE_SCORE_CJK,
        }
    if normalized == "thai":
        return {
            "short_line_threshold": 24,
            "long_line_threshold": 56,
            "accept_score": QUALITY_ACCEPT_SCORE_THAI,
            "force_js_score": QUALITY_FORCE_JS_SCORE_THAI,
            "min_usable_score": MIN_USABLE_SCORE_THAI,
        }
    return {
        "short_line_threshold": 40,
        "long_line_threshold": 80,
        "accept_score": QUALITY_ACCEPT_SCORE,
        "force_js_score": QUALITY_FORCE_JS_SCORE,
        "min_usable_score": MIN_USABLE_SCORE,
    }


def _extract_lang_hint_from_html(html: str) -> str:
    if not html:
        return ""
    match = re.search(r"<html[^>]+lang=['\"]([a-zA-Z-]+)['\"]", html[:3000], re.I)
    if not match:
        return ""
    lang = match.group(1).strip().lower()
    if lang.startswith(("zh", "ja", "ko")):
        return "cjk"
    if lang.startswith("th"):
        return "thai"
    return "latin"


def _detect_language_hint(text: str, html: str = "") -> str:
    html_hint = _extract_lang_hint_from_html(html)
    if html_hint:
        return html_hint

    sample = (text or "")[:2000]
    if not sample:
        return "latin"

    cjk_chars = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", sample))
    thai_chars = len(re.findall(r"[\u0E00-\u0E7F]", sample))
    latin_chars = len(re.findall(r"[A-Za-z]", sample))
    if cjk_chars >= 16 or cjk_chars > max(latin_chars // 2, 8):
        return "cjk"
    if thai_chars >= 16:
        return "thai"
    return "latin"


def _domain_from_url(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().split(":", 1)[0]
    except Exception:
        return ""


def _comment_signal_counts(text: str) -> tuple[int, int]:
    sample = text or ""
    ui_hits = len(COMMENT_UI_PATTERN.findall(sample))
    item_hits = len(COMMENT_ITEM_PATTERN.findall(sample))
    return ui_hits, item_hits


def _comments_likely_dynamic(text: str, metrics: ContentMetrics) -> bool:
    ui_hits, item_hits = _comment_signal_counts(text)
    if item_hits > 0 or metrics.text_len < 300:
        return False
    has_dynamic_hint = bool(COMMENT_DYNAMIC_HINT_PATTERN.search(text or ""))
    return has_dynamic_hint and ui_hits >= 1


def _detect_low_value_reason(text: str) -> str | None:
    sample = (text or "").strip()
    if not sample:
        return "EMPTY_CONTENT"

    for pattern, reason in LOW_VALUE_PATTERNS:
        if pattern.search(sample):
            return reason

    if len(sample) <= 20 and len(sample.split()) <= 3:
        return "LOW_INFO_CONTENT"

    return None


def _content_scope(text: str) -> str:
    ui_hits, item_hits = _comment_signal_counts(text)
    if item_hits >= 2:
        return "article_plus_comments"
    if ui_hits >= 1:
        return "article_only_or_comment_shell"
    return "general"


def _assess_content_quality(text: str, metrics: ContentMetrics) -> QualityDecision:
    low_value_reason = _detect_low_value_reason(text)
    if low_value_reason:
        return QualityDecision(False, False, low_value_reason)

    profile = _quality_profile(metrics.language_hint)
    min_usable_score = int(profile["min_usable_score"])
    force_js_score = int(profile["force_js_score"])
    accept_score = int(profile["accept_score"])

    if metrics.text_len < 40:
        return QualityDecision(False, False, "TEXT_TOO_SHORT")

    if metrics.quality_score <= 0 and metrics.short_line_ratio >= 0.85:
        return QualityDecision(False, False, "LOW_SIGNAL_CONTENT")

    if (
        metrics.quality_score < min_usable_score
        and metrics.text_len < max(TEXT_FORCE_JS_LENGTH, SUCCESS_MIN_TEXT_LENGTH)
    ):
        return QualityDecision(False, False, "LOW_SIGNAL_CONTENT")

    if metrics.nav_hits >= 12 and metrics.quality_score < 200:
        return QualityDecision(False, False, "NAV_HEAVY_CONTENT")

    if (
        metrics.text_len < SUCCESS_MIN_TEXT_LENGTH
        and metrics.quality_score < force_js_score
    ):
        return QualityDecision(False, False, "LOW_VALUE_CONTENT")

    acceptable = False
    if metrics.text_len >= TEXT_ACCEPT_LENGTH and metrics.quality_score >= accept_score:
        acceptable = True
    elif (
        metrics.text_len >= LONG_FORM_ACCEPT_LENGTH
        and metrics.quality_score >= LONG_FORM_ACCEPT_SCORE
        and metrics.nav_hits <= LONG_FORM_ACCEPT_MAX_NAV_HITS
        and metrics.short_line_ratio <= LONG_FORM_ACCEPT_MAX_SHORT_LINE_RATIO
    ):
        acceptable = True

    return QualityDecision(True, acceptable, None if acceptable else "BELOW_ACCEPT_THRESHOLD")


def _is_html_content_type(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return "text/html" in ct or "application/xhtml+xml" in ct


def _is_pdf_content_type(content_type: str) -> bool:
    return is_pdf_content_type(content_type)


def _looks_like_pdf_url(url: str) -> bool:
    return looks_like_pdf_url(url)


def _looks_like_pdf_bytes(data: bytes) -> bool:
    return looks_like_pdf_bytes(data)


def _detect_resource_type(url: str, content_type: str, sample: bytes) -> str:
    return detect_resource_type(url, content_type, sample)


def _strip_markdown_links(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[\s*\]\(\s*[^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\(\s*javascript:[^)]*\)", r"\1", text, flags=re.I)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"javascript:void\\?\([^)]*\)", "", text, flags=re.I)
    text = re.sub(r"\s*;\s*\"[^\"]*\"\)", "", text)
    text = re.sub(r"__+\s*advertisement\s*__+", "", text, flags=re.I)
    text = re.sub(r"\badvertisement\b", "", text, flags=re.I)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _title_anchor_candidates(title: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", (title or "").strip())
    if not normalized:
        return []

    candidates: list[str] = []
    bases: list[str] = []
    for base in (
        normalized,
        re.sub(r"\s+\|\s*-\s+.*$", "", normalized).strip(),
        re.split(r"\s+\|\s+", normalized, maxsplit=1)[0].strip(),
        re.split(r"\s+-\s+", normalized, maxsplit=1)[0].strip(),
    ):
        cleaned_base = base.rstrip(" -|:;,/")
        if len(cleaned_base) >= 12 and cleaned_base not in bases:
            bases.append(cleaned_base)

    for base in bases:
        if base not in candidates:
            candidates.append(base)
        for size in (64, 48, 36, 24, 20):
            if len(base) >= size:
                candidate = base[:size].rstrip(" -|:;,.")
                if len(candidate) >= 12 and candidate not in candidates:
                    candidates.append(candidate)

    return candidates


def _trim_to_title_anchor(text: str, title: str) -> str:
    sample = (text or "").strip()
    if not sample or not title:
        return sample

    max_anchor_offset = min(2500, max(600, int(len(sample) * 0.35)))
    matches: list[tuple[int, str]] = []
    for candidate in _title_anchor_candidates(title):
        start = 0
        while True:
            idx = sample.find(candidate, start)
            if idx < 0 or idx > max_anchor_offset:
                break
            matches.append((idx, candidate))
            start = idx + max(len(candidate), 1)

    best_idx = 0
    best_score = 0
    for idx, candidate in matches:
        if idx <= 0:
            continue
        prefix = sample[:idx].strip()
        prefix_lines = [line.strip() for line in prefix.splitlines() if line.strip()]
        prefix_text = "\n".join(prefix_lines[-12:])
        ui_hits = len(LEADING_UI_NOISE_PATTERN.findall(prefix_text))
        nav_hits = len(NAV_PATTERN.findall(prefix_text))
        breadcrumb_hits = len(re.findall(r"(?:^|\n)\s*[-|>/]+\s*", prefix_text))
        short_lines = sum(1 for line in prefix_lines[-8:] if len(line) <= 24)
        duplicate_hits = sum(1 for pos, cand in matches if cand == candidate and pos > idx)
        score = 0
        if idx >= 60:
            score += 240
        elif idx >= 8 and (ui_hits or nav_hits or breadcrumb_hits or short_lines >= 3):
            score += 180
        score += ui_hits * 140
        score += nav_hits * 120
        score += breadcrumb_hits * 140
        score += min(short_lines * 45, 225)
        if prefix.startswith("#"):
            score -= 160
        if duplicate_hits >= 1:
            score += 180
        if score > best_score:
            best_idx = idx
            best_score = score

    if best_idx > 0:
        return sample[best_idx:].lstrip()
    return sample


def _trim_to_heading_anchor(text: str) -> str:
    sample = (text or "").strip()
    if not sample:
        return sample

    match = re.search(r"(?<!\w)#{1,3}\s+\S", sample[:2500])
    if match and match.start() >= 80:
        return sample[match.start() :].lstrip()
    return sample


def _line_matches_title(line: str, title: str) -> bool:
    sample = re.sub(r"\s+", " ", (line or "").strip().lstrip("#").strip())
    if not sample:
        return False
    lowered = sample.lower()
    for candidate in _title_anchor_candidates(title):
        cand = candidate.lower()
        if len(cand) < 24:
            continue
        if lowered == cand or lowered.startswith(cand):
            return True
        if cand.startswith(lowered) and len(lowered) >= 24:
            return True
    return False


def _is_front_matter_noise_line(line: str) -> bool:
    sample = re.sub(r"\s+", " ", (line or "").strip().strip("-|>/")).strip()
    if not sample:
        return True
    lowered = sample.lower()
    if LEADING_UI_NOISE_PATTERN.search(sample):
        return True
    if TITLE_META_LINE_PATTERN.search(sample):
        return True
    if sample in {"AA", "+", "Small", "Medium", "Large"}:
        return True
    if re.fullmatch(r"[A-Z]{2,4}", sample):
        return True
    if re.fullmatch(r"[A-Za-z]{1,4}", sample) and lowered in {
        "in",
        "us",
        "uk",
    }:
        return True
    return False


def _strip_leading_noise_lines(text: str, title: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""

    output = [lines[0]]
    idx = 1
    skipped = 0
    while idx < len(lines) and skipped < 18:
        line = lines[idx]
        if _line_matches_title(line, title):
            idx += 1
            skipped += 1
            continue
        if _is_front_matter_noise_line(line):
            idx += 1
            skipped += 1
            continue
        if len(line) >= 80 or re.search(r"[.!?。！？]", line):
            break
        if idx + 1 < len(lines) and _is_front_matter_noise_line(lines[idx + 1]):
            idx += 1
            skipped += 1
            continue
        break

    output.extend(lines[idx:])
    return "\n".join(output).strip()


def _trim_trailing_noise_sections(text: str, title: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 4:
        return (text or "").strip()

    title_lower = (title or "").lower()
    tail_start = max(2, int(len(lines) * 0.55), len(lines) - 20)
    for idx in range(tail_start, len(lines)):
        line = lines[idx]
        if not TRAILING_SECTION_PATTERN.search(line):
            continue
        if "live updates" in line.lower() and "live updates" in title_lower:
            continue
        cut_idx = idx
        while cut_idx > 1:
            prev = lines[cut_idx - 1]
            if len(prev) <= 120 and not re.search(r"[.!?。！？]", prev):
                cut_idx -= 1
                continue
            break
        if cut_idx >= 2:
            return "\n".join(lines[:cut_idx]).strip()
    return "\n".join(lines).strip()


def _ensure_title_heading(text: str, title: str) -> str:
    lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0].lstrip()
    if first.startswith("#"):
        return "\n".join(lines).strip()
    if _line_matches_title(first, title):
        lines[0] = f"# {first.lstrip('#').strip()}"
        return "\n".join(lines).strip()

    title_candidates = _title_anchor_candidates(title)
    preferred_title = title_candidates[0] if title_candidates else ""
    head_sample = "\n".join(lines[:4]).lower()
    if preferred_title and preferred_title.lower() not in head_sample:
        return f"# {preferred_title}\n\n" + "\n".join(lines).strip()

    return "\n".join(lines).strip()


def _content_noise_penalty(text: str, title: str) -> int:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return 0

    head = "\n".join(lines[:10])
    tail = "\n".join(lines[-20:])
    penalty = 0
    penalty += min(len(LEADING_UI_NOISE_PATTERN.findall(head)) * 220, 1320)
    penalty += min(len(TRAILING_SECTION_PATTERN.findall(tail)) * 280, 1120)
    penalty += min(len(re.findall(r"(?:^|\n)\s*[-|>/]+\s*", head)) * 120, 360)
    if not lines[0].startswith("#") and _line_matches_title(lines[0], title):
        penalty += 120
    if sum(1 for line in lines[:6] if len(line) <= 24) >= 4:
        penalty += 260
    return penalty


def _record_postprocess_step(
    steps: list[dict[str, Any]], step: str, before: str, after: str
) -> None:
    steps.append(
        {
            "step": step,
            "changed": before != after,
            "before_len": len(before),
            "after_len": len(after),
            "delta": len(after) - len(before),
        }
    )


def _run_postprocess_pipeline(
    text: str, *, title: str = "", extraction_mode: str = EXTRACTION_MODE_GENERAL
) -> TextPostprocessResult:
    current = text or ""
    steps: list[dict[str, Any]] = []

    original = current
    current = _strip_markdown_links(current)
    _record_postprocess_step(steps, "strip_markdown_links", original, current)

    original = current
    current = re.sub(r"^\s*x\s+", "", current, flags=re.I)
    _record_postprocess_step(steps, "trim_leading_x_marker", original, current)

    original = current
    current = _trim_to_title_anchor(current, title)
    _record_postprocess_step(steps, "trim_to_title_anchor", original, current)

    if extraction_mode == EXTRACTION_MODE_STRICT and current == (text or "").strip():
        original = current
        current = _trim_to_heading_anchor(current)
        _record_postprocess_step(steps, "trim_to_heading_anchor", original, current)

    original = current
    current = _strip_leading_noise_lines(current, title)
    _record_postprocess_step(steps, "strip_leading_noise_lines", original, current)

    original = current
    current = _trim_trailing_noise_sections(current, title)
    _record_postprocess_step(steps, "trim_trailing_noise_sections", original, current)

    original = current
    current = _ensure_title_heading(current, title)
    _record_postprocess_step(steps, "ensure_title_heading", original, current)

    original = current
    current = re.sub(r"[^\S\n]+", " ", current)
    current = re.sub(r"\n{3,}", "\n\n", current)
    current = current.strip()
    _record_postprocess_step(steps, "normalize_spacing", original, current)

    return TextPostprocessResult(text=current, steps=steps)


def _analyze_content_quality(text: str, html: str = "") -> ContentMetrics:
    t = (text or "").strip()
    language_hint = _detect_language_hint(t, html)
    profile = _quality_profile(language_hint)
    if not t:
        return ContentMetrics(
            0, 0, 0, 1.0, 0, _detect_js_signals(html, t), language_hint
        )

    lines = [line.strip() for line in t.splitlines() if line.strip()]
    line_count = len(lines)
    scored_lines = [line for line in lines if not line.startswith("#")]
    if not scored_lines:
        scored_lines = lines
    short_threshold = int(profile["short_line_threshold"])
    long_threshold = int(profile["long_line_threshold"])
    short_lines = sum(1 for line in scored_lines if len(line) <= short_threshold)
    long_lines = sum(1 for line in scored_lines if len(line) >= long_threshold)
    short_ratio = short_lines / max(len(scored_lines), 1)
    nav_hits = len(NAV_PATTERN.findall(t[:4000]))

    length = len(t)
    base = min(length, 20000)
    bonus = min(long_lines * 120, 1200)
    penalty = int(short_ratio * 1800) + min(nav_hits * 120, 2400)
    score = max(0, base + bonus - penalty)

    return ContentMetrics(
        quality_score=score,
        text_len=length,
        line_count=line_count,
        short_line_ratio=short_ratio,
        nav_hits=nav_hits,
        js_signals=_detect_js_signals(html, t),
        language_hint=language_hint,
    )


def _detect_js_signals(html: str, text: str) -> int:
    sample = f"{html[:5000]}\n{text[:1200]}"
    return sum(1 for pattern in JS_SIGNAL_PATTERNS if pattern.search(sample))


def _content_meets_accept(metrics: ContentMetrics) -> bool:
    profile = _quality_profile(metrics.language_hint)
    accept_score = int(profile["accept_score"])
    if metrics.text_len >= TEXT_ACCEPT_LENGTH and metrics.quality_score >= accept_score:
        return True
    return (
        metrics.text_len >= LONG_FORM_ACCEPT_LENGTH
        and metrics.quality_score >= LONG_FORM_ACCEPT_SCORE
        and metrics.nav_hits <= LONG_FORM_ACCEPT_MAX_NAV_HITS
        and metrics.short_line_ratio <= LONG_FORM_ACCEPT_MAX_SHORT_LINE_RATIO
    )


def _smart_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text

    for token in ("\n\n", "。", ". ", "\n", "，", ", ", " "):
        pos = text.rfind(token, max(0, limit - 180), limit)
        if pos > 0:
            trimmed = text[: pos + len(token)].strip()
            if len(trimmed) >= int(limit * 0.6):
                return trimmed

    return text[:limit].rstrip()


def _split_blocks(text: str) -> list[str]:
    raw = re.split(r"\n\s*\n", text)
    blocks = [b.strip() for b in raw if b and b.strip()]
    return blocks


def _score_block(block: str) -> float:
    length = len(block)
    if length < 30:
        return -10.0

    sentence_count = len(re.findall(r"[.!?。！？]", block))
    digit_count = len(re.findall(r"\d", block))
    nav_hits = len(NAV_PATTERN.findall(block[:500]))
    link_hits = len(re.findall(r"\[[^\]]+\]\([^)]*\)", block))
    heading_bonus = 180.0 if block.startswith("#") else 0.0

    score = 0.0
    score += min(length, 700) * 1.0
    score += min(sentence_count * 45, 360)
    score += min(digit_count * 12, 120)
    score += heading_bonus
    score -= min(nav_hits * 140, 560)
    score -= min(link_hits * 70, 350)
    return score


def _compress_summary_result(
    text: str, title: str = "", limit: int = SUMMARY_LIMIT
) -> SummaryResult:
    cleaned = (text or "").strip()
    if not cleaned:
        return SummaryResult("", 0, 0)

    blocks = _split_blocks(cleaned)
    if not blocks:
        return SummaryResult(_smart_truncate(cleaned, limit), 1, 1)

    scored: list[tuple[int, float, str]] = []
    for idx, block in enumerate(blocks):
        scored.append((idx, _score_block(block), block))

    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:4]
    top.sort(key=lambda x: x[0])

    pieces: list[str] = []
    if title:
        pieces.append(f"# {title.strip()}")

    for _, _, block in top:
        if not block:
            continue
        pieces.append(block)

    merged = "\n\n".join(pieces).strip()
    merged = _strip_markdown_links(merged)
    return SummaryResult(
        _smart_truncate(merged, limit),
        blocks_total=len(blocks),
        blocks_kept=sum(1 for _, _, block in top if block),
    )


def _parse_html_soup(html: str) -> Any:
    if BeautifulSoup is None or not html:
        return None
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        try:
            return BeautifulSoup(html, "html.parser")
        except Exception:
            return None


def _fallback_text_from_soup(soup: Any) -> str:
    if soup is None:
        return ""
    try:
        working_soup = copy.copy(soup)
        if working_soup is None:
            return ""
        for tag in working_soup.select(
            "script,style,noscript,iframe,svg,canvas,nav,footer,aside,header,form"
        ):
            tag.decompose()
        text = working_soup.get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    except Exception:
        return ""


def _fallback_text_from_html(html: str, soup: Any | None = None) -> str:
    if not html:
        return ""

    if BeautifulSoup is not None:
        cached_soup = soup if soup is not None else _parse_html_soup(html)
        text = _fallback_text_from_soup(cached_soup)
        if text:
            return text

    text = re.sub(
        r"<script[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_title_from_soup(soup: Any, fallback: str = "") -> str:
    if soup is not None:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            h1_text = h1.get_text(" ", strip=True)
            if len(h1_text) >= 12:
                return h1_text[:200]
        if soup.title and soup.title.text:
            return soup.title.text.strip()[:200]

    if fallback:
        first_line = fallback.splitlines()[0].strip()
        if first_line.startswith("#"):
            return first_line.lstrip("#").strip()[:200]

    return ""


def _extract_title(html: str, fallback: str = "", soup: Any | None = None) -> str:
    if BeautifulSoup is not None and html:
        cached_soup = soup if soup is not None else _parse_html_soup(html)
        title = _extract_title_from_soup(cached_soup, fallback=fallback)
        if title:
            return title

    return _extract_title_from_soup(None, fallback=fallback)


def _extract_structural_content(soup: Any) -> tuple[str, str]:
    if soup is None:
        return "", "empty"

    best_text = ""
    best_source = "empty"
    selectors = (
        ("article", "html_article"),
        ("main", "html_main"),
        ("[role='main']", "html_role_main"),
        ("[itemprop='articleBody']", "html_article_body"),
    )
    for selector, source in selectors:
        try:
            for node in soup.select(selector)[:3]:
                fragment_soup = _parse_html_soup(str(node))
                candidate = _fallback_text_from_soup(fragment_soup)
                if len(candidate) > len(best_text):
                    best_text = candidate
                    best_source = source
        except Exception:
            continue
    return best_text, best_source


def _prepare_http_content_candidates(
    html: str,
    url: str,
) -> HTTPContentCandidates:
    """解析 HTML 並建立候選；不套用 strict/general 評分。"""
    if not html:
        return HTTPContentCandidates("", "", "", [])

    soup = _parse_html_soup(html)
    title = _extract_title(html, soup=soup)
    candidates: list[tuple[str, str, str]] = []

    def _add_candidate(text: str, source: str, title_hint: str = "") -> None:
        content = (text or "").strip()
        if len(content) <= 30:
            return
        effective_title = (title_hint or title or _extract_title(html, content, soup=soup)).strip()
        candidates.append((content, source, effective_title))

    if traf_extract is not None:
        for include_links, source in (
            (True, "trafilatura"),
            (False, "trafilatura_nolinks"),
        ):
            try:
                md = traf_extract(
                    html,
                    url=url,
                    output_format="markdown",
                    include_comments=False,
                    include_tables=True,
                    include_links=include_links,
                    include_images=False,
                    favor_precision=False,
                    favor_recall=True,
                    fast=True,
                )
                _add_candidate(md or "", source)
            except Exception:
                pass

        if traf_baseline is not None:
            try:
                _, txt, length = traf_baseline(html)
                if txt and length and length > 30:
                    _add_candidate(txt.strip(), "trafilatura_baseline")
            except Exception:
                pass

    structural_text, structural_source = _extract_structural_content(soup)
    _add_candidate(structural_text, structural_source)

    content = _fallback_text_from_html(html, soup=soup)
    fallback_title = title or _extract_title_from_soup(None, fallback=content)
    _add_candidate(content, "html_fallback", fallback_title)

    return HTTPContentCandidates(
        html=html,
        fallback_content=content,
        fallback_title=fallback_title,
        candidates=candidates,
    )


def _evaluate_http_content_candidates(
    prepared: HTTPContentCandidates,
    *,
    extraction_mode: str = EXTRACTION_MODE_GENERAL,
) -> tuple[str, str, str, list[dict[str, Any]]]:
    """以指定模式評估已準備的候選，保留原本的選擇規則與順序。"""
    if not prepared.html:
        return "", "empty", "", []

    if not prepared.candidates:
        return (
            prepared.fallback_content,
            "html_fallback",
            prepared.fallback_title,
            [],
        )

    best_text = ""
    best_source = "empty"
    best_title = prepared.fallback_title
    best_eval = CandidateEvaluation(
        score=-(10**9),
        metrics=ContentMetrics(0, 0, 0, 1.0, 0, 0, "latin"),
        cleaned="",
        quality=QualityDecision(False, False, "EMPTY_CONTENT"),
    )
    for candidate_text, source, candidate_title in prepared.candidates:
        evaluation = _candidate_selection_score(
            candidate_text,
            source,
            prepared.html,
            title=candidate_title,
            extraction_mode=extraction_mode,
        )
        if evaluation.score > best_eval.score:
            best_eval = evaluation
            best_text = evaluation.cleaned
            best_source = source
            best_title = candidate_title

    return best_text, best_source, best_title, best_eval.postprocess_steps


def _extract_http_content_bundle(
    html: str,
    url: str,
    *,
    extraction_mode: str = EXTRACTION_MODE_GENERAL,
) -> tuple[str, str, str, list[dict[str, Any]]]:
    prepared = _prepare_http_content_candidates(html, url)
    return _evaluate_http_content_candidates(
        prepared,
        extraction_mode=extraction_mode,
    )


def _inspect_supported_kwargs(factory: Any) -> frozenset[str] | None:
    if factory is None:
        return None
    try:
        params = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return None

    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params):
        return None

    return frozenset(
        param.name
        for param in params
        if param.kind
        in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
        and param.name != "self"
    )


def _supported_browser_config_kwargs() -> frozenset[str] | None:
    global _BROWSER_CONFIG_SUPPORTED_KWARGS
    if _BROWSER_CONFIG_SUPPORTED_KWARGS is None:
        _BROWSER_CONFIG_SUPPORTED_KWARGS = _inspect_supported_kwargs(BrowserConfig)
    return _BROWSER_CONFIG_SUPPORTED_KWARGS


def _supported_run_config_kwargs() -> frozenset[str] | None:
    global _RUN_CONFIG_SUPPORTED_KWARGS
    if _RUN_CONFIG_SUPPORTED_KWARGS is None:
        _RUN_CONFIG_SUPPORTED_KWARGS = _inspect_supported_kwargs(CrawlerRunConfig)
    return _RUN_CONFIG_SUPPORTED_KWARGS


def _filter_supported_kwargs(
    kwargs: dict[str, Any],
    *,
    supported: frozenset[str] | None,
    warned_keys: set[str],
    config_name: str,
) -> dict[str, Any]:
    if supported is None:
        return dict(kwargs)

    filtered = {key: value for key, value in kwargs.items() if key in supported}
    unsupported = sorted(key for key in kwargs if key not in supported)
    new_unsupported = [key for key in unsupported if key not in warned_keys]
    if new_unsupported:
        warned_keys.update(new_unsupported)
        logger.warning(
            "%s 略過目前版本不支援的參數: %s",
            config_name,
            ", ".join(new_unsupported),
        )
    return filtered


def _build_browser_config(user_agent: str) -> Any:
    if BrowserConfig is None:
        return None

    kwargs = _filter_supported_kwargs(
        {
        "headless": True,
        "verbose": False,
        "light_mode": True,
        "user_agent": user_agent,
        },
        supported=_supported_browser_config_kwargs(),
        warned_keys=_WARNED_UNSUPPORTED_BROWSER_CONFIG_KEYS,
        config_name="BrowserConfig",
    )
    if kwargs:
        return BrowserConfig(**kwargs)
    return BrowserConfig()


def _build_run_config(**kwargs: Any) -> Any:
    if CrawlerRunConfig is None:
        return None

    current = _filter_supported_kwargs(
        dict(kwargs),
        supported=_supported_run_config_kwargs(),
        warned_keys=_WARNED_UNSUPPORTED_RUN_CONFIG_KEYS,
        config_name="CrawlerRunConfig",
    )
    if current:
        return CrawlerRunConfig(**current)
    return CrawlerRunConfig()


def _get_http_clients_lock() -> asyncio.Lock:
    global _HTTP_CLIENTS_LOCK
    if _HTTP_CLIENTS_LOCK is None:
        _HTTP_CLIENTS_LOCK = asyncio.Lock()
    return _HTTP_CLIENTS_LOCK


def _get_shared_js_crawler_lock() -> asyncio.Lock:
    global _SHARED_JS_CRAWLER_LOCK
    if _SHARED_JS_CRAWLER_LOCK is None:
        _SHARED_JS_CRAWLER_LOCK = asyncio.Lock()
    return _SHARED_JS_CRAWLER_LOCK


def _config_signature(config: Any) -> str:
    if config is None:
        return "none"

    to_dict = getattr(config, "to_dict", None)
    if callable(to_dict):
        try:
            payload = to_dict()
            return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            pass

    dump = getattr(config, "model_dump", None)
    if callable(dump):
        try:
            payload = dump()
            return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            pass

    return repr(config)


def _get_shared_browser_config() -> tuple[Any, str]:
    global _SHARED_BROWSER_CONFIG, _SHARED_BROWSER_CONFIG_SIGNATURE

    if _SHARED_BROWSER_CONFIG is None:
        config = _build_browser_config(HTTP_USER_AGENT)
        _SHARED_BROWSER_CONFIG = config
        _SHARED_BROWSER_CONFIG_SIGNATURE = _config_signature(config)

    return _SHARED_BROWSER_CONFIG, _SHARED_BROWSER_CONFIG_SIGNATURE or "none"


async def _get_http_client(verify_ssl: bool) -> Any:
    if httpx is None:
        return None

    client = _HTTP_CLIENTS.get(verify_ssl)
    if client is not None and not getattr(client, "is_closed", False):
        return client

    async with _get_http_clients_lock():
        client = _HTTP_CLIENTS.get(verify_ssl)
        if client is not None and not getattr(client, "is_closed", False):
            return client

        httpx_mod = httpx
        limits = httpx_mod.Limits(
            max_connections=HTTP_MAX_CONNECTIONS,
            max_keepalive_connections=HTTP_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=HTTP_KEEPALIVE_EXPIRY,
        )
        client = httpx_mod.AsyncClient(
            headers=HTTP_HEADERS,
            follow_redirects=True,
            http2=True,
            verify=verify_ssl,
            limits=limits,
            timeout=httpx_mod.Timeout(HTTP_TIMEOUT_SECONDS),
        )
        _HTTP_CLIENTS[verify_ssl] = client
        return client


async def _close_http_clients() -> None:
    async with _get_http_clients_lock():
        clients = list(_HTTP_CLIENTS.values())
        _HTTP_CLIENTS.clear()

    for client in clients:
        close = getattr(client, "aclose", None)
        if callable(close):
            with suppress(Exception):
                await close()


async def _start_crawler(crawler: Any) -> None:
    start = getattr(crawler, "start", None)
    if callable(start):
        await start()
        return

    aenter = getattr(crawler, "__aenter__", None)
    if callable(aenter):
        await aenter()


async def _close_crawler(crawler: Any) -> None:
    close = getattr(crawler, "close", None)
    if callable(close):
        await close()
        return

    aexit = getattr(crawler, "__aexit__", None)
    if callable(aexit):
        await aexit(None, None, None)


async def _get_shared_js_crawler() -> Any:
    global _SHARED_JS_CRAWLER, _SHARED_JS_CRAWLER_SIGNATURE

    if AsyncWebCrawler is None:
        return None

    browser_config, config_sig = _get_shared_browser_config()
    crawler = _SHARED_JS_CRAWLER
    if crawler is not None and _SHARED_JS_CRAWLER_SIGNATURE == config_sig:
        return crawler

    async with _get_shared_js_crawler_lock():
        crawler = _SHARED_JS_CRAWLER
        if crawler is not None and _SHARED_JS_CRAWLER_SIGNATURE == config_sig:
            return crawler

        if crawler is not None:
            with suppress(Exception):
                await _close_crawler(crawler)
            _SHARED_JS_CRAWLER = None
            _SHARED_JS_CRAWLER_SIGNATURE = None

        crawler = AsyncWebCrawler(config=browser_config)
        await _start_crawler(crawler)
        _SHARED_JS_CRAWLER = crawler
        _SHARED_JS_CRAWLER_SIGNATURE = config_sig
        return crawler


async def _close_shared_js_crawler() -> None:
    global _SHARED_JS_CRAWLER, _SHARED_JS_CRAWLER_SIGNATURE

    async with _get_shared_js_crawler_lock():
        crawler = _SHARED_JS_CRAWLER
        _SHARED_JS_CRAWLER = None
        _SHARED_JS_CRAWLER_SIGNATURE = None

    if crawler is not None:
        with suppress(Exception):
            await _close_crawler(crawler)


async def _shutdown_shared_resources() -> None:
    await _close_shared_js_crawler()
    await _close_http_clients()


async def _fetch_http(
    url: str,
    timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
    *,
    require_public_url: bool = False,
) -> FetchResult:
    if httpx is None:
        return FetchResult(
            success=False,
            status_code=None,
            final_url=url,
            html="",
            content_type="",
            resource_type="other",
            error_code="DEP_MISSING",
            error_message="httpx is not installed",
            retryable=False,
        )

    httpx_mod = httpx
    timeout = httpx_mod.Timeout(timeout_seconds)

    async def _attempt(verify_ssl: bool) -> Any:
        client = await _get_http_client(verify_ssl)
        if require_public_url:
            return await _get_with_public_redirect_validation(
                client,
                url,
                timeout=timeout,
            )
        return await client.get(url, timeout=timeout)

    try:
        response = await _attempt(True)
    except UnsafePublicURL as exc:
        return FetchResult(
            success=False,
            status_code=None,
            final_url=url,
            html="",
            content_type="",
            resource_type="other",
            error_code="UNSAFE_URL",
            error_message=str(exc),
            retryable=False,
        )
    except (httpx_mod.ConnectError, ssl.SSLError):
        try:
            response = await _attempt(False)
        except Exception as exc:
            return FetchResult(
                success=False,
                status_code=None,
                final_url=url,
                html="",
                content_type="",
                resource_type="other",
                error_code="HTTP_CONNECT_ERROR",
                error_message=f"{type(exc).__name__}: {exc}",
                retryable=True,
            )
    except httpx_mod.TimeoutException as exc:
        return FetchResult(
            success=False,
            status_code=None,
            final_url=url,
            html="",
            content_type="",
            resource_type="other",
            error_code="HTTP_TIMEOUT",
            error_message=f"{type(exc).__name__}: {exc}",
            retryable=True,
        )
    except Exception as exc:
        return FetchResult(
            success=False,
            status_code=None,
            final_url=url,
            html="",
            content_type="",
            resource_type="other",
            error_code="HTTP_REQUEST_ERROR",
            error_message=f"{type(exc).__name__}: {exc}",
            retryable=True,
        )

    status = response.status_code
    content_type = response.headers.get("content-type", "")
    body = response.content or b""
    final_url = str(response.url)
    resource_type = _detect_resource_type(
        final_url, content_type, body[:RESOURCE_PROBE_BYTES]
    )
    diagnostics = {
        "resource_type": resource_type,
        "bytes_size": len(body),
        "looks_like_pdf_url": _looks_like_pdf_url(final_url),
        "looks_like_pdf_bytes": _looks_like_pdf_bytes(body[:RESOURCE_PROBE_BYTES]),
        "content_type_is_pdf": _is_pdf_content_type(content_type),
    }

    if status >= 400:
        return FetchResult(
            success=False,
            status_code=status,
            final_url=final_url,
            html=(response.text or "") if _is_html_content_type(content_type) else "",
            content_type=content_type,
            resource_type=resource_type,
            diagnostics=diagnostics,
            error_code="HTTP_STATUS",
            error_message=f"HTTP {status}",
            retryable=status in RETRYABLE_HTTP_STATUS,
        )

    if resource_type == "pdf":
        return FetchResult(
            success=True,
            status_code=status,
            final_url=final_url,
            html="",
            content_type=content_type,
            resource_type="pdf",
            body_bytes=body,
            diagnostics=diagnostics,
            error_code=None,
            error_message=None,
            retryable=False,
        )

    if resource_type != "html":
        return FetchResult(
            success=False,
            status_code=status,
            final_url=final_url,
            html="",
            content_type=content_type,
            resource_type=resource_type,
            diagnostics=diagnostics,
            error_code="NON_HTML",
            error_message=f"unsupported content-type: {content_type}",
            retryable=False,
        )

    # 自訂編碼偵測會掃描多組候選；移到工作執行緒，避免批次抓取時
    # 由單一大型頁面長時間堵塞 asyncio event loop。
    decoded_html = await asyncio.to_thread(_decode_html, response)
    return FetchResult(
        success=True,
        status_code=status,
        final_url=final_url,
        html=decoded_html,
        content_type=content_type,
        resource_type="html",
        diagnostics=diagnostics,
        error_code=None,
        error_message=None,
        retryable=False,
    )


async def _probe_resource_type(
    url: str,
    timeout_seconds: float = RESOURCE_PROBE_TIMEOUT_SECONDS,
    *,
    require_public_url: bool = False,
) -> dict[str, Any]:
    if httpx is None:
        return {
            "resource_type": "unknown",
            "status_code": None,
            "content_type": "",
            "sample_len": 0,
            "error_code": "DEP_MISSING",
        }

    httpx_mod = httpx
    timeout = httpx_mod.Timeout(timeout_seconds)
    range_headers = {"Range": f"bytes=0-{RESOURCE_PROBE_BYTES - 1}"}

    async def _attempt(verify_ssl: bool) -> Any:
        client = await _get_http_client(verify_ssl)
        if require_public_url:
            return await _get_with_public_redirect_validation(
                client,
                url,
                headers=range_headers,
                timeout=timeout,
            )
        return await client.get(url, headers=range_headers, timeout=timeout)

    try:
        response = await _attempt(True)
    except UnsafePublicURL as exc:
        return {
            "resource_type": "unsafe",
            "status_code": None,
            "content_type": "",
            "sample_len": 0,
            "error_code": "UNSAFE_URL",
            "error_message": str(exc),
        }
    except (httpx_mod.ConnectError, ssl.SSLError):
        try:
            response = await _attempt(False)
        except Exception as exc:
            return {
                "resource_type": "unknown",
                "status_code": None,
                "content_type": "",
                "sample_len": 0,
                "error_code": f"{type(exc).__name__}: {exc}",
            }
    except Exception as exc:
        return {
            "resource_type": "unknown",
            "status_code": None,
            "content_type": "",
            "sample_len": 0,
            "error_code": f"{type(exc).__name__}: {exc}",
        }

    sample = (response.content or b"")[:RESOURCE_PROBE_BYTES]
    content_type = response.headers.get("content-type", "")
    final_url = str(response.url)
    return {
        "resource_type": _detect_resource_type(final_url, content_type, sample),
        "status_code": response.status_code,
        "content_type": content_type,
        "sample_len": len(sample),
        "final_url": final_url,
    }

def _candidate_selection_score(
    text: str,
    source: str,
    html: str,
    *,
    title: str,
    extraction_mode: str,
) -> CandidateEvaluation:
    postprocess = _run_postprocess_pipeline(
        text, title=title, extraction_mode=extraction_mode
    )
    cleaned = postprocess.text
    metrics = _analyze_content_quality(cleaned, html)
    quality = _assess_content_quality(cleaned, metrics)
    score = metrics.quality_score - _content_noise_penalty(cleaned, title)

    if not quality.usable:
        score -= 1200
    elif source in {
        "html_article",
        "html_main",
        "html_role_main",
        "html_article_body",
    }:
        score += 120

    if extraction_mode == EXTRACTION_MODE_STRICT:
        if quality.usable and metrics.quality_score >= int(
            _quality_profile(metrics.language_hint)["min_usable_score"]
        ):
            if source in {"trafilatura", "trafilatura_nolinks", "trafilatura_baseline"}:
                score += 220
            elif source.startswith("fit_markdown"):
                score += 120
            elif source.startswith("raw_markdown"):
                score -= 80
            elif source.startswith("html_fallback"):
                score += 40
        if cleaned.startswith("# "):
            score += 120
    elif cleaned.startswith("# "):
        score += 60

    if not quality.acceptable:
        score -= 80

    return CandidateEvaluation(
        score=score,
        metrics=metrics,
        cleaned=cleaned,
        quality=quality,
        postprocess_steps=postprocess.steps,
    )


def _best_text_from_js_result(
    result: Any, *, extraction_mode: str = EXTRACTION_MODE_GENERAL
) -> tuple[str, str, str, list[dict[str, Any]]]:
    def _pick_best(
        candidates: list[tuple[str, str]], html: str, title_hint: str
    ) -> tuple[str, str, ContentMetrics, QualityDecision, list[dict[str, Any]], int]:
        best_text = ""
        best_source = "empty"
        best_metrics = ContentMetrics(0, 0, 0, 1.0, 0, 0, "latin")
        best_quality = QualityDecision(False, False, "EMPTY_CONTENT")
        best_steps: list[dict[str, Any]] = []
        best_score = -1
        for text, source in candidates:
            evaluation = _candidate_selection_score(
                text,
                source,
                html,
                title=title_hint,
                extraction_mode=extraction_mode,
            )
            if evaluation.score > best_score:
                best_score = evaluation.score
                best_text = evaluation.cleaned
                best_source = source
                best_metrics = evaluation.metrics
                best_quality = evaluation.quality
                best_steps = evaluation.postprocess_steps
        return (
            best_text,
            best_source,
            best_metrics,
            best_quality,
            best_steps,
            best_score,
        )

    title = ""
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        title = (metadata.get("title") or "").strip()

    markdown_candidates: list[tuple[str, str]] = []
    md = getattr(result, "markdown", None)
    if md:
        fit = getattr(md, "fit_markdown", "") or ""
        raw = getattr(md, "raw_markdown", "") or str(md)
        if fit:
            markdown_candidates.append((fit, "fit_markdown"))
        if raw:
            markdown_candidates.append((raw, "raw_markdown"))

    cleaned_html = getattr(result, "cleaned_html", "") or ""
    raw_html = getattr(result, "html", "") or ""

    (
        best_text,
        best_source,
        best_metrics,
        best_quality,
        best_steps,
        best_score,
    ) = _pick_best(
        markdown_candidates, raw_html, title
    )

    if extraction_mode == EXTRACTION_MODE_STRICT or not _content_meets_accept(
        best_metrics
    ):
        html_candidates: list[tuple[str, str]] = []
        if cleaned_html:
            txt, source, html_title, _ = _extract_http_content_bundle(
                cleaned_html,
                getattr(result, "url", ""),
                extraction_mode=extraction_mode,
            )
            if txt:
                if not title and html_title:
                    title = html_title
                html_candidates.append((txt, f"{source}:cleaned_html"))

        if raw_html:
            txt, source, html_title, _ = _extract_http_content_bundle(
                raw_html,
                getattr(result, "url", ""),
                extraction_mode=extraction_mode,
            )
            if txt:
                if not title and html_title:
                    title = html_title
                html_candidates.append((txt, f"{source}:raw_html"))

        (
            html_best_text,
            html_best_source,
            html_best_metrics,
            html_best_quality,
            html_best_steps,
            html_best_score,
        ) = _pick_best(
            html_candidates, raw_html, title
        )
        if (
            extraction_mode == EXTRACTION_MODE_STRICT
            and html_best_text
            and (
                html_best_score > best_score
                or (html_best_quality.acceptable and not best_quality.acceptable)
            )
        ):
            best_text, best_source, best_metrics, best_quality, best_steps = (
                html_best_text,
                html_best_source,
                html_best_metrics,
                html_best_quality,
                html_best_steps,
            )
        elif _content_meets_accept(html_best_metrics):
            best_text, best_source, best_metrics, best_quality, best_steps = (
                html_best_text,
                html_best_source,
                html_best_metrics,
                html_best_quality,
                html_best_steps,
            )

    if not title:
        title = _extract_title(raw_html, best_text)

    return best_text, best_source, title, best_steps


async def _crawl_js(
    url: str,
    *,
    wait_time: float = 3.0,
    js_code: str | None = None,
    css_selector: str | None = None,
    extraction_mode: str = EXTRACTION_MODE_GENERAL,
    require_public_url: bool = False,
) -> dict[str, Any]:
    if AsyncWebCrawler is None:
        return {
            "success": False,
            "error_code": "DEP_MISSING",
            "error_message": "crawl4ai is not installed",
            "retryable": False,
        }

    if require_public_url:
        allowed, reason = await _public_url_status(url)
        if not allowed:
            return {
                "success": False,
                "error_code": "UNSAFE_URL",
                "error_message": reason,
                "retryable": False,
            }

    browser_config, _ = _get_shared_browser_config()

    run_kwargs: dict[str, Any] = {
        "delay_before_return_html": wait_time,
        "remove_overlay_elements": True,
        "remove_forms": True,
        "wait_until": "domcontentloaded",
        "verbose": False,
        "excluded_tags": ["nav", "footer", "aside"],
        "exclude_external_images": True,
    }
    if js_code:
        run_kwargs["js_code"] = js_code
    if css_selector:
        run_kwargs["css_selector"] = css_selector
    if CacheMode is not None:
        run_kwargs["cache_mode"] = CacheMode.BYPASS

    run_config = _build_run_config(**run_kwargs)

    async def _run_shared() -> Any | None:
        crawler = await _get_shared_js_crawler()
        if crawler is None:
            return None
        return await crawler.arun(url=url, config=run_config)

    async def _run_fresh() -> Any:
        async with AsyncWebCrawler(config=browser_config) as fresh_crawler:
            return await fresh_crawler.arun(url=url, config=run_config)

    try:
        result = await _run_shared()
        if result is None:
            result = await _run_fresh()
    except Exception:
        with suppress(Exception):
            await _close_shared_js_crawler()
        try:
            result = await _run_fresh()
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            error_code = _classify_js_error(message)
            return {
                "success": False,
                "error_code": error_code,
                "error_message": message,
                "retryable": error_code != "JS_BROWSER_MISSING",
            }

    if not getattr(result, "success", False):
        error_message = getattr(result, "error_message", "crawl failed")
        return {
            "success": False,
            "status_code": getattr(result, "status_code", None),
            "error_code": _classify_js_error(error_message, "JS_RESULT_ERROR"),
            "error_message": error_message,
            "retryable": True,
        }

    final_url = str(getattr(result, "url", "") or url)
    if require_public_url:
        allowed, reason = await _public_url_status(final_url)
        if not allowed:
            return {
                "success": False,
                "error_code": "UNSAFE_REDIRECT_URL",
                "error_message": reason,
                "retryable": False,
            }

    content, source, title, postprocess_steps = _best_text_from_js_result(
        result, extraction_mode=extraction_mode
    )
    html = getattr(result, "html", "") or ""
    metrics = _analyze_content_quality(content, html)
    quality = _assess_content_quality(content, metrics)

    return {
        "success": True,
        "status_code": getattr(result, "status_code", None),
        "content": content,
        "content_source": source,
        "title": title,
        "metrics": metrics,
        "quality": quality,
        "html": html,
        "html_len": len(html),
        "postprocess_steps": postprocess_steps,
        "error_code": None,
        "error_message": None,
        "retryable": False,
        "final_url": final_url,
    }


def _need_js_fallback(
    *,
    status_code: int | None,
    html_len: int,
    content: str,
    metrics: ContentMetrics,
    quality: QualityDecision,
    render: str,
) -> tuple[bool, str | None]:
    if render == "never":
        return False, None
    if render == "always":
        return True, "render_always"

    force_js_score = int(_quality_profile(metrics.language_hint)["force_js_score"])
    if quality.reason and quality.reason != "BELOW_ACCEPT_THRESHOLD":
        return True, quality.reason

    if _comments_likely_dynamic(content, metrics):
        return True, "comments_likely_dynamic"

    if status_code in (403, 429):
        return True, f"status_{status_code}"
    if metrics.text_len < TEXT_FORCE_JS_LENGTH:
        return True, "text_too_short"
    if metrics.quality_score < force_js_score:
        return True, "quality_too_low"
    if html_len > HTML_LARGE_THRESHOLD and metrics.text_len < TEXT_FORCE_JS_LENGTH:
        return True, "spa_shell_suspected"

    if quality.acceptable:
        return False, None
    if metrics.js_signals >= 1:
        return True, "js_signal_detected"
    return True, quality.reason or "below_accept_threshold"


def _finalize_content(content: str, *, title: str, summary_mode: bool) -> str:
    return _finalize_content_result(
        content, title=title, summary_mode=summary_mode
    ).text


def _finalize_content_result(
    content: str, *, title: str, summary_mode: bool
) -> SummaryResult:
    if summary_mode:
        return _compress_summary_result(content, title=title, limit=SUMMARY_LIMIT)
    finalized = _smart_truncate(content, NORMAL_LIMIT)
    blocks = _split_blocks((content or "").strip())
    block_count = len(blocks) if blocks else int(bool(finalized))
    return SummaryResult(finalized, block_count, block_count)


def _finalize_html(html: str, *, summary_mode: bool) -> str:
    limit = HTML_SUMMARY_LIMIT if summary_mode else HTML_NORMAL_LIMIT
    return _smart_truncate((html or "").strip(), limit)


async def _attempt_http(
    url: str,
    *,
    extraction_mode: str,
    http_semaphore: asyncio.Semaphore | None,
    pdf_semaphore: asyncio.Semaphore | None,
) -> tuple[AttemptResult, dict[str, int], str]:
    attempt = AttemptResult(attempted=True)
    timings = {
        "http_queue_wait": 0,
        "http_fetch": 0,
        "http_total": 0,
        "http_extract": 0,
    }

    if http_semaphore is not None:
        queue_started = time.perf_counter()
        async with http_semaphore:
            timings["http_queue_wait"] = int(
                (time.perf_counter() - queue_started) * 1000
            )
            fetch_started = time.perf_counter()
            fetch = await _fetch_http(url)
            timings["http_fetch"] = int((time.perf_counter() - fetch_started) * 1000)
    else:
        fetch_started = time.perf_counter()
        fetch = await _fetch_http(url)
        timings["http_fetch"] = int((time.perf_counter() - fetch_started) * 1000)

    timings["http_total"] = timings["http_queue_wait"] + timings["http_fetch"]
    attempt.fetch_success = fetch.success
    attempt.status_code = fetch.status_code
    attempt.resource_type = fetch.resource_type
    attempt.html = fetch.html or ""
    attempt.title = fetch.title
    attempt.error_code = fetch.error_code
    attempt.error_message = fetch.error_message
    attempt.retryable = fetch.retryable
    attempt.resource_diagnostics = dict(fetch.diagnostics or {})

    if fetch.success and fetch.resource_type == "pdf" and fetch.body_bytes:
        extract_started = time.perf_counter()

        async def _run_pdf_extract() -> Any:
            return await asyncio.to_thread(
                extract_pdf_content,
                fetch.body_bytes,
                source_url=fetch.final_url,
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
            attempt.metrics = _analyze_content_quality(attempt.content)
            attempt.content_scope = _content_scope(attempt.content)
            attempt.quality = _assess_content_quality(attempt.content, attempt.metrics)
            attempt.resource_diagnostics["pdf"] = pdf_result.diagnostics
            timings["http_extract"] = int((time.perf_counter() - extract_started) * 1000)
        except Exception as exc:
            attempt.error_code = "PDF_EXTRACT_ERROR"
            attempt.error_message = f"{type(exc).__name__}: {exc}"
            attempt.retryable = False
            timings["http_extract"] = int((time.perf_counter() - extract_started) * 1000)
        return (
            attempt,
            timings,
            "http_fetch_success" if fetch.success else "http_fetch_failed",
        )

    if attempt.html:
        extract_started = time.perf_counter()
        content, source, title, postprocess_steps = await asyncio.to_thread(
            _extract_http_content_bundle,
            attempt.html,
            fetch.final_url,
            extraction_mode=extraction_mode,
        )
        attempt.content = content
        attempt.content_source = source
        attempt.title = title
        attempt.postprocess_steps = postprocess_steps
        attempt.metrics = _analyze_content_quality(content, attempt.html)
        attempt.content_scope = _content_scope(content)
        attempt.quality = _assess_content_quality(content, attempt.metrics)
        timings["http_extract"] = int((time.perf_counter() - extract_started) * 1000)

    return attempt, timings, "http_fetch_success" if fetch.success else "http_fetch_failed"


async def _attempt_js(
    url: str,
    *,
    extraction_mode: str,
    js_semaphore: asyncio.Semaphore,
    wait_time: float,
    js_code: str | None,
    require_public_url: bool = False,
) -> tuple[AttemptResult, dict[str, int], str]:
    attempt = AttemptResult(attempted=True)
    timings = {
        "js_queue_wait": 0,
        "js_exec": 0,
        "js_crawl": 0,
        "js_total": 0,
    }

    queue_started = time.perf_counter()
    async with js_semaphore:
        timings["js_queue_wait"] = int((time.perf_counter() - queue_started) * 1000)
        js_started = time.perf_counter()
        js_result = await _crawl_js(
            url,
            wait_time=wait_time,
            js_code=js_code,
            extraction_mode=extraction_mode,
            require_public_url=require_public_url,
        )
        timings["js_exec"] = int((time.perf_counter() - js_started) * 1000)
    timings["js_crawl"] = timings["js_queue_wait"] + timings["js_exec"]
    timings["js_total"] = timings["js_crawl"]

    attempt.fetch_success = bool(js_result.get("success"))
    attempt.status_code = js_result.get("status_code")
    attempt.error_code = js_result.get("error_code")
    attempt.error_message = js_result.get("error_message")
    attempt.retryable = bool(js_result.get("retryable", False))
    if attempt.fetch_success:
        attempt.content = js_result.get("content", "")
        attempt.html = js_result.get("html", "") or ""
        attempt.title = js_result.get("title", "")
        attempt.content_source = js_result.get("content_source")
        attempt.metrics = js_result.get("metrics") or _analyze_content_quality(
            attempt.content, attempt.html
        )
        attempt.quality = js_result.get("quality") or _assess_content_quality(
            attempt.content, attempt.metrics
        )
        attempt.content_scope = _content_scope(attempt.content)
        attempt.postprocess_steps = list(js_result.get("postprocess_steps") or [])

    return attempt, timings, "js_fetch_success" if attempt.fetch_success else "js_fetch_failed"


def _should_select_js(http_attempt: AttemptResult, js_attempt: AttemptResult, render: str) -> bool:
    if render == "always":
        return True
    if not js_attempt.fetch_success:
        return False
    if js_attempt.quality.usable and not http_attempt.quality.usable:
        return True
    if js_attempt.quality.usable and (
        http_attempt.status_code is not None and http_attempt.status_code >= 400
    ):
        return True
    if (
        js_attempt.quality.usable
        and js_attempt.content_scope == "article_plus_comments"
        and http_attempt.content_scope != "article_plus_comments"
    ):
        return True
    if (
        js_attempt.quality.usable
        and js_attempt.metrics.text_len >= SUCCESS_MIN_TEXT_LENGTH
        and js_attempt.metrics.text_len
        >= max(180, int(http_attempt.metrics.text_len * 1.2))
    ):
        return True
    if (
        js_attempt.quality.usable
        and js_attempt.metrics.quality_score
        >= http_attempt.metrics.quality_score + QUALITY_JS_BETTER_MARGIN
    ):
        return True
    return js_attempt.quality.usable and (
        not http_attempt.quality.acceptable and js_attempt.quality.acceptable
    )


def _finalize_attempt_output(
    attempt: AttemptResult,
    *,
    content_format: str,
    summary_mode: bool,
) -> tuple[str, ContentMetrics, str, QualityDecision, SummaryResult]:
    if attempt.resource_type == "pdf" and content_format == "html":
        summary = _finalize_content_result(
            attempt.content, title=attempt.title, summary_mode=summary_mode
        )
        metrics = _analyze_content_quality(summary.text)
        quality = _assess_content_quality(summary.text, metrics)
        finalized = _finalize_html(
            render_pdf_text_as_html(summary.text, attempt.title),
            summary_mode=summary_mode,
        )
        return finalized, metrics, _content_scope(summary.text), quality, summary

    if content_format == "html":
        finalized = _finalize_html(attempt.html, summary_mode=summary_mode)
        summary = SummaryResult(finalized, int(bool(finalized)), int(bool(finalized)))
        return (
            finalized,
            attempt.metrics,
            attempt.content_scope,
            QualityDecision(bool(finalized), bool(finalized), None if finalized else "EMPTY_HTML"),
            summary,
        )

    summary = _finalize_content_result(
        attempt.content, title=attempt.title, summary_mode=summary_mode
    )
    metrics = _analyze_content_quality(summary.text, attempt.html)
    quality = _assess_content_quality(summary.text, metrics)
    return summary.text, metrics, _content_scope(summary.text), quality, summary


async def _crawl_single_url(
    url: str,
    *,
    render: str,
    content_format: str,
    extraction_mode: str,
    summary_mode: bool,
    js_semaphore: asyncio.Semaphore,
    pdf_semaphore: asyncio.Semaphore | None = None,
    http_semaphore: asyncio.Semaphore | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    decision_trace: list[str] = []
    normalized = _normalize_url(url)
    output_format = _normalize_content_format(content_format)
    normalized_extraction_mode = _normalize_extraction_mode(extraction_mode)
    attempts: list[str] = []
    http_attempt = AttemptResult()
    js_attempt = AttemptResult()
    fallback_reason = None
    timings_ms: dict[str, int] = {
        "http_queue_wait": 0,
        "http_fetch": 0,
        "http_total": 0,
        "http_extract": 0,
        "js_queue_wait": 0,
        "js_exec": 0,
        "js_crawl": 0,
        "js_total": 0,
        "finalize": 0,
    }

    def _compose_result(
        *,
        success: bool,
        used_render: str,
        status_code: int | None,
        content: str,
        selected_metrics: ContentMetrics | None = None,
        content_scope: str = "general",
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool | None = None,
        fallback_reason_value: str | None = None,
        final_diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        payload: dict[str, Any] = {
            "url": normalized or url,
            "success": success,
            "status_code": status_code,
            "used_render": used_render,
            "resource_type": (
                final_diagnostics.get("resource_type") or http_attempt.resource_type
                if final_diagnostics
                else http_attempt.resource_type
            ),
            "content_format": output_format,
            "content": content,
            "content_scope": content_scope,
            "decision_trace": decision_trace[:],
            "diagnostics": {
                "domain": _domain_from_url(normalized or url),
                "attempts": attempts[:],
                "timings_ms": dict(timings_ms),
                "http": http_attempt.to_diag_dict(),
                "js": js_attempt.to_diag_dict(include_fetch_success=True),
                "final": final_diagnostics or {},
            },
            "elapsed_ms": elapsed_ms,
        }
        pdf_diag = http_attempt.resource_diagnostics.get("pdf") or js_attempt.resource_diagnostics.get("pdf")
        if pdf_diag:
            payload["diagnostics"]["pdf"] = pdf_diag
        if selected_metrics is not None:
            payload["metrics"] = selected_metrics.to_dict()
        if final_diagnostics and final_diagnostics.get("content_source"):
            payload["content_source"] = final_diagnostics["content_source"]
        if fallback_reason_value:
            payload["fallback_reason"] = fallback_reason_value
        if error_code is not None:
            payload["error_code"] = error_code
        if error_message is not None:
            payload["error_message"] = error_message
        if retryable is not None:
            payload["retryable"] = retryable
        return payload

    if not normalized or not normalized.startswith(("http://", "https://")):
        return _compose_result(
            success=False,
            used_render="none",
            status_code=None,
            content="",
            selected_metrics=http_attempt.metrics,
            error_code="BAD_URL",
            error_message="URL must start with http:// or https://",
            retryable=False,
        )

    if render != "always":
        attempts.append("http")
        http_attempt, http_timing, http_trace = await _attempt_http(
            normalized,
            extraction_mode=normalized_extraction_mode,
            http_semaphore=http_semaphore,
            pdf_semaphore=pdf_semaphore,
        )
        timings_ms.update(http_timing)
        decision_trace.append(http_trace)
    else:
        preflight_hit_pdf = False
        if PDF_AUTO_EXTRACT_ENABLED:
            preflight = await _probe_resource_type(normalized)
            decision_trace.append(f"preflight:{preflight.get('resource_type', 'unknown')}")
            if preflight.get("resource_type") == "pdf":
                preflight_hit_pdf = True
                attempts.append("http")
                http_attempt, http_timing, http_trace = await _attempt_http(
                    normalized,
                    extraction_mode=normalized_extraction_mode,
                    http_semaphore=http_semaphore,
                    pdf_semaphore=pdf_semaphore,
                )
                timings_ms.update(http_timing)
                decision_trace.append(http_trace)
        if not preflight_hit_pdf:
            decision_trace.append("render_always_skip_http")

    if http_attempt.resource_type == "pdf":
        finalize_started = time.perf_counter()
        (
            final_content,
            final_metrics,
            final_content_scope,
            final_quality,
            final_summary,
        ) = _finalize_attempt_output(
            http_attempt, content_format=output_format, summary_mode=summary_mode
        )
        timings_ms["finalize"] = int((time.perf_counter() - finalize_started) * 1000)
        decision_trace.append("result_selected:pdf")
        final_diag = {
            "selected": "pdf",
            "resource_type": "pdf",
            "content_source": http_attempt.content_source,
            "summary_mode": bool(summary_mode),
            "summary": final_summary.to_dict(),
            "postprocess_steps": http_attempt.postprocess_steps,
            "quality": final_quality.to_dict(),
        }
        if not http_attempt.content:
            return _compose_result(
                success=False,
                used_render="http",
                status_code=http_attempt.status_code,
                content=final_content,
                selected_metrics=final_metrics,
                content_scope=final_content_scope,
                error_code=http_attempt.error_code or final_quality.reason or "PDF_EXTRACT_ERROR",
                error_message=http_attempt.error_message or "unable to extract pdf content",
                retryable=False,
                final_diagnostics=final_diag,
            )
        if not final_quality.usable:
            return _compose_result(
                success=False,
                used_render="http",
                status_code=http_attempt.status_code,
                content=final_content,
                selected_metrics=final_metrics,
                content_scope=final_content_scope,
                error_code=final_quality.reason or "LOW_VALUE_CONTENT",
                error_message="pdf extracted but below quality threshold",
                retryable=False,
                final_diagnostics=final_diag,
            )
        return _compose_result(
            success=True,
            used_render="http",
            status_code=http_attempt.status_code,
            content=final_content,
            selected_metrics=final_metrics,
            content_scope=final_content_scope,
            final_diagnostics=final_diag,
        )

    need_js, trigger = _need_js_fallback(
        status_code=http_attempt.status_code,
        html_len=len(http_attempt.html),
        content=http_attempt.content,
        metrics=http_attempt.metrics,
        quality=http_attempt.quality,
        render=render,
    )
    decision_trace.append(f"fallback_check:{'js' if need_js else 'http'}:{trigger}")

    if need_js:
        attempts.append("js")
        fallback_reason = trigger
        js_wait_time = (
            4.5
            if trigger in {"comments_likely_dynamic", "below_accept_threshold"}
            else 3.5
        )
        js_code = GENERIC_JS_ENHANCE_SNIPPET
        js_attempt, js_timing, js_trace = await _attempt_js(
            normalized,
            extraction_mode=normalized_extraction_mode,
            js_semaphore=js_semaphore,
            wait_time=js_wait_time,
            js_code=js_code,
        )
        timings_ms.update(js_timing)
        decision_trace.append(js_trace)

        if js_attempt.fetch_success and _should_select_js(http_attempt, js_attempt, render):
            finalize_started = time.perf_counter()
            (
                final_content,
                final_metrics,
                final_content_scope,
                final_quality,
                final_summary,
            ) = _finalize_attempt_output(
                js_attempt, content_format=output_format, summary_mode=summary_mode
            )
            timings_ms["finalize"] = int((time.perf_counter() - finalize_started) * 1000)
            decision_trace.append("result_selected:js")
            final_diag = {
                "selected": "js",
                "summary_mode": bool(summary_mode),
                "summary": final_summary.to_dict(),
                "postprocess_steps": js_attempt.postprocess_steps,
                "quality": final_quality.to_dict(),
            }
            if not final_quality.usable:
                return _compose_result(
                    success=False,
                    used_render="js",
                    status_code=js_attempt.status_code,
                    content=final_content,
                    selected_metrics=final_metrics,
                    content_scope=final_content_scope,
                    error_code=final_quality.reason or "LOW_VALUE_CONTENT",
                    error_message="content extracted but below quality threshold",
                    retryable=False,
                    fallback_reason_value=trigger,
                    final_diagnostics=final_diag,
                )
            return _compose_result(
                success=True,
                used_render="js",
                status_code=js_attempt.status_code,
                content=final_content,
                selected_metrics=final_metrics,
                content_scope=final_content_scope,
                fallback_reason_value=trigger,
                final_diagnostics=final_diag,
            )

        if render == "always" and not http_attempt.content:
            return _compose_result(
                success=False,
                used_render="js",
                status_code=js_attempt.status_code,
                content="",
                selected_metrics=http_attempt.metrics,
                error_code=js_attempt.error_code or "JS_ERROR",
                error_message=js_attempt.error_message or "js crawl failed",
                retryable=js_attempt.retryable,
                fallback_reason_value=trigger,
                final_diagnostics={
                    "selected": "js",
                    "summary_mode": bool(summary_mode),
                    "summary": SummaryResult("", 0, 0).to_dict(),
                    "postprocess_steps": js_attempt.postprocess_steps,
                    "quality": js_attempt.quality.to_dict(),
                },
            )

        if js_attempt.fetch_success:
            fallback_reason = "http_selected_over_js"
            reject_reason = (
                js_attempt.quality.reason
                if not js_attempt.quality.usable
                else "lower_than_http"
            )
            decision_trace.append(f"js_rejected:{reject_reason}")

    if http_attempt.content or (output_format == "html" and http_attempt.html):
        finalize_started = time.perf_counter()
        (
            final_content,
            final_metrics,
            final_content_scope,
            final_quality,
            final_summary,
        ) = _finalize_attempt_output(
            http_attempt, content_format=output_format, summary_mode=summary_mode
        )
        timings_ms["finalize"] = int((time.perf_counter() - finalize_started) * 1000)
        decision_trace.append("result_selected:http")
        final_diag = {
            "selected": "http",
            "summary_mode": bool(summary_mode),
            "summary": final_summary.to_dict(),
            "postprocess_steps": http_attempt.postprocess_steps,
            "quality": final_quality.to_dict(),
        }
        if not final_quality.usable:
            return _compose_result(
                success=False,
                used_render="http",
                status_code=http_attempt.status_code,
                content=final_content,
                selected_metrics=final_metrics,
                content_scope=final_content_scope,
                error_code=final_quality.reason or "LOW_VALUE_CONTENT",
                error_message="content extracted but below quality threshold",
                retryable=False,
                fallback_reason_value=fallback_reason,
                final_diagnostics=final_diag,
            )
        return _compose_result(
            success=True,
            used_render="http",
            status_code=http_attempt.status_code,
            content=final_content,
            selected_metrics=final_metrics,
            content_scope=final_content_scope,
            fallback_reason_value=fallback_reason,
            final_diagnostics=final_diag,
        )

    decision_trace.append("result_selected:none")
    return _compose_result(
        success=False,
        used_render="http",
        status_code=http_attempt.status_code,
        content="",
        selected_metrics=http_attempt.metrics,
        content_scope=http_attempt.content_scope,
        error_code=http_attempt.error_code or "EXTRACT_EMPTY",
        error_message=http_attempt.error_message or "unable to extract content",
        retryable=http_attempt.retryable,
        fallback_reason_value=fallback_reason,
        final_diagnostics={
            "selected": "none",
            "summary_mode": bool(summary_mode),
            "summary": SummaryResult("", 0, 0).to_dict(),
            "postprocess_steps": [],
            "quality": http_attempt.quality.to_dict(),
        },
    )


def _extract_links(html: str, base_url: str, max_links: int) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()

    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        for node in soup.select("a[href]"):
            href_value = node.get("href")
            if isinstance(href_value, list):
                href = (href_value[0] if href_value else "").strip()
            else:
                href = str(href_value or "").strip()
            if not href:
                continue
            absolute = urljoin(base_url, href)
            if absolute in seen:
                continue
            if not absolute.startswith(("http://", "https://")):
                continue
            seen.add(absolute)
            links.append(
                {"url": absolute, "text": node.get_text(" ", strip=True)[:120]}
            )
            if len(links) >= max_links:
                break
        return links

    pattern = re.compile(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
    )
    for match in pattern.finditer(html):
        href = (match.group(1) or "").strip()
        absolute = urljoin(base_url, href)
        if not absolute.startswith(("http://", "https://")):
            continue
        if absolute in seen:
            continue
        text = re.sub(r"<[^>]+>", "", match.group(2) or "")
        text = re.sub(r"\s+", " ", text).strip()
        seen.add(absolute)
        links.append({"url": absolute, "text": text[:120]})
        if len(links) >= max_links:
            break

    return links


def _kwic(
    text: str, keyword: str, context_chars: int = 40, max_snippets: int = 3
) -> list[str]:
    if not text or not keyword:
        return []

    snippets: list[str] = []
    lower_text = text.lower()
    lower_kw = keyword.lower()
    start = 0
    while len(snippets) < max_snippets:
        pos = lower_text.find(lower_kw, start)
        if pos < 0:
            break
        left = max(0, pos - context_chars)
        right = min(len(text), pos + len(keyword) + context_chars)
        snippet = text[left:right]
        if left > 0:
            snippet = "..." + snippet
        if right < len(text):
            snippet = snippet + "..."
        if snippet not in snippets:
            snippets.append(snippet)
        start = pos + len(keyword)
    return snippets


async def crawl4ai_crawl(
    urls: Annotated[
        list[str],
        "目標 URL 列表（必填，1-20 筆）。每個元素必須以 http:// 或 https:// 開頭。",
    ],
    summary_mode: Annotated[
        bool | None,
        (
            "摘要模式開關。true=每筆 ≤6000 字元精簡輸出；false=每筆 ≤12000 字元完整輸出。"
            "null（預設）=自動判斷：多 URL 時啟用，單 URL 時關閉。"
        ),
    ] = None,
    render: Annotated[
        str,
        (
            "渲染策略，可選值："
            "auto（預設）— HTTP 優先，品質不足時自動 JS 降級；"
            "never — 僅 HTTP，完全不啟動瀏覽器（最快，適合已知靜態頁面）；"
            "always — 跳過 HTTP，直接啟動 headless 瀏覽器抓取（適合已知 SPA/重度 JS 頁面）。"
        ),
    ] = "auto",
    max_concurrency: Annotated[
        int,
        (
            "批次最大並行數（1-20，預設 20）。HTTP 並行上限即此值；"
            "JS 瀏覽器並行上限固定為 min(4, max_concurrency)，避免資源耗盡。"
        ),
    ] = 20,
    content_format: Annotated[
        str,
        "輸出格式：markdown（預設）或 html。html 會回傳頁面 HTML 結構內容。",
    ] = "markdown",
    extraction_mode: Annotated[
        str,
        (
            "內容抽取模式：general（預設）偏完整召回；"
            "strict 偏乾淨正文，會更積極移除導航、廣告、站點殼層，但仍盡量保留正文與留言。"
        ),
    ] = EXTRACTION_MODE_GENERAL,
) -> str:
    """
    批量爬取多個 URL 的網頁正文。

    內部流程（render=auto 時）：
    1. 先以輕量 HTTP（httpx）抓取 HTML。
    2. 將 HTML 轉為 Markdown，評估品質分數（quality_score）。
    3. 若品質不足（文字太短、JS 信號偵測到、SPA 殼、HTTP 403/429），
       自動啟動 headless 瀏覽器補抓，並比較兩者結果取較優。

    Args:
        urls: 目標 URL 列表（1-20 筆），必須以 http:// 或 https:// 開頭
        summary_mode: 摘要模式。true=精簡（≤6000 字元），false=完整（≤12000 字元），
                      null=自動（多 URL 啟用，單 URL 關閉）
        render: 渲染策略。auto=HTTP 優先+自動 JS 降級，never=僅 HTTP，always=直接 JS
        max_concurrency: 批次並行數（1-20，預設 20）

    Returns:
        str: JSON 字串，結構如下：
             {
               "success": true,
               "render": "auto",
               "summary_mode": true/false,
               "max_concurrency": 20,
               "stats": { "total", "ok", "failed", "used_js", "elapsed_ms" },
               "results": [
                 {
                   "url": "...",
                   "success": true/false,
                   "status_code": 200,
                   "used_render": "http" | "js",
                   "content": "Markdown 正文...",
                   "content_scope": "general" | "article_plus_comments",
                   "metrics": { "quality_score", "text_len", "line_count", ... },
                   "decision_trace": ["http_fetch_success", "fallback_check:http:...", ...],
                   "diagnostics": { "domain", "http": {...}, "js": {...} },
                   "error_code": null | "BAD_URL" | "EXTRACT_EMPTY" | ...,
                   "error_message": null | "...",
                   "retryable": false,
                   "elapsed_ms": 1234
                 }, ...
               ]
             }
    """
    if not isinstance(urls, list) or not urls:
        return _json(
            {
                "success": False,
                "error": "urls must be a non-empty list",
            }
        )

    if len(urls) > MAX_URLS:
        return _json(
            {
                "success": False,
                "error": f"up to {MAX_URLS} urls are supported",
            }
        )

    render_mode = (render or "auto").strip().lower()
    if render_mode not in {"auto", "never", "always"}:
        render_mode = "auto"

    normalized_content_format = (content_format or "markdown").strip().lower()
    if normalized_content_format not in {"markdown", "html"}:
        return _json(
            {
                "success": False,
                "error": "content_format must be 'markdown' or 'html'",
            }
        )
    output_format = _normalize_content_format(content_format)
    normalized_extraction_mode = _normalize_extraction_mode(extraction_mode)

    if summary_mode is None:
        summary_mode = len(urls) > 1

    effective_concurrency = _clamp(int(max_concurrency or 1), 1, MAX_CONCURRENCY)
    js_concurrency = min(MAX_JS_CONCURRENCY, effective_concurrency)

    http_semaphore = asyncio.Semaphore(effective_concurrency)
    js_semaphore = asyncio.Semaphore(js_concurrency)
    pdf_semaphore = asyncio.Semaphore(MAX_PDF_CONCURRENCY)

    async def _worker(input_url: str) -> dict[str, Any]:
        try:
            return await _crawl_single_url(
                input_url,
                render=render_mode,
                content_format=output_format,
                extraction_mode=normalized_extraction_mode,
                summary_mode=bool(summary_mode),
                js_semaphore=js_semaphore,
                pdf_semaphore=pdf_semaphore,
                http_semaphore=http_semaphore,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("批次爬取工作器發生未預期錯誤：%s", input_url)
            return {
                "url": input_url,
                "success": False,
                "status_code": None,
                "used_render": "none",
                "resource_type": "unknown",
                "content_format": output_format,
                "content": "",
                "content_scope": "general",
                "error_code": "CRAWL_WORKER_ERROR",
                "error_message": f"{type(exc).__name__}: {exc}",
                "retryable": False,
                "elapsed_ms": 0,
            }

    started = time.time()
    results = await asyncio.gather(*[_worker(url) for url in urls])
    elapsed_ms = int((time.time() - started) * 1000)

    success_count = sum(1 for item in results if item.get("success"))
    js_count = sum(1 for item in results if item.get("used_render") == "js")
    pdf_count = sum(1 for item in results if item.get("resource_type") == "pdf")

    payload = {
        "success": True,
        "render": render_mode,
        "content_format": output_format,
        "extraction_mode": normalized_extraction_mode,
        "summary_mode": bool(summary_mode),
        "max_concurrency": effective_concurrency,
        "stats": {
            "total": len(results),
            "ok": success_count,
            "failed": len(results) - success_count,
            "used_js": js_count,
            "used_pdf": pdf_count,
            "elapsed_ms": elapsed_ms,
        },
        "results": results,
    }
    return _json(payload)


async def crawl4ai_with_js(
    url: Annotated[
        str,
        "目標頁面 URL（必填），必須以 http:// 或 https:// 開頭。",
    ],
    js_template: Annotated[
        str,
        (
            "內建 JS 模板名稱（可選，預設 none）。可用值："
            "none — 不執行額外腳本；"
            "scroll_to_bottom — 載入後捲動到頁面底部（觸發 lazy-load）；"
            "dismiss_popups — 嘗試關閉常見彈窗/modal（點擊 close 按鈕）；"
            "wait_for_network_idle — 額外等待 800ms 讓非同步請求完成。"
        ),
    ] = "none",
    wait_time: Annotated[
        float,
        (
            "瀏覽器渲染後等待秒數（1.0-20.0，預設 3.5）。"
            "頁面載入完成後，等待此時長再擷取內容。"
            "較大值適合需要長時間載入的重度頁面；較小值加快速度但可能漏抓。"
        ),
    ] = 3.5,
    summary_mode: Annotated[
        bool,
        "摘要模式。true=精簡輸出（≤6000 字元），false=完整輸出（≤12000 字元，預設）。",
    ] = False,
    content_format: Annotated[
        str,
        "輸出格式：markdown（預設）或 html。html 會回傳頁面 HTML 結構內容。",
    ] = "markdown",
    extraction_mode: Annotated[
        str,
        "內容抽取模式：general（預設）或 strict。",
    ] = EXTRACTION_MODE_GENERAL,
) -> str:
    """
    強制以 headless 瀏覽器渲染並爬取單一頁面。

    與 crawl4ai_crawl(render="always") 的差異：
    - 本工具支援 js_template 參數，可在頁面載入後執行額外腳本。
    - 一次只處理一個 URL，提供更精細的控制。

    Args:
        url: 目標頁面 URL，必須以 http:// 或 https:// 開頭
        js_template: 內建 JS 模板，可選 none / scroll_to_bottom / dismiss_popups / wait_for_network_idle
        wait_time: 渲染後等待秒數（1.0-20.0，預設 3.5）
        summary_mode: true=精簡輸出（≤6000 字元），false=完整輸出（≤12000 字元）

    Returns:
        str: JSON 字串，結構如下：
             {
               "success": true/false,
               "url": "...",
               "used_render": "js",
               "js_template": "none",
               "status_code": 200,
               "content": "Markdown 正文...",
               "metrics": { "quality_score", "text_len", "line_count", ... }
             }
             失敗時包含 error_code、error_message、retryable 欄位。
    """
    normalized = _normalize_url(url)
    if not normalized.startswith(("http://", "https://")):
        return _json({"success": False, "error": "invalid url"})

    normalized_content_format = (content_format or "markdown").strip().lower()
    if normalized_content_format not in {"markdown", "html"}:
        return _json(
            {
                "success": False,
                "error": "content_format must be 'markdown' or 'html'",
            }
        )
    output_format = _normalize_content_format(content_format)
    normalized_extraction_mode = _normalize_extraction_mode(extraction_mode)
    template = (js_template or "none").strip().lower()
    if PDF_AUTO_EXTRACT_ENABLED:
        preflight = await _probe_resource_type(normalized)
        if preflight.get("resource_type") == "pdf":
            pdf_result = await _crawl_single_url(
                normalized,
                render="never",
                content_format=output_format,
                extraction_mode=normalized_extraction_mode,
                summary_mode=bool(summary_mode),
                js_semaphore=asyncio.Semaphore(1),
                pdf_semaphore=asyncio.Semaphore(1),
                http_semaphore=asyncio.Semaphore(1),
            )
            payload = {
                "success": pdf_result.get("success", False),
                "url": normalized,
                "used_render": pdf_result.get("used_render", "http"),
                "js_template": template,
                "content_format": output_format,
                "extraction_mode": normalized_extraction_mode,
                "status_code": pdf_result.get("status_code"),
                "resource_type": pdf_result.get("resource_type"),
                "content": pdf_result.get("content", ""),
                "content_scope": pdf_result.get("content_scope"),
                "content_source": pdf_result.get("content_source"),
                "metrics": pdf_result.get("metrics"),
                "diagnostics": pdf_result.get("diagnostics"),
            }
            if not pdf_result.get("success"):
                payload["error_code"] = pdf_result.get("error_code")
                payload["error_message"] = pdf_result.get("error_message")
                payload["retryable"] = bool(pdf_result.get("retryable", False))
            return _json(payload)

    js_code = JS_TEMPLATE_MAP.get(template, "")

    result = await _crawl_js(
        normalized,
        wait_time=max(1.0, min(float(wait_time or 3.5), 20.0)),
        js_code=js_code,
        extraction_mode=normalized_extraction_mode,
    )

    if not result.get("success"):
        return _json(
            {
                "success": False,
                "url": normalized,
                "error_code": result.get("error_code"),
                "error_message": result.get("error_message"),
                "retryable": bool(result.get("retryable", False)),
            }
        )

    metrics: ContentMetrics = result["metrics"]
    if output_format == "html":
        content = _finalize_html(
            result.get("html", ""), summary_mode=bool(summary_mode)
        )
    else:
        content = _finalize_content(
            result.get("content", ""),
            title=result.get("title", ""),
            summary_mode=bool(summary_mode),
        )

    return _json(
        {
            "success": True,
            "url": normalized,
            "used_render": "js",
            "js_template": template,
            "content_format": output_format,
            "extraction_mode": normalized_extraction_mode,
            "status_code": result.get("status_code"),
            "content": content,
            "content_scope": _content_scope(result.get("content", "")),
            "content_source": result.get("content_source"),
            "resource_type": "html",
            "metrics": metrics.to_dict(),
        }
    )


async def crawl4ai_map(
    seed_url: Annotated[
        str,
        "種子頁面 URL（必填），從此頁面提取所有超連結。必須以 http:// 或 https:// 開頭。",
    ],
    max_links: Annotated[
        int,
        "回傳連結數量上限（1-500，預設 80）。實際可能少於此值（取決於頁面連結數量和過濾結果）。",
    ] = 80,
    same_domain: Annotated[
        bool,
        "是否僅保留與 seed_url 同網域的連結（預設 true）。設為 false 可取得跨域外連結。",
    ] = True,
    render: Annotated[
        str,
        (
            "渲染策略，可選值："
            "auto（預設）— HTTP 優先，失敗時自動 JS 降級；"
            "never — 僅 HTTP；"
            "always — 直接 JS。"
        ),
    ] = "auto",
) -> str:
    """
    從種子頁面提取超連結列表。

    使用情境：
    - 探索一個網站有哪些子頁面（配合 same_domain=true）
    - 取得文章列表頁的所有文章連結
    - 為後續 crawl4ai_crawl 或 crawl4ai_probe 準備 URL 清單

    Args:
        seed_url: 種子頁面 URL，必須以 http:// 或 https:// 開頭
        max_links: 連結數量上限（1-500，預設 80）
        same_domain: 僅保留同網域連結（預設 true）
        render: 渲染策略 auto / never / always

    Returns:
        str: JSON 字串，結構如下：
             {
               "success": true/false,
               "seed_url": "...",
               "source": "http" | "js",
               "status_code": 200,
               "count": 42,
               "links": [
                 { "url": "https://...", "text": "連結文字（≤120 字元）" },
                 ...
               ]
             }
    """
    normalized = _normalize_url(seed_url)
    if not normalized.startswith(("http://", "https://")):
        return _json({"success": False, "error": "invalid seed_url"})

    limit = _clamp(int(max_links or 1), 1, 500)
    render_mode = (render or "auto").strip().lower()
    if render_mode not in {"auto", "never", "always"}:
        render_mode = "auto"

    html = ""
    status_code = None
    source = "http"

    fetch = None
    if render_mode != "always":
        fetch = await _fetch_http(normalized)
        if fetch.success:
            if fetch.resource_type == "pdf":
                return _json(
                    {
                        "success": True,
                        "seed_url": normalized,
                        "source": "http",
                        "resource_type": "pdf",
                        "status_code": fetch.status_code,
                        "count": 0,
                        "links": [],
                        "message": "PDF 資源不支援 HTML 連結擷取",
                    }
                )
            html = fetch.html
            status_code = fetch.status_code
    elif PDF_AUTO_EXTRACT_ENABLED:
        preflight = await _probe_resource_type(normalized)
        if preflight.get("resource_type") == "pdf":
            fetch = await _fetch_http(normalized)
            return _json(
                {
                    "success": True,
                    "seed_url": normalized,
                    "source": "http",
                    "resource_type": "pdf",
                    "status_code": fetch.status_code if fetch else preflight.get("status_code"),
                    "count": 0,
                    "links": [],
                    "message": "PDF 資源不支援 HTML 連結擷取",
                }
            )

    if not html and render_mode in {"auto", "always"}:
        js = await _crawl_js(normalized, wait_time=3.5)
        if js.get("success"):
            source = "js"
            html = js.get("html", "") or ""
            status_code = js.get("status_code")

    if not html:
        return _json(
            {
                "success": False,
                "seed_url": normalized,
                "status_code": status_code,
                "error": "unable to fetch html for map",
            }
        )

    links = _extract_links(html, normalized, limit * 2)
    if same_domain:
        seed_domain = urlparse(normalized).netloc
        links = [item for item in links if urlparse(item["url"]).netloc == seed_domain]

    links = links[:limit]

    return _json(
        {
            "success": True,
            "seed_url": normalized,
            "source": source,
            "status_code": status_code,
            "count": len(links),
            "links": links,
        }
    )


async def crawl4ai_probe(
    seed_url: Annotated[
        str,
        "種子頁面 URL（必填）。從此頁面出發發現同網域連結，再逐一爬取並搜尋關鍵字。",
    ],
    keywords: Annotated[
        list[str],
        (
            "要搜尋的關鍵字列表（必填，不可為空）。"
            "大小寫不敏感。每個關鍵字在每頁最多回傳 2 個 KWIC 片段（前後各約 45 字元）。"
        ),
    ],
    max_pages: Annotated[
        int,
        "最大探測頁面數（1-100，預設 20）。包含種子頁本身。值越大覆蓋越廣但耗時越長。",
    ] = 20,
    render: Annotated[
        str,
        (
            "渲染策略，可選值："
            "auto（預設）— HTTP 優先，品質不足時自動 JS 降級；"
            "never — 僅 HTTP；"
            "always — 直接 JS。"
        ),
    ] = "auto",
    extraction_mode: Annotated[
        str,
        "內容抽取模式：general（預設）或 strict。",
    ] = EXTRACTION_MODE_GENERAL,
) -> str:
    """
    關鍵字探測爬取：從種子頁出發，爬取同網域頁面並回傳關鍵字匹配結果。

    內部流程：
    1. 呼叫 crawl4ai_map(seed_url, same_domain=true) 發現同網域連結。
    2. 取前 max_pages 個候選頁面（含種子頁），呼叫 crawl4ai_crawl(summary_mode=false) 批量爬取。
    3. 對每頁內容以 KWIC（Key Word In Context）搜尋所有 keywords，提取上下文片段。
    4. 只回傳有命中的頁面。

    Args:
        seed_url: 種子頁面 URL，必須以 http:// 或 https:// 開頭
        keywords: 關鍵字列表（不可為空，大小寫不敏感）
        max_pages: 最大探測頁面數（1-100，預設 20）
        render: 渲染策略 auto / never / always

    Returns:
        str: JSON 字串，結構如下：
             {
               "success": true,
               "seed_url": "...",
               "keywords": ["keyword1", "keyword2"],
               "stats": {
                 "pages_scanned": 15,
                 "pages_failed": 2,
                 "matches_found": 5
               },
               "matches": [
                 {
                   "url": "https://...",
                   "snippets": ["...前文 **keyword1** 後文...", ...],
                   "used_render": "http" | "js"
                 }, ...
               ]
             }
    """
    normalized = _normalize_url(seed_url)
    if not normalized.startswith(("http://", "https://")):
        return _json({"success": False, "error": "invalid seed_url"})
    if not keywords:
        return _json({"success": False, "error": "keywords must not be empty"})

    page_limit = _clamp(int(max_pages or 1), 1, 100)
    render_mode = (render or "auto").strip().lower()
    if render_mode not in {"auto", "never", "always"}:
        render_mode = "auto"
    normalized_extraction_mode = _normalize_extraction_mode(extraction_mode)

    map_json = await crawl4ai_map(
        normalized, max_links=page_limit * 2, same_domain=True, render=render_mode
    )
    try:
        map_data = json.loads(map_json)
    except Exception:
        map_data = {"success": False, "links": []}

    candidates = [normalized]
    if map_data.get("success"):
        for item in map_data.get("links", []):
            link = _normalize_url(item.get("url", ""))
            if link and link not in candidates:
                candidates.append(link)
            if len(candidates) >= page_limit:
                break

    crawl_json = await crawl4ai_crawl(
        urls=candidates[:page_limit],
        summary_mode=False,
        render=render_mode,
        max_concurrency=min(page_limit, MAX_CONCURRENCY),
        extraction_mode=normalized_extraction_mode,
    )
    data = json.loads(crawl_json)

    matches: list[dict[str, Any]] = []
    pages_scanned = 0
    pages_failed = 0
    for item in data.get("results", []):
        content = item.get("content", "")
        if not item.get("success"):
            pages_failed += 1
            continue
        pages_scanned += 1

        snippets: list[str] = []
        for kw in keywords:
            snippets.extend(_kwic(content, kw, context_chars=45, max_snippets=2))
        unique_snippets = []
        for snippet in snippets:
            if snippet not in unique_snippets:
                unique_snippets.append(snippet)
        if unique_snippets:
            matches.append(
                {
                    "url": item.get("url"),
                    "snippets": unique_snippets[:4],
                    "used_render": item.get("used_render"),
                    "resource_type": item.get("resource_type", "html"),
                }
            )

    return _json(
        {
            "success": True,
            "seed_url": normalized,
            "keywords": keywords,
            "stats": {
                "pages_scanned": pages_scanned,
                "pages_failed": pages_failed,
                "matches_found": len(matches),
            },
            "matches": matches,
        }
    )


if __name__ == "__main__":
    import sys

    from pro_search_crawl_mcp import run_server

    run_server(sys.modules[__name__])
