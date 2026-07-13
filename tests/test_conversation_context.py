"""多輪對話的受控上下文與 SSE 路由測試。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import simplex_app.llm as 模型模組
import simplex_app.main as 應用模組
from simplex_app.conversation import (
    合併並重新編號證據,
    建立證據膠囊,
    準備對話歷史,
    解封證據膠囊,
    選取可刷新來源,
)
from simplex_app.settings import 設定儲存庫


class 對話上下文測試(unittest.TestCase):
    def test_膠囊遭竄改不會成為證據且會重新編號引用(self) -> None:
        with tempfile.TemporaryDirectory() as 暫存:
            儲存庫 = 設定儲存庫(Path(暫存) / "settings.db", Path(暫存) / "settings.key")
            膠囊 = 建立證據膠囊(
                儲存庫.取得本機密封器(),
                turn_id="turn-a",
                standalone_question="原題",
                queries=["query one", "query two", "query three"],
                evidence_bundle=[
                    {
                        "title": "已驗證來源",
                        "url": "https://example.com/article",
                        "chunks": [{"chunk_id": "C1", "text": "可引用的原始證據。"}],
                    }
                ],
            )
            解封 = 解封證據膠囊(儲存庫.取得本機密封器(), [膠囊, "tampered"])

        self.assertEqual(len(解封), 1)
        self.assertEqual(解封[0]["evidence"][0]["chunks"][0]["chunk_id"], "turn-a:C1")
        evidence, registry = 合併並重新編號證據([], 解封[0]["evidence"])
        self.assertEqual(registry[0]["citation_marker"], "[citation](1://example.com/article)")
        self.assertEqual(evidence[0]["source_index"], 1)

    def test_歷史保留首輪與最近七輪並受控壓縮(self) -> None:
        原始 = []
        for 索引 in range(10):
            原始.extend([
                {"role": "user", "content": f"問題 {索引}"},
                {"role": "assistant", "content": f"# 回答 {索引}\n- 證據重點\n" + "內容" * 900},
            ])
        歷史 = 準備對話歷史(原始)

        self.assertLessEqual(len(歷史), 16)
        self.assertEqual(歷史[0]["content"], "問題 0")
        self.assertIn("問題 9", [項目["content"] for 項目 in 歷史 if 項目["role"] == "user"])


class 多輪搜尋API測試(unittest.TestCase):
    def setUp(self) -> None:
        self.暫存 = tempfile.TemporaryDirectory()
        根 = Path(self.暫存.name)
        self.儲存庫 = 設定儲存庫(根 / "settings.db", 根 / "settings.key")

    def tearDown(self) -> None:
        self.暫存.cleanup()

    @staticmethod
    def _讀取結果(文字: str) -> dict[str, object]:
        區塊 = next(項目 for 項目 in 文字.split("\n\n") if 項目.startswith("event: result"))
        return json.loads(next(行[5:].strip() for 行 in 區塊.splitlines() if 行.startswith("data:")))

    def test_追問沿用已驗證證據且不啟動深搜(self) -> None:
        膠囊 = 建立證據膠囊(
            self.儲存庫.取得本機密封器(),
            turn_id="old-turn",
            standalone_question="原始研究問題",
            queries=["one", "two", "three"],
            evidence_bundle=[
                {
                    "title": "原始來源",
                    "url": "https://example.com/evidence",
                    "chunks": [{"chunk_id": "C1", "text": "第一點的已驗證資料。"}],
                }
            ],
        )
        模擬深搜 = AsyncMock()
        async def 模擬回答(*_args, **_kwargs):
            yield "整理後的追問答案"

        with (
            patch.object(應用模組, "取得設定儲存庫", return_value=self.儲存庫),
            patch.object(應用模組, "deep_search", new=模擬深搜),
            patch.object(應用模組, "產生搜尋字詞", new=AsyncMock(return_value={"standalone_question": "把第一點整理成表格", "strategy": "reuse", "queries": []})),
            patch.object(應用模組, "串流產生引用回答", new=模擬回答),
        ):
            with TestClient(應用模組.app) as 客戶端:
                回應 = 客戶端.post("/api/search/stream", json={
                    "question": "把第一點整理成表格",
                    "turn_id": "next-turn",
                    "conversation_history": [
                        {"role": "user", "content": "原始研究問題"},
                        {"role": "assistant", "content": "先前回答"},
                    ],
                    "context_capsules": [膠囊],
                })

        self.assertEqual(回應.status_code, 200)
        模擬深搜.assert_not_awaited()
        結果 = self._讀取結果(回應.text)
        self.assertEqual(結果["research_strategy"], "reuse")
        self.assertEqual(結果["answer"], "整理後的追問答案")
        self.assertTrue(結果["context_capsule"])

    def test_重新研究會交付獨立問題與先前證據給管線(self) -> None:
        膠囊 = 建立證據膠囊(
            self.儲存庫.取得本機密封器(),
            turn_id="old-turn",
            standalone_question="原始研究問題",
            queries=["one", "two", "three"],
            evidence_bundle=[
                {
                    "title": "原始來源",
                    "url": "https://example.com/evidence",
                    "chunks": [{"chunk_id": "C1", "text": "舊資料"}],
                }
            ],
        )
        模擬結果 = {
            "completion_state": "complete",
            "elapsed_ms": 1,
            "search_results_summary": {},
            "source_registry": [],
            "evidence_bundle": [{"title": "新來源", "url": "https://new.example/article", "chunks": [{"chunk_id": "N1", "text": "新資料"}]}],
            "error": None,
        }
        模擬深搜 = AsyncMock(return_value=模擬結果)
        async def 模擬回答(*_args, **_kwargs):
            yield "新的研究答案"

        with (
            patch.object(應用模組, "取得設定儲存庫", return_value=self.儲存庫),
            patch.object(應用模組, "deep_search", new=模擬深搜),
            patch.object(應用模組, "產生搜尋字詞", new=AsyncMock(return_value={"standalone_question": "找最新的反對研究", "strategy": "research", "queries": ["latest opposition one", "latest opposition two", "latest opposition three"]})),
            patch.object(應用模組, "串流產生引用回答", new=模擬回答),
        ):
            with TestClient(應用模組.app) as 客戶端:
                回應 = 客戶端.post("/api/search/stream", json={
                    "question": "那找最新的反對研究",
                    "context_capsules": [膠囊],
                    "force_research": True,
                })

        self.assertEqual(回應.status_code, 200)
        self.assertEqual(模擬深搜.await_args.kwargs["question"], "找最新的反對研究")
        self.assertEqual(len(模擬深搜.await_args.kwargs["prior_evidence_chunks"]), 1)
        結果 = self._讀取結果(回應.text)
        self.assertEqual(結果["research_strategy"], "research")
        self.assertEqual(結果["standalone_question"], "找最新的反對研究")

    def test_指定網址證據充分時不啟動一般搜尋(self) -> None:
        網址 = "https://example.com/provided"
        直接頁面 = {"url": 網址, "title": "指定來源", "content": "指定來源的完整可引用內容。", "success": True}
        直接審核 = {
            "review": {"verdict": "sufficient"},
            "source_registry": [{"source_index": 1, "title": "指定來源", "url": 網址, "citation_marker": "[citation](1://example.com/provided)"}],
            "evidence_bundle": [{"title": "指定來源", "url": 網址, "chunks": [{"chunk_id": "L0-S1-C001", "text": "指定來源的完整可引用內容。"}]}],
            "selected_chunks": [{"chunk_id": "L0-S1-C001", "title": "指定來源", "source_url": 網址, "from_query": "Provided URL", "text": "指定來源的完整可引用內容。"}],
            "elapsed_ms": 8,
        }
        模擬深搜 = AsyncMock()

        async def 模擬回答(*_args, **_kwargs):
            yield "直接連結回答"

        with (
            patch.object(應用模組, "取得設定儲存庫", return_value=self.儲存庫),
            patch.object(應用模組, "crawl_explicit_urls", new=AsyncMock(return_value={"pages": [直接頁面], "failed": [], "elapsed_ms": 12})),
            patch.object(應用模組, "build_direct_planner_context", return_value=[{"chunk_id": "L0-S1-C001", "title": "指定來源", "url": 網址, "text": "指定來源的完整可引用內容。"}]),
            patch.object(應用模組, "review_explicit_pages", new=AsyncMock(return_value=直接審核)),
            patch.object(應用模組, "產生搜尋字詞", new=AsyncMock(return_value={"standalone_question": "請摘要指定連結", "strategy": "direct", "queries": []})),
            patch.object(應用模組, "deep_search", new=模擬深搜),
            patch.object(應用模組, "串流產生引用回答", new=模擬回答),
        ):
            with TestClient(應用模組.app) as 客戶端:
                回應 = 客戶端.post("/api/search/stream", json={"question": f"請摘要這個網址：{網址}"})

        self.assertEqual(回應.status_code, 200)
        模擬深搜.assert_not_awaited()
        結果 = self._讀取結果(回應.text)
        self.assertEqual(結果["research_strategy"], "direct")
        self.assertEqual(結果["answer"], "直接連結回答")
        self.assertIn('"type": "direct_sources"', 回應.text)
        self.assertIn('"type": "direct_evidence"', 回應.text)

    def test_指定網址不足時會混合既有直接證據與新搜尋(self) -> None:
        網址 = "https://example.com/provided"
        直接頁面 = {"url": 網址, "title": "指定來源", "content": "只回答部分內容。", "success": True}
        直接審核 = {
            "review": {"verdict": "insufficient"},
            "source_registry": [],
            "evidence_bundle": [{"title": "指定來源", "url": 網址, "chunks": [{"chunk_id": "L0-S1-C001", "text": "只回答部分內容。"}]}],
            "selected_chunks": [{"chunk_id": "L0-S1-C001", "title": "指定來源", "source_url": 網址, "from_query": "Provided URL", "text": "只回答部分內容。"}],
            "elapsed_ms": 8,
        }
        模擬深搜 = AsyncMock(return_value={
            "completion_state": "complete",
            "elapsed_ms": 11,
            "search_results_summary": {},
            "source_registry": [],
            "evidence_bundle": [{"title": "新來源", "url": "https://new.example/article", "chunks": [{"chunk_id": "L1-S1-C001", "text": "搜尋補足內容。"}]}],
            "error": None,
        })

        async def 模擬回答(*_args, **_kwargs):
            yield "混合研究回答"

        with (
            patch.object(應用模組, "取得設定儲存庫", return_value=self.儲存庫),
            patch.object(應用模組, "crawl_explicit_urls", new=AsyncMock(return_value={"pages": [直接頁面], "failed": [], "elapsed_ms": 12})),
            patch.object(應用模組, "build_direct_planner_context", return_value=[{"chunk_id": "L0-S1-C001", "title": "指定來源", "url": 網址, "text": "只回答部分內容。"}]),
            patch.object(應用模組, "review_explicit_pages", new=AsyncMock(return_value=直接審核)),
            patch.object(應用模組, "產生搜尋字詞", new=AsyncMock(return_value={"standalone_question": "指定連結還缺什麼", "strategy": "research", "queries": ["補足一", "補足二", "補足三"]})),
            patch.object(應用模組, "deep_search", new=模擬深搜),
            patch.object(應用模組, "串流產生引用回答", new=模擬回答),
        ):
            with TestClient(應用模組.app) as 客戶端:
                回應 = 客戶端.post("/api/search/stream", json={"question": f"請補強這個網址：{網址}"})

        self.assertEqual(回應.status_code, 200)
        self.assertEqual(模擬深搜.await_args.kwargs["excluded_urls"], [網址])
        self.assertEqual(模擬深搜.await_args.kwargs["refresh_urls"], [])
        self.assertIn("只回答部分內容。", [區塊["text"] for 區塊 in 模擬深搜.await_args.kwargs["prior_evidence_chunks"]])
        結果 = self._讀取結果(回應.text)
        self.assertEqual(結果["research_strategy"], "hybrid")
        self.assertEqual([來源["url"] for 來源 in 結果["sources"]], [網址, "https://new.example/article"])

    def test_刷新來源只能由證據帳本的參考選出(self) -> None:
        膠囊 = [{
            "turn_id": "old-turn",
            "evidence": [
                {"title": "可刷新", "url": "https://example.com/old", "chunks": [{"chunk_id": "C1", "text": "舊資料"}]},
                {"title": "另一來源", "url": "https://example.com/other", "chunks": [{"chunk_id": "C2", "text": "另一份資料"}]},
            ],
        }]

        選取 = 選取可刷新來源(膠囊, ["unknown:S1", "old-turn:S2", "old-turn:S1"], max_sources=1)

        self.assertEqual(選取, [{"source_ref": "old-turn:S2", "title": "另一來源", "url": "https://example.com/other"}])

    def test_明確刷新帳本來源時會與首輪搜尋並行傳入管線(self) -> None:
        舊網址 = "https://example.com/evidence"
        膠囊 = 建立證據膠囊(
            self.儲存庫.取得本機密封器(),
            turn_id="old-turn",
            standalone_question="原始研究問題",
            queries=["one", "two", "three"],
            evidence_bundle=[
                {"title": "舊來源", "url": 舊網址, "chunks": [{"chunk_id": "C1", "text": "需要重新確認的舊資料"}]}
            ],
        )
        模擬深搜 = AsyncMock(return_value={
            "completion_state": "complete",
            "elapsed_ms": 11,
            "search_results_summary": {},
            "source_registry": [],
            "evidence_bundle": [{"title": "刷新後來源", "url": 舊網址, "chunks": [{"chunk_id": "L1-S1-C001", "text": "刷新後資料"}]}],
            "error": None,
        })

        async def 模擬回答(*_args, **_kwargs):
            yield "已重新確認來源"

        with (
            patch.object(應用模組, "取得設定儲存庫", return_value=self.儲存庫),
            patch.object(應用模組, "deep_search", new=模擬深搜),
            patch.object(應用模組, "產生搜尋字詞", new=AsyncMock(return_value={"standalone_question": "重新確認舊來源後再找補充", "strategy": "research", "queries": ["補充一", "補充二", "補充三"], "refresh_source_refs": ["old-turn:S1"]})),
            patch.object(應用模組, "串流產生引用回答", new=模擬回答),
        ):
            with TestClient(應用模組.app) as 客戶端:
                回應 = 客戶端.post("/api/search/stream", json={
                    "question": "請重新確認前一個來源，再找補充資料",
                    "context_capsules": [膠囊],
                })

        self.assertEqual(回應.status_code, 200)
        self.assertEqual(模擬深搜.await_args.kwargs["refresh_urls"], [舊網址])
        self.assertEqual(模擬深搜.await_args.kwargs["excluded_urls"], [])
        self.assertEqual(模擬深搜.await_args.kwargs["prior_evidence_chunks"], [])
        結果 = self._讀取結果(回應.text)
        self.assertEqual(結果["research_strategy"], "hybrid")


class 多輪規劃提示詞測試(unittest.IsolatedAsyncioTestCase):
    async def test_規劃可回傳追問路由而不增加模型呼叫(self) -> None:
        模擬模型 = AsyncMock(return_value='{"standalone_question":"獨立問題","strategy":"reuse","queries":["","",""]}')
        with patch.object(模型模組, "呼叫聊天模型", new=模擬模型):
            規劃 = await 模型模組.產生搜尋字詞(
                "把第二點整理成表格",
                {"provider": "test", "model": "test"},
                "zh-TW",
                對話歷史=[{"role": "user", "content": "前一題"}, {"role": "assistant", "content": "前一答"}],
                證據帳本=[{"question": "前一題", "selected_chunk_count": 2}],
                結構化規劃=True,
            )

        self.assertEqual(規劃, {"standalone_question": "獨立問題", "strategy": "reuse", "queries": []})
        self.assertEqual(模擬模型.await_count, 1)
