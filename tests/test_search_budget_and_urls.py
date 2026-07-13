"""URL、搜尋配額與爬取預算的離線回歸測試。"""

from __future__ import annotations

import unittest

import deep_search_tool as 搜尋管線


class URL正規化測試(unittest.TestCase):
    """鎖定去追蹤參數、排序與去重所依賴的 canonical URL。"""

    def test_URL會補協定去片段並排序有效參數(self) -> None:
        實際 = 搜尋管線._normalize_url(
            "  example.com/path/?utm_source=news&b=2&a=1&fbclid=追蹤#段落  "
        )

        self.assertEqual(實際, "https://example.com/path?a=1&b=2")

    def test_網域轉小寫但保留路徑大小寫(self) -> None:
        實際 = 搜尋管線._normalize_url("https://EXAMPLE.COM/CasePath/")

        self.assertEqual(實際, "https://example.com/CasePath")

    def test_空URL維持空字串(self) -> None:
        self.assertEqual(搜尋管線._normalize_url("  "), "")

    def test_實際爬取URL保留參數順序與追蹤值(self) -> None:
        原始 = "https://Example.com/path?signature=z&utm_source=feed&a=1"

        self.assertEqual(搜尋管線._request_url_for_crawl(原始), 原始)
        self.assertEqual(
            搜尋管線._normalize_url(原始),
            "https://example.com/path?a=1&signature=z",
        )

    def test_直接網址會去重並移除訊息尾端標點(self) -> None:
        實際 = 搜尋管線.extract_explicit_urls(
            "請看（https://example.com/a?x=1）。以及 https://EXAMPLE.com/a?x=1 和 https://other.example/b)."
        )

        self.assertEqual(
            實際,
            {
                "urls": ["https://example.com/a?x=1", "https://other.example/b"],
                "overflow": False,
                "total": 2,
            },
        )

    def test_直接網址超過上限會回報而非靜默遺漏(self) -> None:
        網址 = " ".join(f"https://example.com/{索引}" for 索引 in range(6))

        實際 = 搜尋管線.extract_explicit_urls(網址)

        self.assertEqual(len(實際["urls"]), 5)
        self.assertTrue(實際["overflow"])
        self.assertEqual(實際["total"], 6)


class 搜尋配額測試(unittest.TestCase):
    """確保缺席來源的配額會盡量補到可用來源且不越過 API 上限。"""

    def test_配額跨越多輪分配直到全部用完(self) -> None:
        來源 = [
            {"api": "brave", "per_query": 18},
            {"api": "tavily", "per_query": 19},
            {"api": "exa", "per_query": 10},
        ]

        搜尋管線._redistribute_source_quota(來源, 10)

        self.assertEqual([項目["per_query"] for 項目 in 來源], [20, 20, 17])
        self.assertEqual(sum(項目["per_query"] for 項目 in 來源), 57)

    def test_所有來源抵達上限後不會超額(self) -> None:
        來源 = [
            {"api": "brave", "per_query": 20},
            {"api": "tavily", "per_query": 20},
            {"api": "serpapi_google_scholar", "per_query": 20},
        ]

        搜尋管線._redistribute_source_quota(來源, 100)

        self.assertEqual([項目["per_query"] for 項目 in 來源], [20, 20, 20])


class 爬取預算測試(unittest.TestCase):
    """鎖定 query 覆蓋、網域軟上限、URL 去重與總量硬上限。"""

    def test_預算優先涵蓋不同查詢組並遵守目標總量(self) -> None:
        候選 = [
            {"url": "https://example.com/a", "group_index": 0},
            {"url": "https://example.com/b", "group_index": 0},
            {"url": "https://example.com/c", "group_index": 0},
            {"url": "https://other.example/d", "group_index": 1},
            {"url": "https://third.example/e", "group_index": 2},
            {"url": "https://fourth.example/f", "group_index": 1},
        ]

        已選, 統計 = 搜尋管線._allocate_loop_crawl_budget(
            候選,
            min_total=3,
            target_total=4,
            max_total=5,
        )

        self.assertEqual(
            [項目["url"] for 項目 in 已選],
            [
                "https://example.com/a",
                "https://other.example/d",
                "https://third.example/e",
                "https://example.com/b",
            ],
        )
        self.assertEqual({項目["group_index"] for 項目 in 已選}, {0, 1, 2})
        self.assertEqual(統計["selected_count"], 4)
        self.assertEqual(統計["budget_trimmed"], 2)

    def test_追蹤參數不同的同頁只會佔一份預算(self) -> None:
        候選 = [
            {
                "url": "https://example.com/a?utm_source=one",
                "group_index": 0,
            },
            {"url": "https://example.com/a", "group_index": 1},
            {"url": "https://other.example/b", "group_index": 1},
        ]

        已選, 統計 = 搜尋管線._allocate_loop_crawl_budget(
            候選,
            min_total=1,
            target_total=3,
            max_total=3,
        )

        self.assertEqual(len(已選), 2)
        self.assertEqual(統計["selected_count"], 2)
        self.assertEqual(
            len({搜尋管線._normalize_url(項目["url"]) for 項目 in 已選}),
            2,
        )

    def test_三組候選都充足時仍保留每組深爬名額(self) -> None:
        候選 = [
            {"url": "https://first.example/a", "group_index": 0},
            {"url": "https://second.example/b", "group_index": 1},
            {"url": "https://first.example/c", "group_index": 0},
            {"url": "https://second.example/d", "group_index": 1},
            {"url": "https://third.example/e", "group_index": 2},
        ]

        已選, _ = 搜尋管線._allocate_loop_crawl_budget(
            候選,
            min_total=3,
            target_total=4,
            max_total=5,
        )

        self.assertEqual({項目["group_index"] for 項目 in 已選}, {0, 1, 2})

    def test_零硬上限會回傳可預期的空統計(self) -> None:
        已選, 統計 = 搜尋管線._allocate_loop_crawl_budget(
            [{"url": "https://example.com", "group_index": 0}],
            min_total=1,
            target_total=3,
            max_total=0,
        )

        self.assertEqual(已選, [])
        self.assertEqual(統計["selected_count"], 0)
        self.assertEqual(統計["budget_trimmed"], 1)
        self.assertEqual(統計["max_total"], 0)


if __name__ == "__main__":
    unittest.main()
