# Antigravity Adapter — MCP 槽位 → Antigravity 實際工具名

脚本路径在任何 harness 下通用（纯 python3 + docker），优先用脚本。
实在要用 MCP 直调时对照下表：

| 槽位 | Antigravity 實際 MCP 工具 | 用途 |
|---|---|---|
| 〈exa.semantic〉 | `exa` → `web_search_exa` / `web_fetch_exa` | 英文技術文檔、官方資源；取全文走 fetch |
| 〈brave.web〉 | `brave-search` → `brave_web_search`（`brave_local_search` 仅本地商家） | 中英新聞、社區動態 |
| 〈anysearch.batch〉 | `anysearch` → `batch_search` / `search`；全文走 `extract`，子域走 `get_sub_domains` | 多關鍵詞批量並行探測 |
| 〈searxng.agg〉 | `searxng` → `searxng_web_search`（备选 `web_url_read`） | 自託管聚合，429/402 熔斷備胎 |
| Tavily | 無 MCP，直呼 API | 中文覆蓋 + 摘要，失敗跳過（主鍵限額耗盡自動切 `TAVILY_API_KEY_BACKUP`） |
| Serper | 無 MCP，直呼 `POST https://google.serper.dev/search`（`X-API-KEY`） | 唯一的 Google 索引通道，補其他引擎共同盲區 |
| Firecrawl | 無 MCP，直呼 `POST /v2/search`（腳本內置） | 實時爬取型搜索，補索引型引擎的「極新鮮內容」盲區 |
| DeepSeek | 無 MCP，直呼 `POST /v1/tools/search`（腳本內置） | 官方自研引擎、自帶打分；抓取走 `/v1/tools/fetch`（第 0 級） |
| Jina rerank | 無 MCP，`scripts/agg_search.py` 内置（需 `JINA_API_KEY`） | 精排 |
| 爬蟲正文 | 無 MCP 一一對應 | 一律走 `scripts/crawl_fetch.py`（容器），不用冒充任意 URL-reader MCP |

密钥：`~/.config/agent-env` 为主（脚本回退链亦读 `~/.config/ai-keys.env`）。
