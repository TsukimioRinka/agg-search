---
name: aggregate-search
description: "全景聚合搜索编排器。支持斜杠命令 /aggregate-search 及多引擎并行召回。并行调用 Exa、Brave、Serper(Google)、DeepSeek、Firecrawl、AnySearch、Tavily、SearXNG，用 RRF 粗排 + Jina v3.5 精排合并去重，支持 DeepSeek Fetch 直取 + 容器化正文深度抓取。"
triggers:
  - "/aggregate-search"
  - "/hermes-skills:aggregate-search"
  - "聚合搜索"
  - "多引擎搜索"
  - "全景检索"
---

# Aggregate Search (全景聚合搜索)

跨多引擎的**并行召回 + 两阶段重排（RRF 粗排 + Jina 精排）**搜索能力，专为 Antigravity / `agy`、OpenCode、Codex 等 Agent 生态设计。

**触发条件（满足任一即启用）：**
- 用户输入斜杠命令 `/aggregate-search` 或 `/hermes-skills:aggregate-search`
- 用户明确说“聚合搜索”、“多引擎搜索”、“全景检索”、“帮我搜一下”
- 单一搜索源信息不全或需要时效性多源校验

---

## 核心工作流 (Workflow)

### 1. 搜索召回 (Recall)
客户端已接入 MCP 时优先用工具 `search` / `fetch`（stdio 或 HTTP，协议 2025-06-18，参数与下表一一对应）；没有 MCP 时用 CLI：

```bash
agg-search "<query>" [--top-n 10] [--recency day|week|month|year] [--no-jina] [--expand N] [--answer [deepseek]] [--json] [--fast] [--budget-secs 30] [--engine-timeout 25] [--include-domains arxiv.org,.edu] [--exclude-domains reddit.com] [--snippet-chars 400] [--no-cache] [--no-cooldown] [--no-diversify] [--engines exa,brave,serper,deepseek,firecrawl,anysearch,tavily,searxng]
agg-search --show-config      # 生效配置 + 各引擎密钥是否就绪（只输出布尔值）
```
或直接用模块方式执行（无需安装）：
```bash
python3 -m agg_search.search "<query>" [--top-n 10]
```
- **并发检索**：同时并发请求 Exa、Brave、Serper、DeepSeek、Firecrawl、AnySearch、Tavily、SearXNG。单引擎超时或报错不阻塞其他通道。
- **韧性层（默认开启，可旁路）**：5分钟磁盘结果缓存（`fast`与全量模式键隔离，命中约 0.06s，`--no-cache` 旁路）；失败分类冷却——额度类1h/反爬10min/超时90s，健康空结果永不冷却（`--no-cooldown` 旁路；`record` 直调 recall 本来就不经过冷却/缓存）；缺key引擎预过滤不占线程；SearXNG上游`unresponsive_engines`随状态上报（缓存命中时沿用入库快照）。`--json` 新增 `engine_health`（`cached/diversify/distinct_domains/searxng_unresponsive/recall_budget/fast`）字段，旧字段不变。
- **`--fast`（默认关闭）**：单池扇出+30s召回预算（`--budget-secs` 调，覆盖改写+召回；Jina另计8s单尝试）+最早2个有结果引擎即返（`--min-sources` 调，默认2，显式0=等全引擎；未达标只记 `recall_budget` 不失败；空结果不计源）；`--expand` 自动封顶3；Jina只跑首选模型。默认行为保持等全引擎+Jina全链（408/429/5xx重试一次）。
- **Serper 引擎（Google 索引桥）**：`POST https://google.serper.dev/search`（`X-API-KEY`），与 SearXNG 的 `google cse` 通道互为备份，补足其他引擎的共同盲区；`--recency` 映射 `tbs=qdr:d/w/m/y`；注册送 2500 次**一次性**免费额度（非每月刷新），之后 credit pack $1/千次起（$50/5 万次），付费 credits **6 个月有效**。脚本固定每请求 ≤10 条 → **每次恰好 1 credit**（>10 条会双倍计费，已规避）。缺 `SERPER_API_KEY` 时自动跳过。
- **DeepSeek 引擎**：官方自研搜索引擎（`POST https://api.deepseek.com/v1/tools/search`，QPS 5，¥5/千次成功请求）。返回自带 `rerank_score`（0–3，含权威度与时效性）的结果，脚本按该分数预排序后再进 RRF；`--recency` 会自动翻译为 `after:YYYY-MM-DD` 查询算子；缺 `DEEPSEEK_API_KEY` 时自动跳过本引擎。
- **Firecrawl 引擎**：实时爬取型搜索（`POST https://api.firecrawl.dev/v2/search`），无自建索引、现抓现解析，与索引型引擎互补、擅长极新鲜内容；`--recency` 映射 `tbs=qdr:d/w/m/y`；按计划扣 credits（实测约 2 credits/次）。
- **查询扩展 `--expand N`（RAG-Fusion，默认关闭）**：`N≥2` 时用本地 ollama（原生 `/api/chat`，`think:false`，默认模型 `qwen3.5:9b`，约 3 秒）把查询改写成 N-1 个变体，每个变体各自跑全部所选引擎，所有 `引擎×变体` 排名列表作为独立榜单进 RRF（多路共识加权）。引擎成本 ×N；改写失败/ollama 离线时自动退回单查询，绝不阻塞。
- **答案层 `--answer [deepseek]`（默认关闭）**：取 Top 结果标题+摘要，在输出顶部生成带 `[n]` 引用的综合答案（`## Answer` 段，列表原样保留）。裸 `--answer` 走本地 ollama（0 元，约 5–15s）；`--answer deepseek` 走 chat API（`deepseek-flash`，`thinking:disabled`，约 2–6s，约 ¥0.003–0.012/次）。生成失败静默跳过。
- **机器可读 `--json`**：输出 `{query, engines, expand, rerank, answer, results:[{rank,title,url,snippet,engines,rrf}]}`，供脚本/agent 零解析消费；文本模式不变。
- **两阶段排序**：各引擎前 Top 5 结果汇聚后通过倒数排名融合（RRF）选出前 15 名候选，再经 **Jina Reranker v3.5**（listwise，链式回退 v3 → v2）语义重排输出最终 Top 10；遇 408/429/5xx 自动重试一次，仍失败则平滑降级为 RRF 顺序（无 Jina Key 同样降级），绝不阻塞结果。融合前默认做 eTLD+1 域多样性轮转（前5纯RRF保头、尾部轮转后再截断，`--no-diversify` 关闭；20条基线共识召回无损、top-15域数+1.3）。
- 排序数学原理可参考：`docs/reranking.md`。

