# PDF 主文未進入 chunk 的診斷與建議方案

日期：2026-07-13
狀態：P0 已完成並以原始測試 PDF 回歸；P1 的幾何清洗與 OCR 決策優化尚待處理

## 結論摘要

這次問題不是 PDF 網址沒有被辨識，也不是伺服器回傳了假的 JSON。測試網址雖然以 `.ssocheck.json` 結尾，實際回應是有效 PDF；Simplex 也正確將它辨識為 `resource_type=pdf`，並由 PyMuPDF 抽出 84 頁、約 64,000 字的文字。

真正的主要故障發生在 PDF 抽取之後：

1. 通用網頁清洗器把 PDF 原有的頁面與段落空行全部壓成單換行。
2. chunker 看不到段落後退回「逐行處理」，再把少於 80 字的每一行丟掉。
3. 這份中文 PDF 的正文中位行長只有 27 字，因此 99.38% 的行被過濾；最後只留下 18 行較長的英文書目或混排雜訊作為 chunk。
4. PDF 頁邊的直排欄位被 `sort=True` 插入正文閱讀順序；部分圖版頁又被錯誤判定需要 OCR，而本機只有英文 OCR，產生額外亂碼。

所以這是「PDF 幾何文字 → 通用網頁清洗 → 逐行 chunk」三層交互造成的確定性錯誤。建議建立 PDF 專用的幾何清洗與段落重建路徑，不應只調低 `CHUNK_MIN_CHARS`。

## P0 實施與回歸結果

已完成下列低風險修正：

1. 本機已安裝 Homebrew `tesseract-lang 4.1.0`，並實測 `tesseract --list-langs` 可列出 `eng`、`chi_tra`、`chi_sim`、`jpn`。PyMuPDF 本身不管理 OCR 字體包，實際由本機 Tesseract 的語言資料提供中日文辨識能力。
2. `PDF_OCR_LANGUAGES` 的預設值改為 `eng+chi_tra+chi_sim+jpn`；Docker、原生安裝器、範例環境變數與 doctor 檢查同步要求這四種語言資料。
3. `_refresh_attempt_after_clean()` 現依 `resource_type` 分流。PDF 使用不會刪除空行的 `_clean_pdf_content()`，不再套用 HTML 的導覽與尾段裁切規則。
4. `_page_to_review_chunks()` 將頁面的 `resource_type` 傳入 chunker；PDF 以 `## 第 N 頁` 分段，先累積相鄰物理短行到正常 chunk 長度，再套用最小長度門檻。

以本文件所列同一個原始 URL 重跑正式 `_crawl_single_url() → _page_to_review_chunks()`（2026-07-13）結果如下：

| 指標 | P0 回歸結果 |
|---|---:|
| HTTP／資源類型 | 成功／`pdf` |
| PDF 頁數 | 84 |
| OCR 語言 | `eng+chi_tra+chi_sim+jpn` |
| 抽取文字 | 64,068 chars |
| Judge 前 review chunks | 106 |
| chunks 文字總量 | 62,131 chars |
| chunk 保留率 | 96.98% |
| 含「青銅器、春秋、禮制、傳承」任一正文詞的 chunk | 77 |

首個 chunk 已包含文章題名、摘要與「西周晚期到春秋中期……」等正文；原先只剩 18 條長英文書目、2.39% 保留率的問題已排除。這次 P0 不處理頁邊直排欄與 OCR 誤觸發，它們仍依下方 P1-A、P1-B 的方案後續改善。

## 測試範圍

測試網址：

<https://jas.hkbu.edu.hk/content/ito/en/_jcr_content.ssocheck.json?pathPdf=/content/dam/jas-assets/publications/bulletin-of-the-jao-tsung-i-academy-of-sinology/issue-9/i9-05.pdf>

對照的表面 PDF 路徑：

<https://jas.hkbu.edu.hk/content/dam/jas-assets/publications/bulletin-of-the-jao-tsung-i-academy-of-sinology/issue-9/i9-05.pdf>

本次重現使用專案自己的 `_crawl_single_url()`、`extract_pdf_content()`、`_page_to_review_chunks()`，不是另寫一套抽取器後推測正式流程。

## 實測證據

### 1. HTTP 與資源辨識正常

測試網址的實際回應：

| 項目 | 結果 |
|---|---|
| HTTP | `200` |
| `Content-Type` | `application/pdf` |
| `Content-Disposition` | `inline; filename=i9-05.pdf` |
| Body 開頭 | `%PDF-1.4` |
| 下載大小 | 6,378,317 bytes |

