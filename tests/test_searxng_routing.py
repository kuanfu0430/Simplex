"""SearXNG 原生搜尋軌、每 query 上限與來源韌性的離線測試。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import deep_search_tool as 搜尋管線


def 僅啟用SearXNG設定() -> dict:
    return {
        "providers": {
            "searxng": {
                "enabled": True,
                "base_url": "http://127.0.0.1:18888",
            },
            "brave": {"enabled": False},
            "tavily": {"enabled": False},
            "exa": {"enabled": False},
            "serpapi": {"enabled": False},
        },
        "custom": [],
    }


class SearXNG結果上限測試(unittest.IsolatedAsyncioTestCase):
    async def test_第一頁超過三十筆時保留排序前三十筆(self) -> None:
        回應 = AsyncMock()
        回應.raise_for_status = lambda: None
        回應.json = lambda: {
            "results": [
                {
                    "title": f"結果 {序號}",
                    "url": f"https://example.com/{序號}",
                    "content": f"摘要 {序號}",
                    "engines": ["engine-a"],
                }
                for 序號 in range(37)
            ]
        }
        客戶端 = AsyncMock()
        客戶端.get.return_value = 回應

        with patch.object(
            搜尋管線,
            "_get_search_api_client",
            new=AsyncMock(return_value=客戶端),
        ):
            結果 = await 搜尋管線._searxng_search(
                "測試字詞",
                category="science",
                search_lane="academic",
                base_url="http://127.0.0.1:18888",
            )

        self.assertEqual(len(結果), 30)
        self.assertEqual([項目["title"] for 項目 in 結果], [f"結果 {序號}" for 序號 in range(30)])
        self.assertTrue(all(項目["search_lane"] == "academic" for 項目 in 結果))
        self.assertEqual(客戶端.get.await_args.kwargs["params"]["pageno"], 1)


class SearXNG搜尋軌測試(unittest.IsolatedAsyncioTestCase):
    async def test_學術模式每組同時走一般與學術搜尋軌(self) -> None:
        呼叫: list[tuple[str, str]] = []

        async def 模擬來源(api_name, query, per_query, params, **kwargs):
            呼叫.append((api_name, params.get("search_lane", "")))
            return [
                {
                    "title": f"{query}-{params.get('search_lane')}",
                    "url": f"https://example.com/{query}/{params.get('search_lane')}",
                    "content": "摘要",
                    "engine": "searxng",
                    "search_lane": params.get("search_lane"),
                }
            ]

        with patch.object(搜尋管線, "_call_api_source", new=模擬來源):
            結果 = await 搜尋管線.multi_source_search(
                ["甲", "乙", "丙"],
                search_mode="academic",
                search_provider_config=僅啟用SearXNG設定(),
            )

        self.assertEqual(len(呼叫), 6)
        self.assertEqual(呼叫.count(("searxng", "general")), 3)
        self.assertEqual(呼叫.count(("searxng", "academic")), 3)
        self.assertEqual(結果["raw_total_results"], 6)
        self.assertEqual(結果["total_results"], 6)

    async def test_社群模式保留一般搜尋量並另走社群搜尋軌(self) -> None:
        搜尋軌: list[str] = []

        async def 模擬來源(api_name, query, per_query, params, **kwargs):
            搜尋軌.append(params.get("search_lane", ""))
            return []

        with patch.object(搜尋管線, "_call_api_source", new=模擬來源):
            await 搜尋管線.multi_source_search(
                ["甲", "乙", "丙"],
                search_mode="social",
                search_provider_config=僅啟用SearXNG設定(),
            )

        self.assertEqual(搜尋軌.count("general"), 3)
        self.assertEqual(搜尋軌.count("social"), 3)


class 搜尋來源合併測試(unittest.TestCase):
    def test_相同網址去重但保留所有搜尋軌與引擎(self) -> None:
        結果 = 搜尋管線._merge_search_results(
            [
                {
                    "title": "一般結果",
                    "url": "https://example.com/a?utm_source=general",
                    "content": "摘要",
                    "engine": "searxng",
                    "source_engines": ["brave"],
                    "search_lane": "general",
                },
                {
                    "title": "學術結果",
                    "url": "https://example.com/a",
                    "content": "",
                    "engine": "searxng",
                    "source_engines": ["google scholar"],
                    "search_lane": "academic",
                },
            ]
        )

        self.assertEqual(len(結果), 1)
        self.assertEqual(結果[0]["search_lanes"], ["general", "academic"])
        self.assertEqual(
            結果[0]["source_engines"],
            ["brave", "searxng", "google scholar"],
        )


class 自定義搜尋服務測試(unittest.IsolatedAsyncioTestCase):
    async def test_自定義JSON服務會套用授權與欄位映射(self) -> None:
        回應 = AsyncMock()
        回應.raise_for_status = lambda: None
        回應.json = lambda: {
            "payload": {
                "items": [
                    {
                        "headline": "自定義結果",
                        "link": "https://custom.example/result",
                        "abstract": "可用摘要",
                    }
                ]
            }
        }
        客戶端 = AsyncMock()
        客戶端.get.return_value = 回應
        供應商 = {
            "id": "private-search",
            "base_url": "https://search.example/api",
            "method": "GET",
            "api_key": "測試密鑰",
            "auth_mode": "header",
            "auth_name": "X-Search-Key",
            "query_param": "query",
            "count_param": "limit",
            "result_path": "payload.items",
            "fields": {"title": "headline", "url": "link", "content": "abstract"},
        }

        with patch.object(
            搜尋管線,
            "_get_search_api_client",
            new=AsyncMock(return_value=客戶端),
        ):
            結果 = await 搜尋管線._custom_search("測試", 12, provider=供應商)

        self.assertEqual(結果[0]["title"], "自定義結果")
        self.assertEqual(結果[0]["engine"], "private-search")
        參數 = 客戶端.get.await_args.kwargs
        self.assertEqual(參數["params"], {"query": "測試", "limit": 12})
        self.assertEqual(參數["headers"]["X-Search-Key"], "測試密鑰")

    def test_自定義服務只加入指定搜尋模式(self) -> None:
        設定 = {
            "custom": [
                {
                    "id": "academic-only",
                    "enabled": True,
                    "base_url": "https://search.example/api",
                    "modes": ["academic"],
                    "per_query": 25,
                }
            ]
        }

        self.assertEqual(搜尋管線._custom_search_sources(設定, "web", None), [])
        學術來源 = 搜尋管線._custom_search_sources(設定, "academic", None)
        self.assertEqual(len(學術來源), 1)
        self.assertEqual(學術來源[0]["per_query"], 25)


if __name__ == "__main__":
    unittest.main()
