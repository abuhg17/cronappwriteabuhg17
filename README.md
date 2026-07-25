# cronappwriteabuhg17

用 GitHub Actions **定時匯出 Appwrite 資料庫**，把完整 JSON 快照寫進本 repo，作為可版本控管的備份。

---

## 運作原理

```text
┌─────────────────┐     cron / dispatch      ┌──────────────────────┐
│ External cron   │ ───────────────────────► │  GitHub Actions      │
│ (optional)      │   repository_dispatch    │  Hourly Appwrite Sync│
└─────────────────┘                          └──────────┬───────────┘
                                                        │
                         schedule :33 & :37 UTC         │
                         workflow_dispatch              ▼
                                               ┌────────────────────┐
                                               │ fetch_appwrite_    │
                                               │ backup.py          │
                                               └─────────┬──────────┘
                                                         │
           ┌─────────────────────────────────────────────┼──────────────────────────┐
           ▼                                             ▼                          ▼
   讀取 Secrets/Vars                          Appwrite REST API              寫入 repo
   APPWRITE_* 或                              GET /databases/{id}/           data/appwrite/
   NEXT_PUBLIC_APPWRITE_*                     collections + documents       latest.json
                                              (cursor 分頁, limit=100)       history/snapshot-*.json
                                                         │
                                                         ▼
                                              contentHash 比對舊 latest
                                              相同 → 不寫新快照
                                              不同 → 寫 latest + history
                                              最後 prune 舊 history
                                                         │
                                                         ▼
                                              git commit + push（有 diff 才 commit）
```

### 資料流（單次 run）

| 步驟 | 做什麼 | 失敗時 |
|------|--------|--------|
| 1. 設定 | 組出一組或多組 `AppwriteConfig`（主設定 + 前端命名備援） | 缺 env 直接 exit 1 |
| 2. 匯出 | 列出全部 collections，再對每個 collection 用 `cursorAfter` 拉完 documents | `project_paused` → warning 並 **exit 0**（略過）；`project_not_found` → 試下一組 config |
| 3. 脫敏 | 遞迴掃字串 / 欄位名，把 API key、token 等改成 `[REDACTED_SECRET]` | — |
| 4. 指紋 | 對「業務內容」（不含 `exportedAt`）做 SHA-256 → `contentHash` | — |
| 5. 落盤 | 與 `latest.json` 比 hash：有變才寫 `latest` + `history/snapshot-時間戳.json` | — |
| 6. landtophistory | UTC **單數小時** 寫入 `landtophistory.json`；**偶數小時** 刪除該檔 | — |
| 7. 保留 | 只留最近 `APPWRITE_HISTORY_KEEP_COUNT` 份 history（預設 336） | — |
| 8. 提交 | workflow 看 `git status data/appwrite`，有變更才 commit/push | concurrency 避免同分支重疊 run |

### 為什麼要雙 cron + 外部觸發？

GitHub Actions 的 `schedule` **不保證準時**，負載高時可能延遲或漏跑。因此：

- 每小時 UTC **:33** 與 **:37** 各排一次，提高命中率  
- 可用外部 cron 打 `repository_dispatch`（event: `external-hourly-sync`）當第二保險  
- `concurrency.group` + `cancel-in-progress: false`：同分支同時觸發時排隊，不互砍  

### 變更偵測為什麼重要？

舊版每次都會改 `exportedAt` 並寫入 history，**資料沒變也會產生 commit 與新 JSON**。  
新版用 `contentHash` 只在 **文件內容真的變了** 時才新增 history，大幅減少無意義 commit 與 repo 膨脹。

### 產出檔案

| 路徑 | 用途 |
|------|------|
| `data/appwrite/latest.json` | 目前最新完整匯出 |
| `data/appwrite/history/snapshot-YYYYMMDDTHHMMSSZ.json` | 有變更時的時間戳歷史（受保留數量上限約束） |
| `data/appwrite/landtophistory.json` | **單數 UTC 小時寫入、偶數 UTC 小時刪除**（見下） |

### landtophistory（奇偶小時切換）

