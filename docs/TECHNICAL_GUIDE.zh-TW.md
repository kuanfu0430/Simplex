# Simplex 技術與操作指南

[返回入口 README](../README.md) · [English technical guide](TECHNICAL_GUIDE.en.md)

本文件放置安裝、設定、研究管線、對話上下文、PDF、API、安全與測試細節。第一次接觸專案時，建議先閱讀入口 README。

## 1. 設計目標

Simplex 的核心取捨是「速度優先，但不能把證據品質交給模型臆測」：

- 搜尋、Judge、爬蟲與回答各自有明確責任，避免把搜尋摘要直接當成事實。
- 沒有明確網址時，不替一般追問自動增加爬蟲或額外 agent loop。
- 有明確網址時先取得正文，再由查詢規劃器與 Judge 決定直接回答或進入搜尋。
- 多輪只保留受控歷史與 evidence capsule，不把所有舊工具輸出無限累積到 prompt。
- 所有可引用內容都要來自深爬後的原文 chunks，並由 source registry 重新編號 citation。

## 2. 安裝與啟動

### 原生安裝

需求：Python 3.11 以上、Git、Node.js/npm。執行：

```bash
./simplex install
./simplex start
```

啟動器會建立：

- Simplex Python 環境：`.venv/`
- SearXNG Python 環境：`.runtime/searxng-venv/`
- Playwright／Patchright Chromium 執行環境
- PDF 解析與 Tesseract OCR 所需依賴

服務只綁定本機回環介面：

| 服務 | 位址 | 用途 |
|---|---|---|
| Simplex Web | `http://127.0.0.1:8787` | FastAPI、SSE 與前端 |
| SearXNG | `http://127.0.0.1:8888` | 本機搜尋基建 |

若要檢查安裝狀態：

```bash
./simplex doctor
```

停止服務可在啟動器前景程序按 `Ctrl-C`。啟動前會檢查連接埠，不能用 `0.0.0.0` 取代本機綁定位址。

### Docker

```bash
docker compose up --build
```

完成後開啟 `http://127.0.0.1:8787/`。Compose 只把宿主機的本機埠映射到 Web 服務，SearXNG 留在容器網路中。長期部署前請在 `.env` 設定隨機 `SEARXNG_SECRET`。

### 環境變數

`.env.example` 只放範例，不包含密鑰。常用設定包括：

```dotenv
SEARXNG_URL=http://127.0.0.1:8888
PDF_ENABLE_OCR=true
PDF_OCR_LANGUAGES=eng+chi_tra+chi_sim+jpn
```

Web 版的 Provider API Key 應在 Settings 設定；後端會以本機 SQLite 與 Fernet 加密保存，不會把明文回傳給前端。

## 3. 初次設定

右上角 Settings 分成三類：

### Models

1. 在 Provider 中填入 API Key 並儲存。
2. 使用 Sync models 取得可用模型。
3. 將要提供給單次提問選擇的模型加入 `Model pool`。
4. 分別指定 `Question model` 與 `Judge model`。

Question model 負責查詢規劃、追問路由與最終引用回答；Judge 負責 URL 選址、chunk 選擇與證據充分性判斷。Model pool 只是指定某一題的回答模型清單，不代表研究流程使用一個名為「研究模型」的角色。

### Search services

Simplex 有兩種互斥模式：

- Native SearXNG：使用本機 SearXNG，不需要商業搜尋 API Key。
- Custom search engines：停用內建 SearXNG，只向已啟用且設定完整的自定義 JSON 或商業 Provider 發送查詢。

可用外部 Provider 包括 Brave、Tavily、Exa、SerpApi 與自定義 JSON API。自定義 API 可設定 GET／POST、授權方式、query／count 欄位、結果陣列路徑與欄位映射。

### Other

- UI language：預設 English，可切換 `繁體中文`。
- Theme：Dark 或 Light。
- Interface scale：整體縮放，而非只放大文字。

