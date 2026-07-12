"""Chunk 編號、reviewer 解析與引用輸出的離線回歸測試。"""

from __future__ import annotations

import json
import unittest

import deep_search_tool as 搜尋管線


class Chunk測試(unittest.TestCase):
    """確保沿用 V3 的短段清理、去重、切分與完整 Judge 輸入。"""

    def test_Chunk編號採固定格式並將序號下限設為一(self) -> None:
        self.assertEqual(搜尋管線._chunk_id(2, 3, 4), "L2-S3-C004")
        self.assertEqual(搜尋管線._chunk_id(0, 0, 0), "L1-S1-C001")

    def test_短段會移除且重複段落只保留一次(self) -> None:
        有效段落 = "這是一段超過八十個字元、包含足夠上下文並可直接交給 Judge 判斷的完整證據內容。" * 3
        頁面 = {
            "url": "https://example.com/article",
            "title": "範例文章",
            "from_query": "範例查詢",
            "content": f"短導覽\n\n{有效段落}\n\n{有效段落}",
        }

        chunks = 搜尋管線._page_to_review_chunks(
            頁面,
            round_number=2,
            source_ordinal=3,
            question="可引用資訊",
        )

        self.assertEqual(
            [項目["chunk_id"] for 項目 in chunks],
            ["L2-S3-C001"],
        )
        self.assertEqual(chunks[0]["text"], 有效段落)
        self.assertTrue(all(項目["source_url"] == 頁面["url"] for 項目 in chunks))

    def test_超長段落按句界切開且沒有內容遺失(self) -> None:
        原文 = "這是一句完整且可驗證的內容。" * 80

        chunks = 搜尋管線._split_overlong_text(
            原文,
            max_chars=200,
        )

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(項目) <= 200 for 項目 in chunks))
        self.assertEqual("".join(chunks), 原文)

    def test_reviewer只接受存在的Chunk並去除重複ID(self) -> None:
        chunk_map = {
            "L1-S1-C001": {
                "chunk_id": "L1-S1-C001",
                "round": 1,
                "source_ref": "L1-S1",
                "source_url": "https://example.com/a",
                "title": "甲",
                "from_query": "查詢",
                "text": "甲內容",
            },
            "L1-S2-C001": {
                "chunk_id": "L1-S2-C001",
                "round": 1,
                "source_ref": "L1-S2",
                "source_url": "https://example.com/b",
                "title": "乙",
                "from_query": "查詢",
                "text": "乙內容",
            },
        }
        原始回覆 = json.dumps(
            {
                "selected_chunk_ids": [
                    "L1-S1-C001",
                    "不存在",
                    "L1-S1-C001",
                    "L1-S2-C001",
                ],
                "verdict": "足夠",
                "coverage": {"answered": ["甲"], "missing": []},
                "next_search_queries": ["一", "二", "三", "四"],
                "search_mode": "academic",
            },
            ensure_ascii=False,
        )

        結果 = 搜尋管線._parse_chunk_reviewer_response(
            原始回覆,
            chunk_map,
            default_search_mode="web",
        )

        self.assertEqual(
            結果["selected_chunk_ids"],
            ["L1-S1-C001", "L1-S2-C001"],
        )
        self.assertEqual(結果["verdict"], "sufficient")
        self.assertEqual(結果["next_search_queries"], ["一", "二", "三"])
        self.assertEqual(結果["search_mode"], "academic")

    def test_清理後所有Chunk都交給Reviewer(self) -> None:
        chunks = []
        for source in range(1, 5):
            for index in range(1, 101):
                chunks.append(
                    {
                        "chunk_id": f"L1-S{source}-C{index:03d}",
                        "source_ref": f"L1-S{source}",
                        "source_url": f"https://example.com/{source}",
                        "text": "證據" * 100,
                        "_score": float(index),
                    }
                )

        selected = 搜尋管線._select_chunks_for_reviewer_prompt(chunks)

        self.assertEqual(len(selected), len(chunks))
        self.assertEqual(selected, chunks)
        self.assertEqual({item["source_ref"] for item in selected}, {f"L1-S{i}" for i in range(1, 5)})

    def test_Reviewer來源Metadata每個來源只出現一次(self) -> None:
        chunks = [
            {
                "chunk_id": f"L1-S1-C{序號:03d}",
                "source_ref": "L1-S1",
                "source_url": "https://example.com/article",
                "title": "同一來源",
                "from_query": "測試查詢",
                "text": f"證據 {序號}",
            }
            for 序號 in range(1, 4)
        ]

        prompt = 搜尋管線._format_chunks_for_prompt(chunks)

        self.assertEqual(prompt.count("URL: https://example.com/article"), 1)
        self.assertEqual(prompt.count("From query: 測試查詢"), 1)
        self.assertTrue(all(項目["chunk_id"] in prompt for 項目 in chunks))


class 引用輸出測試(unittest.TestCase):
    """確保引用 URL 可安全開啟，且 evidence 只有 reviewer 選中的原文。"""

    def test_引用ID會清除追蹤參數並編碼空白(self) -> None:
        引用ID = 搜尋管線._citation_id_from_url(
            "https://Example.COM/a path/?utm_source=news&b=2"
        )

        self.assertEqual(引用ID, "//example.com/a%20path?b=2")

    def test_Evidence依來源分組並只輸出選中Chunk(self) -> None:
        原始頁面 = [
            {
                "url": "https://example.com/a",
                "title": "甲來源",
                "from_query": "甲查詢",
                "loop": 1,
                "content": "未篩選全文不可公開",
                "html": "<p>不可公開</p>",
                "debug": {"不可公開": True},
                "metrics": {"不可公開": True},
            },
            {
                "url": "https://example.com/b",
                "title": "未選來源",
                "content": "未選內容",
            },
        ]
        已選chunks = [
            {
                "chunk_id": "L1-S1-C002",
                "round": 1,
                "source_ref": "L1-S1",
                "source_url": "https://EXAMPLE.com/a/?utm_source=追蹤",
                "title": "甲來源",
                "from_query": "甲查詢",
                "text": "唯一允許公開的可引用原文。",
            }
        ]

        證據頁, 來源表, evidence, 平坦chunks = 搜尋管線._build_evidence_outputs(
            原始頁面,
            已選chunks,
        )

        self.assertEqual(len(證據頁), 1)
        self.assertNotIn("html", 證據頁[0])
        self.assertNotIn("debug", 證據頁[0])
        self.assertNotIn("metrics", 證據頁[0])
        self.assertNotIn("未篩選全文不可公開", 證據頁[0]["content"])
        self.assertIn("唯一允許公開的可引用原文。", 證據頁[0]["content"])

        self.assertEqual(來源表[0]["source_index"], 1)
        self.assertEqual(來源表[0]["citation_id"], "//example.com/a")
        self.assertEqual(
            來源表[0]["citation_marker"],
            "[citation](1://example.com/a)",
        )
        self.assertEqual(
            evidence,
            [
                {
                    "source_index": 1,
                    "title": "甲來源",
                    "url": "https://example.com/a",
                    "citation_id": "//example.com/a",
                    "citation_marker": "[citation](1://example.com/a)",
                    "chunks": [
                        {
                            "chunk_id": "L1-S1-C002",
                            "text": "唯一允許公開的可引用原文。",
                        }
                    ],
                }
            ],
        )
        self.assertEqual(平坦chunks[0]["source_index"], 1)
        self.assertEqual(
            平坦chunks[0]["citation_marker"],
            "[citation](1://example.com/a)",
        )


if __name__ == "__main__":
    unittest.main()
