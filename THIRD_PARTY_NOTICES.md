# 第三方軟體聲明

Simplex 本身以 [MIT License](LICENSE) 發布。安裝或執行時會使用下列獨立的第三方元件；各元件仍受其原授權條款約束。

| 元件 | 用途 | 上游與授權 |
|---|---|---|
| SearXNG | 原生聚合搜尋服務 | https://github.com/searxng/searxng — AGPL-3.0-or-later |
| Crawl4AI | JavaScript 網頁爬取 | https://github.com/unclecode/crawl4ai — Apache-2.0 |
| Playwright | Chromium 自動化 | https://github.com/microsoft/playwright-python — Apache-2.0 |
| Patchright | 抗偵測 Chromium 後備 | https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python — Apache-2.0 |
| FastAPI | Web API 框架 | https://github.com/fastapi/fastapi — MIT |
| React | 使用者介面 | https://github.com/facebook/react — MIT |

Docker 部署直接使用官方 SearXNG 映像；非 Docker 安裝器則從上游固定 commit 取得原始碼並建立隔離環境。若重新散布修改過的 SearXNG，請依其 AGPL 條款提供對應原始碼。
