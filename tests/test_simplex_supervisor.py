"""Simplex 一鍵啟動器測試。"""

from unittest.mock import patch

from scripts import simplex_supervisor as 啟動器


def test_開啟前端使用新分頁與乾淨根網址() -> None:
    with patch.object(啟動器.webbrowser, "open_new_tab") as 開新分頁:
        啟動器.開啟前端()

    開新分頁.assert_called_once_with("http://127.0.0.1:8787/")


def test_啟動網址不包含版本或測試參數() -> None:
    assert 啟動器.前端網址 == "http://127.0.0.1:8787/"
    assert "?" not in 啟動器.前端網址
