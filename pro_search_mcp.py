#!/usr/bin/env python3
"""
Pro Search MCP Server V3
========================
Deep Search Pipeline V3 的 MCP 包裝。

提供 pro_search 工具：Brave/Tavily/Exa/SerpApi 多源搜尋 → LLM 審核 → 批次深爬 → 位置策略排序。
支援 instant / fast / full 三種模式。
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# 外側 main_server.py 會以 importlib 動態載入本檔，該情境不會自動把本目錄
# 放入 sys.path。補入自身目錄後仍採標準模組匯入，讓 MCP 與 Python 直調共用
# 同一份 deep_search_tool 模組與連線池。
_MODULE_DIR = str(Path(__file__).resolve().parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

_deep_search_tool = importlib.import_module("deep_search_tool")

logger = logging.getLogger(__name__)

try:
    from fastmcp import FastMCP
except ImportError:
    # 某些 fastmcp 安裝形態只在 mcp.server.fastmcp 匯出 FastMCP。
    from mcp.server.fastmcp import FastMCP

# 使用標準模組匯入，確保 MCP 與 Python 直調共用同一份連線池與生命週期狀態。
_deep_search = _deep_search_tool.deep_search
_shutdown_shared_resources = getattr(
    _deep_search_tool, "shutdown_shared_resources", None
)
_normalize_search_mode = getattr(_deep_search_tool, "normalize_search_mode", None)
_normalize_execution_mode = getattr(
    _deep_search_tool, "normalize_execution_mode", None
)
_normalize_model_route = getattr(_deep_search_tool, "normalize_model_route", None)
_validate_search_inputs = getattr(_deep_search_tool, "validate_search_inputs", None)
_citation_protocol = getattr(_deep_search_tool, "_citation_protocol", None)


PRO_SEARCH_VERBOSE_LOGS = (
    os.environ.get("PRO_SEARCH_VERBOSE_LOGS")
    or os.environ.get("RESEARCHER_VERBOSE_LOGS", "")
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MCP_HOST = "127.0.0.1"
MCP_PORT = 8074
os.environ["FASTMCP_HOST"] = MCP_HOST
os.environ["FASTMCP_PORT"] = str(MCP_PORT)
PUBLIC_SUMMARY_DEFAULTS: dict[str, Any] = {
    "total_found": 0,
    "total_selected": 0,
    "total_crawl_attempted": 0,
    "total_crawled_success": 0,
    "total_crawled_failed": 0,
    "total_chunks_seen": 0,
    "total_chunks_prompted": 0,
    "total_chunks_selected": 0,
    "loops_executed": 0,
    "judge_verdict": None,
}


def _empty_citation_metadata() -> dict[str, Any]:
    """建立所有失敗路徑共用的空引用與 evidence 結構。"""
    protocol = (
        _citation_protocol()
        if callable(_citation_protocol)
        else {
            "format": "[citation](source_index:citation_id)",
            "source_of_truth": "只以 evidence_bundle[].chunks[].text 中已篩選過的引用內容作為回答事實來源。",
        }
    )
    return {
        "citation_protocol": protocol,
        "source_registry": [],
        "evidence_bundle": [],
    }


def _public_tool_payload(result: dict[str, Any]) -> dict[str, Any]:
    """收斂 MCP 對外回傳，避免外側回答模型讀到研究過程或搜尋 snippets。"""
    if not isinstance(result, dict):
        result = {
            "success": False,
            "error": "internal result is not a dict",
        }

    summary = result.get("search_results_summary")
    public_summary = dict(PUBLIC_SUMMARY_DEFAULTS)
    if isinstance(summary, dict):
        for key in PUBLIC_SUMMARY_DEFAULTS:
            if key in summary:
                public_summary[key] = summary[key]

    payload = {
        "success": bool(result.get("success", False)),
        "mode": result.get("mode"),
        "question": result.get("question"),
        "search_queries": result.get("search_queries"),
        "search_mode": result.get("search_mode"),
        "completion_state": result.get("completion_state"),
        "judge_success": result.get("judge_success"),
        "elapsed_ms": result.get("elapsed_ms"),
        "search_results_summary": public_summary,
        "citation_protocol": result.get("citation_protocol")
        or _empty_citation_metadata()["citation_protocol"],
        "answer_guidance": result.get("answer_guidance") or {},
        "source_registry": result.get("source_registry") or [],
        "evidence_bundle": result.get("evidence_bundle") or [],
        "error": result.get("error"),
    }
    return payload


def _text_or_empty(value: Any) -> str:
    """將模型可能送來的非字串值轉成可檢查文字。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _parse_jsonish(value: str) -> Any:
    text = (value or "").strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _strip_list_marker(value: str) -> str:
    return (
        value.strip()
        .lstrip("-*• \t")
        .removeprefix("1.")
        .removeprefix("2.")
        .removeprefix("3.")
        .strip()
    )