語言設定會影響介面、狀態訊息、錯誤訊息、研究軌跡與最終生成回答。回答模型一律依使用者原始提問語言回答，即使搜尋使用其他語言。

## 4. 研究管線

```mermaid
flowchart LR
  Q[問題或追問] --> U{目前訊息有 HTTP(S) URL？}
  U -->|有| DC[直接讀取 URL]
  DC --> DJ[指定內容 Chunk Judge]
  DJ --> DR{可直接回答？}
  DR -->|是| A[回答模型]
  DR -->|否| P[查詢規劃]
  U -->|沒有| R{舊 evidence 足夠？}
  R -->|是| A
  R -->|否| P
  P --> S[SearXNG／搜尋 Provider]
  S --> F[URL Judge]
  F --> C[Crawl4AI／HTTP／PDF]
  C --> K[切 chunks]
  K --> J[Chunk Judge]
  J --> E[合併 evidence 與 citation]
  E --> A
```

### 4.1 查詢規劃

規劃器會先建立 `standalone_question`，再輸出 `strategy`：

- `reuse`：問題可以完全由已驗證舊 evidence 回答。
- `direct`：本輪指定 URL 已足以回答，且沒有要求最新資訊、外部驗證或額外來源。
- `research`：需要新事實、最新資訊、驗證、擴大範圍或規劃器不確定。

進入 `research` 時會準備三組互補查詢。搜尋語言依問題屬性決定；時效性問題會依使用者要求取得最新日期的資料，不會只因使用者使用中文就強制所有 query 使用中文。

### 4.2 搜尋與配額

目前的搜尋分流：

| 模式 | SearXNG 分流 | 目的 |
|---|---|---|
| Web | `general` | 一般網路資料 |
| Academic | `general` + `science` | 學術與一般背景交叉資料 |
| Social | `general` + `social media` | 社群觀點與一般背景 |

多個 Provider 的結果會先 canonicalize URL、合併來源軌跡、去重，再進入 URL Judge。缺少某一類專用 Provider 時，已啟用的其他 Provider 仍可承接查詢。

### 4.3 URL Judge

URL Judge 只看搜尋候選的標題、摘要與來源資訊，用來選擇值得深爬的頁面。它不是最終回答模型，也不應把 snippets 當作回答事實。選址時保留不同來源與互相矛盾的觀點，不預設偏好單一立場。

### 4.4 深爬與 PDF

單頁爬取順序是：

1. 先使用快速 HTTP 正文抽取。
2. 若偵測到 SPA、互動內容或正文品質不足，再使用 Playwright／Patchright。
3. PDF 優先使用 PyMuPDF，必要時以 pypdf 後備。
4. 掃描 PDF 或正文不足時，依設定使用 Tesseract OCR。

預設 OCR 語言為 `eng+chi_tra+chi_sim+jpn`。PDF 解析會保留頁面邊界，清理重複 header／footer 與版面噪音，再將中文短排版行合併成可供 Judge 審核的正文 chunks。PDF 解析問題的實例請見 [PDF chunk 診斷報告](pdf-chunk-diagnosis-jas-hkbu.md)。

### 4.5 Chunk Judge 與回答

Chunk Judge 會從深爬正文中選出最小充分 evidence set，並回報：

- `verdict`：`sufficient` 或 `insufficient`。
- `coverage`：已回答面向與缺口。
- `selected_chunks`：可引用的原文片段。
- `next_search_queries`：需要補洞時的查詢。

最終回答模型只接收合併後 evidence bundle 的原文 chunks。歷史回答、搜尋 snippets、query plan、content map 與 source registry 都不能被當成事實來源。引用 marker 會緊貼在使用該來源的句子後面。

## 5. 直接網址模式

使用者在本輪問題貼入 HTTP(S) URL 時：

