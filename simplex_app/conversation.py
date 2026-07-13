"""多輪研究的受控上下文、證據膠囊與引用合併。"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken


歷史訊息上限 = 16
歷史估算Token上限 = 6000
沿用證據估算Token上限 = 8000
膠囊證據估算Token上限 = 12000
膠囊版本 = 1


def 估算Token數(文字: str) -> int:
    """不引入 tokenizer，保守估算中英文混合內容的提示詞成本。"""
    文字 = str(文字 or "")
    中文字數 = sum(1 for 字元 in 文字 if "\u3400" <= 字元 <= "\u9fff")
    其他字數 = max(0, len(文字) - 中文字數)
    return 中文字數 + (其他字數 + 3) // 4


def _裁切(文字: Any, 字元上限: int) -> str:
    內容 = str(文字 or "").strip()
    if len(內容) <= 字元上限:
        return 內容
    前段 = max(80, int(字元上限 * 0.72))
    後段 = max(40, 字元上限 - 前段 - 24)
    return f"{內容[:前段].rstrip()}\n…（內容已受控截短）…\n{內容[-後段:].lstrip()}"


def _回答摘錄(回答: str, 字元上限: int = 1200) -> str:
    """保留標題、條列與首尾段，避免舊回答吞沒新的規劃上下文。"""
    原文 = str(回答 or "").strip()
    if len(原文) <= 字元上限:
        return 原文

    行列 = [行.strip() for 行 in 原文.splitlines() if 行.strip()]
    重點 = [
        行
        for 行 in 行列
        if 行.startswith(("#", "- ", "* ", "•", "1.", "2.", "3."))
    ]
    片段 = ["（較早回答的受控摘錄）", *重點[:7]]
    if 行列:
        片段.extend([行列[0], 行列[-1]])
    去重後: list[str] = []
    for 行 in 片段:
        if 行 and 行 not in 去重後:
            去重後.append(行)
    return _裁切("\n".join(去重後), 字元上限)


def 準備對話歷史(原始訊息: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """保留首輪與最近七輪，並讓最近兩個回答優先保有完整脈絡。"""
    清理後: list[dict[str, str]] = []
    for 項目 in 原始訊息:
        if not isinstance(項目, dict):
            continue
        角色 = str(項目.get("role") or "")
        內容 = str(項目.get("content") or "").strip()
        if 角色 not in {"user", "assistant"} or not 內容:
            continue
        清理後.append({"role": 角色, "content": _裁切(內容, 12000)})

    if len(清理後) <= 歷史訊息上限:
        選取 = 清理後
    else:
        首輪 = 清理後[:2]
        最近 = 清理後[-(歷史訊息上限 - len(首輪)) :]
        選取 = 首輪 + [項目 for 項目 in 最近 if 項目 not in 首輪]

    回答索引 = [索引 for 索引, 項目 in enumerate(選取) if 項目["role"] == "assistant"]
    完整回答索引 = set(回答索引[-2:])
    受控: list[dict[str, str]] = []
    for 索引, 項目 in enumerate(選取):
        if 項目["role"] == "assistant":
            上限 = 6000 if 索引 in 完整回答索引 else 1200
            內容 = _裁切(項目["content"], 上限) if 索引 in 完整回答索引 else _回答摘錄(項目["content"], 上限)
        else:
            內容 = _裁切(項目["content"], 1800)
        受控.append({"role": 項目["role"], "content": 內容})

    while sum(估算Token數(項目["content"]) for 項目 in 受控) > 歷史估算Token上限:
        可縮短 = next(
            (
                索引
                for 索引, 項目 in enumerate(受控)
                if 項目["role"] == "assistant" and 索引 not in 完整回答索引 and len(項目["content"]) > 480
            ),
            None,
        )
        if 可縮短 is None:
            可縮短 = next(
                (索引 for 索引, 項目 in enumerate(受控) if len(項目["content"]) > 800),
                None,
            )
        if 可縮短 is None:
            break
        受控[可縮短] = {
            **受控[可縮短],
            "content": _裁切(受控[可縮短]["content"], max(360, len(受控[可縮短]["content"]) // 2)),
        }
    return 受控


def _正規化網址(url: Any) -> str:
    原值 = str(url or "").strip()
    if not 原值:
        return ""
    try:
        已拆 = urlsplit(原值)
    except ValueError:
        return 原值.rstrip("/")
    if not 已拆.scheme or not 已拆.netloc:
        return 原值.rstrip("/")
    return urlunsplit((已拆.scheme.lower(), 已拆.netloc.lower(), 已拆.path.rstrip("/"), 已拆.query, ""))


def _引用ID(url: str) -> str:
    """維持與深搜管線相同的可解析 citation id 表示。"""
    已拆 = urlsplit(url)
    if 已拆.scheme not in {"http", "https"} or not 已拆.netloc:
        return url
    內容 = f"//{已拆.netloc}{已拆.path or '/'}"
    if 已拆.query:
        內容 += f"?{已拆.query}"
    if 已拆.fragment:
        內容 += f"#{已拆.fragment}"
    return 內容


def _清理證據來源(來源: Any, *, turn_id: str = "") -> dict[str, Any] | None:
    if not isinstance(來源, dict):
        return None
    url = str(來源.get("url") or "").strip()
    if not url:
        return None
    chunks: list[dict[str, str]] = []
    已見區塊: set[tuple[str, str]] = set()
    for 索引, 區塊 in enumerate(來源.get("chunks") or []):
        if not isinstance(區塊, dict):
            continue
        文字 = str(區塊.get("text") or "").strip()
        if not 文字:
            continue
        原始ID = str(區塊.get("chunk_id") or f"C{索引 + 1}")
        區塊ID = f"{turn_id}:{原始ID}" if turn_id and not 原始ID.startswith(f"{turn_id}:") else 原始ID
        指紋 = (區塊ID, 文字)
        if 指紋 in 已見區塊:
            continue
        已見區塊.add(指紋)
        chunks.append({"chunk_id": 區塊ID, "text": 文字})
    if not chunks:
        return None
    return {
        "title": _裁切(來源.get("title") or url, 800),
        "url": url,
        "chunks": chunks,
    }


def _分散裁切證據(證據: Iterable[dict[str, Any]], token上限: int) -> list[dict[str, Any]]:
    """先保留不同來源的一段，再輪替補入後續 chunks。"""
    來源清單 = [來源 for 來源 in 證據 if isinstance(來源, dict)]
    來源游標 = [0 for _ in 來源清單]
    結果 = [
        {"title": 來源.get("title", ""), "url": 來源.get("url", ""), "chunks": []}
        for 來源 in 來源清單
    ]
    已用 = 0
    while True:
        有新增 = False
        for 索引, 來源 in enumerate(來源清單):
            chunks = 來源.get("chunks") or []
            游標 = 來源游標[索引]
            if 游標 >= len(chunks):
                continue
            區塊 = chunks[游標]
            來源游標[索引] += 1
            文字 = str(區塊.get("text") or "").strip()
            成本 = 估算Token數(文字)
            剩餘 = token上限 - 已用
            if 剩餘 <= 0:
                continue
            if 成本 > 剩餘:
                文字 = _裁切(文字, max(240, 剩餘 * 3))
                成本 = 估算Token數(文字)
            if not 文字 or 成本 > 剩餘:
                continue
            結果[索引]["chunks"].append({"chunk_id": str(區塊.get("chunk_id") or ""), "text": 文字})
            已用 += 成本
            有新增 = True
        if not 有新增 or 已用 >= token上限:
            break
    return [來源 for 來源 in 結果 if 來源["chunks"]]


def 建立證據膠囊(
    密碼器: Fernet,
    *,
    turn_id: str,
    standalone_question: str,
    queries: Iterable[str],
    evidence_bundle: Iterable[dict[str, Any]],
) -> str:
    清理後 = [
        來源
        for 來源 in (_清理證據來源(項目, turn_id=turn_id) for 項目 in evidence_bundle)
        if 來源 is not None
    ]
    證據 = _分散裁切證據(清理後, 膠囊證據估算Token上限)
    payload = {
        "version": 膠囊版本,
        "turn_id": str(turn_id),
        "standalone_question": _裁切(standalone_question, 4000),
        "queries": [_裁切(查詢, 600) for 查詢 in queries if str(查詢).strip()][:3],
        "evidence": 證據,
    }
    原文 = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return 密碼器.encrypt(原文).decode("utf-8")


def 解封證據膠囊(密碼器: Fernet, tokens: Iterable[str]) -> list[dict[str, Any]]:
    結果: list[dict[str, Any]] = []
    for token in list(tokens)[:8]:
        if not isinstance(token, str) or not token.strip() or len(token) > 180000:
            continue
        try:
            payload = json.loads(密碼器.decrypt(token.encode("utf-8")).decode("utf-8"))
        except (InvalidToken, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("version") != 膠囊版本:
            continue
        turn_id = str(payload.get("turn_id") or "")
        證據 = [
            來源
            for 來源 in (_清理證據來源(項目, turn_id=turn_id) for 項目 in payload.get("evidence") or [])
            if 來源 is not None
        ]
        if not 證據:
            continue
        結果.append(
            {
                "turn_id": turn_id,
                "standalone_question": _裁切(payload.get("standalone_question"), 4000),
                "queries": [_裁切(查詢, 600) for 查詢 in payload.get("queries") or [] if str(查詢).strip()][:3],
                "evidence": 證據,
            }
        )
    return 結果


def 建立研究帳本(膠囊: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """供規劃器辨識既有研究範圍；不把舊全文爬取結果送入規劃提示詞。"""
    帳本: list[dict[str, Any]] = []
    for 項目 in 膠囊:
        證據 = 項目.get("evidence") or []
        turn_id = str(項目.get("turn_id") or "")
        帳本.append(
            {
                "turn_id": turn_id,
                "question": 項目.get("standalone_question", ""),
                "queries": 項目.get("queries", []),
                "sources": [
                    {
                        "source_ref": f"{turn_id}:S{索引}",
                        "title": 來源.get("title", ""),
                        "url": 來源.get("url", ""),
                    }
                    for 索引, 來源 in enumerate(證據[:10], start=1)
                ],
                "selected_chunk_count": sum(len(來源.get("chunks") or []) for 來源 in 證據),
            }
        )
    return 帳本


def 選取可刷新來源(
    膠囊: Iterable[dict[str, Any]],
    source_refs: Iterable[Any],
    *,
    max_sources: int = 2,
) -> list[dict[str, str]]:
    """只接受證據帳本內的 source_ref，避免規劃模型自行指定任意 URL。"""
    index: dict[str, dict[str, str]] = {}
    for 項目 in 膠囊:
        turn_id = str(項目.get("turn_id") or "")
        for source_index, 來源 in enumerate(項目.get("evidence") or [], start=1):
            url = str(來源.get("url") or "").strip()
            if not turn_id or not url:
                continue
            source_ref = f"{turn_id}:S{source_index}"
            index[source_ref] = {
                "source_ref": source_ref,
                "title": str(來源.get("title") or url),
                "url": url,
            }

    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in source_refs:
        source_ref = str(value or "").strip()
        source = index.get(source_ref)
        if source is None:
            continue
        normalized = _正規化網址(source["url"])
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        selected.append(source)
        if len(selected) >= max(1, max_sources):
            break
    return selected


def _關鍵詞(問題: str) -> set[str]:
    小寫 = str(問題 or "").lower()
    英數 = {詞 for 詞 in re.findall(r"[\w-]{2,}", 小寫) if len(詞) >= 2}
    中文 = "".join(re.findall(r"[\u3400-\u9fff]", 小寫))
    雙字詞 = {中文[索引 : 索引 + 2] for 索引 in range(max(0, len(中文) - 1))}
    return 英數 | 雙字詞


def 選取相關先前證據(膠囊: Iterable[dict[str, Any]], 問題: str) -> list[dict[str, Any]]:
    """以問題關鍵詞與新近度選出少量已驗證 chunks，供追問沿用。"""
    詞彙 = _關鍵詞(問題)
    來源候選: list[tuple[int, int, dict[str, Any]]] = []
    for 新近度, 項目 in enumerate(膠囊):
        for 來源 in 項目.get("evidence") or []:
            可搜尋文字 = f"{來源.get('title', '')}\n" + "\n".join(
                str(區塊.get("text") or "") for 區塊 in 來源.get("chunks") or []
            )
            小寫 = 可搜尋文字.lower()
            分數 = sum(1 for 詞 in 詞彙 if 詞 in 小寫)
            來源候選.append((分數, -新近度, 來源))
    來源候選.sort(key=lambda 項目: (項目[0], 項目[1]), reverse=True)
    去重來源: list[dict[str, Any]] = []
    已見網址: set[str] = set()
    for _, _, 來源 in 來源候選:
        key = _正規化網址(來源.get("url"))
        if not key or key in 已見網址:
            continue
        已見網址.add(key)
        去重來源.append(來源)
    return _分散裁切證據(去重來源, 沿用證據估算Token上限)


def 合併並重新編號證據(
    現行證據: Iterable[dict[str, Any]],
    先前證據: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """現行證據優先，與沿用證據按 canonical URL 去重並重建 citation marker。"""
    合併來源: dict[str, dict[str, Any]] = {}
    順序: list[str] = []
    已見區塊: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for 原始來源 in [*現行證據, *先前證據]:
        來源 = _清理證據來源(原始來源)
        if 來源 is None:
            continue
        key = _正規化網址(來源["url"])
        if not key:
            continue
        if key not in 合併來源:
            合併來源[key] = {"title": 來源["title"], "url": 來源["url"], "chunks": []}
            順序.append(key)
        目標 = 合併來源[key]
        if not 目標.get("title") and 來源.get("title"):
            目標["title"] = 來源["title"]
        for 區塊 in 來源["chunks"]:
            指紋 = (區塊["chunk_id"], 區塊["text"])
            if 指紋 in 已見區塊[key]:
                continue
            已見區塊[key].add(指紋)
            目標["chunks"].append(區塊)

    evidence_bundle: list[dict[str, Any]] = []
    source_registry: list[dict[str, Any]] = []
    for 索引, key in enumerate(順序, start=1):
        來源 = 合併來源[key]
        citation_id = _引用ID(來源["url"])
        marker = f"[citation]({索引}:{citation_id})"
        evidence_bundle.append(
            {
                "source_index": 索引,
                "title": 來源["title"],
                "url": 來源["url"],
                "citation_id": citation_id,
                "citation_marker": marker,
                "chunks": 來源["chunks"],
            }
        )
        source_registry.append(
            {
                "source_index": 索引,
                "citation_id": citation_id,
                "citation_marker": marker,
                "title": 來源["title"],
                "url": 來源["url"],
            }
        )
    return evidence_bundle, source_registry


def 排除指定網址證據(
    證據: Iterable[dict[str, Any]],
    urls: Iterable[str],
) -> list[dict[str, Any]]:
    """刷新或本輪重讀同一來源時，移除膠囊中舊版本的 chunks。"""
    replaced = {_正規化網址(url) for url in urls if _正規化網址(url)}
    if not replaced:
        return [來源 for 來源 in 證據 if isinstance(來源, dict)]
    return [
        來源
        for 來源 in 證據
        if isinstance(來源, dict) and _正規化網址(來源.get("url")) not in replaced
    ]


def 展平先前證據供Judge使用(證據: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    結果: list[dict[str, Any]] = []
    for 來源 in 證據:
        for 區塊 in 來源.get("chunks") or []:
            結果.append(
                {
                    "chunk_id": 區塊.get("chunk_id", ""),
                    "text": 區塊.get("text", ""),
                    "source_url": 來源.get("url", ""),
                    "title": 來源.get("title", ""),
                    "from_query": "Prior verified evidence",
                }
            )
    return 結果
