"""LLM provider 模型探索、查詢規劃與回答生成。"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx


模型逾時秒數 = 90.0

搜尋規劃補充系統提示詞 = """## 搜尋時效性
在面對有時效性的問題時一律依用戶要求的新鮮度搜尋最新日期之資訊。

## 搜尋語言策略
在設計搜尋關鍵詞時應視問題的屬性而決定搜尋語言，若無特別指向性應以英文優先或混合使用以確保資料多樣性，用戶的提問語言與國家是其次，其所提問的問題才是搜尋分詞的關鍵。（例如若用戶問“哪個日本明星最受日本人喜愛”，你就應該三組搜尋詞都使用日文，以確認取得日本的搜尋資料以增加回答品質，又若用戶提問“史普尼克危機是如何產生的”，由於這是美蘇冷戰史，你就應該用三組英文搜尋詞去進行搜尋）"""

回答補充系統提示詞 = """## 回答語言
無論用什麼語言進行搜尋，在回答時一律以用戶所提問的語言進行回答。

## 回答形式與詳實程度
給予清晰的條列式回答，逐步而詳盡的陳述你拿到的資料內容，轉化為清楚而詳實的內容給用戶，你不應隨便的斷章取義，也不該直接融合做總結，你要交出的是一份匯報而不是單純地給出直接答案。"""


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


def 解析搜尋模型設定(
    設定: dict[str, Any],
    選擇: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """只接受設定頁模型池或預設問答模型，避免 API 任意指定 Provider。"""
    預設 = 解析模型設定(設定, "question")
    if not 選擇:
        return 預設

    供應商ID = str(選擇.get("provider_id") or "").strip()
    模型 = str(選擇.get("model") or "").strip()
    if not 供應商ID or not 模型:
        return 預設

    可選模型 = [
        {
            "provider_id": str(項目.get("provider_id") or ""),
            "model": str(項目.get("model") or ""),
        }
        for 項目 in 設定.get("llm", {}).get("model_pool", [])
        if isinstance(項目, dict)
    ]
    預設選擇 = 設定.get("llm", {}).get("question_model", {})
    可選模型.append(
        {
            "provider_id": str(預設選擇.get("provider_id") or ""),
            "model": str(預設選擇.get("model") or ""),
        }
    )
    if not any(項目["provider_id"] == 供應商ID and 項目["model"] == 模型 for 項目 in 可選模型):
        raise ValueError("選取的研究模型不在模型池中")

    供應商 = 尋找供應商(設定, 供應商ID)
    if not 供應商 or not 供應商.get("enabled", True):
        raise ValueError("選取的研究模型 Provider 無法使用")
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


def _預設推理請求欄位(模型設定: dict[str, Any]) -> dict[str, Any]:
    """頭尾 LLM 使用預設推理；OpenRouter 使用其官方統一 reasoning 物件。"""
    供應商 = str(模型設定.get("provider") or "").strip().lower()
    base_url = str(模型設定.get("base_url") or "").lower()
    if 供應商 == "openrouter" or "openrouter.ai" in base_url:
        return {"reasoning": {"enabled": True}}
    # OpenAI-compatible Chat Completions 的標準欄位；不暴露給設定頁調整。
    return {"reasoning_effort": "medium"}


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
    啟用推理: bool = False,
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
    if 啟用推理:
        請求.update(_預設推理請求欄位(模型設定))

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
    啟用推理: bool = False,
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
    if 啟用推理:
        請求.update(_預設推理請求欄位(模型設定))

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


def _解析JSON物件(文字: str) -> dict[str, Any]:
    """容忍模型外層的 Markdown，但只接受單一 JSON 物件。"""
    decoder = json.JSONDecoder()
    for 起點, 字元 in enumerate(文字 or ""):
        if 字元 != "{":
            continue
        try:
            資料, _ = decoder.raw_decode((文字 or "")[起點:])
        except json.JSONDecodeError:
            continue
        return 資料 if isinstance(資料, dict) else {}
    return {}


def 備用搜尋字詞(問題: str, 語言: str = "en") -> list[str]:
    if 語言 == "zh-TW":
        return [問題, f"{問題} 最新資料", f"{問題} 深度分析"]
    return [問題, f"{問題} latest information", f"{問題} in-depth analysis"]


async def 產生搜尋字詞(
    問題: str,
    模型設定: dict[str, Any] | None,
    語言: str = "en",
    *,
    對話歷史: list[dict[str, str]] | None = None,
    證據帳本: list[dict[str, Any]] | None = None,
    指定連結脈絡: list[dict[str, str]] | None = None,
    強制研究: bool = False,
    結構化規劃: bool = False,
) -> list[str] | dict[str, Any]:
    """用既有的查詢規劃呼叫同時完成追問解析與研究路由。"""
    備用規劃 = {
        "standalone_question": 問題,
        "strategy": "research",
        "queries": 備用搜尋字詞(問題, 語言),
    }
    if not 模型設定:
        return 備用規劃 if 結構化規劃 else 備用規劃["queries"]
    多輪 = bool(對話歷史 or 證據帳本)
    有指定連結脈絡 = bool(指定連結脈絡)
    if 語言 == "zh-TW":
        規劃提示詞 = """你是高速研究查詢與追問路由規劃器。請先把當前提問改寫成脫離對話也能理解的 standalone_question，再決定 strategy。
