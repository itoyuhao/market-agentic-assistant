# market-agentic-assistant

每日美股 + 台股 + 宏觀指標 digest，純資訊聚合、**不做買賣建議**。資料層走
yfinance / FRED / NewsAPI 抓 raw signals，agent 層用 `claude-agent-sdk`
把結構化資料轉成繁體中文 markdown，落地到 `digests/YYYY-MM-DD.md`。

寫給未來的自己。如果你在新機器上、或半年後忘記怎麼動了，從這裡開始。

---

## Setup

需求：Python 3.12+、`claude` CLI（Claude Code）已登入、Asia/Taipei 時區的 macOS。

```bash
# clone 後
cd market-agentic-assistant
python3.12 -m venv .venv
.venv/bin/pip install -e .

# .env （複製 .env.example 後填入兩支金鑰）
#   FRED_API_KEY    https://fred.stlouisfed.org/docs/api/api_key.html
#   NEWSAPI_KEY     https://newsapi.org
```

驗證 claude CLI 確實在 PATH 裡（LaunchAgent 啟動時會找這個）：
```bash
which claude && claude --version
```

---

## 跑一次（手動）

```bash
.venv/bin/python scripts/daily_run.py
```

預期：fetcher 階段約 15-30 秒、compose 階段約 30-60 秒，最後寫到
`digests/YYYY-MM-DD.md`（同日重跑會覆寫）。

各 fetcher 也能獨立執行驗證，例如 `python fetchers/market.py` 或
`python fetchers/macro.py` 印 Rich 表格。

---

## 每日自動執行（LaunchAgent）

排程預設 Tue-Sat 07:30 Asia/Taipei——抓 Mon-Fri 美股收盤、台股開盤前 1.5 小時可讀。

```bash
# 安裝
bash scripts/launchd/install.sh

# 不等到明早，立刻觸發一次驗證
launchctl kickstart -k gui/$UID/com.itoyuhao.market-digest

# 看 log（fetcher 進度、claude-cli 訊息、錯誤都在 stderr）
tail -f digests/.logs/launchd-stderr.log

# 移除
bash scripts/launchd/uninstall.sh
```

Mac 在 07:30 睡著也沒關係——launchd 會在下次喚醒時自動補跑。如果想讓
Mac 主動醒來，再加一條 `sudo pmset repeat wakeorpoweron TWRFS 07:25:00`，
但通常用不到。

要改時間/頻率：編輯 `scripts/launchd/com.itoyuhao.market-digest.plist` 裡
的 `StartCalendarInterval`，重跑 `install.sh` 覆蓋。Weekday 數字是
Sun=0, Mon=1, ..., Sat=6。

---

## 專案結構

```
agent/
  digest_agent.py        # claude-agent-sdk 呼叫入口；options 刻意隔離 plugin
  prompts/digest.md      # system prompt（6 章節結構 + 觀察性語氣）
fetchers/
  market.py              # yfinance：美股/台股 watchlist
  macro.py               # FRED：11 個宏觀指標
  news.py                # NewsAPI：48h 內 watchlist 相關新聞
  twstock_extra.py       # 台股三大法人（目前 stub，待 observe-first 期結束再決定來源）
config/
  watchlist.yaml         # 9 個 ticker（7 美 + 3 台）
  macro_indicators.yaml  # 11 個 FRED series
scripts/
  daily_run.py           # orchestrator：sync fetch → async compose
  launchd/               # macOS LaunchAgent 安裝套件
digests/                 # 每日輸出（gitignored，僅 .gitkeep 入版本）
  .logs/                 # launchd stdout/stderr（gitignored）
```

---

## 關鍵設計原則

**Fetcher / agent 嚴格分層。** Fetchers 只抓資料、做純數值衍生（漲跌幅、52W
position、20 日均量比）。Agent 只做文本生成、`allowed_tools=[]`，連檔案
讀寫都不給。任何「邏輯判斷」都該在 fetcher 層完成、用結構化欄位餵給 agent。

**觀察性語言。** Digest 嚴格描述觀察到的現象，不寫「推測」「可能受 X
影響」「應該是」這類因果連結語，即使相關性直覺上很明顯。價值來自把 raw
signals 攤出來給人類解讀（例如 TGA / RRP / 淨流動性同向擺著讓你自己連點），
不是替人下結論。詳見 `agent/prompts/digest.md`。

**資料缺失絕不湊。** Fetcher 失敗 → snapshot 的 `error` 欄位填訊息、其餘
數值欄位 `None`。Digest 看到 error 非空就標 `[資料缺失]`，不用舊資料補。

**SDK 隔離。** `claude-agent-sdk` 預設會吃 user/project settings、
SessionStart hooks、MCP servers。本專案的 Claude Code 環境裝了
superpowers plugin（~22k tokens 的 hook）會污染 agent 行為，所以
`ClaudeAgentOptions` 明確傳 `allowed_tools=[]`。如果未來想試
`setting_sources=[]` 完全隔離、注意過去測過會 silent crash CLI（CLI 2.1.119
+ SDK 0.1.68），需重新驗證。

---

## Troubleshooting

`✗ Digest composition failed: CLINotFoundError` — 確認 plist 裡
`EnvironmentVariables/PATH` 有包含 `which claude` 的目錄（預設
`/Users/yuhao/.local/bin`）。改完重跑 `install.sh`。

`✗ Digest composition failed: ProcessError: exit code 1` — 通常是 Claude
Code 認證問題或 plugin 衝突。手動 `claude` 在 terminal 裡跑一次確認登入
狀態，再看 `digests/.logs/launchd-stderr.log` 有沒有 `[claude-cli]` 開頭的
具體錯誤行。

`RuntimeError: Claude returned an empty response` — 通常 prompt 大小爆掉
或被拒答。檢查 `_build_user_prompt` 序列化出來的 JSON 大小。

`✗ Fetcher phase failed: Missing env: [...]` — `.env` 沒載到。確認檔案
在 repo root、且 `load_dotenv` 走的路徑正確。

排程跑了但沒新檔案：先看 `digests/.logs/launchd-stderr.log` 的時間戳，
再 `launchctl print gui/$UID/com.itoyuhao.market-digest` 看 last exit
status。
