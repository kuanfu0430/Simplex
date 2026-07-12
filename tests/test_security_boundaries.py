"""Simplex 本機 API 與外部深爬安全邊界測試。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pro_search_crawl_backend as 爬蟲


class 深爬SSRF測試(unittest.IsolatedAsyncioTestCase):
    async def test_拒絕回環私有與Metadata位址(self) -> None:
        for url in (
            "http://127.0.0.1/admin",
            "http://10.0.0.8/private",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/",
            "http://localhost/",
        ):
            with self.subTest(url=url):
                安全, _ = await 爬蟲._public_url_status(url)
                self.assertFalse(安全)

    async def test_redirect送出前會重新驗證目標(self) -> None:
        客戶端 = AsyncMock()
        客戶端.get.return_value = SimpleNamespace(
            status_code=302,
            headers={"location": "http://127.0.0.1/secret"},
            url="https://public.example/start",
        )

        async def 模擬安全判斷(url: str) -> tuple[bool, str]:
            if url.startswith("https://public.example"):
                return True, ""
            return False, "不允許回環位址"

        with patch.object(爬蟲, "_public_url_status", new=模擬安全判斷):
            with self.assertRaises(爬蟲.UnsafePublicURL):
                await 爬蟲._get_with_public_redirect_validation(
                    客戶端,
                    "https://public.example/start",
                    timeout=3,
                )

        self.assertEqual(客戶端.get.await_count, 1)


if __name__ == "__main__":
    unittest.main()
