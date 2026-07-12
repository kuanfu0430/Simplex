"""LLM provider 模型探索、查詢規劃與回答生成。"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx


模型逾時秒數 = 90.0


def 尋找供應商(設定: dict[str, Any], 供應商ID: str) -> dict[str, Any] | None:
    for 供應商 in 設定.get("llm", {}).get("providers", []):
        if str(供應商.get("id")) == 供應商ID:
            return dict(供應商)
    return None


def 解析模型設定(設定: dict[str, Any], 用途: str) -> dict[str, Any] | None:
    選擇 = 設定.get("llm", {}).get(f"{用途}_model", {})
    供應商ID = str(選擇.get("provider_id") or "")
    模型 = str(選擇.get("model") or "").strip()
    供應商 = 尋找供應商(設定, 供應商ID)
    if not 供應商 or not 模型 or not 供應商.get("enabled", True):
        return None
    return {**供應商, "provider": 供應商ID, "model": 模型}


def _授權標頭(供應商: dict[str, Any]) -> dict[str, str]:
    標頭 = {"Accept": "application/json", "Content-Type": "application/json"}
    api_key = str(供應商.get("api_key") or "").strip()
    if api_key:
        標頭["Authorization"] = f"Bearer {api_key}"
    額外標頭 = 供應商.get("headers")
    if isinstance(額外標頭, dict):
        標頭.update({str(鍵): str(值) for 鍵, 值 in 額外標頭.items()})
    return 標頭


async def 取得模型清單(供應商: dict[str, Any]) -> list[dict[str, str]]:
    base_url = str(供應商.get("base_url") or "").strip().rstrip("/")
    models_path = str(供應商.get("models_path") or "/models").strip()
    if models_path and not models_path.startswith("/"):
        models_path = f"/{models_path}"
    if not base_url:
        raise ValueError("供應商 API 地址不能為空")
    if not str(供應商.get("api_key") or "").strip():
        raise ValueError("請先填入 API Key")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as 客戶端:
        回應 = await 客戶端.get(
            f"{base_url}{models_path}",
            headers=_授權標頭(供應商),
        )
        回應.raise_for_status()
        資料 = 回應.json()

    原始模型 = 資料.get("data", 資料.get("models", [])) if isinstance(資料, dict) else []
    結果: list[dict[str, str]] = []
    for 項目 in 原始模型 if isinstance(原始模型, list) else []:
        if isinstance(項目, str):
            模型ID = 項目
            名稱 = 項目
        elif isinstance(項目, dict):
            模型ID = str(項目.get("id") or 項目.get("name") or "").strip()
            名稱 = str(項目.get("name") or 項目.get("display_name") or 模型ID).strip()
        else:
            continue
        if 模型ID:
            結果.append({"id": 模型ID, "name": 名稱 or 模型ID})
    return sorted(結果, key=lambda 項目: 項目["id"].casefold())


async def 呼叫聊天模型(
    模型設定: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
) -> str:
    base_url = str(模型設定.get("base_url") or "").strip().rstrip("/")
    endpoint = str(模型設定.get("chat_endpoint") or "/chat/completions").strip()
    if endpoint and not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"
    if not base_url or not endpoint:
        raise ValueError("模型 API 地址不完整")
    if not str(模型設定.get("api_key") or "").strip():
        raise ValueError("模型 API Key 未設定")

    請求: dict[str, Any] = {
        "model": str(模型設定.get("model") or ""),
        "messages": messages,
    }
    if temperature is not None:
        請求["temperature"] = temperature

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(模型逾時秒數),
        follow_redirects=True,
    ) as 客戶端:
        回應 = await 客戶端.post(
            f"{base_url}{endpoint}",
            headers=_授權標頭(模型設定),
            json=請求,
        )
        回應.raise_for_status()
        資料 = 回應.json()

    choices = 資料.get("choices", []) if isinstance(資料, dict) else []
    if not choices:
        raise RuntimeError("模型沒有回傳 choices")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        片段 = [str(項目.get("text")) for 項目 in content if isinstance(項目, dict) and 項目.get("text")]
        if 片段:
            return "\n".join(片段).strip()
    raise RuntimeError("模型沒有回傳有效文字")


def _串流文字片段(資料: Any) -> str:
    """解析 OpenAI-compatible 串流事件中的文字增量。"""
    choices = 資料.get("choices", []) if isinstance(資料, dict) else []
    if not choices or not isinstance(choices[0], dict):
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content") if isinstance(delta, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text"))
            for item in content
            if isinstance(item, dict) and item.get("text")
        )
    return ""


async def 串流聊天模型(
    模型設定: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
) -> AsyncIterator[str]:
    """以 OpenAI-compatible SSE 串流回傳回答文字。"""
    base_url = str(模型設定.get("base_url") or "").strip().rstrip("/")
    endpoint = str(模型設定.get("chat_endpoint") or "/chat/completions").strip()
    if endpoint and not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"
    if not base_url or not endpoint:
        raise ValueError("模型 API 地址不完整")
    if not str(模型設定.get("api_key") or "").strip():
        raise ValueError("模型 API Key 未設定")

    請求: dict[str, Any] = {
        "model": str(模型設定.get("model") or ""),
        "messages": messages,
        "stream": True,
    }
    if temperature is not None:
        請求["temperature"] = temperature

    有文字 = False
    已完成 = False
    當前事件 = "message"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(模型逾時秒數),
        follow_redirects=True,
    ) as 客戶端:
        async with 客戶端.stream(
            "POST",
            f"{base_url}{endpoint}",
            headers={**_授權標頭(模型設定), "Accept": "text/event-stream"},
            json=請求,
        ) as 回應:
            回應.raise_for_status()
            async for 行 in 回應.aiter_lines():
                if not 行:
                    當前事件 = "message"
                    continue
                if 行.startswith("event:"):
                    當前事件 = 行[6:].strip().lower() or "message"
                    continue
                if not 行.startswith("data:"):
                    continue
                payload = 行[5:].strip()
                if not payload:
                    continue
                if payload == "[DONE]":
                    已完成 = True
                    break
                try:
                    資料 = json.loads(payload)
                except json.JSONDecodeError:
                    if 當前事件 == "error":
                        raise RuntimeError(f"模型串流錯誤：{payload}")
                    continue
                if 當前事件 == "error" or (isinstance(資料, dict) and 資料.get("error")):
                    錯誤 = 資料.get("error") if isinstance(資料, dict) else 資料
                    if isinstance(錯誤, dict):
                        錯誤 = 錯誤.get("message") or 錯誤.get("code") or 錯誤
                    raise RuntimeError(f"模型串流錯誤：{錯誤}")
                片段 = _串流文字片段(資料)
                if 片段:
                    有文字 = True
                    yield 片段
                choices = 資料.get("choices", []) if isinstance(資料, dict) else []
                if choices and isinstance(choices[0], dict) and choices[0].get("finish_reason"):
                    已完成 = True
    if not 有文字:
        raise RuntimeError("模型串流沒有回傳有效文字")
    if not 已完成:
        raise RuntimeError("模型串流在完成訊號前中斷，回答可能不完整")


def _解析JSON陣列(文字: str) -> list[str]:
    候選 = re.search(r"\[[\s\S]*\]", 文字 or "")
    if not 候選:
        return []
    try:
        資料 = json.loads(候選.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(資料, list):
        return []
    結果: list[str] = []
    for 項目 in 資料:
        if isinstance(項目, str) and 項目.strip() and 項目.strip() not in 結果:
            結果.append(項目.strip())
    return 結果[:3]


async def 產生搜尋字詞(問題: str, 模型設定: dict[str, Any] | None) -> list[str]:
    if not 模型設定:
        return [問題, f"{問題} 最新資料", f"{問題} 深度分析"]
    文字 = await 呼叫聊天模型(
        模型設定,
        [
            {
                "role": "system",
                "content": "你是高速研究查詢規劃器。依問題產生三個互補、精準且可直接送搜尋引擎的查詢。只輸出 JSON 字串陣列。",
            },
            {"role": "user", "content": 問題},
        ],
        temperature=0.1,
    )
    查詢 = _解析JSON陣列(文字)
    while len(查詢) < 3:
        候選 = [問題, f"{問題} 最新資料", f"{問題} 深度分析"][len(查詢)]
        if 候選 not in 查詢:
            查詢.append(候選)
        else:
            查詢.append(f"{問題} 觀點 {len(查詢) + 1}")
    return 查詢[:3]


def _引用回答系統提示詞() -> str:
    """只供最終回答模型使用；Judge 與查詢規劃器不得套用。"""
    return (
        "你是 Simplex 的最終回答模型。只能閱讀 evidence 中各來源 chunks 的原文來回答，"
        "不得把搜尋摘要、query profile、query plans、content map 或 source registry 當作事實來源。"
        "當回答使用某來源的具體事實、數據、觀點或結論時，必須在相關句子後緊貼該來源的 "
        "citation_marker；不要把所有引用集中在段落或回答結尾。若同一句依據多個來源，"
        "可以連續放入多個 citation_marker。普通網址可使用 Markdown 連結，但可查證的來源引用"
        "應優先使用 citation_marker。不得虛構證據中沒有的資訊或引用；若證據不足或沒有可用來源，"
        "必須明確說明限制。使用與用戶相同的語言，輸出清楚的 Markdown。"
    )


async def 產生引用回答(
    問題: str,
    搜尋結果: dict[str, Any],
    模型設定: dict[str, Any] | None,
) -> str:
    evidence = 搜尋結果.get("evidence_bundle", [])
    if not evidence:
        return "目前沒有取得足以回答的可引用內容。請檢查搜尋服務或調整問題後重試。"
    if not 模型設定:
        摘要 = []
        for 來源 in evidence[:5]:
            標題 = 來源.get("title") or 來源.get("url") or "來源"
            標記 = 來源.get("citation_marker") or ""
            摘要.append(f"- {標題} {標記}".strip())
        return "已完成研究，但尚未設定問答模型。可用來源如下：\n\n" + "\n".join(摘要)

    精簡證據 = []
    for 來源 in evidence:
        精簡證據.append(
            {
                "title": 來源.get("title"),
                "url": 來源.get("url"),
                "citation_marker": 來源.get("citation_marker"),
                "chunks": [
                    {"id": 區塊.get("chunk_id"), "text": 區塊.get("text")}
                    for 區塊 in 來源.get("chunks", [])
                ],
            }
        )
    return await 呼叫聊天模型(
        模型設定,
        [
            {
                "role": "system",
                "content": _引用回答系統提示詞(),
            },
            {
                "role": "user",
                "content": f"問題：{問題}\n\nevidence：\n{json.dumps(精簡證據, ensure_ascii=False)}",
            },
        ],
        temperature=0.2,
    )


async def 串流產生引用回答(
    問題: str,
    搜尋結果: dict[str, Any],
    模型設定: dict[str, Any] | None,
) -> AsyncIterator[str]:
    """串流產生引用回答；無模型時仍以單一片段回傳降級內容。"""
    evidence = 搜尋結果.get("evidence_bundle", [])
    if not evidence or not 模型設定:
        yield await 產生引用回答(問題, 搜尋結果, 模型設定)
        return

    精簡證據 = []
    for 來源 in evidence:
        精簡證據.append(
            {
                "title": 來源.get("title"),
                "url": 來源.get("url"),
                "citation_marker": 來源.get("citation_marker"),
                "chunks": [
                    {"id": 區塊.get("chunk_id"), "text": 區塊.get("text")}
                    for 區塊 in 來源.get("chunks", [])
                ],
            }
        )
    async for 片段 in 串流聊天模型(
        模型設定,
        [
            {
                "role": "system",
                "content": _引用回答系統提示詞(),
            },
            {
                "role": "user",
                "content": f"問題：{問題}\n\nevidence：\n{json.dumps(精簡證據, ensure_ascii=False)}",
            },
        ],
        temperature=0.2,
    ):
        yield 片段
