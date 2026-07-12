# Simplex 架構

## 執行流程

```text
使用者問題
  ↓
問答模型：產生三組互補查詢
  ↓
搜尋路由：原生 SearXNG 或使用者自有搜尋 API
  ↓
所有模式使用 Judge 模型進行 URL 篩選
  ↓
Crawl4AI／Playwright／PDF／OCR 深爬
  ↓
所有模式使用 Judge 模型進行 Chunk 審核
  ↓
evidence_bundle + source_registry
  ↓
問答模型：只根據證據串流生成附引用答案
```

## 模組邊界

- `simplex_app/`：FastAPI、加密設定、LLM Provider 與前端託管。
- `deep_search_tool.py`：SearXNG、外部搜尋服務、Judge 與證據管線。
- `pro_search_crawl_backend.py`：Crawl4AI、瀏覽器、PDF 與 OCR 爬取基建。
- `frontend/`：React 設定頁、搜尋進度、答案與引用介面。
- `scripts/simplex_supervisor.py`：原生環境的一鍵啟動與程序生命週期。
- `searxng/settings.yml`：Simplex 內建 SearXNG 搜尋引擎設定。

## 搜尋模式

- `searxng`：預設模式；三組 query 全部由本機 SearXNG 承接。
- `custom`：停用原生 SearXNG，只使用已啟用且具備有效設定的商業或自定義搜尋 API。

學術模式會保留一般 Web 搜尋並增加 SearXNG `science` 分流；社群模式會保留一般 Web 搜尋並增加 `social media` 分流。SearXNG 保留頁面排序，各搜尋軌合併去重後每組 query 最多取前 30 筆。

`instant`、`fast`、`full` 都保留 V3 的兩階段 LLM Judge。全文先移除短於 80 字元的結構碎片並做段落去重，再切成目標 900、最大 1400 字元的 chunk；清理後的全部有效 chunk 都交給 Judge。Chunk prompt 每個來源只宣告一次標題、URL 與 query，以避免小 chunk 重複 metadata。所有中間 Judge 固定關閉 reasoning。

回答階段使用 Provider 的 OpenAI-compatible SSE，FastAPI 將文字增量轉送為 `answer_delta`，React 收到後立即追加 Markdown；最終 `result` 仍包含完整答案與引用資料，供斷線重試、記錄與一致性檢查。

## 部署

- 原生：`Simplex Search.command` → `simplex` → `simplex_supervisor.py`，只監聽 `127.0.0.1:8787` 與 `127.0.0.1:8888`。
- Docker：Compose 同時啟動 SearXNG 與 Simplex；宿主機只公開 `127.0.0.1:8787`。

前端入口頁採重新驗證策略，JavaScript 與 CSS 使用 Vite 內容雜湊檔名。公開網址固定為 `http://127.0.0.1:8787/`，不加入建置或測試參數。
