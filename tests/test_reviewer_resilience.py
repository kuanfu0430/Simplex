"""Reviewer 網路故障與請求隔離的離線回歸測試。"""

from __future__ import annotations

import asyncio
import json
import re
import unittest
from unittest.mock import AsyncMock, patch

import deep_search_tool as 搜尋管線


class Reviewer故障容錯測試(unittest.IsolatedAsyncioTestCase):
    """LLM 網路錯誤不得抹掉已取得的搜尋或深爬結果。"""

    async def test_URLReviewer請求失敗會改用原有前N筆策略(self) -> None:
        查詢組 = [
            {
                "query": "第一組",
                "results": [
                    {"title": f"標題 {i}", "url": f"https://a.example/{i}"}
                    for i in range(4)
                ],
            },
            {
                "query": "第二組",
                "results": [
                    {"title": f"標題 {i}", "url": f"https://b.example/{i}"}
                    for i in range(3)
                ],
            },
        ]
        失敗請求 = AsyncMock(side_effect=TimeoutError("模擬逾時"))

        with patch.object(搜尋管線, "_call_llm_raw_content", new=失敗請求):
            結果 = await 搜尋管線.llm_filter_results(
                "測試問題",
                查詢組,
                min_per_group=2,
                max_per_group=3,
                monitor=搜尋管線.PipelineMonitor(enabled=False),
            )

        self.assertEqual(失敗請求.await_count, 1)
        self.assertEqual(結果["total_selected"], 4)
        self.assertEqual(
            [(item["group_index"], item["result_index"]) for item in 結果["selected_urls"]],
            [(0, 0), (0, 1), (1, 0), (1, 1)],
        )

    def test_URLReviewer只限制Prompt而不修改完整搜尋結果(self) -> None:
        原始 = [{"query": "甲", "results": [{"title": str(i)} for i in range(30)]}]
        有界 = 搜尋管線._limit_filter_query_groups(原始, max_results_per_group=10)
        self.assertEqual(len(有界[0]["results"]), 10)
        self.assertEqual(len(原始[0]["results"]), 30)

    async def test_ChunkReviewer連續請求失敗仍回傳證據(self) -> None:
        失敗請求 = AsyncMock(side_effect=ConnectionError("模擬斷線"))
        頁面 = [
            {
                "url": "https://example.com/article",
                "title": "測試文章",
                "from_query": "測試查詢",
                "content": "這是可供 reviewer 選擇的完整證據句子。" * 30,
            }
        ]

        with patch.object(搜尋管線, "_call_llm_raw_content", new=失敗請求):
            結果 = await 搜尋管線.review_crawled_chunks(
                question="測試問題",
                search_queries=["測試查詢"],
                search_mode="web",
                pages=頁面,
                round_number=1,
                llm_config={},
                mon=搜尋管線.PipelineMonitor(enabled=False),
                final_round=True,
            )

        self.assertEqual(失敗請求.await_count, 搜尋管線.CHUNK_REVIEW_MAX_RETRIES)
        self.assertFalse(結果["success"])
        self.assertEqual(結果["verdict"], "insufficient")
        self.assertTrue(結果["selected_chunks"])
        self.assertEqual(結果["parse_retries"], 搜尋管線.CHUNK_REVIEW_MAX_RETRIES)

    async def test_Instant仍依序呼叫URL與ChunkJudge(self) -> None:
        async def 模擬搜尋(**_kwargs):
            groups = []
            for group_index in range(3):
                groups.append(
                    {
                        "query": f"查詢{group_index}",
                        "results": [
                            {
                                "title": f"來源 {group_index}-{index}",
                                "url": f"https://source{group_index}.example/{index}",
                                "content": "測試問題的直接證據",
                                "relevance_score": 10 - index,
                                "engine": "searxng",
                            }
                            for index in range(4)
                        ],
                    }
                )
            return {
                "query_groups": groups,
                "total_results": 12,
                "raw_total_results": 12,
                "query_profile": {},
            }

        async def 模擬爬取(*, urls, **_kwargs):
            return [
                {
                    "url": url,
                    "title": url,
                    "content": ("測試問題有可驗證的直接證據。" * 80),
                    "success": True,
                }
                for url in urls
            ]

        async def 模擬Judge(*, system_prompt, user_prompt, **_kwargs):
            if "搜尋結果審核專家" in system_prompt:
                return json.dumps(
                    {
                        "selected": [
                            {"group_index": 索引, "result_indices": [0], "reasoning": "測試"}
                            for 索引 in range(3)
                        ]
                    },
                    ensure_ascii=False,
                )
            chunk_ids = list(dict.fromkeys(re.findall(r"L\d+-S\d+-C\d{3}", user_prompt)))
            return json.dumps(
                {
                    "selected_chunk_ids": chunk_ids,
                    "verdict": "sufficient",
                    "gap_analysis": "已取得證據",
                    "coverage": {"answered": ["測試問題"], "missing": []},
                    "next_search_queries": [],
                    "search_mode": "web",
                },
                ensure_ascii=False,
            )

        外部Judge = AsyncMock(side_effect=模擬Judge)
        with (
            patch.object(搜尋管線, "multi_source_search", new=模擬搜尋),
            patch.object(搜尋管線, "batch_deep_crawl", new=模擬爬取),
            patch.object(搜尋管線, "_call_llm_raw_content", new=外部Judge),
        ):
            結果 = await 搜尋管線.deep_search(
                question="測試問題",
                search_queries=["查詢0", "查詢1", "查詢2"],
                mode="instant",
                verbose=False,
            )

        self.assertEqual(外部Judge.await_count, 2)
        self.assertEqual(結果["chunk_filter"]["model_stage"], "round_reviewer")
        self.assertGreaterEqual(len(結果["evidence_bundle"]), 2)


class Monitor請求隔離測試(unittest.IsolatedAsyncioTestCase):
    async def test_並行Task各自保留Monitor(self) -> None:
        async def 讀取當前狀態(enabled: bool) -> bool:
            monitor = 搜尋管線.PipelineMonitor(enabled=enabled)
            token = 搜尋管線._monitor_context.set(monitor)
            try:
                await asyncio.sleep(0)
                return 搜尋管線._current_monitor().enabled
            finally:
                搜尋管線._monitor_context.reset(token)

        self.assertEqual(
            await asyncio.gather(讀取當前狀態(True), 讀取當前狀態(False)),
            [True, False],
        )


if __name__ == "__main__":
    unittest.main()
