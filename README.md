# 投資組合儀表板（Portfolio Dashboard）

個人化投資組合追蹤、歷史資產回溯、樂活五線譜（含 `-3SD` / `-4SD`）、市場情緒指標（CNN Fear & Greed、CBOE VIX、Nasdaq 100 Forward P/E）與借貸攤銷儀表板。

本專案採用 **雙倉儲隔離 + 前端加密（方案 B）** 架構：

- **公開程式碼倉儲（本 repo：`vicabon/portfolio`）**：僅包含程式、設定檔、測試與部署流程，**不含任何個人持股、負債或靜態快照檔案**。
- **私有資料倉儲（`vicabon/portfolio_private_data`）**：僅存放個人 `portfolio.json`、`2025portfolio.json`、`debt.json`。
- **公開 GitHub Pages（`https://vicabon.github.io/portfolio/`）**：由 GitHub Actions 透過唯讀 Deploy Key 讀取私有資料建置後，以 **PBKDF2-HMAC-SHA256（600,000 次疊代）+ AES-256-GCM** 將所有個人資產、歷史與借貸資料加密成 `window.ENCRYPTED_DATA`，僅發佈 `dist/`（`index.html`、`core.js`），開啟網頁需輸入解鎖密碼於瀏覽器本機離線解密。

---

## 安全架構與建置管線（方案 B）

```text
vicabon/portfolio (Public)          vicabon/portfolio_private_data (Private)
  ├─ app.py / portfolio_data.py       ├─ portfolio.json
  ├─ build_static.py                  ├─ 2025portfolio.json
  ├─ validate_static.py               └─ debt.json
  ├─ crypto_static.py                       │
  └─ .github/workflows/deploy.yml           │ (Read-only Deploy Key)
                 │                          ▼
                 └────────► GitHub Actions Build Job
                              1. Offline pytest + Node 3-TZ golden + WebCrypto interop
                              2. build_static.py --data-dir _private_data --debt _private_data/debt.json
                              3. validate_static.py (明文資料品質閘門)
                              4. crypto_static.py encrypt (AES-256-GCM + PBKDF2-SHA256 600k)
                              5. validate_static.py --encrypted --no-leak-from _private_data
                              6. crypto_static.py verify (記憶體解密往返驗證)
                              7. 只上傳 dist/{index.html, core.js, .nojekyll} → GitHub Pages
```

### 必要 GitHub Secrets（設於 `vicabon/portfolio`）

| Secret 名稱 | 用途 | 權限範圍 |
| --- | --- | --- |
| `PRIVATE_DATA_SSH_KEY` | Ed25519 私鑰（對應公鑰為 `vicabon/portfolio_private_data` 的唯讀 Deploy Key） | 僅能唯讀 `portfolio_private_data` |
| `SITE_PASSPHRASE` | 靜態頁 AES-256-GCM 加密密碼（瀏覽器開啟 `github.io` 時輸入） | 至少 8 字元，建議高強度隨機密碼 |

### 更換網頁解鎖密碼

隨時可透過 GitHub CLI 或 GitHub 網頁（`Settings → Secrets and variables → Actions → SITE_PASSPHRASE`）更換密碼，不需改動任何程式碼：

```bash
gh secret set SITE_PASSPHRASE -R vicabon/portfolio
gh workflow run deploy.yml -R vicabon/portfolio --ref main
```

---

## 本機開發與執行

### 1. 目錄配置

建議將兩個倉儲放在同一層目錄（`app.py` 會自動偵測相鄰的 `../portfolio_private_data`，亦可用環境變數 `PORTFOLIO_DATA_DIR` 指定）：

```text
workspace/
├── portfolio/               # 本倉儲（公開程式碼）
└── portfolio_private_data/  # 私有資料（portfolio.json, 2025portfolio.json, debt.json）
```

若僅測試範例資料，可將 `*.example.json` 複製到本機暫存目錄並設定 `PORTFOLIO_DATA_DIR`。

### 2. 啟動本機 Flask 模式

```bash
pip install -r requirements-dev.txt
python app.py
# 瀏覽器開啟 http://127.0.0.1:5000
```

### 3. 測試與部署前檢查

```bash
# 離線測試（Python 單元測試 + core.js 三時區 golden + WebCrypto 解密互通）
scripts/dev.sh test

# 完整檢查（離線測試 → 建置到暫存目錄 → 資料品質閘門）
scripts/dev.sh check
```

---

## 排程與監控

GitHub 內建 `schedule`（`cron: '17 * * * *'`）為盡力而為排程，尖峰時段可能被延遲或略過。若需穩定每小時更新並監控快照新鮮度，可在常開機環境設定：

- 外部觸發：[scripts/trigger_deploy.sh](scripts/trigger_deploy.sh)
- 逾期監控：[scripts/check_deploy_age.py](scripts/check_deploy_age.py)
- 成效量測計畫：[docs/EVALUATION.md](docs/EVALUATION.md)
