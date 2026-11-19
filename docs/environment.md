# ENV — 密钥只从环境变量读，严禁硬编码

| 变量 | 用途 | 必需 |
|---|---|---|
| `EXA_API_KEY` | Exa 语义检索 | 是 |
| `BRAVE_API_KEY` | Brave 实时索引 | 是 |
| `SERPER_API_KEY` | Serper Google SERP 桥（`/search`，`X-API-KEY`；2500 次一次性免费；付费 $1/千次起、6 个月有效；脚本每请求 ≤10 条 = 1 credit） | 否，缺失即跳过 |
| `MCP_ANYSEARCH_API_KEY` | AnySearch 批量探测 | 是 |
| `FIRECRAWL_API_KEY` | Firecrawl 实时爬取搜索（`/v2/search`，按计划扣 credits，实测约 2/次） | 否，缺失即跳过 |
| `TAVILY_API_KEY` | Tavily 中文覆盖（API 直呼） | 否，失败跳过 |
| `TAVILY_API_KEY_BACKUP` | Tavily 备用密钥；主键遇 432/429/401/402/403（限额耗尽类）自动切换 | 否 |
| `DEEPSEEK_API_KEY` | DeepSeek 官方 Search/Fetch API（搜索引擎 + 抓取第 0 级；`~/.config/ai-keys.env`） | 否，缺失即跳过 |
| `DEEPSEEK_BASE_URL` | DeepSeek API 基址，默认 `https://api.deepseek.com` | 否 |
| `SEARXNG_URL` | 本地 SearXNG，默认 `http://127.0.0.1:8080` | 否 |
| `JINA_API_KEY` | Jina Reranker v3.5 精排（listwise；链式回退 v3 → v2）；缺失即降级 RRF | 否 |
| `JINA_RERANK_MODEL` | 覆盖精排模型（默认 `jina-reranker-v3.5`；408/429/5xx 自动重试一次） | 否 |
| `OLLAMA_URL` | 本地 ollama 基址，`--expand` 查询改写用（默认 `http://127.0.0.1:11434`） | 否 |
| `EXPAND_MODEL` | `--expand` 改写模型（默认 `qwen3.5:9b`；需 `think:false` 支持，用原生 `/api/chat`） | 否 |
| `ANSWER_MODEL` | `--answer` 本地答案模型（默认 `qwen3.5:9b`） | 否 |
| `ANSWER_DS_MODEL` | `--answer deepseek` 的 chat 模型（默认 `deepseek-flash`，`thinking:disabled`） | 否 |
| `CRAWL_PROXY` | 抓取容器出网代理（如 `http://127.0.0.1:7897`，需同时加 `--host-net`），`NO_PROXY="*"` 强制直连（优先级最高），其余回落 `HTTPS_PROXY` | 否 |

来源：`~/.config/agent-env` 与 `~/.config/ai-keys.env` 的 `export`/`KEY=` 行（脚本自行解析）+ 进程 env。交互 TUI 自然继承；
非交互 `opencode run`（脚本/cron）丢 env 时需显式注入。DeepSeek Search/Fetch 按成功请求计费（¥5/千次，QPS 各 5），
402 = 余额不足；408/429/5xx 脚本会自动重试一次（其余 4xx 不重试）。