表面 `.pdf` 路徑反而回傳 `302`，重新導向上述 `.ssocheck.json?pathPdf=...` 網址。

目前資源偵測同時檢查 body 簽名、Content-Type 與 URL，因此這個案例仍正確進入 PDF 分支：

- [`crawl4ai_pdf.detect_resource_type()`](../crawl4ai_pdf.py#L92)
- [`pro_search_crawl_backend._fetch_http()`](../pro_search_crawl_backend.py#L1720)

結論：不應把修正放在副檔名判斷或重新導向處理。

### 2. PDF 文字層其實有成功抽出

正式管線回傳的主要診斷：

| 項目 | 結果 |
|---|---:|
| `success` | `true` |
| `resource_type` | `pdf` |
| `content_source` | `pdf_ocr_hybrid` |
| PDF 頁數 | 84 |
| 清洗後文字長度 | 62,809 chars |
| PyMuPDF 原始整理結果 | 64,063 chars |
| 抽取引擎 | `pymupdf` |
| OCR 頁 | 7、11、13、15 |

第一頁與後續正文可讀，包含文章標題、摘要、正文及註釋。因此「PyMuPDF 完全沒有解析主文」並不是主因。

### 3. 主文是在 chunk 前被丟掉

正式清洗後內容的統計：

| 指標 | 結果 |
|---|---:|
| 非空內容行 | 2,891 |
| 少於 80 字的行 | 2,873 |
| 少於 80 字比例 | 99.38% |
| 行長中位數 | 27 |
| 最長行 | 92 |
| 最終 review chunks | 18 |
| 18 個 chunk 的總字數 | 1,504 |
| chunk／抽取文字保留率 | 2.39% |

18 個倖存 chunk 的內容均落在長英文書目、機構名稱或頁邊直排字混排，例如：

```text
L1-S1-C001  國 Zhou Dynasty (1045–771 BCE), (New York: Columbia University...)
L1-S1-C003  大 54 Jessica Rawson, Western Zhou Bronzes...
L1-S1-C004  浸 57 Jenny So, Eastern Zhou Bronzes...
```

這解釋了為什麼 Judge 收到的 chunk 看起來像某個重複欄位或無效內容：Judge 並沒有機會看到被提前丟棄的正文。

### 4. 幾何探針證明可恢復正文

以 PyMuPDF `dict` 輸出保留 `bbox` 與文字方向，先移除頁邊直排欄位和純頁碼，再保留頁面／段落邊界，未修改正式程式的對照結果為：

| 指標 | 正式流程 | 幾何探針 |
|---|---:|---:|
| 內容字數 | 62,809 | 59,803 |
| chunk 數 | 18 | 84 |
| 含已知正文詞的 chunk | 0 | 76 |
| 已知側邊欄序列污染 | 有 | 0 |
| 移除的頁邊直排行 | — | 1,512 |
| 移除的純頁碼 | — | 84 |

幾何探針產生的首批 chunk 已恢復文章題名、摘要及「古代中國青銅器……」等正文。這不是單純增加 chunk 數量，而是內容來源回到主文。

## 根因分析

### 根因 A：HTML 專用清洗破壞 PDF 邊界

PDF 抽取器原本以 `## 第 N 頁\n\n正文` 保存頁面邊界：

- [`crawl4ai_pdf._join_page_texts()`](../crawl4ai_pdf.py#L201)

但 PDF 抽取完成後，仍無條件進入 `_refresh_attempt_after_clean()`，再呼叫通用 `_clean_pipeline_content()`：

- [`deep_search_tool._refresh_attempt_after_clean()`](../deep_search_tool.py#L4081)
- [`deep_search_tool._clean_pipeline_content()`](../deep_search_tool.py#L4049)

其中 `_trim_pipeline_trailing_sections()` 先丟棄所有空行，再用單一換行重組全文：

```python
lines = [line.rstrip() for line in text.splitlines() if line.strip()]
return "\n".join(lines).strip()
```

這段對 HTML 導覽、頁尾清洗合理，但對 PDF 會消除頁面與段落語意。

### 根因 B：逐行 fallback 與 80 字門檻不適用排版後的中文

chunker 先用雙換行找段落；找不到時改成逐行，再丟棄少於 `CHUNK_MIN_CHARS=80` 的項目：

- [`deep_search_tool._extract_review_paragraphs()`](../deep_search_tool.py#L4790)

中文期刊 PDF 的每個排版行通常只有 20–30 字。這個門檻適合「段落」，不適合「物理排版行」。因為根因 A 已移除段落空行，根因 B 就會穩定刪除正文。

### 根因 C：`sort=True` 把直排頁邊欄插入正文

目前頁面抽取直接使用：

```python
page.get_text("text", sort=True)
```

位置：[`crawl4ai_pdf._extract_pdf_page_text()`](../crawl4ai_pdf.py#L168)

本 PDF 的奇數頁右側重複直排文章名，偶數頁左側重複直排「香港浸會大學饒宗頤國學院」。它們的方向向量是 `(0, 1)`，每個字是一個獨立文字行。`sort=True` 依座標重排後，這些字被穿插到同高度的正文中，形成：

```text
傳 承 、 中 斷 、 變 革 與 創 新 — 春 秋……
香 港 浸 會 大 學 饒 宗 頤 國 學 院……
```

PyMuPDF 的 `dict`／`rawdict` 輸出本來就提供 block、line、bbox 與 `dir`，可在轉成純文字前處理幾何資訊：

- [PyMuPDF Page.get_text 官方文件](https://pymupdf.readthedocs.io/en/latest/page.html#Page.get_text)
- [PyMuPDF TextPage.extractDICT 官方文件](https://pymupdf.readthedocs.io/en/latest/textpage.html#TextPage.extractDICT)

### 根因 D：OCR 判斷把版面空白誤認為不可列印文字

`_printable_ratio()` 的分子排除空白，分母卻包含所有版面空白：

- [`crawl4ai_pdf._printable_ratio()`](../crawl4ai_pdf.py#L117)

`sort=True` 為保持座標會產生大量空格，因此圖版頁即使已有 1,300–1,500 個文字字元與有效 text blocks，比例仍只有 0.056–0.091，低於 0.15 後觸發 OCR。

實測：

| 頁 | base char count | text blocks | images | printable ratio | OCR |
|---:|---:|---:|---:|---:|---|
| 7 | 1,382 | 9 | 6 | 0.084 | 誤觸發 |
| 11 | 1,412 | 7 | 12 | 0.056 | 誤觸發 |
| 13 | 1,386 | 11 | 6 | 0.071 | 誤觸發 |
| 15 | 1,509 | 11 | 14 | 0.091 | 誤觸發 |

本機 Tesseract 僅安裝 `eng`、`osd`、`snum`，沒有 `chi_tra`。而目前只要 OCR 有任意輸出，就以 `ocr_text or base_text` 覆蓋原文字層：

- [`crawl4ai_pdf.extract_pdf_with_pymupdf()`](../crawl4ai_pdf.py#L257)
- [Tesseract traineddata 官方說明](https://tesseract-ocr.github.io/tessdoc/Data-Files.html)

因此圖版頁會出現英文 OCR 亂碼，且蓋掉原本有效的中文圖說。

### 根因 E：品質閘門只看整份內容，沒有檢查 chunk 保留率

62,809 字讓文件層品質分數很高，管線回報成功；但真正交給 Judge 的只有 1,504 字。現有品質判斷沒有比較：

- 抽取字數與 chunk 字數；
- 有內容的 PDF 頁數與 chunk 覆蓋頁數；
- 單字／單字元重複率；
- base text 與 OCR candidate 的相對品質。

因此「爬取成功」與「可供 Judge 使用」目前是兩個未連接的判斷。

## 建議解決方案

### P0：先修復正文流失，降低上線風險（已完成）

1. 在 `_refresh_attempt_after_clean()` 依 `attempt.resource_type` 分流。
2. PDF 不再經過 `_trim_pipeline_leading_shell()`、`_trim_pipeline_trailing_sections()` 等 HTML 專用規則。
3. 新增 `_clean_pdf_content()`，至少保留 `## 第 N 頁` 與雙換行頁面邊界。
4. PDF chunker 遇到大量短行時，先把相鄰行累積到目標長度，再套用最小 chunk 長度；不得逐行先做 80 字淘汰。
5. 將 `resource_type` 傳進 `_page_to_review_chunks()`，不要以文字特徵猜測 PDF。

這個階段能立即把本案例從 18 個書目 chunk 恢復為正文 chunk。只跳過通用清洗的對照測試已從 18 個增加到 175 個，其中 95 個含已知正文詞；這證明 P0 能止血，但仍會保留頁邊欄污染。

### P1-A：建立正式的 PDF 幾何清洗路徑

建議不要在取得純文字後再猜哪些字是頁眉，而是在 PyMuPDF 還保有幾何資料時處理：

1. 使用 `page.get_text("dict", sort=False)` 取得 block、line、span、bbox、dir。
2. 對每個 block 建立正規化指紋，記錄它在各頁的相對位置。
3. 僅在「頁邊區域 + 多頁重複」時移除頁眉、頁尾、直排側欄，避免誤刪合法的直排古籍正文。
4. 移除位於固定頁邊位置的純頁碼。
5. 依欄位與 block 邊界重建閱讀順序；同一 block 的中文換行應視為軟換行，block 之間保留段落空行。
6. 輸出 chunk 時保留 `page_no`，最好再保留來源 block 的 bbox，方便除錯與日後精準引用。

本次簡化幾何探針已證明這條路徑可把 1,512 個頁邊直排行與 84 個頁碼排除，同時保留 59,803 字主體內容。

### P1-B：修正 OCR 決策與候選選擇

1. 將可列印比率的分母改為非空白字元，或另立 `non_whitespace_text_chars`；不要把版面空白當作亂碼。
2. 有大量有效文字與 text blocks 的頁面，不因存在圖片就自動 OCR。
3. 偵測主文字系；缺少對應 Tesseract 語言包時，不允許低相容 OCR 覆蓋 base text。
4. OCR 結果必須作為 candidate，與 base text 比較字元品質、語言一致性、重複率及有效字數後再選擇。
5. 將 `ocr_attempted_pages` 與 `ocr_selected_pages` 分開記錄；嘗試 OCR 不代表採用 OCR。

### P2：在 chunk 後加入可觀測的品質閘門

至少新增以下診斷：

```text
extracted_chars
chunk_chars
chunk_retention_ratio
text_pages
chunk_covered_pages
page_coverage_ratio
median_source_line_length
repeated_margin_blocks_removed
ocr_attempted_pages
ocr_selected_pages
```

建議條件：

- text-dominant PDF 的 `chunk_retention_ratio < 0.20` 時不得直接標記成功；
- 有效文字頁很多、chunk 覆蓋頁極少時觸發替代抽取或明確失敗；
- OCR candidate 品質低於 base text 時必須保留 base text；
- PyMuPDF 與 pypdf 的 fallback 應比較品質，而不是只在 PyMuPDF 全文少於 80 字時才嘗試。

## 不建議的修法

- 只把 `CHUNK_MIN_CHARS` 從 80 調低：會產生數千個單行／單字 chunk，且側邊欄仍在。
- 只改用 pypdf：仍會失去 bbox 與方向資訊，無法穩定移除頁邊重複欄位。
- 只依 `.pdf` 副檔名分流：本案例正好證明有效 PDF 可能藏在 `.json` 路徑，現有簽名與 Content-Type 偵測才是正確作法。
- 對整份文件強制 OCR：此 PDF 已有可用文字層，會增加延遲並引入語言不匹配亂碼。
- 讓 Judge 自行忽略雜訊：正文已在 Judge 前被刪除，提示詞無法補救不存在的證據。

## 建議回歸測試

1. 建立小型 PDF fixture：三頁中文短行正文、固定直排側欄、頁碼及一頁圖片。
2. 驗證副檔名不是 `.pdf`、但 Content-Type 與 body signature 是 PDF 時仍進入 PDF 分支。
3. 驗證清洗後仍保留頁面／段落邊界，短中文行會先合併再切 chunk。
4. 驗證固定頁邊重複欄位被移除，但非重複直排正文不被誤刪。
5. 驗證有有效文字層的圖片頁不會因版面空白誤觸發 OCR。
6. 驗證缺少 `chi_tra` 時，英文 OCR 不能覆蓋中文 base text。
7. 以本測試網址做 opt-in 整合測試，至少確認：
   - `resource_type == "pdf"`；
   - chunk 數不少於 40；
   - chunk 保留率不少於 60%；
   - 首批 chunk 可找到「青銅器」或「晉公盤」；
   - 不包含連續的頁邊側欄序列；
   - 失敗時回傳具體品質原因，不得以 `success=true` 靜默通過。

## 建議實作順序

1. 先完成 P0 與對應測試，阻止正文再被 80 字門檻清空。
2. 再完成幾何式頁邊清洗，取代純文字後的正則猜測。
3. 同步修正 OCR candidate 選擇，避免低品質 OCR 覆蓋 base text。
4. 最後加入 chunk retention／page coverage 閘門與診斷欄位。

完成標準不是「PDF 回傳成功」，而是「Judge 收到的 chunk 可以追溯到 PDF 主文，而且頁面覆蓋與內容保留率合理」。