def _query_items_from_any(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, str):
        parsed = _parse_jsonish(value)
        if parsed is not value:
            return _query_items_from_any(parsed)

        text = value.strip()
        if not text:
            return []

        separators = r"[\n\r;|]+"
        parts = [part for part in re.split(separators, text) if part.strip()]
        if len(parts) == 1 and "," in text:
            parts = [part for part in text.split(",") if part.strip()]
        return [_strip_list_marker(part) for part in parts if _strip_list_marker(part)]

    if isinstance(value, dict):
        for key in (
            "search_queries",
            "queries",
            "search_terms",
            "terms",
            "keywords",
        ):
            if key in value:
                found = _query_items_from_any(value.get(key))
                if found:
                    return found
        ordered = []
        for key in ("query_1", "query1", "q1", "query_2", "query2", "q2", "query_3", "query3", "q3"):
            if key in value:
                ordered.extend(_query_items_from_any(value.get(key)))
        if ordered:
            return ordered
        for key in ("query", "search_query", "keyword"):
            if key in value:
                found = _query_items_from_any(value.get(key))
                if found:
                    return found
        return []

    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            items.extend(_query_items_from_any(item))
        return items

    text = _text_or_empty(value)
    return [text] if text else []


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _coerce_mcp_inputs(
    *,
    question: Any,
    search_queries: Any,
    queries: Any,
    search_query: Any,
    query: Any,
    query_1: Any,
    query_2: Any,
    query_3: Any,
) -> tuple[str, list[str]]:
    question_text = _text_or_empty(question) or _text_or_empty(query)

    query_items = _query_items_from_any(search_queries)
    if not query_items:
        query_items = _query_items_from_any(queries)
    if not query_items:
        query_items = _query_items_from_any(search_query)
    if not query_items:
        query_items = _query_items_from_any(query)
    explicit_numbered = (
        _query_items_from_any(query_1)
        + _query_items_from_any(query_2)
        + _query_items_from_any(query_3)
    )
    if explicit_numbered:
        query_items = explicit_numbered

    query_items = _dedupe_keep_order(query_items)
    if not query_items and question_text:
        query_items = [question_text]

    fallback_base = question_text or (query_items[0] if query_items else "")
    existing_query_keys = {item.casefold() for item in query_items}
    for suffix in ("", " overview", " latest"):
        if len(query_items) >= 3:
            break
        candidate = f"{fallback_base}{suffix}".strip()
        if candidate and candidate.casefold() not in existing_query_keys:
            query_items.append(candidate)
            existing_query_keys.add(candidate.casefold())

    query_items = _dedupe_keep_order(query_items)
    while len(query_items) < 3 and fallback_base:
        query_items.append(f"{fallback_base} query {len(query_items) + 1}")

    return question_text, query_items[:3]


# ============================================================================
# MCP Server 初始化
# ============================================================================


@asynccontextmanager
async def _mcp_lifespan(_: FastMCP):
    try:
        yield
    finally:
        if callable(_shutdown_shared_resources):
            await _shutdown_shared_resources()

