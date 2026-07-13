"""Simplex 設定安全性與 Web 研究串流測試。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import simplex_app.main as 應用模組
import simplex_app.llm as 模型模組
import deep_search_tool as 搜尋管線
from simplex_app.settings import 設定儲存庫


class 設定儲存安全測試(unittest.TestCase):
    def test_搜尋引擎預設為原生SearXNG(self) -> None:
        with tempfile.TemporaryDirectory() as 暫存:
            根 = Path(暫存)
            儲存庫 = 設定儲存庫(根 / "settings.db", 根 / "settings.key")
            設定 = 儲存庫.讀取()

        self.assertEqual(設定["search"]["engine_mode"], "searxng")
        self.assertTrue(設定["search"]["providers"]["searxng"]["enabled"])

    def test_介面語言預設為英文(self) -> None:
        with tempfile.TemporaryDirectory() as 暫存:
            根 = Path(暫存)
            儲存庫 = 設定儲存庫(根 / "settings.db", 根 / "settings.key")
            self.assertEqual(儲存庫.讀取()["ui"]["language"], "en")

    def test_模型池預設為空清單並可供搜尋模型選取(self) -> None:
        with tempfile.TemporaryDirectory() as 暫存:
            根 = Path(暫存)
            儲存庫 = 設定儲存庫(根 / "settings.db", 根 / "settings.key")
            設定 = 儲存庫.讀取()
            self.assertEqual(設定["llm"]["model_pool"], [])
            設定["llm"]["question_model"] = {"provider_id": "openrouter", "model": "default-model"}
            設定["llm"]["model_pool"] = [
                {"provider_id": "openai", "model": "pool-model", "name": "Pool model"}
            ]

        選取 = 模型模組.解析搜尋模型設定(
            設定,
            {"provider_id": "openai", "model": "pool-model"},
        )
        self.assertEqual(選取["provider"], "openai")
        self.assertEqual(選取["model"], "pool-model")
        with self.assertRaisesRegex(ValueError, "模型池"):
            模型模組.解析搜尋模型設定(
                設定,
                {"provider_id": "openai", "model": "not-in-pool"},
            )

    def test_回答提示詞依介面語言切換(self) -> None:
        繁中提示詞 = 模型模組._引用回答系統提示詞("zh-TW")
        英文提示詞 = 模型模組._引用回答系統提示詞("en")

        self.assertIn("只能閱讀 evidence", 繁中提示詞)
        self.assertIn("使用用戶原始問題的語言", 繁中提示詞)
        self.assertIn("Write the final answer in the language of the user's original question", 英文提示詞)
        self.assertIn(模型模組.回答補充系統提示詞, 繁中提示詞)
        self.assertIn(模型模組.回答補充系統提示詞, 英文提示詞)

    def test_Web未選Judge時不會回退使用舊Env模型(self) -> None:
        設定 = 搜尋管線._build_llm_route_config("d", None, {})
        self.assertEqual(設定["transport"], "disabled")
        self.assertEqual(設定["model"], "")

    def test_WebJudge固定關閉推理(self) -> None:
        設定 = 搜尋管線._build_llm_route_config(
            "d",
            None,
            {
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://openrouter.ai/api/v1",
                "reasoning": {"effort": "high"},
            },
        )

        self.assertEqual(設定["reasoning"], {"effort": "none"})

    def test_一般ChatCompletions的Judge使用標準停用欄位(self) -> None:
        設定 = 搜尋管線._build_llm_route_config(
            "d",
            None,
            {
                "provider": "openai",
                "model": "gpt-test",
                "base_url": "https://api.openai.com/v1",
            },
        )

        self.assertIsNone(設定["reasoning"])
        self.assertEqual(設定["reasoning_effort"], "none")

    def test_問答推理欄位依供應商格式切換(self) -> None:
        self.assertEqual(
            模型模組._預設推理請求欄位(
                {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1"}
            ),
            {"reasoning": {"enabled": True}},
        )
        self.assertEqual(
            模型模組._預設推理請求欄位(
                {"provider": "openai", "base_url": "https://api.openai.com/v1"}
            ),
            {"reasoning_effort": "medium"},
        )

    def test_Citation規則只存在最終回答提示詞(self) -> None:
        提示詞 = 模型模組._引用回答系統提示詞("zh-TW")

        self.assertIn("只能閱讀 evidence", 提示詞)
        self.assertIn("緊貼", 提示詞)
        self.assertIn("多個 citation_marker", 提示詞)
        self.assertNotIn("citation_marker", "你是高速研究查詢規劃器")

    def test_查詢規劃提示詞保留新鮮度與問題導向語言策略(self) -> None:
        self.assertIn(
            "在面對有時效性的問題時一律依用戶要求的新鮮度搜尋最新日期之資訊。",
            模型模組.搜尋規劃補充系統提示詞,
        )
        self.assertIn(
            "若無特別指向性應以英文優先或混合使用以確保資料多樣性",
            模型模組.搜尋規劃補充系統提示詞,
        )
        self.assertIn("哪個日本明星最受日本人喜愛", 模型模組.搜尋規劃補充系統提示詞)
        self.assertIn("史普尼克危機是如何產生的", 模型模組.搜尋規劃補充系統提示詞)

    def test_URL與ChunkJudge都保留開放性觀點規則(self) -> None:
        url_judge_prompt = 搜尋管線._build_filter_system_prompt("web", 1, 3)
        chunk_judge_prompt = 搜尋管線._build_chunk_reviewer_system_prompt()
        規則 = "你應該保持開放性的態度看待問題，不偏好任何觀點，且應盡力維持論述的多元性，若你發現搜尋資料存在相互矛盾的不同觀點，應將各種觀點予以保留，不得擅自進行判斷與挑選。"

        self.assertIn(規則, url_judge_prompt)
        self.assertIn(規則, chunk_judge_prompt)

    def test_APIKey加密保存且公開設定只回傳狀態(self) -> None:
        with tempfile.TemporaryDirectory() as 暫存:
            根 = Path(暫存)
            儲存庫 = 設定儲存庫(根 / "settings.db", 根 / "settings.key")
            設定 = 儲存庫.讀取()
            設定["llm"]["providers"][0]["api_key"] = "高度敏感密鑰"
            儲存庫.儲存(設定)

            資料庫位元 = (根 / "settings.db").read_bytes()
            self.assertNotIn("高度敏感密鑰".encode("utf-8"), 資料庫位元)
            公開 = 儲存庫.公開設定(儲存庫.讀取())
            self.assertEqual(公開["llm"]["providers"][0]["api_key"], "")
            self.assertTrue(公開["llm"]["providers"][0]["has_api_key"])

    def test_公開表單留空會保留既有密鑰(self) -> None:
        with tempfile.TemporaryDirectory() as 暫存:
            根 = Path(暫存)
            儲存庫 = 設定儲存庫(根 / "settings.db", 根 / "settings.key")
            設定 = 儲存庫.讀取()
            設定["search"]["providers"]["tavily"]["api_key"] = "保留的密鑰"
            儲存庫.儲存(設定)
            公開 = 儲存庫.公開設定(儲存庫.讀取())
            儲存庫.儲存(公開)

            self.assertEqual(
                儲存庫.讀取()["search"]["providers"]["tavily"]["api_key"],
                "保留的密鑰",
            )


class Prompt組裝測試(unittest.IsolatedAsyncioTestCase):
    async def test_查詢規劃模型實際收到完整新增規則(self) -> None:
        模擬模型 = AsyncMock(return_value='["query 1", "query 2", "query 3"]')

        with patch.object(模型模組, "呼叫聊天模型", new=模擬模型):
            查詢 = await 模型模組.產生搜尋字詞(
                "哪個日本明星最受日本人喜愛？",
                {"provider": "test", "model": "test"},
                "zh-TW",
            )

        self.assertEqual(查詢, ["query 1", "query 2", "query 3"])
        系統提示詞 = 模擬模型.call_args.args[1][0]["content"]
        self.assertIn(模型模組.搜尋規劃補充系統提示詞, 系統提示詞)
        self.assertNotIn("查詢必須使用繁體中文", 系統提示詞)


class SimplexAPI測試(unittest.TestCase):
    def setUp(self) -> None:
        self.暫存 = tempfile.TemporaryDirectory()
        根 = Path(self.暫存.name)
        self.儲存庫 = 設定儲存庫(根 / "settings.db", 根 / "settings.key")
        self.儲存庫.讀取()["search"]["providers"]["searxng"]["enabled"] = False

    def tearDown(self) -> None:
        self.暫存.cleanup()

    def test_設定API拒絕只放大字體的異常比例(self) -> None:
        with patch.object(應用模組, "取得設定儲存庫", return_value=self.儲存庫):
            with TestClient(應用模組.app) as 客戶端:
                設定 = 客戶端.get("/api/settings").json()
                設定["ui"]["scale"] = 2
                回應 = 客戶端.put("/api/settings", json={"settings": 設定})

        self.assertEqual(回應.status_code, 422)
        self.assertIn("0.8", 回應.json()["detail"])

    def test_設定API可以保存繁體中文(self) -> None:
        with patch.object(應用模組, "取得設定儲存庫", return_value=self.儲存庫):
            with TestClient(應用模組.app) as 客戶端:
                設定 = 客戶端.get("/api/settings").json()
                設定["ui"]["language"] = "zh-TW"
                回應 = 客戶端.put("/api/settings", json={"settings": 設定})

        self.assertEqual(回應.status_code, 200)
        self.assertEqual(回應.json()["ui"]["language"], "zh-TW")
        self.assertEqual(self.儲存庫.讀取()["ui"]["language"], "zh-TW")

    def test_設定API拒絕未知搜尋引擎模式(self) -> None:
        with patch.object(應用模組, "取得設定儲存庫", return_value=self.儲存庫):
            with TestClient(應用模組.app) as 客戶端:
                設定 = 客戶端.get("/api/settings").json()
                設定["search"]["engine_mode"] = "mixed"
                回應 = 客戶端.put("/api/settings", json={"settings": 設定})

        self.assertEqual(回應.status_code, 422)
        self.assertIn("searxng", 回應.json()["detail"])

    def test_前端入口與ServiceWorker要求重新驗證快取(self) -> None:
        資產檔案 = next((應用模組.前端目錄 / "assets").glob("index-*.js"))
        with TestClient(應用模組.app) as 客戶端:
            入口 = 客戶端.get("/")
            工作者 = 客戶端.get("/service-worker.js")
            資產 = 客戶端.get(f"/assets/{資產檔案.name}")

        self.assertEqual(入口.status_code, 200)
        self.assertEqual(工作者.status_code, 200)
        self.assertIn("no-cache", 入口.headers.get("cache-control", ""))
        self.assertIn("no-store", 工作者.headers.get("cache-control", ""))
        self.assertIn("simplex-v3", 工作者.text)
        self.assertEqual(資產.status_code, 200)
        self.assertIn("原生 SearXNG", 資產.text)

    def test_未知API不會回傳前端首頁(self) -> None:
        with TestClient(應用模組.app) as 客戶端:
            回應 = 客戶端.get("/api/search-services")

        self.assertEqual(回應.status_code, 404)
        self.assertEqual(回應.headers["content-type"], "application/json")
        self.assertIn("找不到 API 路徑", 回應.json()["detail"])

    def test_後端依模式互斥啟用原生或自有搜尋引擎(self) -> None:
        設定 = self.儲存庫.讀取()
        設定["search"]["providers"]["tavily"].update(
            {"enabled": True, "api_key": "測試密鑰"}
        )
        設定["search"]["custom"] = [
            {
                "id": "custom-one",
                "name": "自有引擎",
                "enabled": True,
                "base_url": "https://search.example/api",
                "api_key": "",
            }
        ]

        原生 = 應用模組._有效搜尋設定(設定)
        self.assertTrue(原生["providers"]["searxng"]["enabled"])
        self.assertFalse(原生["providers"]["tavily"]["enabled"])
        self.assertFalse(原生["custom"][0]["enabled"])

        設定["search"]["engine_mode"] = "custom"
        自有 = 應用模組._有效搜尋設定(設定)
        self.assertFalse(自有["providers"]["searxng"]["enabled"])
        self.assertTrue(自有["providers"]["tavily"]["enabled"])
        self.assertTrue(自有["custom"][0]["enabled"])

    def test_拒絕非本機Host與跨站Origin(self) -> None:
        with patch.object(應用模組, "取得設定儲存庫", return_value=self.儲存庫):
            with TestClient(應用模組.app) as 客戶端:
                錯誤Host = 客戶端.get(
                    "/api/settings",
                    headers={"host": "attacker.example"},
                )
                跨站來源 = 客戶端.get(
                    "/api/settings",
                    headers={"origin": "https://attacker.example"},
                )
                本機來源 = 客戶端.get(
                    "/api/settings",
                    headers={"origin": "http://127.0.0.1:8787"},
                )

        self.assertEqual(錯誤Host.status_code, 400)
        self.assertEqual(跨站來源.status_code, 403)
        self.assertEqual(本機來源.status_code, 200)

    def test_研究串流依序回報狀態結果與完成(self) -> None:
        模擬結果 = {
            "completion_state": "complete",
            "elapsed_ms": 12,
            "search_results_summary": {"total_found": 37},
            "source_registry": [{"source_index": 1, "url": "https://example.com"}],
            "evidence_bundle": [
                {
                    "title": "來源",
                    "url": "https://example.com",
                    "citation_marker": "[citation](1://example.com)",
                    "chunks": [{"chunk_id": "L1-S1-C1", "text": "證據"}],
                }
            ],
            "error": None,
        }
        模擬深搜 = AsyncMock(return_value=模擬結果)
        async def 模擬回答串流(*_args, **_kwargs):
            yield "答"
            yield "案"
        with (
            patch.object(應用模組, "取得設定儲存庫", return_value=self.儲存庫),
            patch.object(應用模組, "deep_search", new=模擬深搜),
            patch.object(應用模組, "產生搜尋字詞", new=AsyncMock(return_value=["甲", "乙", "丙"])),
            patch.object(應用模組, "串流產生引用回答", new=模擬回答串流),
        ):
            with TestClient(應用模組.app) as 客戶端:
                回應 = 客戶端.post(
                    "/api/search/stream",
                    json={"question": "問題", "search_mode": "academic", "mode": "fast"},
                )

        self.assertEqual(回應.status_code, 200)
        文字 = 回應.text
        self.assertIn("event: status", 文字)
        self.assertIn("event: answer_start", 文字)
        self.assertEqual(文字.count("event: answer_delta"), 2)
        self.assertIn("event: result", 文字)
        self.assertIn("event: done", 文字)
        結果區塊 = next(
            區塊 for 區塊 in 文字.split("\n\n") if 區塊.startswith("event: result")
        )
        資料 = json.loads(next(行[5:].strip() for 行 in 結果區塊.splitlines() if 行.startswith("data:")))
        self.assertEqual(資料["answer"], "答案")
        self.assertEqual(資料["search_queries"], ["甲", "乙", "丙"])
        搜尋設定 = 模擬深搜.await_args.kwargs["search_provider_config"]
        self.assertTrue(搜尋設定["providers"]["searxng"]["enabled"])
        self.assertFalse(搜尋設定["providers"]["tavily"]["enabled"])

    def test_研究串流即時轉送研究軌跡事件(self) -> None:
        模擬結果 = {
            "completion_state": "complete",
            "elapsed_ms": 12,
            "search_results_summary": {"total_found": 3},
            "source_registry": [{"source_index": 1, "url": "https://example.com", "title": "Example"}],
            "evidence_bundle": [],
            "error": None,
        }

        async def 模擬深搜(**參數):
            回報 = 參數["progress_callback"]
            回報({"type": "search_results", "stage": "url_judge", "queries": [{"query": "甲", "sources": [{"title": "Example", "url": "https://example.com"}]}]})
            回報({"type": "url_selection", "stage": "crawling", "queries": [{"query": "甲", "sources": [{"title": "Example", "url": "https://example.com"}]}]})
            回報({"type": "final_evidence", "stage": "chunk_judge", "round": 1, "chunks": [{"chunk_id": "L1-S1-C1", "title": "Example", "source_url": "https://example.com", "from_query": "甲", "preview": "證據"}]})
            return 模擬結果

        async def 模擬回答串流(*_args, **_kwargs):
            yield "逐"
            yield "段"

        with (
            patch.object(應用模組, "取得設定儲存庫", return_value=self.儲存庫),
            patch.object(應用模組, "deep_search", new=模擬深搜),
            patch.object(應用模組, "產生搜尋字詞", new=AsyncMock(return_value=["甲", "乙", "丙"])),
            patch.object(應用模組, "串流產生引用回答", new=模擬回答串流),
        ):
            with TestClient(應用模組.app) as 客戶端:
                文字 = 客戶端.post("/api/search/stream", json={"question": "問題"}).text

        軌跡 = [
            json.loads(next(行[5:].strip() for 行 in 區塊.splitlines() if 行.startswith("data:")))
            for 區塊 in 文字.split("\n\n")
            if 區塊.startswith("event: research_trace")
        ]
        self.assertEqual(軌跡[0]["type"], "plan")
        self.assertEqual([項目["query"] for 項目 in 軌跡[0]["queries"]], ["甲", "乙", "丙"])
        self.assertEqual([項目["type"] for 項目 in 軌跡[1:4]], ["search_results", "url_selection", "final_evidence"])
        self.assertNotIn("judge_selection", [項目["type"] for 項目 in 軌跡])
        self.assertEqual(軌跡[-1], {"type": "stage", "stage": "complete"})

    def test_OpenAI串流片段支援文字與內容區塊(self) -> None:
        self.assertEqual(
            模型模組._串流文字片段({"choices": [{"delta": {"content": "文字"}}]}),
            "文字",
        )
        self.assertEqual(
            模型模組._串流文字片段(
                {"choices": [{"delta": {"content": [{"text": "甲"}, {"text": "乙"}]}}]}
            ),
            "甲乙",
        )

    def test_回答首字前失敗的降級文字仍有首字計時(self) -> None:
        模擬結果 = {
            "completion_state": "complete",
            "elapsed_ms": 10,
            "search_results_summary": {},
            "source_registry": [],
            "evidence_bundle": [],
            "error": None,
        }

        async def 失敗串流(*_args, **_kwargs):
            if False:
                yield ""
            raise RuntimeError("模擬首字前失敗")

        with (
            patch.object(應用模組, "取得設定儲存庫", return_value=self.儲存庫),
            patch.object(應用模組, "deep_search", new=AsyncMock(return_value=模擬結果)),
            patch.object(應用模組, "產生搜尋字詞", new=AsyncMock(return_value=["甲", "乙", "丙"])),
            patch.object(應用模組, "串流產生引用回答", new=失敗串流),
        ):
            with TestClient(應用模組.app) as 客戶端:
                文字 = 客戶端.post(
                    "/api/search/stream",
                    json={"question": "問題", "mode": "instant"},
                ).text

        結果區塊 = next(
            區塊 for 區塊 in 文字.split("\n\n") if 區塊.startswith("event: result")
        )
        資料 = json.loads(next(行[5:].strip() for 行 in 結果區塊.splitlines() if 行.startswith("data:")))
        self.assertIsNotNone(資料["timings"]["answer_first_token_ms"])
        self.assertIn("event: warning", 文字)


class LLM串流終止測試(unittest.IsolatedAsyncioTestCase):
    async def test_問答模型依供應商送出推理請求欄位(self) -> None:
        原始客戶端 = httpx.AsyncClient
        請求內容: list[dict[str, object]] = []

        def 處理請求(請求: httpx.Request) -> httpx.Response:
            請求內容.append(json.loads(請求.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "完成"}}]})

        transport = httpx.MockTransport(處理請求)

        def 建立客戶端(*_args, **_kwargs):
            return 原始客戶端(transport=transport)

        with patch.object(模型模組.httpx, "AsyncClient", side_effect=建立客戶端):
            await 模型模組.呼叫聊天模型(
                {
                    "provider": "openrouter",
                    "base_url": "https://openrouter.ai/api/v1",
                    "chat_endpoint": "/chat/completions",
                    "api_key": "test-key",
                    "model": "test-model",
                },
                [{"role": "user", "content": "問題"}],
                啟用推理=True,
            )
            await 模型模組.呼叫聊天模型(
                {
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "chat_endpoint": "/chat/completions",
                    "api_key": "test-key",
                    "model": "test-model",
                },
                [{"role": "user", "content": "問題"}],
                啟用推理=True,
            )

        self.assertEqual(請求內容[0]["reasoning"], {"enabled": True})
        self.assertNotIn("reasoning_effort", 請求內容[0])
        self.assertEqual(請求內容[1]["reasoning_effort"], "medium")
        self.assertNotIn("reasoning", 請求內容[1])

    async def _收集(self, body: str) -> list[str]:
        原始客戶端 = httpx.AsyncClient
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                text=body,
                headers={"content-type": "text/event-stream"},
            )
        )

        def 建立客戶端(*_args, **_kwargs):
            return 原始客戶端(transport=transport)

        chunks: list[str] = []
        with patch.object(模型模組.httpx, "AsyncClient", side_effect=建立客戶端):
            async for chunk in 模型模組.串流聊天模型(
                {
                    "base_url": "https://provider.example/v1",
                    "chat_endpoint": "/chat/completions",
                    "api_key": "test-key",
                    "model": "test-model",
                },
                [{"role": "user", "content": "問題"}],
            ):
                chunks.append(chunk)
        return chunks

    async def test_部分Token後ErrorPayload必須拋錯(self) -> None:
        body = (
            'data: {"choices":[{"delta":{"content":"半句"}}]}\n\n'
            'data: {"error":{"message":"quota"}}\n\n'
        )
        with self.assertRaisesRegex(RuntimeError, "quota"):
            await self._收集(body)

    async def test_部分Token後EOF但無完成訊號必須拋錯(self) -> None:
        body = 'data: {"choices":[{"delta":{"content":"半句"}}]}\n\n'
        with self.assertRaisesRegex(RuntimeError, "完成訊號前中斷"):
            await self._收集(body)

    async def test_DONE完成訊號可正常結束(self) -> None:
        body = (
            'data: {"choices":[{"delta":{"content":"完整"}}]}\n\n'
            'data: [DONE]\n\n'
        )
        self.assertEqual(await self._收集(body), ["完整"])


if __name__ == "__main__":
    unittest.main()