以 **UTC 小時** 判斷（與 GitHub Actions cron 時區一致）：

| UTC 小時 | 行為 |
|----------|------|
| **單數** `1, 3, 5, …, 23` | 寫入 `data/appwrite/landtophistory.json`（完整快照） |
| **偶數** `0, 2, 4, …, 22` | **移除** `landtophistory.json`（檔案不存在則略過） |

每次成功 export 後都會套用此規則，與 `contentHash` 是否變更無關（奇數小時固定寫、偶數小時固定刪）。

快照大致結構：

```json
{
  "exportedAt": "…ISO8601…",
  "projectId": "…",
  "databaseId": "…",
  "collectionCount": 12,
  "contentHash": "sha256…",
  "collections": [
    {
      "collection": { "$id": "…", "name": "…", "attributes": [] },
      "documentsCount": 0,
      "documents": []
    }
  ]
}
```

---

## 必要的 GitHub Secrets / Variables

建議放在 **Secrets**（API key 務必）：

| 名稱 | 說明 |
|------|------|
| `APPWRITE_ENDPOINT` | 例如 `https://….appwrite.io/v1` |
| `APPWRITE_PROJECT_ID` | 專案 ID |
| `APPWRITE_DATABASE_ID` | 要備份的 database |
| `APPWRITE_API_KEY` | 具讀取 DB / collections 權限的 Server API Key |

也可沿用前端常見命名（Secrets 或 Variables 皆可）：

- `NEXT_PUBLIC_APPWRITE_ENDPOINT`
- `NEXT_PUBLIC_APPWRITE_PROJECT_ID`
- `NEXT_PUBLIC_APPWRITE_DATABASE_ID`
- `NEXT_PUBLIC_APPWRITE_API_KEY`

腳本優先用 `APPWRITE_*`；若回 `project_not_found` 再試 `NEXT_PUBLIC_APPWRITE_*`。

---

## 可調環境變數（腳本）

| 變數 | 預設 | 說明 |
|------|------|------|
| `APPWRITE_HISTORY_KEEP_COUNT` | `336` | 保留幾份 history（最舊先刪） |
| `APPWRITE_PAGE_SIZE` | `100` | Appwrite 分頁大小（上限 100） |
| `APPWRITE_HTTP_TIMEOUT` | `60` | 單次 HTTP 逾時（秒） |
| `APPWRITE_HTTP_RETRIES` | `2` | 429/5xx/網路錯誤重試次數 |
| `APPWRITE_EXPORT_DEBUG` | `1` | `0`/`false` 關閉 debug log |
| `APPWRITE_EXPORT_DIR` | `data/appwrite` | 輸出根目錄 |

---

## 本地執行

```powershell
$env:APPWRITE_ENDPOINT="https://your.endpoint/v1"
$env:APPWRITE_PROJECT_ID="your-project-id"
$env:APPWRITE_DATABASE_ID="your-database-id"
$env:APPWRITE_API_KEY="your-api-key"
python scripts/fetch_appwrite_backup.py
```

---

## 外部 cron 觸發範例

```bash
curl -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer YOUR_GITHUB_TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/abuhg17/cronappwriteabuhg17/dispatches \
  -d '{"event_type":"external-hourly-sync"}'
```

Token 放在外部排程服務，不要寫進 repo。與 GitHub schedule 接近時，concurrency 會讓 runs 排隊。

---

## 本次重構重點

1. **`AppwriteClient`**：設定、HTTP、cursor 分頁集中一處，避免參數滿天飛  
2. **`contentHash` 變更偵測**：資料未變不寫新 snapshot、不製造空 commit  
3. **History 保留上限**：預設 336，避免 history 無限累積（目前 repo 內舊檔會在下次成功 run 時被 prune）  
4. **短暫失敗重試**：429 / 5xx / 網路錯誤可重試  
5. **Workflow**：`timeout-minutes: 30`，並帶入保留與重試設定  
6. **脫敏與 paused 略過**：行為保留，避免把 secret 推進 git、專案暫停時不把 CI 標成失敗  
