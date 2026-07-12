#!/usr/bin/env python3
"""可選的獨立爬蟲 MCP adapter；核心 backend 匯入時不會建立伺服器。"""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from types import ModuleType
from typing import Any


MCP_NAME = "crawl4ai-plus-fast"
MCP_HOST = "127.0.0.1"
MCP_PORT = 8001


def _fastmcp_class() -> Any:
    try:
        from fastmcp import FastMCP
    except ImportError:
        # 某些 fastmcp 安裝形態只在舊版相容路徑匯出 FastMCP。
        from mcp.server.fastmcp import FastMCP
    return FastMCP


def _build_mcp(fastmcp: Any, **kwargs: Any) -> Any:
    """相容不同 FastMCP 版本可接受的建構參數。"""
    current = dict(kwargs)
    server_name = kwargs.get("name", MCP_NAME)
    instructions = kwargs.get("instructions")

    while current:
        try:
            return fastmcp(**current)
        except TypeError as exc:
            message = str(exc)
            match = re.search(r"unexpected keyword argument '([^']+)'", message)
            if not match:
                match = re.search(r"no longer accepts `([^`]+)`", message)
            if not match:
                raise
            current.pop(match.group(1), None)

    try:
        return fastmcp(name=server_name, instructions=instructions)
    except TypeError:
        try:
            return fastmcp(name=server_name)
        except TypeError:
            return fastmcp(server_name)


def create_mcp(backend_module: ModuleType | None = None) -> Any:
    """建立並註冊獨立爬蟲 MCP；僅在明確呼叫時載入 FastMCP。"""
    if backend_module is None:
        import pro_search_crawl_backend as backend_module

    os.environ.setdefault("FASTMCP_HOST", MCP_HOST)
    os.environ.setdefault("FASTMCP_PORT", str(MCP_PORT))

    @asynccontextmanager
    async def lifespan(_: Any):
        try:
            yield {}
        finally:
            await backend_module._shutdown_shared_resources()

    mcp = _build_mcp(
        _fastmcp_class(),
        name=MCP_NAME,
        host=MCP_HOST,
        port=MCP_PORT,
        instructions="""高速確定性網頁爬取 MCP，適用於批量抓取、內容探測、連結發現。

架構：HTTP-first + JS 自動降級。
- 所有請求先以輕量 HTTP 嘗試。
- HTTP 結果品質不足時，在 render=auto 模式自動啟動 headless browser。
- 支援批量正文抓取、強制 JS、連結發現與站內關鍵字探測。""",
        lifespan=lifespan,
    )

    common_annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    tool_specs = (
        (
            "crawl4ai_crawl",
            "批量爬取 1-20 個 URL，HTTP-first 並自動 JS 降級；回傳內容、品質分數、渲染方式與診斷資訊。",
            "批量爬取（自動降級）",
        ),
        (
            "crawl4ai_with_js",
            "強制 JS 渲染單頁爬取，可執行內建捲動、關閉彈窗與等待模板。",
            "強制 JS 渲染爬取",
        ),
        (
            "crawl4ai_map",
            "從種子頁面發現並提取連結列表，可限制為相同網域。",
            "頁面連結發現",
        ),
        (
            "crawl4ai_probe",
            "從種子頁出發爬取同網域頁面，回傳關鍵字命中的上下文片段。",
            "關鍵字探測爬取",
        ),
    )
    for name, description, title in tool_specs:
        annotations = {**common_annotations, "title": title}
        mcp.tool(
            name=name,
            description=description,
            annotations=annotations,
        )(getattr(backend_module, name))
    return mcp


def run_server(backend_module: ModuleType | None = None) -> None:
    create_mcp(backend_module).run(transport="sse")


if __name__ == "__main__":
    run_server()