_MCP_KWARGS = {
    "name": "Simplex Research",
    "lifespan": _mcp_lifespan,
    "instructions": """Simplex — 深度搜尋研究工具。

整合原生 SearXNG、可選搜尋 API、LLM Judge 與 Crawl4AI 批次深爬的全自動搜尋管線。

功能：
- 接收用戶問題和搜尋字詞，自動完成「多源搜尋 → LLM 篩選 → 深爬 → 排序」全流程
- 自動根據問題與 3 組 query 判定 locale，並為不同搜尋引擎生成不同版本的查詢字串
- 支援三種搜尋模式：web（網路）、academic（學術）、social（社交聚合）
- 支援三種深度模式：instant（單輪快速取證）、fast（不足時多做一次輕量補洞）、full（多輪 reviewer 補洞，最多第三輪窄搜尋）

搜尋模式路由：
- web: SearXNG general + 可用的 Brave / Tavily
- academic: SearXNG general + science + 可用的 Exa / SerpApi Scholar / Brave
- social: SearXNG general + social media + 可用的 Brave / Tavily / Exa
- SearXNG 採一頁結果語意，單頁超過 20 筆時完整保留，不強制裁切
- SerpApi 僅用於 academic 模式的 Google Scholar（引用數、出版資訊等無可替代功能）
- API 不可用時自動降級，會先均分配額；若某來源先碰到上限，剩餘配額會續分給其他仍有空間的 API
- 搜尋階段只提供 snippets 給內部 reviewer 選 URL；snippets 不會作為最終回答事實來源
- 深爬全文會先切成 chunk 並由 reviewer 依問題選出 evidence chunks；MCP 對外只回傳 canonical evidence_bundle，避免同一 chunk 重複耗用上下文
- 最終回答若使用某來源事實，必須在相關句子後緊貼該來源 citation_marker，例如 [citation](1://example.com/article)
- 不要把引用集中在文末；不要把 search_results_summary、query_plans、content_map 或 source_registry 當作事實來源

使用時機：
- 當需要深入研究某個主題時
- 當需要收集多方觀點和資料時
- 當需要從社群、學術或網路上獲取最新資訊時

注意事項：
- search_queries 建議提供 3 組不同角度的搜尋字詞；若模型用字串或別名送入，MCP 入口會先正規化
- 系統會保留每個模式定義好的 per-query 配額，query rewrite 只在引擎內部發生
- social 模式遇到來源不足會自動啟用站點補搜，整體耗時可能高於 web/academic
- fast 模式若第一輪 reviewer 判斷不足，會再做一次輕量 Loop 2；第二輪 URL reviewer 會同時看到補搜結果與第一輪未爬 snippets，並只深爬約 3~5 個 URL
- instant 模式維持完整搜尋來源與 3 組 query，每個搜尋引擎每組 query 取 10 筆結果；只深爬約 3~5 個 URL，預設目標 4，且不進入第二輪搜尋
- full 模式會依 reviewer judge 決定是否進入第二輪；第二輪仍不足時最多再做一次窄補洞""",
}

try:
    mcp = FastMCP(**_MCP_KWARGS, host=MCP_HOST, port=MCP_PORT)
except TypeError as exc:
    msg = str(exc)
    if "host" not in msg and "port" not in msg:
        raise
    mcp = FastMCP(**_MCP_KWARGS)


# ============================================================================
# MCP Tools
# ============================================================================