只有當問題能完全以既有的「已驗證證據」回答、改寫、整理、比較或延伸說明時才用 reuse；凡是詢問最新狀態、新事實、要求驗證、要求新來源、擴大範圍、存在明確缺口或你不確定時，一律用 research。歷史回答只用來理解代詞、承接關係與使用者意圖，絕不能當成事實證據。證據帳本僅列出既有研究範圍，並非全文。
strategy 為 research 時，queries 必須是三個互補、精準且可直接送搜尋引擎的查詢；strategy 為 reuse 時仍輸出三個空字串。查詢語言應依問題的屬性與搜尋語言策略決定，不受介面語言限制。
若提供「已讀取指定連結摘要」，只有當該些連結原文已足以完整回答、且使用者沒有要求最新資訊、外部驗證或額外來源時，才能使用 direct；direct 時 queries 必須是三個空字串。指定連結摘要只是受限摘錄，選擇 direct 時務必保守。
strategy 為 research 時，可輸出 refresh_source_refs：只可使用既有證據帳本提供的 source_ref，且僅在使用者明確要求重看、重新驗證、更新或提取某個先前來源時選擇，最多兩個；一般追問不要為了方便而重爬舊來源。
只輸出 JSON 物件：{"standalone_question":"...","strategy":"reuse、direct 或 research","queries":["...","...","..."],"refresh_source_refs":["turn:S1"]}。"""
    else:
        規劃提示詞 = """You are a fast research query and follow-up router. First rewrite the current request into a standalone_question that is understandable without the conversation, then decide strategy.
