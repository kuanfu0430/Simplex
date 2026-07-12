"""輸入正規化與 MCP 公開契約的離線回歸測試。"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

import deep_search_tool as 搜尋管線
import pro_search_mcp as 搜尋入口


公開頂層欄位 = {
    "success",
    "completion_state",
    "mode",
    "question",
    "search_queries",
    "search_mode",
    "judge_success",
    "elapsed_ms",
    "search_results_summary",
    "citation_protocol",
    "answer_guidance",
    "source_registry",
    "evidence_bundle",
    "error",
}


def 整理入口輸入(**覆寫: Any) -> tuple[str, list[str]]:
    """補齊 MCP helper 的所有別名參數，讓案例只標示關注欄位。"""
    參數 = {
        "question": None,
        "search_queries": None,
        "queries": None,
        "search_query": None,
        "query": None,
        "query_1": None,
        "query_2": None,
        "query_3": None,
    }
    參數.update(覆寫)
    return 搜尋入口._coerce_mcp_inputs(**參數)


class 輸入正規化測試(unittest.TestCase):
    """鎖定搜尋模式、模型路線與三組查詢的容錯行為。"""

    def test_搜尋執行與模型模式正規化(self) -> None:
        self.assertEqual(搜尋管線.normalize_search_mode(" ACADEMIC "), "academic")
        self.assertEqual(搜尋管線.normalize_search_mode("未知模式"), "web")
        self.assertEqual(搜尋管線.normalize_execution_mode(" INSTANT "), "instant")
        self.assertEqual(搜尋管線.normalize_execution_mode("未知模式"), "fast")
        self.assertEqual(搜尋管線.normalize_model_route(" G "), "g")
        self.assertEqual(搜尋管線.normalize_model_route("未知路線"), "d")

    def test_JSON字串查詢會去重並補足三組(self) -> None:
        問題, 查詢 = 整理入口輸入(
            question="  原始問題  ",
            search_queries='["第一組", "第一組", "第二組"]',
        )

        self.assertEqual(問題, "原始問題")
        self.assertEqual(查詢, ["第一組", "第二組", "原始問題"])

    def test_編號查詢優先於其他別名(self) -> None:
        問題, 查詢 = 整理入口輸入(
            question="原始問題",
            search_queries=["不應採用"],
            queries="也不應採用",
            query_1="第一角度",
            query_2="第二角度",
        )

        self.assertEqual(問題, "原始問題")
        self.assertEqual(查詢, ["第一角度", "第二角度", "原始問題"])

    def test_換行清單與列表標記可被解析(self) -> None:
        _, 查詢 = 整理入口輸入(
            question="問題",
            search_query="1. 甲方向\n2. 乙方向\n3. 丙方向",
        )

        self.assertEqual(查詢, ["甲方向", "乙方向", "丙方向"])

    def test_核心輸入驗證會修剪文字並拒絕錯誤數量(self) -> None:
        問題, 查詢 = 搜尋管線.validate_search_inputs(
            "  問題  ",
            [" 甲 ", "乙", " 丙"],
            exact_query_count=3,
        )
        self.assertEqual((問題, 查詢), ("問題", ["甲", "乙", "丙"]))

        with self.assertRaisesRegex(ValueError, "3 組"):
            搜尋管線.validate_search_inputs(
                "問題",
                ["只有一組"],
                exact_query_count=3,
            )


class MCP公開契約測試(unittest.IsolatedAsyncioTestCase):
    """確保研究過程資料不會洩漏給外側回答模型。"""

    def test_公開payload只保留白名單與固定摘要欄位(self) -> None:
        evidence = [{"url": "https://example.com", "chunks": []}]
        payload = 搜尋入口._public_tool_payload(
            {
                "success": True,
                "completion_state": "complete",
                "mode": "fast",
                "question": "問題",
                "search_queries": ["甲", "乙", "丙"],
                "search_mode": "web",
                "judge_success": True,
                "elapsed_ms": 123,
                "search_results_summary": {
                    "total_found": 9,
                    "total_selected": 3,
                    "total_selected_raw": 7,
                    "內部統計": "不可公開",
                },
                "citation_protocol": {"format": "引用格式"},
                "answer_guidance": {"policy": "回答規則"},
                "source_registry": [{"source_index": 1}],
                "evidence_bundle": evidence,
                "query_profile": {"country": "TW"},
                "query_plans": [{"內部": True}],
                "content_map": {"內部": True},
                "selected_chunks": [{"內部": True}],
                "crawled_pages": [{"內部": True}],
                "failed_pages": [{"內部": True}],
                "research_trace": [{"內部": True}],
                "error": None,
            }
        )

        self.assertEqual(set(payload), 公開頂層欄位)
        self.assertEqual(
            set(payload["search_results_summary"]),
            set(搜尋入口.PUBLIC_SUMMARY_DEFAULTS),
        )
        self.assertEqual(payload["search_results_summary"]["total_found"], 9)
        self.assertEqual(payload["evidence_bundle"], evidence)
        for 禁止欄位 in (
            "query_profile",
            "query_plans",
            "content_map",
            "selected_chunks",
            "crawled_pages",
            "failed_pages",
            "research_trace",
        ):
            self.assertNotIn(禁止欄位, payload)

    def test_非字典核心結果仍回傳完整失敗契約(self) -> None:
        payload = 搜尋入口._public_tool_payload(None)  # type: ignore[arg-type]

        self.assertEqual(set(payload), 公開頂層欄位)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["evidence_bundle"], [])
        self.assertEqual(payload["source_registry"], [])
        self.assertIn("not a dict", payload["error"])

    async def test_MCP入口會正規化後再呼叫核心且輸出白名單(self) -> None:
        核心結果 = {
            "success": True,
            "completion_state": "complete",
            "mode": "instant",
            "question": "問題",
            "search_queries": ["甲", "乙", "問題"],
            "search_mode": "web",
            "judge_success": True,
            "elapsed_ms": 15,
            "search_results_summary": {"total_found": 3},
            "evidence_bundle": [],
            "query_profile": {"不可公開": True},
            "error": None,
        }
        模擬核心 = AsyncMock(return_value=核心結果)

        with patch.object(搜尋入口, "_deep_search", new=模擬核心):
            回傳文字 = await 搜尋入口.deep_search(
                question="  問題  ",
                search_queries="甲\n乙",
                search_mode="未知模式",
                mode=" INSTANT ",
                model=" G ",
            )

        呼叫參數 = 模擬核心.await_args.kwargs
        self.assertEqual(呼叫參數["question"], "問題")
        self.assertEqual(呼叫參數["search_queries"], ["甲", "乙", "問題"])
        self.assertEqual(呼叫參數["search_mode"], "web")
        self.assertEqual(呼叫參數["mode"], "instant")
        self.assertEqual(呼叫參數["model"], "g")

        payload = json.loads(回傳文字)
        self.assertEqual(set(payload), 公開頂層欄位)
        self.assertNotIn("query_profile", payload)

    async def test_空問題在入口即失敗且不呼叫核心(self) -> None:
        模擬核心 = AsyncMock()
        with patch.object(搜尋入口, "_deep_search", new=模擬核心):
            回傳文字 = await 搜尋入口.deep_search(
                question="   ",
                search_queries=["甲", "乙", "丙"],
            )

        模擬核心.assert_not_awaited()
        payload = json.loads(回傳文字)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["completion_state"], "failed")
        self.assertEqual(payload["evidence_bundle"], [])


if __name__ == "__main__":
    unittest.main()
