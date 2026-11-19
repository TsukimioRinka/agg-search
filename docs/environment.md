# ENV — 密钥只从环境变量读，严禁硬编码

| 变量 | 用途 | 必需 |
|---|---|---|
| `EXA_API_KEY` | Exa 语义检索 | 否，缺失即跳过 |
| `BRAVE_API_KEY` | Brave 实时索引 | 否，缺失即跳过 |
| `SERPER_API_KEY` | Serper Google SERP 桥（`/search`，`X-API-KEY`；2500 次一次性免费；付费 $1/千次起、6 个月有效；脚本每请求 ≤10 条 = 1 credit） | 否，缺失即跳过 |
| `MCP_ANYSEARCH_API_KEY` | AnySearch 批量探测 | 否，缺失即跳过 |
| `FIRECRAWL_API_KEY` | Firecrawl 实时爬取搜索（`/v2/search`，按计划扣 credits，实测约 2/次） | 否，缺失即跳过 |
| `TAVILY_API_KEY` | Tavily 中文覆盖（API 直呼） | 否，失败跳过 |
| `TAVILY_API_KEY_BACKUP` | Tavily 备用密钥；主键遇 432/429/401/402/403（限额耗尽类）自动切换 | 否 |
| `DEEPSEEK_API_KEY` | DeepSeek 官方 Search/Fetch API（搜索引擎 + 抓取第 0 级；`~/.config/ai-keys.env`） | 否，缺失即跳过 |
| `DEEPSEEK_BASE_URL` | DeepSeek API 基址，默认 `https://api.deepseek.com` | 否 |
| `SEARXNG_URL` | 本地 SearXNG，默认 `http://127.0.0.1:8080` | 否 |
| `OPENALEX_API_KEY` | OpenAlex 学术检索的 key（免 key 也能用，额度按 IP 共享） | 否 |
| `OPENALEX_MAILTO` | OpenAlex polite pool 邮箱（更高优先级/额度） | 否 |
| `STACKEXCHANGE_SITE` | Stack Exchange 站点，默认 `stackoverflow`（可换 `serverfault`、`askubuntu` 等） | 否 |
| `STACKEXCHANGE_KEY` | Stack Exchange key（免 key 每日 300 次/IP） | 否 |
| `JINA_API_KEY` | Jina Reranker v3.5 精排（listwise；链式回退 v3 → v2）；缺失即降级 RRF | 否 |
| `JINA_RERANK_MODEL` | 覆盖精排模型（默认 `jina-reranker-v3.5`；408/429/5xx 自动重试一次） | 否 |
| `OLLAMA_URL` | 本地 ollama 基址，`--expand` 查询改写用（默认 `http://127.0.0.1:11434`） | 否 |
| `EXPAND_MODEL` | `--expand` 改写模型（默认 `qwen3.5:9b`；需 `think:false` 支持，用原生 `/api/chat`） | 否 |
| `ANSWER_MODEL` | `--answer` 本地答案模型（默认 `qwen3.5:9b`） | 否 |
| `ANSWER_DS_MODEL` | `--answer deepseek` 的 chat 模型（默认 `deepseek-flash`，`thinking:disabled`） | 否 |
| `CRAWL_PROXY` | 抓取容器出网代理（如 `http://127.0.0.1:7897`，需同时加 `--host-net`），`NO_PROXY="*"` 强制直连（优先级最高），其余回落 `HTTPS_PROXY` | 否 |
| `AGG_SEARCH_CACHE_DIR` | 覆盖缓存根目录（默认 `~/.cache/agg-search`；结果缓存、冷却状态、页面缓存、反馈计数都在这里） | 否 |
| `AGG_SEARCH_CONFIG_DIR` | 覆盖调优权重目录（默认 `~/.config/agg-search`，`weights.json` 在此） | 否 |
| `AGG_SEARCH_PROFILE` | 默认领域画像：`general` / `academic` / `code` / `news`（显式 `--profile` 优先） | 否 |
| `AGG_SEARCH_NO_LEARN` | `1/true/yes` 关闭反馈读写（等价 `--no-learn`） | 否 |
| `AGG_SEARCH_ALLOW_PRIVATE` | `1/true/yes` 让抓取允许私网/回环目标（服务端策略；MCP 调用方无法逐次开启；云元数据地址始终拒绝） | 否 |
| `AGG_SEARCH_ALLOW_CIDRS` | 逗号分隔的允许网段（TUN 代理的 `198.18.0.0/15` fake-IP 需要它；元数据地址不受影响） | 否 |
| `AGG_SEARCH_ENGINES` | 默认引擎列表（逗号分隔；显式 `--engines` 优先） | 否 |
| `AGG_SEARCH_TOP_N` | 默认返回条数 | 否 |
| `AGG_SEARCH_PER_ENGINE` | 每引擎召回条数 | 否 |
| `AGG_SEARCH_SNIPPET_CHARS` | 输出摘要长度（80–2000） | 否 |
| `AGG_SEARCH_FAST` | `1/true/yes` 默认开启 fast 召回 | 否 |
| `AGG_SEARCH_NO_JINA` | `1/true/yes` 默认跳过精排 | 否 |

来源：`~/.config/agent-env` 与 `~/.config/ai-keys.env` 的 `export`/`KEY=` 行（脚本自行解析）+ 进程 env。交互 TUI 自然继承；
非交互 `opencode run`（脚本/cron）丢 env 时需显式注入。DeepSeek Search/Fetch 按成功请求计费（¥5/千次，QPS 各 5），
402 = 余额不足；408/429/5xx 脚本会自动重试一次（其余 4xx 不重试）。