1. 擷取並 canonicalize URL，單輪最多五個；超過上限會明確回報，不會靜默丟掉網址。
2. 透過與搜尋結果相同的安全 HTTP／JS／PDF 管線讀取正文。
3. 將受限的相關 chunks 同時交給查詢規劃器，並讓指定內容 Judge 與規劃並行執行。
4. 只有「所有指定 URL 讀取成功 + Judge 判定充分 + 規劃器選 `direct`」才會跳過一般搜尋。
5. 內容不足、讀取失敗、使用者要求最新資料／外部驗證，或開啟強制研究時，改走 `hybrid` 或一般 `research`。

直接讀取的 URL 會傳入搜尋管線的排除集合，避免同一輪再被搜尋結果重複深爬。所有直接 URL 一樣會做 DNS、私有位址、回環位址與 redirect 安全檢查。

這個模式是受控的前置判定，不會讓 LLM 自由決定 `instant`、`fast` 或 `full`，也不會把 Simplex 變成無限制的 agentic search。

## 6. 多輪對話與 evidence capsule

### 6.1 送入下一輪的內容

下一輪會保留：

- 受控的 user／assistant 歷史訊息。
- 加密、驗證過的 evidence capsule。
- 由問題關鍵詞與來源分散策略選出的少量舊 evidence。
- 不把上一輪所有搜尋 snippets、工具 trace 與完整爬蟲全文重送進規劃 prompt。

這樣可以維持代詞與討論脈絡，同時避免輪次增加後 token 成本無限上升與 lost in the middle。

### 6.2 reuse、research 與刷新

一般追問若能由舊 evidence 完整回答，走 `reuse`，不會啟動深搜。若是最新資訊、要求驗證、明確要求新來源或存在缺口，走 `research`。

規劃器只能從 evidence ledger 提供的 `source_ref` 中選擇要刷新來源，且只有使用者明確要求重看、重新驗證、更新或擷取先前來源細節時才會使用；後端最多接受兩個已驗證 URL，並與第一輪搜尋並行刷新。刷新後的 chunks 會取代同 URL 的舊 evidence，避免新舊版本同時污染回答。

## 7. 執行深度

| 模式 | 行為 | 適合情境 |
|---|---|---|
| `instant` | 單輪搜尋與取證，不補搜 | 已有明確題目、重視首字速度 |
| `fast` | 首輪不足時進行一次輕量補洞 | 一般研究與追問的預設選擇 |
| `full` | 最多三輪，依 Judge 缺口逐步補搜 | 需要較完整的多面向匯報 |

前端的 Fast／Full 是研究預算上限；URL 或 chunk Judge 可以判定證據不足，但不能無限制擴張流程。

## 8. Web API 與 SSE

### 8.1 主要端點

- `GET /api/health`：SearXNG、爬蟲、Chromium 與 OCR 狀態。
- `GET /api/ready`：程序 readiness。
- `GET/PUT /api/settings`：讀取或保存遮罩後設定。
- `GET /api/llm/providers/{id}/models`：同步 Provider 模型。
- `POST /api/search-engines/{id}/test`：驗證搜尋服務設定。
- `POST /api/search/stream`：啟動研究與回答串流。

### 8.2 搜尋請求

```json
{
  "question": "請比較這些來源的主要觀點",
  "search_mode": "academic",
  "mode": "fast",
  "conversation_history": [
    {"role": "user", "content": "上一輪問題"},
    {"role": "assistant", "content": "上一輪回答"}
  ],
  "context_capsules": ["由後端簽發的密文"],
  "force_research": false
}
```

`model_selection` 只能指定預設問答模型或 Model pool 中的模型。`context_capsules` 是後端簽發的密文，不接受任意客戶端自造 evidence。

### 8.3 SSE 事件

常見順序如下：

1. `status`：planning、direct crawl、searching、answering 等狀態。
2. `research_trace`：query、來源、URL selection、direct sources、chunks 與各階段。
3. `answer_start`：回答開始前的證據、來源、capsule 與初始 timings。
4. 多個 `answer_delta`：回答模型的串流片段。
5. `result`：完整回答、`research_strategy`、sources、evidence bundle、timings。
6. `done`：本輪結束。

