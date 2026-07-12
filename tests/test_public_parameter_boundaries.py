"""deep_search 公開可選參數的離線邊界契約測試。"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

import deep_search_tool as 搜尋管線


def 空搜尋迴圈結果() -> dict[str, Any]:
    """建立不會觸發後續 reviewer 的離線搜尋結果。"""
    return {
        "success": False,
        "error": "離線測試不應真正執行搜尋",
        "pages": [],
        "failed": [],
        "total_found": 0,
        "total_selected": 0,
        "total_selected_raw": 0,
        "total_deduped_before_crawl": 0,
        "total_budget_trimmed_before_crawl": 0,
        "total_crawl_attempted": 0,
        "query_profile": None,
        "query_plans": [],
    }


class 公開參數邊界測試(unittest.IsolatedAsyncioTestCase):
    """非法邊界應在任何搜尋、LLM 或爬取工作開始前回傳固定失敗契約。"""

    基本參數 = {
        "question": "離線參數測試問題",
        "search_queries": ["第一組", "第二組", "第三組"],
        "search_mode": "web",
        "mode": "fast",
        "model": "d",
        "verbose": False,
    }

    async def 斷言參數被入口拒絕(
        self,
        *,
        參數名稱: str,
        **覆寫: Any,
    ) -> dict[str, Any]:
        """驗證錯誤為結構化回傳，且不會進入搜尋迴圈。"""
        呼叫參數 = dict(self.基本參數)
        呼叫參數.update(覆寫)
        模擬搜尋迴圈 = AsyncMock(return_value=空搜尋迴圈結果())

        with patch.object(
            搜尋管線,
            "_run_search_loop",
            new=模擬搜尋迴圈,
        ):
            結果 = await 搜尋管線.deep_search(**呼叫參數)

        模擬搜尋迴圈.assert_not_awaited()
        self.assertFalse(結果["success"])
        self.assertEqual(結果["completion_state"], "failed")
        self.assertEqual(結果["judge_success"], None)
        self.assertEqual(結果["judge_result"]["error"], "invalid_input")
        self.assertEqual(結果["evidence_bundle"], [])
        self.assertEqual(結果["source_registry"], [])
        self.assertIn(參數名稱, 結果["error"])
        return 結果

    async def test_零爬取併發數會在入口被拒絕(self) -> None:
        await self.斷言參數被入口拒絕(
            參數名稱="crawl_concurrency",
            crawl_concurrency=0,
        )

    async def test_負數每頁字元上限會在入口被拒絕(self) -> None:
        await self.斷言參數被入口拒絕(
            參數名稱="max_chars_per_page",
            max_chars_per_page=-1,
        )

    async def test_負數每組結果數會在入口被拒絕(self) -> None:
        await self.斷言參數被入口拒絕(
            參數名稱="results_per_query",
            results_per_query=-1,
        )

    async def test_零每組結果數會在入口被拒絕(self) -> None:
        await self.斷言參數被入口拒絕(
            參數名稱="results_per_query",
            results_per_query=0,
        )

    async def test_選擇數上下限反轉會在入口被拒絕(self) -> None:
        結果 = await self.斷言參數被入口拒絕(
            參數名稱="min_select_per_group",
            min_select_per_group=8,
            max_select_per_group=3,
        )
        self.assertIn("max_select_per_group", 結果["error"])

    async def test_非正整數選擇上限會在入口被拒絕(self) -> None:
        await self.斷言參數被入口拒絕(
            參數名稱="max_select_per_group",
            max_select_per_group=0,
        )

    async def test_非法渲染策略會在入口被拒絕(self) -> None:
        await self.斷言參數被入口拒絕(
            參數名稱="render",
            render="sometimes",
        )

    async def test_數值參數不接受布林值冒充整數(self) -> None:
        await self.斷言參數被入口拒絕(
            參數名稱="crawl_concurrency",
            crawl_concurrency=False,
        )

    async def test_數值參數不接受小數(self) -> None:
        await self.斷言參數被入口拒絕(
            參數名稱="max_chars_per_page",
            max_chars_per_page=100.5,
        )

    async def test_模型覆寫參數不接受非字串(self) -> None:
        await self.斷言參數被入口拒絕(
            參數名稱="filter_model",
            filter_model=123,
        )

    async def test_搜尋模式不接受非字串(self) -> None:
        await self.斷言參數被入口拒絕(
            參數名稱="search_mode",
            search_mode=123,
        )

    async def test_合法渲染策略會忽略大小寫與周圍空白(self) -> None:
        模擬搜尋迴圈 = AsyncMock(return_value=空搜尋迴圈結果())
        呼叫參數 = dict(self.基本參數)
        呼叫參數["render"] = "  NEVER  "

        with patch.object(
            搜尋管線,
            "_run_search_loop",
            new=模擬搜尋迴圈,
        ):
            await 搜尋管線.deep_search(**呼叫參數)

        模擬搜尋迴圈.assert_awaited_once()
        self.assertEqual(模擬搜尋迴圈.await_args.kwargs["render"], "never")

    async def test_合法數值參數原樣傳入搜尋迴圈(self) -> None:
        模擬搜尋迴圈 = AsyncMock(return_value=空搜尋迴圈結果())
        呼叫參數 = dict(self.基本參數)
        呼叫參數.update(
            {
                "results_per_query": 7,
                "min_select_per_group": 2,
                "max_select_per_group": 5,
                "max_chars_per_page": 12345,
                "crawl_concurrency": 3,
                "render": "never",
            }
        )

        with patch.object(
            搜尋管線,
            "_run_search_loop",
            new=模擬搜尋迴圈,
        ):
            await 搜尋管線.deep_search(**呼叫參數)

        傳入 = 模擬搜尋迴圈.await_args.kwargs
        self.assertEqual(傳入["results_per_query"], 7)
        self.assertEqual(傳入["min_select_per_group"], 2)
        self.assertEqual(傳入["max_select_per_group"], 5)
        self.assertEqual(傳入["max_chars_per_page"], 12345)
        self.assertEqual(傳入["crawl_concurrency"], 3)
        self.assertEqual(傳入["render"], "never")


if __name__ == "__main__":
    unittest.main()
