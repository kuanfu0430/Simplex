# Security Policy

## 支援範圍

目前支援 `main` 分支的最新版本。

## 回報漏洞

請不要公開可能洩漏金鑰、允許任意網路存取或遠端執行的 Issue。發布 GitHub 倉庫後，請啟用 Private vulnerability reporting，並透過 Security Advisories 回報。

## 秘密保存

- Provider Key、內建搜尋 API Key 與自訂搜尋服務 Key 以 Fernet 加密後寫入 `data/settings.db`。
- 本機加密金鑰位於 `data/.settings.key`，權限為 `0600`。
- API 只回傳 `has_api_key` 或布林設定狀態。
- `.env`、`data/`、`.venv` 與前端建置輸出都不會提交。
- 不要備份或分享 `data/secret.key`；遺失後，現有加密設定無法還原。

## 部署模型

預設只監聽 `127.0.0.1`。本專案目前沒有登入、租戶隔離與權限系統，不應直接暴露在公網。若要對外部署，至少加入：

- HTTPS 反向代理與身份驗證
- CSRF／Origin 政策
- 速率與請求大小限制
- 受管秘密服務
- 日誌去識別化
- 容器與網路層出站政策

## Provider URL

使用者可明確設定本機 Ollama／LM Studio，因此 Provider Base URL 允許 `127.0.0.1`。這項能力只適合單機可信管理者；公開部署時應由管理員 allowlist Provider Host。
Provider Base URL 不可夾帶 username、password、query 或 fragment，避免秘密被寫入公開設定欄位或日誌。

自訂搜尋服務端點同樣不可夾帶帳密、query 或 fragment。自訂服務是使用者明確授權的出站目的地，可指向本機服務；公開部署時請以 allowlist、容器網路政策與身份驗證限制可連線主機。Simplex 會以 `Authorization: Bearer` 傳送 Key，並只接受結構化 JSON 結果。

搜尋結果的深爬路徑與 Provider URL 採不同政策：深爬會拒絕 localhost、私網、link-local、保留位址與 metadata endpoint，並在每次 HTTP redirect 前重新解析與驗證；HTTP body 也有固定 byte cap。內建 SearXNG 只在受控的本機或 Compose 內部網路使用，公開部署仍建議再加容器層的出站網路政策。