### 2. 网页正文抓取 (Fetch - 仅在需要完整正文时使用)
对于搜索返回的高价值链接，若需要提取正文内容（绝不用于简单摘要）：
```bash
crawl-fetch --url <url...> [--tier auto|deepseek|static|dynamic|stealth] [--no-deepseek] [--out json|md]
```
或直接用模块方式执行：
```bash
python3 -m agg_search.fetch --url <url...>
```
- **DeepSeek Fetch 直取（阶梯第 0 级）**：配置了 `DEEPSEEK_API_KEY` 时，`auto` 先用官方 Fetch API（`POST /v1/tools/fetch`）在服务端取页并直接返回干净 Markdown —— 单次 HTTP 调用、约 1 秒、无需容器，且出口 IP 在服务商侧（可绕开本机网络/地域限制）。返回 403/404、内容过薄或超时会静默落入下方容器阶梯；`--tier deepseek` 只跑这一级，`--no-deepseek` 在 auto 中跳过它。
- **容器隔离抓取**：`static`/`dynamic`/`stealth` 等抓取任务在轻量 `crawl-agent` Docker 容器内执行，宿主机无任何爬虫残留或浏览器依赖。
- **三级自适应阶梯**：`auto` 模式自动按 `static` -> `dynamic` -> `stealth` 升级以绕过反爬封锁。
- 容器与抓取机制详见：`docs/fetching.md`。

### 3. 结果呈现与去重
- 展示结果去重，保留来源引擎归属（例如 `via exa+brave`）与排名分数。
- 过滤无效死链并提炼核心信息呈现给用户。

---

## Antigravity 原生 MCP 工具降级对照

当所在环境受限无法执行外部 Bash 脚本时，可直接调用 Antigravity 中已挂载的官方 MCP 搜索工具：

| 引擎槽位 | Antigravity 实际 MCP 工具名 | 典型用途 |
|---|---|---|
| **Exa** | `exa` → `web_search_exa` (正文提取用 `web_fetch_exa`) | 英文技术文档、学术论文、权威来源 |
| **Brave** | `brave-search` → `brave_web_search` | 即时新闻、热点事件、中文社区 |
| **AnySearch** | `anysearch` → `batch_search` / `search` (正文提取用 `extract`) | 多关键词批量并发探测 |
| **DeepSeek** | 无 MCP：HTTP 直呼 `POST https://api.deepseek.com/v1/tools/search`（Bearer `DEEPSEEK_API_KEY`） | 中文/权威源、时效校验、自带打分结果 |
| **SearXNG** | `searxng` → `searxng_web_search` | 本地自托管无配额限制引擎，熔断兜底 |

---

## 环境变量配置

所有引擎密钥由系统级环境变量注入，脚本内部自动读取进程环境并兜底回落至 `~/.config/agent-env` 与 `~/.config/ai-keys.env`：
- `EXA_API_KEY`: Exa 检索密钥
- `BRAVE_API_KEY`: Brave Search 密钥
- `SERPER_API_KEY`: Serper（Google SERP 桥）密钥（可选；缺失时跳过该引擎；serper.dev 注册送 2500 次）
- `MCP_ANYSEARCH_API_KEY`: AnySearch 并发搜索密钥
- `FIRECRAWL_API_KEY`: Firecrawl 实时爬取搜索密钥（可选；缺失时跳过该引擎）
- `TAVILY_API_KEY`（+ `TAVILY_API_KEY_BACKUP` 限额耗尽自动切换）: Tavily 中文及摘要补充（可选）
- `DEEPSEEK_API_KEY`: DeepSeek Search/Fetch API 密钥（可选；缺失时搜索跳过该引擎、抓取跳过第 0 级）
- `DEEPSEEK_BASE_URL`: DeepSeek API 基址（可选，默认 `https://api.deepseek.com`）
- `SEARXNG_URL`: 本地自托管 SearXNG 实例地址（默认 `http://127.0.0.1:8080`）
- `JINA_API_KEY`: Jina Rerank 精排密钥（可选，缺失时降级为纯 RRF）
- `JINA_RERANK_MODEL`: 精排模型覆盖（可选，默认 `jina-reranker-v3.5`；链式回退 `v3` → `v2-base-multilingual`）
- `OLLAMA_URL`: 本地 ollama 基址（可选，默认 `http://127.0.0.1:11434`；`--expand` 的改写器）
- `EXPAND_MODEL`: 改写模型（可选，默认 `qwen3.5:9b`）
- `ANSWER_MODEL`: `--answer` 本地答案模型（可选，默认 `qwen3.5:9b`）
- `ANSWER_DS_MODEL`: `--answer deepseek` 的 chat 模型（可选，默认 `deepseek-flash`）
- `CRAWL_PROXY`: 容器爬虫出网代理配置（可选）
