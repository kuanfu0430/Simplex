# 貢獻指南

感謝你協助改善 Simplex Search。

## 開發流程

1. Fork 並建立主題分支。
2. 不要提交 API Key、`data/`、`.env` 或模型輸出中的敏感資料。
3. 修改搜尋核心時，同步新增或調整 `tests/` 回歸測試。
4. 修改 Provider／編排時，新增 `tests/test_simplex_app.py` 或相應測試。
5. 修改前端時，補上元件測試並確認桌面與行動版。
6. 更新 README 或架構文件，讓實作與說明一致。

提交 PR 前請執行 README「驗證」章節中的所有指令。

## 提交訊息

建議使用簡短、可讀的 Conventional Commits，例如：

- `feat: 新增 Groq Provider preset`
- `fix: 拒絕不存在的 citation marker`
- `test: 補上並行 Judge 模型隔離測試`
