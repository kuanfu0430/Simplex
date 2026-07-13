"""Simplex FastAPI：設定、模型探索、研究搜尋與前端託管。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from deep_search_tool import (
    build_direct_planner_context,
    crawl_explicit_urls,
    deep_search,
    extract_explicit_urls,
    review_explicit_pages,
    shutdown_shared_resources,
)

from . import __version__
from .conversation import (
    合併並重新編號證據,
    展平先前證據供Judge使用,
    建立研究帳本,
    建立證據膠囊,
    排除指定網址證據,
    準備對話歷史,
    解封證據膠囊,
    選取可刷新來源,
    選取相關先前證據,
)
from .llm import 備用搜尋字詞, 取得模型清單, 串流產生引用回答, 產生搜尋字詞, 解析模型設定, 解析搜尋模型設定, 尋找供應商
from .settings import 取得設定儲存庫


專案根目錄 = Path(__file__).resolve().parent.parent
前端目錄 = 專案根目錄 / "frontend" / "dist"
支援語言 = {"en", "zh-TW"}


def _介面語言(設定: dict[str, Any]) -> str:
    語言 = str(設定.get("ui", {}).get("language") or "en")
    return 語言 if 語言 in 支援語言 else "en"


def _訊息(語言: str, 英文: str, 繁中: str) -> str:
    return 繁中 if 語言 == "zh-TW" else 英文


class 模型選擇(BaseModel):
    provider_id: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=300)


class 對話訊息(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=20000)


class 搜尋請求(BaseModel):
    question: str = Field(min_length=1, max_length=20000)
    search_mode: str = Field(default="web", pattern="^(web|academic|social)$")
    mode: str = Field(default="fast", pattern="^(instant|fast|full)$")
    search_queries: list[str] | None = None
    model_selection: 模型選擇 | None = None
    conversation_history: list[對話訊息] = Field(default_factory=list, max_length=16)
    context_capsules: list[str] = Field(default_factory=list, max_length=8)
    force_research: bool = False
    turn_id: str = Field(default="", max_length=120)


class 設定請求(BaseModel):
    settings: dict[str, Any]


def _SSE(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _直接連結區塊軌跡(區塊清單: list[dict[str, Any]]) -> list[dict[str, str]]:
    """將直接連結的 Judge 結果轉成前端可安全顯示的受限預覽。"""
    軌跡: list[dict[str, str]] = []
    for 區塊 in 區塊清單:
        文字 = " ".join(str(區塊.get("text") or "").split())
        軌跡.append(
            {
                "chunk_id": str(區塊.get("chunk_id") or ""),
                "title": str(區塊.get("title") or ""),
                "source_url": str(區塊.get("source_url") or ""),
                "from_query": str(區塊.get("from_query") or "Provided URL"),
                "preview": f"{文字[:277].rstrip()}…" if len(文字) > 280 else 文字,
            }
        )
    return 軌跡


async def _檢查SearXNG(base_url: str) -> dict[str, Any]:
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        return {"status": "disabled", "latency_ms": None, "message": "Address not configured"}
    開始 = time.perf_counter()
    try:
        本機代理標頭 = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}
        async with httpx.AsyncClient(timeout=4, follow_redirects=True) as 客戶端:
            回應 = await 客戶端.get(f"{base_url}/healthz", headers=本機代理標頭)
            if 回應.status_code == 404:
                回應 = await 客戶端.get(
                    f"{base_url}/search",
                    params={"q": "simplex health", "format": "json", "pageno": 1},
                    headers=本機代理標頭,
                )
            回應.raise_for_status()
        return {
            "status": "ok",
            "latency_ms": round((time.perf_counter() - 開始) * 1000),
            "message": "Available",
        }
    except Exception as exc:
        return {
            "status": "degraded",
            "latency_ms": round((time.perf_counter() - 開始) * 1000),
            "message": type(exc).__name__,
        }


def _爬蟲能力() -> dict[str, Any]:
    try:
        import crawl4ai  # noqa: F401

        crawl4ai狀態 = "ok"
    except Exception:
        crawl4ai狀態 = "missing"
    return {
        "status": crawl4ai狀態,
        "chromium_command": shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome"),
        "tesseract": shutil.which("tesseract"),
    }


def _有效搜尋設定(設定: dict[str, Any]) -> dict[str, Any]:
    """依前端選擇只啟用原生 SearXNG 或用戶自有搜尋服務。"""
    搜尋設定 = deepcopy(設定.get("search", {}))
    引擎模式 = str(搜尋設定.get("engine_mode") or "searxng")
    供應商 = 搜尋設定.get("providers", {})
    自定義 = 搜尋設定.get("custom", [])

    if isinstance(供應商, dict):
        for 供應商ID, 項目 in 供應商.items():
            if not isinstance(項目, dict):
                continue
            if 引擎模式 == "searxng":
                項目["enabled"] = 供應商ID == "searxng"
            elif 供應商ID == "searxng":
                項目["enabled"] = False
    if 引擎模式 == "searxng" and isinstance(自定義, list):
        for 項目 in 自定義:
            if isinstance(項目, dict):
                項目["enabled"] = False
    return 搜尋設定


@asynccontextmanager
async def lifespan(_: FastAPI):
    取得設定儲存庫()
    yield
    await shutdown_shared_resources()


app = FastAPI(
    title="Simplex",
    version=__version__,
    description="速度與精度優先的腳本化研究搜尋工具",
    lifespan=lifespan,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver"],
)


@app.middleware("http")
async def 防止跨站本機API請求(請求: Request, call_next):
    """阻擋 DNS rebinding 與跨站頁面驅動本機 API。"""
    if 請求.url.path.startswith("/api/"):
        來源 = 請求.headers.get("origin", "").strip()
        跨站提示 = 請求.headers.get("sec-fetch-site", "").strip().lower()
        if 來源:
            try:
                來源主機 = httpx.URL(來源).host
            except Exception:
                來源主機 = None
            if 來源主機 not in {"127.0.0.1", "localhost"}:
                return JSONResponse({"detail": "拒絕跨站本機 API 請求"}, status_code=403)
        if 跨站提示 == "cross-site":
            return JSONResponse({"detail": "拒絕跨站本機 API 請求"}, status_code=403)
    return await call_next(請求)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    設定 = 取得設定儲存庫().讀取()
    搜尋設定 = 設定.get("search", {})
    searxng = 搜尋設定.get("providers", {}).get("searxng", {})
    使用原生 = 搜尋設定.get("engine_mode", "searxng") == "searxng"
    searxng狀態 = (
        await _檢查SearXNG(str(searxng.get("base_url") or ""))
        if 使用原生
        else {
            "status": "disabled",
            "latency_ms": None,
            "message": _訊息(_介面語言(設定), "Using custom search engines", "目前使用自有搜尋引擎"),
        }
    )
    return {
        "status": "ok",
        "version": __version__,
        "searxng": searxng狀態,
        "crawler": _爬蟲能力(),
    }


@app.get("/api/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    儲存庫 = 取得設定儲存庫()
    return 儲存庫.公開設定(儲存庫.讀取())


@app.put("/api/settings")
async def put_settings(請求: 設定請求) -> dict[str, Any]:
    儲存庫 = 取得設定儲存庫()
    ui設定 = 請求.settings.get("ui", {})
    語言 = str(ui設定.get("language", "en"))
    if 語言 not in 支援語言:
        raise HTTPException(status_code=422, detail="Language must be en or zh-TW")
    try:
        scale = float(ui設定.get("scale", 1.0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=_訊息(語言, "UI scale must be a number", "UI 縮放比例必須是數字"))
    if not 0.8 <= scale <= 1.35:
        raise HTTPException(status_code=422, detail=_訊息(語言, "UI scale must be between 0.8 and 1.35", "UI 縮放比例必須介於 0.8 與 1.35"))
    theme = str(ui設定.get("theme", "dark"))
    if theme not in {"dark", "light"}:
        raise HTTPException(status_code=422, detail=_訊息(語言, "Theme must be dark or light", "主題必須是 dark 或 light"))
    引擎模式 = str(請求.settings.get("search", {}).get("engine_mode", "searxng"))
    if 引擎模式 not in {"searxng", "custom"}:
        raise HTTPException(status_code=422, detail=_訊息(語言, "Search engine mode must be searxng or custom", "搜尋引擎模式必須是 searxng 或 custom"))
    已存 = 儲存庫.儲存(請求.settings)
    return 儲存庫.公開設定(已存)


@app.get("/api/llm/providers/{provider_id}/models")
async def list_models(provider_id: str) -> dict[str, Any]:
    設定 = 取得設定儲存庫().讀取()
    供應商 = 尋找供應商(設定, provider_id)
    if not 供應商:
        raise HTTPException(status_code=404, detail="找不到 LLM provider")
    try:
        模型 = await 取得模型清單(供應商)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Provider 回傳 HTTP {exc.response.status_code}",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"讀取模型失敗：{type(exc).__name__}")
    return {"provider_id": provider_id, "models": 模型}


@app.post("/api/search-engines/{provider_id}/test")
async def test_search_engine(provider_id: str) -> dict[str, Any]:
    設定 = 取得設定儲存庫().讀取()
    內建 = 設定.get("search", {}).get("providers", {})
    供應商 = 內建.get(provider_id)
    if provider_id == "searxng" and isinstance(供應商, dict):
        return await _檢查SearXNG(str(供應商.get("base_url") or ""))
    if not isinstance(供應商, dict):
        供應商 = next(
            (
                項目
                for 項目 in 設定.get("search", {}).get("custom", [])
                if str(項目.get("id")) == provider_id
            ),
            None,
        )
    if not isinstance(供應商, dict):
        raise HTTPException(status_code=404, detail="找不到搜尋引擎")
    base_url = str(供應商.get("base_url") or "").strip()
    if not base_url:
        raise HTTPException(status_code=422, detail="API 地址不能為空")
    return {
        "status": "configured",
        "message": _訊息(
            _介面語言(設定),
            "Configuration format is valid; the provider protocol will be verified during search",
            "設定格式有效；實際搜尋會依供應商協定驗證",
        ),
    }


async def _串流最終回答(
    *,
    原始問題: str,
    搜尋結果: dict[str, Any],
    問答模型: dict[str, Any] | None,
    語言: str,
    對話歷史: list[dict[str, str]],
    turn_id: str,
    規劃毫秒: int,
    研究毫秒: int,
    研究策略: str,
    standalone_question: str,
    密封器: Any,
    直接爬取毫秒: int = 0,
    直接審核毫秒: int = 0,
    回答前總毫秒: int | None = None,
) -> AsyncIterator[str]:
    """將研究或沿用證據統一交給回答串流，保持 SSE payload 相容。"""
    yield _SSE("research_trace", {"type": "stage", "stage": "answering"})
    yield _SSE(
        "status",
        {
            "phase": "answering",
            "message": _訊息(語言, "Preparing cited answer", "整理引用回答中"),
            "summary": 搜尋結果.get("search_results_summary", {}),
            "research_strategy": 研究策略,
        },
    )
    共同結果 = {
        "question": 原始問題,
        "standalone_question": standalone_question,
        "research_strategy": 研究策略,
        "search_queries": 搜尋結果.get("search_queries", []),
        "search_mode": 搜尋結果.get("search_mode", "web"),
        "mode": 搜尋結果.get("mode", "fast"),
        "completion_state": 搜尋結果.get("completion_state"),
        "elapsed_ms": 搜尋結果.get("elapsed_ms"),
        "summary": 搜尋結果.get("search_results_summary", {}),
        "sources": 搜尋結果.get("source_registry", []),
        "evidence_bundle": 搜尋結果.get("evidence_bundle", []),
        "error": 搜尋結果.get("error"),
    }
    證據膠囊 = 建立證據膠囊(
        密封器,
        turn_id=turn_id,
        standalone_question=standalone_question,
        queries=共同結果["search_queries"],
        evidence_bundle=共同結果["evidence_bundle"],
    )
    起始結果 = {
        **共同結果,
        "context_capsule": 證據膠囊,
        "answer": "",
        "timings": {
            "planning_ms": 規劃毫秒,
            "research_ms": 研究毫秒,
            "direct_crawl_ms": 直接爬取毫秒,
            "direct_judge_ms": 直接審核毫秒,
            "answer_first_token_ms": None,
            "answer_ms": None,
            "total_ms": None,
        },
    }
    yield _SSE("answer_start", 起始結果)

    回答開始 = time.perf_counter()
    回答片段: list[str] = []
    首字毫秒: int | None = None
    try:
        async for 片段 in 串流產生引用回答(
            原始問題,
            搜尋結果,
            問答模型,
            語言,
            對話歷史,
        ):
            if 首字毫秒 is None:
                首字毫秒 = int((time.perf_counter() - 回答開始) * 1000)
            回答片段.append(片段)
            yield _SSE("answer_delta", {"delta": 片段})
    except Exception as exc:
        if not 回答片段:
            降級回答 = _訊息(
                語言,
                "The question model failed, but the research evidence is preserved. Check the question model settings.",
                "回答模型執行失敗，但研究證據已保留。請檢查問答模型設定。",
            )
            首字毫秒 = int((time.perf_counter() - 回答開始) * 1000)
            回答片段.append(降級回答)
            yield _SSE("answer_delta", {"delta": 降級回答})
        yield _SSE(
            "warning",
            {"message": f"{_訊息(語言, 'Answer generation failed', '回答生成失敗')}：{type(exc).__name__}"},
        )
    回答 = "".join(回答片段).strip()
    回答毫秒 = int((time.perf_counter() - 回答開始) * 1000)
    總毫秒 = (回答前總毫秒 if 回答前總毫秒 is not None else 規劃毫秒 + 研究毫秒) + 回答毫秒

    yield _SSE("research_trace", {"type": "stage", "stage": "complete"})
    yield _SSE(
        "result",
        {
            **共同結果,
            "context_capsule": 證據膠囊,
            "answer": 回答,
            "timings": {
                "planning_ms": 規劃毫秒,
                "research_ms": 研究毫秒,
                "direct_crawl_ms": 直接爬取毫秒,
                "direct_judge_ms": 直接審核毫秒,
                "answer_first_token_ms": 首字毫秒,
                "answer_ms": 回答毫秒,
                "total_ms": 總毫秒,
            },
        },
    )


async def _執行研究(請求: 搜尋請求) -> AsyncIterator[str]:
    全程開始 = time.perf_counter()
    儲存庫 = 取得設定儲存庫()
    設定 = 儲存庫.讀取()
    語言 = _介面語言(設定)
    原始問題 = 請求.question.strip()
    對話歷史 = 準備對話歷史([項目.model_dump() for 項目 in 請求.conversation_history])
    密封器 = 儲存庫.取得本機密封器()
    已驗證膠囊 = 解封證據膠囊(密封器, 請求.context_capsules)
    先前證據 = 選取相關先前證據(已驗證膠囊, 原始問題)
    turn_id = 請求.turn_id.strip() or uuid4().hex
    指定網址資訊 = extract_explicit_urls(原始問題)
    指定網址 = list(指定網址資訊.get("urls") or [])
    if 指定網址資訊.get("overflow"):
        yield _SSE(
            "error",
            {
                "message": _訊息(
                    語言,
                    "A single question can include at most five direct URLs.",
                    "單一提問最多可附上五個直接爬取網址。",
                )
            },
        )
        return
    yield _SSE(
        "status",
        {"phase": "planning", "message": _訊息(語言, "Preparing conversation context and search strategy", "整理對話脈絡與搜尋策略中")},
    )

    try:
        選取模型 = 請求.model_selection.model_dump() if 請求.model_selection else None
        問答模型 = 解析搜尋模型設定(設定, 選取模型)
    except ValueError as exc:
        yield _SSE("error", {"message": str(exc)})
        return
    Judge模型 = 解析模型設定(設定, "judge")

    直接頁面: list[dict[str, Any]] = []
    直接失敗: list[dict[str, Any]] = []
    直接爬取毫秒 = 0
    指定連結脈絡: list[dict[str, str]] = []
    if 指定網址:
        yield _SSE("research_trace", {"type": "stage", "stage": "direct_crawl"})
        yield _SSE(
            "status",
            {
                "phase": "direct_crawl",
                "message": _訊息(語言, "Reading the provided links", "讀取使用者提供的連結中"),
            },
        )
        try:
            直接結果 = await crawl_explicit_urls(指定網址)
        except Exception as exc:
            直接結果 = {"pages": [], "failed": [], "elapsed_ms": 0}
            yield _SSE(
                "warning",
                {
                    "message": f"{_訊息(語言, 'Could not read the provided links; continuing with research', '無法讀取指定連結，將改以搜尋補足')}：{type(exc).__name__}"
                },
            )
        直接頁面 = [頁面 for 頁面 in 直接結果.get("pages", []) if isinstance(頁面, dict)]
        直接失敗 = [頁面 for 頁面 in 直接結果.get("failed", []) if isinstance(頁面, dict)]
        直接爬取毫秒 = int(直接結果.get("elapsed_ms") or 0)
        if any(str(頁面.get("error_code") or "") == "UNSAFE_URL" for 頁面 in 直接失敗):
            yield _SSE(
                "error",
                {
                    "message": _訊息(
                        語言,
                        "One or more provided links are not safe to crawl.",
                        "提供的連結中含有不允許爬取的網址。",
                    )
                },
            )
            return
        if 直接失敗:
            yield _SSE(
                "warning",
                {
                    "message": _訊息(
                        語言,
                        "Some provided links could not be read; direct-only answering is disabled for this turn.",
                        "部分指定連結無法讀取；本輪不會只依連結直接回答。",
                    )
                },
            )
        if 直接頁面:
            yield _SSE(
                "research_trace",
                {
                    "type": "direct_sources",
                    "stage": "direct_crawl",
                    "sources": [
                        {
                            "title": str(頁面.get("title") or 頁面.get("url") or ""),
                            "url": str(頁面.get("url") or ""),
                        }
                        for 頁面 in 直接頁面
                    ],
                },
            )
            指定連結脈絡 = build_direct_planner_context(原始問題, 直接頁面)

    查詢 = [值.strip() for 值 in (請求.search_queries or []) if 值.strip()][:3]
    直接審核任務: asyncio.Task[dict[str, Any]] | None = None
    if 直接頁面:
        直接審核任務 = asyncio.create_task(
            review_explicit_pages(
                question=原始問題,
                pages=直接頁面,
                search_mode=請求.search_mode,
                execution_mode=請求.mode,
                judge_model_config=Judge模型 or {},
            )
        )

    規劃開始 = time.perf_counter()
    刷新來源參考: list[str] = []
    try:
        if len(查詢) != 3:
            規劃 = await 產生搜尋字詞(
                原始問題,
                問答模型,
                語言,
                對話歷史=對話歷史,
                證據帳本=建立研究帳本(已驗證膠囊),
                指定連結脈絡=指定連結脈絡 or None,
                強制研究=請求.force_research,
                結構化規劃=True,
            )
            if isinstance(規劃, dict):
                查詢 = [str(值).strip() for 值 in 規劃.get("queries", []) if str(值).strip()][:3]
                研究策略 = str(規劃.get("strategy") or "research")
                獨立問題 = str(規劃.get("standalone_question") or 原始問題).strip()[:20000] or 原始問題
                刷新來源參考 = [str(值).strip() for 值 in 規劃.get("refresh_source_refs", []) if str(值).strip()][:2]
            else:
                查詢 = [str(值).strip() for 值 in 規劃 if str(值).strip()][:3]
                研究策略 = "research"
                獨立問題 = 原始問題
        else:
            研究策略 = "research"
            獨立問題 = 原始問題
    except Exception as exc:
        查詢 = 備用搜尋字詞(原始問題, 語言)
        研究策略 = "research"
        獨立問題 = 原始問題
        yield _SSE(
            "warning",
            {"message": f"{_訊息(語言, 'Query planning failed; using fallback queries', '問答模型規劃失敗，已使用快速查詢規則')}：{type(exc).__name__}"},
        )
    規劃毫秒 = int((time.perf_counter() - 規劃開始) * 1000)

    直接審核: dict[str, Any] = {}
    if 直接審核任務 is not None:
        try:
            直接審核 = await 直接審核任務
            直接區塊 = [區塊 for 區塊 in 直接審核.get("selected_chunks", []) if isinstance(區塊, dict)]
            if 直接區塊:
                yield _SSE(
                    "research_trace",
                    {
                        "type": "direct_evidence",
                        "stage": "chunk_judge",
                        "chunks": _直接連結區塊軌跡(直接區塊),
                    },
                )
        except Exception as exc:
            yield _SSE(
                "warning",
                {
                    "message": f"{_訊息(語言, 'Provided-link evidence review failed; continuing with search', '指定連結證據審核失敗，將以搜尋補足')}：{type(exc).__name__}"
                },
            )
    直接證據 = [證據 for 證據 in 直接審核.get("evidence_bundle", []) if isinstance(證據, dict)]
    直接審核毫秒 = int(直接審核.get("elapsed_ms") or 0)

    if 請求.force_research:
        研究策略 = "research"
    if 研究策略 not in {"reuse", "direct", "research"}:
        研究策略 = "research"
    if 指定網址 and 研究策略 == "reuse":
        研究策略 = "research"
        yield _SSE(
            "warning",
            {
                "message": _訊息(
                    語言,
                    "A provided link must be assessed before reusing previous evidence; starting research.",
                    "本輪附有指定連結，必須先評估其內容；已改為啟動研究。",
                )
            },
        )
    直接審核結論 = 直接審核.get("review") if isinstance(直接審核.get("review"), dict) else {}
    直接可回答 = (
        bool(指定網址)
        and len(直接頁面) == len(指定網址)
        and not 直接失敗
        and str(直接審核結論.get("verdict") or "") == "sufficient"
        and bool(直接證據)
    )
    if 研究策略 == "direct" and not 直接可回答:
        研究策略 = "research"
        yield _SSE(
            "warning",
            {
                "message": _訊息(
                    語言,
                    "The provided links are not sufficient for a direct answer; expanding to search.",
                    "指定連結不足以直接回答；將擴大為搜尋研究。",
                )
            },
        )
    if 研究策略 == "reuse" and not 先前證據:
        研究策略 = "research"
        yield _SSE(
            "warning",
            {"message": _訊息(語言, "Previous verified evidence is unavailable; starting fresh research", "先前已驗證證據無法使用，已改為重新研究")},
        )
    if 研究策略 == "research":
        while len(查詢) < 3:
            候選 = 備用搜尋字詞(獨立問題, 語言)[len(查詢)]
            if 候選 not in 查詢:
                查詢.append(候選)
            else:
                查詢.append(f"{獨立問題} {len(查詢) + 1}")
        查詢 = 查詢[:3]

    if 研究策略 == "reuse":
        evidence_bundle, source_registry = 合併並重新編號證據([], 先前證據)
        沿用結果 = {
            "search_queries": [],
            "search_mode": 請求.search_mode,
            "mode": 請求.mode,
            "completion_state": "complete",
            "elapsed_ms": 0,
            "search_results_summary": {"reused_sources": len(source_registry)},
            "source_registry": source_registry,
            "evidence_bundle": evidence_bundle,
            "error": None,
        }
        yield _SSE(
            "status",
            {
                "phase": "answering",
                "message": _訊息(語言, "Using verified evidence from this conversation", "沿用本次對話中已驗證的證據"),
                "research_strategy": "reuse",
            },
        )
        async for 事件 in _串流最終回答(
            原始問題=原始問題,
            搜尋結果=沿用結果,
            問答模型=問答模型,
            語言=語言,
            對話歷史=對話歷史,
            turn_id=turn_id,
            規劃毫秒=規劃毫秒,
            研究毫秒=0,
            研究策略="reuse",
            standalone_question=獨立問題,
            密封器=密封器,
            回答前總毫秒=int((time.perf_counter() - 全程開始) * 1000),
        ):
            yield 事件
        yield _SSE("done", {"message": _訊息(語言, "Answer complete", "回答完成")})
        return

    if 研究策略 == "direct":
        直接結果 = {
            "search_queries": [],
            "search_mode": 請求.search_mode,
            "mode": 請求.mode,
            "completion_state": "complete",
            "elapsed_ms": 直接爬取毫秒 + 直接審核毫秒,
            "search_results_summary": {
                "direct_sources": len(直接頁面),
                "direct_chunks": len(直接審核.get("selected_chunks", [])),
            },
            "source_registry": 直接審核.get("source_registry", []),
            "evidence_bundle": 直接證據,
            "error": None,
        }
        async for 事件 in _串流最終回答(
            原始問題=原始問題,
            搜尋結果=直接結果,
            問答模型=問答模型,
            語言=語言,
            對話歷史=對話歷史,
            turn_id=turn_id,
            規劃毫秒=規劃毫秒,
            研究毫秒=0,
            研究策略="direct",
            standalone_question=獨立問題,
            密封器=密封器,
            直接爬取毫秒=直接爬取毫秒,
            直接審核毫秒=直接審核毫秒,
            回答前總毫秒=int((time.perf_counter() - 全程開始) * 1000),
        ):
            yield 事件
        yield _SSE("done", {"message": _訊息(語言, "Answer complete", "回答完成")})
        return

    直接重讀網址 = [str(頁面.get("url") or "") for 頁面 in 直接頁面 if str(頁面.get("url") or "")]
    直接網址鍵 = {網址.rstrip("/").lower() for 網址 in 直接重讀網址}
    刷新來源 = [
        來源
        for 來源 in 選取可刷新來源(已驗證膠囊, 刷新來源參考)
        if str(來源.get("url") or "").rstrip("/").lower() not in 直接網址鍵
    ]
    刷新網址 = [str(來源.get("url") or "") for 來源 in 刷新來源 if str(來源.get("url") or "")]
    被取代的先前證據 = 排除指定網址證據(先前證據, [*直接重讀網址, *刷新網址])
    Judge先前證據, _ = 合併並重新編號證據(直接證據, 被取代的先前證據)
    if 刷新來源:
        yield _SSE(
            "research_trace",
            {
                "type": "refresh_sources",
                "stage": "crawling",
                "sources": [{"title": 來源["title"], "url": 來源["url"]} for 來源 in 刷新來源],
            },
        )

    yield _SSE(
        "research_trace",
        {
            "type": "plan",
            "stage": "searching",
            "queries": [{"query": 查詢字詞} for 查詢字詞 in 查詢],
        },
    )
    yield _SSE(
        "status",
        {
            "phase": "searching",
            "message": _訊息(語言, "Searching, judging, and deep crawling", "搜尋、Judge 與深爬進行中"),
            "queries": 查詢,
        },
    )

    研究開始 = time.perf_counter()
    進度佇列: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def 回報進度(事件: dict[str, Any]) -> None:
        進度佇列.put_nowait(事件)

    研究任務 = asyncio.create_task(
        deep_search(
            question=獨立問題,
            search_queries=查詢,
            search_mode=請求.search_mode,
            mode=請求.mode,
            judge_model_config=Judge模型 or {},
            search_provider_config=_有效搜尋設定(設定),
            language=語言,
            progress_callback=回報進度,
            prior_evidence_chunks=展平先前證據供Judge使用(Judge先前證據),
            refresh_urls=刷新網址,
            excluded_urls=指定網址,
            verbose=False,
        )
    )
    try:
        while True:
            if 研究任務.done():
                while not 進度佇列.empty():
                    yield _SSE("research_trace", 進度佇列.get_nowait())
                結果 = 研究任務.result()
                break
            事件任務 = asyncio.create_task(進度佇列.get())
            已完成, _ = await asyncio.wait(
                {研究任務, 事件任務},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if 事件任務 in 已完成:
                yield _SSE("research_trace", 事件任務.result())
            else:
                事件任務.cancel()
                with suppress(asyncio.CancelledError):
                    await 事件任務
    except asyncio.CancelledError:
        研究任務.cancel()
        with suppress(asyncio.CancelledError):
            await 研究任務
        raise
    except Exception as exc:
        yield _SSE(
            "error",
            {"message": f"{_訊息(語言, 'Research failed', '研究失敗')}：{type(exc).__name__}"},
        )
        return
    研究毫秒 = int((time.perf_counter() - 研究開始) * 1000)
    合併證據, 合併來源 = 合併並重新編號證據(
        [*直接證據, *結果.get("evidence_bundle", [])],
        被取代的先前證據,
    )
    本輪策略 = "hybrid" if 直接證據 or 刷新網址 else "research"
    結果 = {
        **結果,
        "evidence_bundle": 合併證據,
        "source_registry": 合併來源,
        "search_queries": 查詢,
        "search_mode": 請求.search_mode,
        "mode": 請求.mode,
    }
    async for 事件 in _串流最終回答(
        原始問題=原始問題,
        搜尋結果=結果,
        問答模型=問答模型,
        語言=語言,
        對話歷史=對話歷史,
        turn_id=turn_id,
        規劃毫秒=規劃毫秒,
        研究毫秒=研究毫秒,
        研究策略=本輪策略,
        standalone_question=獨立問題,
        密封器=密封器,
        直接爬取毫秒=直接爬取毫秒,
        直接審核毫秒=直接審核毫秒,
        回答前總毫秒=int((time.perf_counter() - 全程開始) * 1000),
    ):
        yield 事件
    yield _SSE("done", {"message": _訊息(語言, "Search complete", "搜尋完成")})


@app.post("/api/search/stream")
async def search_stream(請求: 搜尋請求) -> StreamingResponse:
    return StreamingResponse(
        _執行研究(請求),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def unknown_api(path: str) -> JSONResponse:
    """避免舊版前端把 SPA 首頁 HTML 誤認成 API 回應。"""
    return JSONResponse({"detail": f"找不到 API 路徑：/api/{path}"}, status_code=404)


if 前端目錄.is_dir():
    assets = 前端目錄 / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        目標 = 前端目錄 / path
        if path and 目標.is_file() and 前端目錄 in 目標.resolve().parents:
            if path in {"index.html", "service-worker.js"}:
                return FileResponse(
                    目標,
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
                )
            return FileResponse(目標)
        index = 前端目錄 / "index.html"
        if index.is_file():
            return FileResponse(
                index,
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
        return JSONResponse({"message": "Simplex 前端尚未建置"}, status_code=503)