@mcp.tool(name="pro_search")
async def deep_search(
    question: Any = None,
    search_queries: Any = None,
    search_mode: Any = "web",
    mode: Any = "fast",
    model: Any = "d",
    queries: Any = None,
    search_query: Any = None,
    query: Any = None,
    query_1: Any = None,
    query_2: Any = None,
    query_3: Any = None,
) -> str:
    """
    執行 Simplex 深度搜尋管線。

    Args:
        question: 用戶的原始問題（用於指導 LLM 篩選、chunk reviewer 與 judge）
        search_queries: 搜尋字詞列表（建議 3 組不同角度；不足時入口會以 question 補齊）
        search_mode: 搜尋模式 — "web"（一般網路）、"academic"（學術論文 + 少量 web 補充）、"social"（社群聚合 + web 補充 + 自動 fallback）
        mode: 深度模式 — "instant"（單輪快速取證）、"fast"（不足時多做一次輕量補洞，快速）、"full"（多輪 reviewer 補洞，最多第三輪窄搜尋）
        model: LLM 路線 — "d"（預設，OpenRouter DeepSeek V4 Flash，顯式關閉 reasoning）或 "g"（OpenRouter Gemini 3 Flash）

    Returns:
        JSON 格式的搜尋結果，包含：
        - search_results_summary: 搜尋摘要統計
        - evidence_bundle: 唯一 canonical evidence；依來源分組的 reviewer 篩選原文 chunk
        - citation_protocol: 引用格式規範，要求最終回答直接依據 evidence_bundle 中的篩選內容
        - answer_guidance: 回答策略提示；instant 證據不足時會要求提醒用戶資料不足、品質可議
        - source_registry: 來源索引表，只供引用定位，不是摘要也不是事實來源

    回答規範：
        - 只使用 evidence_bundle[].chunks[].text 中的已篩選原文資訊回答。
        - 每個關鍵事實句後緊貼對應頁面的 citation_marker。
        - 不要將所有引用集中在回答末尾。
        - 不要把 search_results_summary、query_plans、content_map、source_registry、chunk_filter 或 research_trace 當作事實來源。
    """
    search_mode_text = _text_or_empty(search_mode) or "web"
    mode_text = _text_or_empty(mode) or "fast"
    model_text = _text_or_empty(model) or "d"

    if callable(_normalize_search_mode):
        search_mode = _normalize_search_mode(search_mode_text)
    elif search_mode_text not in ("web", "academic", "social"):
        search_mode = "web"
    else:
        search_mode = search_mode_text

    if callable(_normalize_execution_mode):
        mode = _normalize_execution_mode(mode_text)
    elif mode_text not in ("instant", "fast", "full"):
        mode = "fast"
    else:
        mode = mode_text

    if callable(_normalize_model_route):
        model = _normalize_model_route(model_text)
    elif model_text not in ("d", "g"):
        model = "d"
    else:
        model = model_text

    try:
        question, search_queries = _coerce_mcp_inputs(
            question=question,
            search_queries=search_queries,
            queries=queries,
            search_query=search_query,
            query=query,
            query_1=query_1,
            query_2=query_2,
            query_3=query_3,
        )
        if callable(_validate_search_inputs):
            question, search_queries = _validate_search_inputs(
                question,
                search_queries,
                exact_query_count=3,
            )
        else:
            if not question or not question.strip():
                raise ValueError("question 不能為空")
            if (
                not search_queries
                or not isinstance(search_queries, list)
                or len(search_queries) != 3
            ):
                raise ValueError("search_queries 必須是包含 3 組字詞的列表")
    except ValueError as exc:
        return json.dumps(
            _public_tool_payload(
                {
                    "error": str(exc),
                    "success": False,
                    "mode": mode,
                    "question": _text_or_empty(question),
                    "search_queries": [],
                    "search_mode": search_mode,
                    "completion_state": "failed",
                    "judge_success": None,
                    "elapsed_ms": 0,
                }
            ),
            ensure_ascii=False,
        )

    try:
        result = await _deep_search(
            question=question,
            search_queries=search_queries,
            search_mode=search_mode,
            mode=mode,
            model=model,
            verbose=PRO_SEARCH_VERBOSE_LOGS,
        )

        return json.dumps(_public_tool_payload(result), ensure_ascii=False, default=str)

    except Exception:
        logger.exception("pro_search 執行失敗")
        return json.dumps(
            _public_tool_payload(
                {
                    "error": "pro_search 執行失敗，請稍後再試。",
                    "success": False,
                    "mode": mode,
                    "question": question,
                    "search_queries": search_queries,
                    "search_mode": search_mode,
                    "completion_state": "failed",
                    "judge_success": None,
                    "elapsed_ms": 0,
                }
            ),
            ensure_ascii=False,
        )


# ============================================================================
# 入口點
# ============================================================================

if __name__ == "__main__":
    print("[Simplex MCP] 正在啟動...")
    mcp.run(transport="sse")
