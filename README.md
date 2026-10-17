# 市場情報雷達 Market Signal

保險商品企劃使用的市場情報網站。系統每天從公開來源抓取保險市場資訊，依八大保險類別分類、去重、關聯事件，形成市場訊號，並評估可信度、重大性、訊號狀態與資料完整度。

## 運作方式

```
GitHub Actions（每天台灣時間 06:00）
  └─ pipeline/update.py
       1. 抓取資料：金管會保險局、中央社、壽險公會、保險代理人公會新聞彙整
       2. 八大分類（關鍵字詞庫）→ 去重 → 原始資料品質檢核
       3. 形成市場訊號：有 Claude API 金鑰時由 Claude 判讀；沒有時改用規則分析
       4. 寫入 data/signals.json 並提交回 repo
網站 index.html 讀取 data/signals.json 顯示
```

| 來源 | 抓取內容 |
|---|---|
| 金管會保險局 | 新聞稿、重要公告（含內文） |
| 中央社 | 依八大類別關鍵字站內搜尋（含內文） |
| 壽險公會 | 最新消息、重要政策 |
| 保險代理人公會 | 各大網站新聞彙整（連到原始媒體） |

- 資料保留近 365 天；形成訊號時以近 180 天的事件為主。
- 時間判斷一律以發布日期為準，較晚才收錄的舊資料會排回正確的時間位置。
- 某個來源暫時無法連線時（例如網站維護），既有資料保留，網站「評估方法」頁會顯示該來源的狀態。
- 抓取時每次請求間隔 1 秒，避免對來源網站造成負擔。

## 初次設定

1. **開啟 GitHub Pages**：Settings → Pages → Source 選 `Deploy from a branch`，分支 `main`、資料夾 `/ (root)`。
2. **允許 Actions 寫入**：Settings → Actions → General → Workflow permissions 選 `Read and write permissions`。
3. **（建議）設定 Claude API 金鑰**：Settings → Secrets and variables → Actions → New repository secret，名稱 `ANTHROPIC_API_KEY`。
   沒有設定時系統仍會運作，但訊號改由關鍵字規則產生，方向判斷較粗略。
4. **第一次執行**：Actions → 「更新市場資料」→ Run workflow。之後每天自動執行。

## 本機執行

```bash
pip install -r pipeline/requirements.txt
python pipeline/update.py              # 抓取並分析
python pipeline/update.py --no-fetch   # 只用已收錄的資料重新分析
python pipeline/update.py --no-ai      # 不呼叫 Claude
```

網站需要透過 HTTP 開啟才讀得到資料檔，例如在專案根目錄執行 `python -m http.server`，再開啟 http://localhost:8000 。直接雙擊 `index.html` 時會顯示企劃書的示範資料。

## 檔案

| 路徑 | 說明 |
|---|---|
| `index.html` | 網站（儀表板、訊號詳細頁、原始資料、評估方法） |
| `pipeline/config.py` | 來源、八大分類詞庫、搜尋關鍵字、時間範圍等設定 |
| `pipeline/sources.py` | 各來源的抓取程式 |
| `pipeline/analyze.py` | 分類、去重、品質檢核、規則分析與 Claude 分析 |
| `pipeline/update.py` | 每日更新的主程式 |
| `data/articles.json` | 累積收錄的原始資料 |
| `data/signals.json` | 網站讀取的分析結果 |
| `.github/workflows/update-data.yml` | 每日排程 |

## 調整

- 新增或修改分類關鍵字：編輯 `pipeline/config.py` 的 `CATEGORIES`。
- 中央社搜尋的主題：編輯 `CNA_QUERIES`。
- 資料保留天數與訊號時間範圍：`LOOKBACK_DAYS`、`SIGNAL_WINDOW_DAYS`。
- 使用的 Claude 模型：`AI_MODEL`。每次分析約送出數百項事件，費用依當天事件量而定。
