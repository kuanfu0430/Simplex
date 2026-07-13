"""Simplex 本機設定儲存與 API Key 加密。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


專案根目錄 = Path(__file__).resolve().parent.parent
資料目錄 = Path(os.environ.get("SIMPLEX_DATA_DIR", 專案根目錄 / "data"))
資料庫路徑 = 資料目錄 / "settings.db"
金鑰路徑 = 資料目錄 / ".settings.key"
遮罩 = "••••••••"


LLM供應商預設值 = [
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models_path": "/models",
        "chat_endpoint": "/chat/completions",
        "api_key": "",
        "enabled": True,
        "custom": False,
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models_path": "/models",
        "chat_endpoint": "/chat/completions",
        "api_key": "",
        "enabled": True,
        "custom": False,
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "models_path": "/models",
        "chat_endpoint": "/chat/completions",
        "api_key": "",
        "enabled": True,
        "custom": False,
    },
    {
        "id": "groq",
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models_path": "/models",
        "chat_endpoint": "/chat/completions",
        "api_key": "",
        "enabled": True,
        "custom": False,
    },
    {
        "id": "mistral",
        "name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "models_path": "/models",
        "chat_endpoint": "/chat/completions",
        "api_key": "",
        "enabled": True,
        "custom": False,
    },
    {
        "id": "nvidia",
        "name": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models_path": "/models",
        "chat_endpoint": "/chat/completions",
        "api_key": "",
        "enabled": True,
        "custom": False,
    },
]


def 預設設定() -> dict[str, Any]:
    return {
        "ui": {"theme": "dark", "scale": 1.0, "language": "en"},
        "llm": {
            "providers": deepcopy(LLM供應商預設值),
            "model_pool": [],
            "question_model": {"provider_id": "openrouter", "model": ""},
            "judge_model": {"provider_id": "openrouter", "model": ""},
        },
        "search": {
            "engine_mode": "searxng",
            "providers": {
                "searxng": {
                    "name": "SearXNG",
                    "enabled": True,
                    "base_url": os.environ.get(
                        "SEARXNG_URL", "http://127.0.0.1:8888"
                    ),
                    "api_key": "",
                },
                "brave": {
                    "name": "Brave Search",
                    "enabled": False,
                    "base_url": "https://api.search.brave.com/res/v1/web/search",
                    "api_key": "",
                },
                "tavily": {
                    "name": "Tavily",
                    "enabled": False,
                    "base_url": "https://api.tavily.com/search",
                    "api_key": "",
                },
                "exa": {
                    "name": "Exa",
                    "enabled": False,
                    "base_url": "https://api.exa.ai/search",
                    "api_key": "",
                },
                "serpapi": {
                    "name": "SerpApi",
                    "enabled": False,
                    "base_url": "https://serpapi.com/search.json",
                    "api_key": "",
                },
            },
            "custom": [],
        },
    }


def _深度合併(基礎: Any, 覆寫: Any) -> Any:
    if isinstance(基礎, dict) and isinstance(覆寫, dict):
        結果 = deepcopy(基礎)
        for 鍵, 值 in 覆寫.items():
            結果[鍵] = _深度合併(結果.get(鍵), 值) if 鍵 in 結果 else deepcopy(值)
        return 結果
    return deepcopy(覆寫)


class 設定儲存庫:
    """以 SQLite 儲存單一設定文件，敏感內容使用本機 Fernet 金鑰加密。"""

    def __init__(self, 資料庫: Path = 資料庫路徑, 金鑰檔: Path = 金鑰路徑):
        self.資料庫 = Path(資料庫)
        self.金鑰檔 = Path(金鑰檔)
        self._鎖 = threading.RLock()
        self.資料庫.parent.mkdir(parents=True, exist_ok=True)
        self._密碼器 = Fernet(self._取得或建立金鑰())
        self._初始化()

    def _取得或建立金鑰(self) -> bytes:
        if self.金鑰檔.exists():
            return self.金鑰檔.read_bytes().strip()
        金鑰 = Fernet.generate_key()
        self.金鑰檔.write_bytes(金鑰 + b"\n")
        try:
            self.金鑰檔.chmod(0o600)
        except OSError:
            pass
        return 金鑰

    def _連線(self) -> sqlite3.Connection:
        連線 = sqlite3.connect(self.資料庫, timeout=10)
        連線.execute("PRAGMA journal_mode=WAL")
        return 連線

    def _初始化(self) -> None:
        with self._鎖, self._連線() as 連線:
            連線.execute(
                "CREATE TABLE IF NOT EXISTS app_settings "
                "(id INTEGER PRIMARY KEY CHECK(id = 1), payload BLOB NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            是否存在 = 連線.execute(
                "SELECT 1 FROM app_settings WHERE id = 1"
            ).fetchone()
            if not 是否存在:
                self._寫入連線(連線, 預設設定())

    def _寫入連線(self, 連線: sqlite3.Connection, 設定: dict[str, Any]) -> None:
        原文 = json.dumps(設定, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        密文 = self._密碼器.encrypt(原文)
        連線.execute(
            "INSERT INTO app_settings(id, payload, updated_at) VALUES(1, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=CURRENT_TIMESTAMP",
            (密文,),
        )

    def 讀取(self) -> dict[str, Any]:
        with self._鎖, self._連線() as 連線:
            資料列 = 連線.execute(
                "SELECT payload FROM app_settings WHERE id = 1"
            ).fetchone()
            if not 資料列:
                設定 = 預設設定()
                self._寫入連線(連線, 設定)
                return 設定
            try:
                已存設定 = json.loads(self._密碼器.decrypt(資料列[0]).decode("utf-8"))
            except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
                raise RuntimeError("Simplex 設定無法解密，請確認 data/.settings.key 未被替換")
            return _深度合併(預設設定(), 已存設定)

    def 儲存(self, 新設定: dict[str, Any]) -> dict[str, Any]:
        with self._鎖, self._連線() as 連線:
            目前 = self.讀取()
            合併 = _深度合併(目前, 新設定)
            self._保留未重填的密鑰(目前, 合併)
            self._寫入連線(連線, 合併)
            return 合併

    def 取得本機密封器(self) -> Fernet:
        """供同一台機器的短期上下文膠囊加密與驗證使用。"""
        return self._密碼器

    @staticmethod
    def _保留未重填的密鑰(目前: dict[str, Any], 新值: dict[str, Any]) -> None:
        def 依ID保留(舊清單: list[dict[str, Any]], 新清單: list[dict[str, Any]]) -> None:
            舊索引 = {str(項目.get("id")): 項目 for 項目 in 舊清單}
            for 項目 in 新清單:
                舊項目 = 舊索引.get(str(項目.get("id")), {})
                if 項目.get("api_key") in (None, "", 遮罩):
                    項目["api_key"] = 舊項目.get("api_key", "")

        依ID保留(
            list(目前.get("llm", {}).get("providers", [])),
            list(新值.get("llm", {}).get("providers", [])),
        )
        依ID保留(
            list(目前.get("search", {}).get("custom", [])),
            list(新值.get("search", {}).get("custom", [])),
        )
        舊內建 = 目前.get("search", {}).get("providers", {})
        新內建 = 新值.get("search", {}).get("providers", {})
        for 供應商ID, 項目 in 新內建.items():
            if 項目.get("api_key") in (None, "", 遮罩):
                項目["api_key"] = 舊內建.get(供應商ID, {}).get("api_key", "")

    @staticmethod
    def 公開設定(設定: dict[str, Any]) -> dict[str, Any]:
        結果 = deepcopy(設定)

        def 隱藏(項目: dict[str, Any]) -> None:
            有密鑰 = bool(str(項目.get("api_key") or "").strip())
            項目["api_key"] = ""
            項目["has_api_key"] = 有密鑰

        for 項目 in 結果.get("llm", {}).get("providers", []):
            隱藏(項目)
        for 項目 in 結果.get("search", {}).get("providers", {}).values():
            隱藏(項目)
        for 項目 in 結果.get("search", {}).get("custom", []):
            隱藏(項目)
        return 結果


_預設儲存庫: 設定儲存庫 | None = None
_預設鎖 = threading.Lock()


def 取得設定儲存庫() -> 設定儲存庫:
    global _預設儲存庫
    if _預設儲存庫 is None:
        with _預設鎖:
            if _預設儲存庫 is None:
                _預設儲存庫 = 設定儲存庫()
    return _預設儲存庫
