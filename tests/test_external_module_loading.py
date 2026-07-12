"""模擬統一 main_server 以 importlib 載入 Pro Search MCP 的回歸測試。"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


class 外側動態載入測試(unittest.TestCase):
    """主伺服器未必會把本 MCP 目錄放入 sys.path。"""

    def test_importlib動態載入可註冊ProSearch工具(self) -> None:
        專案目錄 = Path(__file__).resolve().parents[1]
        外側工作目錄 = 專案目錄.parent
        模擬程式 = textwrap.dedent(
            """
            import asyncio
            import importlib.util
            import sys
            from pathlib import Path

            module_path = Path(sys.argv[1]).resolve()
            spec = importlib.util.spec_from_file_location('pro_search_v3_mcp_test', module_path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)

            async def main():
                assert {tool.name for tool in await module.mcp.list_tools()} == {'pro_search'}
                await module._shutdown_shared_resources()

            asyncio.run(main())
            """
        )
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        結果 = subprocess.run(
            [sys.executable, "-c", 模擬程式, str(專案目錄 / "pro_search_mcp.py")],
            cwd=外側工作目錄,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(
            結果.returncode,
            0,
            msg=f"stdout:\n{結果.stdout}\nstderr:\n{結果.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