`research_strategy` 可能是：

- `reuse`：沿用舊 evidence。
- `direct`：只用本輪指定 URL。
- `hybrid`：指定／刷新來源加上新搜尋。
- `research`：一般搜尋研究。

`timings` 會記錄 `planning_ms`、`research_ms`、`direct_crawl_ms`、`direct_judge_ms`、`answer_first_token_ms`、`answer_ms` 與 `total_ms`。

## 9. 前端與程式結構

```text
simplex_app/                  FastAPI、設定加密、模型設定、SSE 路由
frontend/                     React + Vite + TypeScript PWA
deep_search_tool.py           搜尋、配額、Judge、深爬、chunks 與研究編排
pro_search_crawl_backend.py   HTTP／JS／PDF 深爬核心
crawl4ai_pdf.py               PDF 解析與 OCR
searxng/settings.yml          SearXNG general/science/social 設定
scripts/                      安裝器、doctor、雙服務 supervisor
docs/                         技術文件與診斷報告
tests/                        離線回歸測試
```

前端的 `ResearchTracePanel` 將 SSE trace 聚合為可展開的 query、來源、指定 URL 與 chunks。對話與研究歷史保存在瀏覽器本機；敏感的模型 API Key 與 evidence capsule 不會由前端歷史明文保存。

## 10. 安全邊界

- Web 與 MCP 服務只綁定 `127.0.0.1`，不應直接暴露到公網。
- 搜尋結果、使用者直接貼入 URL 與受控刷新 URL 都會在深爬前解析 DNS，拒絕私有、回環、link-local 與保留位址。
- HTTP redirect 每一跳都重新驗證，避免透過公開 URL 轉向讀取內網。
- `.env`、`data/`、`.runtime/`、`.venv/`、前端 node_modules／dist、資料庫與金鑰檔都不應提交。
- API 設定回傳空字串與 `has_api_key`，不回傳明文密鑰。
- evidence capsule 使用本機 Fernet 金鑰加密與驗證；遭竄改、過期或無法解密的 capsule 會被丟棄。
- 服務拒絕不受信任 Host、跨站 Origin 與不符合本機請求條件的 metadata。

## 11. 開發與測試

安裝開發依賴後：

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
npm --prefix frontend run lint
npm --prefix frontend run build
bash -n scripts/*.sh simplex "Simplex Search.command"
./simplex doctor
```

後端語法快速檢查：

```bash
.venv/bin/python -m py_compile \
  simplex_app/main.py simplex_app/conversation.py \
  simplex_app/llm.py deep_search_tool.py
```

新增或修改研究路由時，至少應覆蓋：無 URL 的普通追問、指定 URL 的 `direct`、指定 URL 不足時的 `hybrid`、受控舊來源刷新、URL 安全與 evidence citation。

## 12. 常見問題

### 服務啟動但搜尋沒有結果

先執行 `./simplex doctor`，確認 `127.0.0.1:8888` 的 SearXNG 可用，再確認 Settings 中至少有一個有效搜尋 Provider。外部搜尋引擎被 rate-limit 時，SearXNG 可能回傳部分結果；這不等同於 Simplex 的本機服務故障。

### 為什麼指定 URL 沒有走 direct？

`direct` 是保守路由。只要有一個 URL 讀取失敗、正文不足、Judge 判定缺口、規劃器不確定，或問題要求最新／外部驗證，就會改走 `hybrid` 或 `research`。

### 為什麼追問沒有刷新上一輪來源？

只有明確要求重新閱讀、驗證、更新或抽取特定舊來源時，規劃器才會輸出合法的 `source_ref`。一般追問沿用舊 evidence 或重新搜尋即可，不會自動重爬所有舊網址。

### PDF chunk 品質不理想

先確認 OCR 語言包與 PyMuPDF／Tesseract 安裝狀態，再查看 [PDF chunk 診斷報告](pdf-chunk-diagnosis-jas-hkbu.md) 的抽取、清理、頁面邊界與 fallback 建議。
