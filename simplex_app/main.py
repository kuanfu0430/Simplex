"""Simplex FastAPI：設定、模型探索、研究搜尋與前端託管。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from deep_search_tool import deep_search, shutdown_shared_resources

from . import __version__
from .llm import 取得模型清單, 串流產生引用回答, 產生搜尋字詞, 解析模型設定, 尋找供應商
from .settings import 取得設定儲存庫


專案根目錄 = Path(__file__).resolve().parent.parent
前端目錄 = 專案根目錄 / "frontend" / "dist"


class 搜尋請求(BaseModel):
    question: str = Field(min_length=1, max_length=20000)
    search_mode: str = Field(default="web", pattern="^(web|academic|social)$")
    mode: str = Field(default="fast", pattern="^(instant|fast|full)$")
    search_queries: list[str] | None = None


class 設定請求(BaseModel):
    settings: dict[str, Any]


def _SSE(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _檢查SearXNG(base_url: str) -> dict[str, Any]:
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        return {"status": "disabled", "latency_ms": None, "message": "未設定地址"}
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
            "message": "可用",
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
            "message": "目前使用自有搜尋引擎",
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
    try:
        scale = float(請求.settings.get("ui", {}).get("scale", 1.0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="UI 縮放比例必須是數字")
    if not 0.8 <= scale <= 1.35:
        raise HTTPException(status_code=422, detail="UI 縮放比例必須介於 0.8 與 1.35")
    theme = str(請求.settings.get("ui", {}).get("theme", "dark"))
    if theme not in {"dark", "light"}:
        raise HTTPException(status_code=422, detail="主題必須是 dark 或 light")
    引擎模式 = str(請求.settings.get("search", {}).get("engine_mode", "searxng"))
    if 引擎模式 not in {"searxng", "custom"}:
        raise HTTPException(status_code=422, detail="搜尋引擎模式必須是 searxng 或 custom")
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
    return {"status": "configured", "message": "設定格式有效；實際搜尋會依供應商協定驗證"}


async def _執行研究(請求: 搜尋請求) -> AsyncIterator[str]:
    全程開始 = time.perf_counter()
    儲存庫 = 取得設定儲存庫()
    設定 = 儲存庫.讀取()
    問題 = 請求.question.strip()
    yield _SSE("status", {"phase": "planning", "message": "規劃搜尋字詞中"})

    問答模型 = 解析模型設定(設定, "question")
    Judge模型 = 解析模型設定(設定, "judge")
    try:
        查詢 = [值.strip() for 值 in (請求.search_queries or []) if 值.strip()][:3]
        if len(查詢) != 3:
            查詢 = await 產生搜尋字詞(問題, 問答模型)
    except Exception as exc:
        查詢 = [問題, f"{問題} 最新資料", f"{問題} 深度分析"]
        yield _SSE(
            "warning",
            {"message": f"問答模型規劃失敗，已使用快速查詢規則：{type(exc).__name__}"},
        )
    規劃毫秒 = int((time.perf_counter() - 全程開始) * 1000)

    yield _SSE(
        "status",
        {"phase": "searching", "message": "搜尋、Judge 與深爬進行中", "queries": 查詢},
    )
    研究開始 = time.perf_counter()
    try:
        結果 = await deep_search(
            question=問題,
            search_queries=查詢,
            search_mode=請求.search_mode,
            mode=請求.mode,
            judge_model_config=Judge模型 or {},
            search_provider_config=_有效搜尋設定(設定),
            verbose=False,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        yield _SSE("error", {"message": f"研究失敗：{type(exc).__name__}"})
        return
    研究毫秒 = int((time.perf_counter() - 研究開始) * 1000)

    yield _SSE(
        "status",
        {
            "phase": "answering",
            "message": "整理引用回答中",
            "summary": 結果.get("search_results_summary", {}),
        },
    )
    共同結果 = {
        "question": 問題,
        "search_queries": 查詢,
        "search_mode": 請求.search_mode,
        "mode": 請求.mode,
        "completion_state": 結果.get("completion_state"),
        "elapsed_ms": 結果.get("elapsed_ms"),
        "summary": 結果.get("search_results_summary", {}),
        "sources": 結果.get("source_registry", []),
        "evidence_bundle": 結果.get("evidence_bundle", []),
        "error": 結果.get("error"),
    }
    yield _SSE(
        "answer_start",
        {
            **共同結果,
            "answer": "",
            "timings": {
                "planning_ms": 規劃毫秒,
                "research_ms": 研究毫秒,
                "answer_first_token_ms": None,
                "answer_ms": None,
                "total_ms": None,
            },
        },
    )
    回答開始 = time.perf_counter()
    回答片段: list[str] = []
    首字毫秒: int | None = None
    try:
        async for 片段 in 串流產生引用回答(問題, 結果, 問答模型):
            if 首字毫秒 is None:
                首字毫秒 = int((time.perf_counter() - 回答開始) * 1000)
            回答片段.append(片段)
            yield _SSE("answer_delta", {"delta": 片段})
    except Exception as exc:
        if not 回答片段:
            降級回答 = "回答模型執行失敗，但研究證據已保留。請檢查問答模型設定。"
            首字毫秒 = int((time.perf_counter() - 回答開始) * 1000)
            回答片段.append(降級回答)
            yield _SSE("answer_delta", {"delta": 降級回答})
        yield _SSE("warning", {"message": f"回答生成失敗：{type(exc).__name__}"})
    回答 = "".join(回答片段).strip()
    回答毫秒 = int((time.perf_counter() - 回答開始) * 1000)
    總毫秒 = int((time.perf_counter() - 全程開始) * 1000)

    yield _SSE(
        "result",
        {
            **共同結果,
            "answer": 回答,
            "timings": {
                "planning_ms": 規劃毫秒,
                "research_ms": 研究毫秒,
                "answer_first_token_ms": 首字毫秒,
                "answer_ms": 回答毫秒,
                "total_ms": 總毫秒,
            },
        },
    )
    yield _SSE("done", {"message": "搜尋完成"})


@app.post("/api/search/stream")
async def search_stream(請求: 搜尋請求) -> StreamingResponse:
    return StreamingResponse(
        _執行研究(請求),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
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