Use reuse only when the request can be fully answered, transformed, organized, compared, or explained from already verified evidence. Use research for current information, new facts, verification, new sources, expanded scope, explicit gaps, or any uncertainty. Conversation history may resolve references and intent but is never factual evidence. The evidence ledger only describes prior coverage; it is not source text.
For research, queries must contain three complementary, precise search-engine-ready queries. For reuse, output three empty strings. Choose the query language from the subject and search-language strategy, not the UI language.
When a Read provided links context is present, use direct only when those source excerpts fully answer the request and the user does not require current information, external verification, or additional sources; for direct, queries must be three empty strings. The link context is bounded, so choose direct conservatively.
For research, refresh_source_refs may contain at most two source_ref values supplied by the verified evidence ledger, and only when the user explicitly asks to reread, recheck, update, or extract more detail from a prior source. Do not recrawl prior sources by default.
Output only this JSON object: {"standalone_question":"...","strategy":"reuse, direct, or research","queries":["...","...","..."],"refresh_source_refs":["turn:S1"]}."""
    規劃提示詞 = f"{規劃提示詞}\n\n{搜尋規劃補充系統提示詞}"
    輸入段落: list[str] = []
    if 多輪:
        輸入段落.append(
            f"{'歷史對話' if 語言 == 'zh-TW' else 'Conversation history'}：\n"
            f"{json.dumps(對話歷史 or [], ensure_ascii=False)}\n\n"
            f"{'既有證據帳本' if 語言 == 'zh-TW' else 'Verified evidence ledger'}：\n"
            f"{json.dumps(證據帳本 or [], ensure_ascii=False)}"
        )
    if 有指定連結脈絡:
        輸入段落.append(
            f"{'已讀取指定連結摘要' if 語言 == 'zh-TW' else 'Read provided links context'}：\n"
            f"{json.dumps(指定連結脈絡 or [], ensure_ascii=False)}"
        )
    輸入段落.append(
        f"{'本輪問題' if 語言 == 'zh-TW' else 'Current question'}：{問題}\n"
        f"{'使用者已要求強制重新研究。' if 強制研究 else ''}"
    )
    輸入內容 = "\n\n".join(輸入段落)
    文字 = await 呼叫聊天模型(
        模型設定,
        [
            {
                "role": "system",
                "content": 規劃提示詞,
            },
            {"role": "user", "content": 輸入內容},
        ],
        temperature=0.1,
        啟用推理=True,
    )
    if not 結構化規劃:
        查詢 = _解析JSON陣列(文字)
        if not 查詢:
            查詢 = list(_解析JSON物件(文字).get("queries") or [])
    else:
        物件 = _解析JSON物件(文字)
        strategy = str(物件.get("strategy") or "research").lower()
        if strategy == "reuse" and 多輪 and not 強制研究:
            strategy = "reuse"
        elif strategy == "direct" and 有指定連結脈絡 and not 強制研究:
            strategy = "direct"
        else:
            strategy = "research"
        standalone_question = str(物件.get("standalone_question") or 問題).strip()[:20000] or 問題
        查詢 = [str(值).strip() for 值 in (物件.get("queries") or []) if str(值).strip()]
        if strategy in {"reuse", "direct"}:
            規劃 = {"standalone_question": standalone_question, "strategy": strategy, "queries": []}
            return 規劃
        refresh_source_refs = [
            str(value).strip()
            for value in (物件.get("refresh_source_refs") or [])
            if str(value).strip()
        ][:2]
    while len(查詢) < 3:
        候選 = 備用搜尋字詞(問題, 語言)[len(查詢)]
        if 候選 not in 查詢:
            查詢.append(候選)
        else:
            後綴 = f"觀點 {len(查詢) + 1}" if 語言 == "zh-TW" else f"perspective {len(查詢) + 1}"
            查詢.append(f"{問題} {後綴}")
    查詢 = 查詢[:3]
    if 結構化規劃:
        規劃 = {"standalone_question": standalone_question, "strategy": "research", "queries": 查詢}
        if refresh_source_refs:
            規劃["refresh_source_refs"] = refresh_source_refs
        return 規劃
    return 查詢


def _引用回答系統提示詞(語言: str = "en") -> str:
    """只供最終回答模型使用；Judge 與查詢規劃器不得套用。"""
    if 語言 == "zh-TW":
        return (
            "你是 Simplex 的最終回答模型。只能閱讀 evidence 中各來源 chunks 的原文來回答，"
            "不得把搜尋摘要、query profile、query plans、content map 或 source registry 當作事實來源。"
            "當回答使用某來源的具體事實、數據、觀點或結論時，必須在相關句子後緊貼該來源的 "
            "citation_marker；不要把所有引用集中在段落或回答結尾。若同一句依據多個來源，"
            "可以連續放入多個 citation_marker。普通網址可使用 Markdown 連結，但可查證的來源引用"
            "應優先使用 citation_marker。歷史對話可用於理解代詞、承接關係與格式偏好，但不是事實或引用來源；"
            "不得根據歷史回答補入 evidence 沒有的資訊。不得虛構證據中沒有的資訊或引用；若證據不足或沒有可用來源，"
            "必須明確說明限制。使用用戶原始問題的語言與標準 GitHub-Flavored Markdown 輸出；可在比較或彙整時使用正確的 Markdown 表格（必須包含標題列與分隔列），也可使用標題、清單與程式碼區塊。"
            f"\n\n{回答補充系統提示詞}"
        )
    return (
        "You are Simplex's final answer model. Answer only from the original text in each evidence chunk. "
        "Do not treat search snippets, query profiles, query plans, content maps, or the source registry as factual evidence. "
        "When an answer uses a source's fact, number, view, or conclusion, place that source's citation_marker immediately after the relevant sentence; "
        "do not collect citations only at the end of a paragraph or answer. If a sentence relies on multiple sources, "
        "place their citation_markers next to one another. Ordinary URLs may use Markdown links, but verifiable source citations should prefer citation_marker. "
        "Conversation history may resolve references, continuity, and formatting preferences, but it is not a factual or citation source; never add facts absent from evidence because they appeared in earlier answers. "
        "Do not invent information or citations absent from the evidence. If the evidence is insufficient or no source is available, state the limitation clearly. "
        "Write the final answer in the language of the user's original question using standard GitHub-Flavored Markdown. Use a valid Markdown table with a header row and separator row when a comparison or summary is clearer as a table; headings, lists, and fenced code blocks are also allowed."
        f"\n\n{回答補充系統提示詞}"
    )


def _組裝回答訊息(
    問題: str,
    精簡證據: list[dict[str, Any]],
    語言: str,
    對話歷史: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    訊息: list[dict[str, str]] = [{"role": "system", "content": _引用回答系統提示詞(語言)}]
    for 項目 in 對話歷史 or []:
        角色 = str(項目.get("role") or "")
        內容 = str(項目.get("content") or "").strip()
        if 角色 in {"user", "assistant"} and 內容:
            訊息.append({"role": 角色, "content": 內容})
    問題標籤 = "問題" if 語言 == "zh-TW" else "Question"
    證據標籤 = "可引用 evidence" if 語言 == "zh-TW" else "Citable evidence"
    訊息.append(
        {
            "role": "user",
            "content": f"{問題標籤}：{問題}\n\n{證據標籤}：\n{json.dumps(精簡證據, ensure_ascii=False)}",
        }
    )
    return 訊息


async def 產生引用回答(
    問題: str,
    搜尋結果: dict[str, Any],
    模型設定: dict[str, Any] | None,
    語言: str = "en",
    對話歷史: list[dict[str, str]] | None = None,
) -> str:
    evidence = 搜尋結果.get("evidence_bundle", [])
    if not evidence:
        if 語言 == "zh-TW":
            return "目前沒有取得足以回答的可引用內容。請檢查搜尋服務或調整問題後重試。"
        return "No citable content was retrieved to answer this question. Check the search service or try again with a refined question."
    if not 模型設定:
        摘要 = []
        for 來源 in evidence[:5]:
            標題 = 來源.get("title") or 來源.get("url") or "來源"
            標記 = 來源.get("citation_marker") or ""
            摘要.append(f"- {標題} {標記}".strip())
        if 語言 == "zh-TW":
            return "已完成研究，但尚未設定問答模型。可用來源如下：\n\n" + "\n".join(摘要)
        return "Research is complete, but no question model is configured. Available sources:\n\n" + "\n".join(摘要)

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
        _組裝回答訊息(問題, 精簡證據, 語言, 對話歷史),
        temperature=0.2,
        啟用推理=True,
    )


async def 串流產生引用回答(
    問題: str,
    搜尋結果: dict[str, Any],
    模型設定: dict[str, Any] | None,
    語言: str = "en",
    對話歷史: list[dict[str, str]] | None = None,
) -> AsyncIterator[str]:
    """串流產生引用回答；無模型時仍以單一片段回傳降級內容。"""
    evidence = 搜尋結果.get("evidence_bundle", [])
    if not evidence or not 模型設定:
        yield await 產生引用回答(問題, 搜尋結果, 模型設定, 語言, 對話歷史)
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
        _組裝回答訊息(問題, 精簡證據, 語言, 對話歷史),
        temperature=0.2,
        啟用推理=True,
    ):
        yield 片段
