"""HTML 抽取、批次隔離與共用資源的離線回歸測試。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import deep_search_tool as 搜尋管線
import pro_search_crawl_backend as 爬取核心


class HTML抽取模式測試(unittest.TestCase):
    """鎖定 strict 優先、general 補救及正文去殼行為。"""

    def test_Strict會從遠端Markdown標題起點裁切而General保留前文(self) -> None:
        前文 = "這是一段足夠長的前置說明，刻意超過標題搜尋的最小偏移。" * 8
        原文 = f"{前文}\n\n# 真正正文標題\n\n這是正文內容，應該被保留下來。"

        strict = 爬取核心._run_postprocess_pipeline(
            原文,
            extraction_mode=爬取核心.EXTRACTION_MODE_STRICT,
        )
        general = 爬取核心._run_postprocess_pipeline(
            原文,
            extraction_mode=爬取核心.EXTRACTION_MODE_GENERAL,
        )

        self.assertTrue(strict.text.startswith("# 真正正文標題"))
        self.assertNotIn(前文[:30], strict.text)
        self.assertIn(前文[:30], general.text)
        self.assertIn(
            "trim_to_heading_anchor",
            [步驟["step"] for 步驟 in strict.steps],
        )
        self.assertNotIn(
            "trim_to_heading_anchor",
            [步驟["step"] for 步驟 in general.steps],
        )

    def test_HTML抽取保留Article正文並移除導覽與頁尾(self) -> None:
        正文 = "這是文章核心內容，包含足夠長度與完整句子，可供離線抽取品質測試。" * 20
        html = f"""
        <html lang="zh-TW">
          <head><title>離線 HTML 抽取測試文章標題</title></head>
          <body>
            <nav>首頁 登入 訂閱 選單 不應進入正文</nav>
            <article>
              <h1>離線 HTML 抽取測試文章標題</h1>
              <p>{正文}</p>
            </article>
            <footer>聯絡我們 隱私權 頁尾不應進入正文</footer>
          </body>
        </html>
        """

        with patch.object(爬取核心, "traf_extract", None), patch.object(
            爬取核心,
            "traf_baseline",
            None,
        ):
            strict內容, strict來源, _, _ = 爬取核心._extract_http_content_bundle(
                html,
                "https://example.com/article",
                extraction_mode=爬取核心.EXTRACTION_MODE_STRICT,
            )
            general內容, general來源, _, _ = 爬取核心._extract_http_content_bundle(
                html,
                "https://example.com/article",
                extraction_mode=爬取核心.EXTRACTION_MODE_GENERAL,
            )

        for 內容 in (strict內容, general內容):
            self.assertIn("文章核心內容", 內容)
            self.assertNotIn("不應進入正文", 內容)
        self.assertIn(strict來源, {"html_article", "html_fallback"})
        self.assertIn(general來源, {"html_article", "html_fallback"})

    def test_Strict不足時才採用General抽取結果(self) -> None:
        strict結果 = SimpleNamespace(
            html="<html></html>",
            quality=SimpleNamespace(
                acceptable=False,
                usable=True,
                reason="BELOW_ACCEPT_THRESHOLD",
            ),
            metrics=SimpleNamespace(text_len=100),
        )
        general結果 = SimpleNamespace(
            html="<html></html>",
            quality=SimpleNamespace(acceptable=True, usable=True, reason=None),
            metrics=SimpleNamespace(text_len=500),
        )

        with patch.object(
            搜尋管線,
            "_build_attempt_from_fetch",
            side_effect=[strict結果, general結果],
        ) as 模擬抽取:
            結果, 模式 = 搜尋管線._select_http_attempt(object())

        self.assertIs(結果, general結果)
        self.assertEqual(模式, "general")
        self.assertEqual(模擬抽取.call_count, 2)
        self.assertEqual(
            [呼叫.args[1] for 呼叫 in 模擬抽取.call_args_list],
            [
                爬取核心.EXTRACTION_MODE_STRICT,
                爬取核心.EXTRACTION_MODE_GENERAL,
            ],
        )


class 批次爬取隔離測試(unittest.IsolatedAsyncioTestCase):
    """單一頁面崩潰時，其餘 worker 仍必須完成並依輸入順序回傳。"""

    async def test_單一Worker例外不會中止整批(self) -> None:
        async def 模擬單頁(url: str, **_: object) -> dict[str, object]:
            await asyncio.sleep(0)
            if "bad.example" in url:
                raise RuntimeError("模擬單頁崩潰")
            return {
                "url": 搜尋管線._normalize_url(url),
                "success": True,
                "content": f"{url} 的內容",
                "content_length": len(url),
                "used_render": "http",
            }

        with patch.object(搜尋管線, "_crawl_single_url", new=模擬單頁):
            結果 = await 搜尋管線.batch_deep_crawl(
                [
                    "https://ok.example/a",
                    "https://bad.example/b",
                    "https://ok.example/c",
                ],
                max_concurrency=2,
                monitor=搜尋管線.PipelineMonitor(enabled=False),
            )

        self.assertEqual(len(結果), 3)
        self.assertTrue(結果[0]["success"])
        self.assertFalse(結果[1]["success"])
        self.assertEqual(結果[1]["url"], "https://bad.example/b")
        self.assertEqual(結果[1]["content"], "")
        self.assertEqual(結果[1]["error_code"], "CRAWL_WORKER_ERROR")
        self.assertTrue(結果[2]["success"])

    async def test_父Worker失敗會取消投機子任務(self) -> None:
        子任務列表: list[asyncio.Task[None]] = []

        async def 永久等待() -> None:
            await asyncio.Event().wait()

        async def 父任務() -> None:
            child = asyncio.create_task(永久等待())
            子任務列表.append(child)
            搜尋管線._cancel_child_task_when_parent_finishes(child)
            raise RuntimeError("模擬 worker 異常")

        with self.assertRaisesRegex(RuntimeError, "worker"):
            await asyncio.create_task(父任務())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(len(子任務列表), 1)
        self.assertTrue(子任務列表[0].cancelled())

    async def test_投機子任務例外會被收取(self) -> None:
        async def 子任務失敗() -> None:
            raise RuntimeError("模擬瀏覽器已關閉")

        child = asyncio.create_task(子任務失敗())
        搜尋管線._cancel_child_task_when_parent_finishes(child)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertTrue(child.done())
        self.assertFalse(child._log_traceback)


class 假HTTP客戶端:
    """只實作共用 client 測試所需的關閉狀態。"""

    def __init__(self) -> None:
        self.is_closed = False
        self.關閉次數 = 0

    async def aclose(self) -> None:
        self.關閉次數 += 1
        self.is_closed = True


class 共用LLM客戶端測試(unittest.IsolatedAsyncioTestCase):
    """確保 URL/chunk reviewer 可重用連線池，並能在生命週期結束時重建。"""

    async def asyncSetUp(self) -> None:
        搜尋管線._LLM_API_CLIENT = None
        搜尋管線._LLM_API_CLIENT_LOCK = None

    async def asyncTearDown(self) -> None:
        client = 搜尋管線._LLM_API_CLIENT
        搜尋管線._LLM_API_CLIENT = None
        搜尋管線._LLM_API_CLIENT_LOCK = None
        if client is not None and not getattr(client, "is_closed", False):
            await client.aclose()

    async def test_併發取得只建立一個Client且關閉後可重建(self) -> None:
        第一個 = 假HTTP客戶端()
        第二個 = 假HTTP客戶端()

        with patch.object(
            搜尋管線.httpx,
            "AsyncClient",
            side_effect=[第一個, 第二個],
        ) as 建構器:
            第一輪 = await asyncio.gather(
                *[搜尋管線._get_llm_api_client() for _ in range(8)]
            )

            self.assertTrue(all(client is 第一個 for client in 第一輪))
            self.assertEqual(建構器.call_count, 1)

            await 搜尋管線._close_llm_api_client()
            self.assertEqual(第一個.關閉次數, 1)
            self.assertIsNone(搜尋管線._LLM_API_CLIENT)

            重建結果 = await 搜尋管線._get_llm_api_client()
            self.assertIs(重建結果, 第二個)
            self.assertEqual(建構器.call_count, 2)


if __name__ == "__main__":
    unittest.main()
