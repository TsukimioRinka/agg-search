# 领域画像与本地反馈回路

两条互不干扰的能力：

1. **profile** — 按领域预设「用哪些引擎、各自权重、默认时效、摘要长度」。搜索逻辑是**数据**，换 profile 就是换策略，不用改代码。
2. **feedback** — 记录哪些引擎召回的页面被真正读过，`agg-search-tune` 把这些统计折算成小的权重乘数，之后每次搜索自动生效。

## 内置 profile

```bash
agg-search --list-profiles
agg-search "speculative decoding" --profile academic
agg-search "sqlite wal corruption" --profile code
agg-search "openai board" --profile news --top-n 5
```

| profile | 引擎（按先后） | 权重覆盖 | 其他默认 |
|---|---|---|---|
| `general` | 全部已配置引擎 | 无 | 与裸命令一致 |
| `academic` | `openalex, exa, deepseek, brave, tavily` | openalex 1.8 / exa 1.3 / deepseek 1.1 | 摘要 500 字，top-n 15 |
| `code` | `stack, brave, serper, deepseek, exa` | stack 1.8 / brave 1.2 / serper 1.1 | 摘要 600 字 |
| `news` | `brave, serper, tavily, firecrawl` | brave 1.3 / serper 1.2 / tavily 1.1 | `recency=day`，摘要 300 字 |
| `platform` | `ar_github, ar_youtube, ar_bilibili, brave, searxng` | ar_github 0.8 / ar_youtube 0.6 / ar_bilibili 0.5 | 摘要 400 字；`fast` 模式至少等一个平台源返回；平台源经可选 Agent-Reach CLI，未安装时自动跳过 |

优先级：**显式 flag > `AGG_SEARCH_*` 环境变量 > profile > 内置默认**。所以 `--profile academic --engines openalex` 只跑一个引擎，`--profile news --recency week` 会覆盖 1 天窗口。

以下引擎**不需要 key**：

| 引擎 | 数据源 | 可选环境变量 |
|---|---|---|
| `openalex` | OpenAlex 学术作品（含摘要重建、DOI 链接） | `OPENALEX_API_KEY`（更高日额度）、`OPENALEX_MAILTO`（polite pool） |
| `hn` | Hacker News（Algolia API） | — |
| `stack` | Stack Exchange 问答 | `STACKEXCHANGE_SITE`（默认 `stackoverflow`；逗号列表可多站点，见 environment.md）、`STACKEXCHANGE_KEY` |
| `arxiv` | arXiv 预印本（Atom API，只用拉丁字母关键词做 AND 检索） | —（不进默认引擎；作为 `academic` 门户的免 key 补召源） |
| `ar_github` / `ar_youtube` / `ar_bilibili` | Agent-Reach 平台通道（GitHub 仓库/Issue、YouTube、B站） | `AGENT_REACH_BIN`（默认 `agent-reach`）、`AGENT_REACH_TIMEOUT`（默认 8s）；需另装 [Agent-Reach](https://github.com/Panniantong/Agent-Reach) |

新闻垂直引擎复用已有 key：`brave_news`（Brave News，`BRAVE_API_KEY`）、`serper_news`（Google News，`SERPER_API_KEY`），摘要前缀是服务商给的发布日期，例如 `[2026-11-20]` 或 `[4 hours ago]`。它们**不在** `news` 默认引擎里（0.7.0 曾改为默认，0.9.0 撤回，见 `docs/benchmarks.md` 候选 J）。想要最高质量可叠加：`--profile news --engines brave,serper,tavily,firecrawl,brave_news,serper_news`（42 条评测 nDCG 0.806 → 0.836，每次多 2 个付费请求）。

门户的免 key 补召源：`academic` = `openalex, arxiv, searxng`；`code` = `stack, searxng, hn`；`news` = `hn, searxng`；`platform` = `searxng, hn`。评测数据见 `docs/benchmarks.md` 候选 G。

## 每个 agent 一套搜索逻辑

profile 是配置，不需要改代码，也不需要在每个 agent 里写分支：

```bash
# 把 Cursor 钉在 code 画像上（写入该客户端配置的 env）
agg-search-install -c cursor --profile code --write

# Claude Code 用学术画像
agg-search-install -c claude-code --profile academic
```

`AGG_SEARCH_PROFILE` 是普通环境变量，任何客户端都能设；MCP 调用方还可以**逐次覆盖**：

```json
{"name": "search", "arguments": {"query": "…", "profile": "academic"}}
```

工具 schema 的 `profile` 枚举与内置 profile 同源（`agg_search.profiles`），新增 profile 只改一个文件。

## 反馈回路：读过的页面 → 引擎权重

数据来源只有两件事：

- `search` 记录每个返回 URL 是**哪些引擎召回的**（`surfaced`）；
- `fetch` 成功读到其中某个 URL 时，记一次 `used`（能对上 hash 才算，普通 fetch 不计）。

存储：

| 文件 | 内容 | 位置 |
|---|---|---|
| `feedback.json` | `profile → engine → {surfaced, used}` 计数 | `$AGG_SEARCH_CACHE_DIR`（默认 `~/.cache/agg-search`） |
| `recent.json` | 最近 400 条 URL 的**哈希** → 引擎、profile、时间 | 同上 |
| `weights.json` | 每个 profile 的引擎乘数 | `$AGG_SEARCH_CONFIG_DIR`（默认 `~/.config/agg-search`） |

不存查询词、不存页面正文、不存原始 URL（只有 sha256 前缀）。`--no-learn` 或 `AGG_SEARCH_NO_LEARN=1` 同时关闭读写。

调参与查看：

```bash
agg-search-tune --show          # 反馈表 + 当前乘数，不写盘
agg-search-tune                 # 写入 weights.json
agg-search-tune --reset         # 删除乘数，回到 profile 原始权重
```

算法（对每个 profile、每个引擎）：

```
rate       = (used + 0.15 * 20) / (surfaced + 20)     # 贝叶斯收缩，先验 15%
multiplier = clamp(rate / 0.15, 0.5, 2.0)
surfaced < 30                                             → multiplier = 1.0
```

即：样本少时乘数恒为 1；某引擎召回的页面被读得明显多，权重最多翻倍；召回一堆没人读的，权重最低减半。乘数作用在 profile 权重之上（学术 profile 下 openalex 1.8 × 2.0 = 3.6），并进入搜索缓存键——调权重不会命中旧结果。

## 为什么不是「自动学习排序」

本地只有「读没读」这一个信号，没有相关性标注；用它做**引擎级**权重是稳的，用它直接改单条结果的排序容易过拟合。需要更细的调参时，`agg-search-eval record/sweep` 提供离线、可复现的 RRF 参数扫描（见 [reranking.md](reranking.md)）。

## 领域微调实测（2026-11-19）

方法：每个领域 3 条真实工程问句（学术：speculative decoding / RLHF reward hacking / diffusion distillation；工程：asyncio 取消 / sqlite WAL / rust 借用检查），
`--fast --budget-secs 20 --no-cache --no-cooldown`，按 top-3 命中领域源（DOI/arXiv/NeurIPS/ACL 等；Stack Overflow/官方文档等）计分，同一台机器、同一批密钥，`general` 作为对照。

| 领域 | general | 对应 profile | 提升 |
|---|---|---|---|
| academic | 4 / 9 | **8 / 9** | +4 |
| code | 4 / 9 | **7 / 9** | +3 |

两个调优来自这轮实测，而不是拍脑袋：

1. **fast 模式的早退必须等主引擎**：`openalex`/`stack` 比 brave 慢，早期版本会在它们返回前就用次要引擎的结果收工，画像名存实亡。现在 profile 声明 `primary`，`--fast` 只有在主引擎出结果（或预算耗尽）后才提前返回；`engine_health.primary` 会报告主引擎状态。
2. **Jina 精排与 RRF 按比例混合**：listwise 模型只看标题+摘要，会把「标题里正好有查询词」的通用网页排在论文/问答之前。`academic` 与 `code` 均采用 `blend=0.4`（1.0 = 纯 Jina 排序），并且请求**全量**候选的分数再混合，否则 Jina 的 top-n 截断会先丢掉主引擎的候选。

随后扩展的 18 条领域评测与 5 项增强机制对照实验见 [benchmarks.md](benchmarks.md)：在合入内容级去重（默认开启，`--no-dedupe` 可关闭）与权威域软先验（`academic=1.35`、`code=1.25`）后，`academic` 权威源@3 从 `11/18` 升至 `16/18`，`code` 从 `3/18` 升至 `9/18`；`general` 10 条回归 `nDCG@10=0.9345` 无回退。`agent × domain` 门户隔离规格见 [portals.md](portals.md)。

复现：

```bash
agg-search "speculative decoding draft model" --profile academic --json \
  --fast --budget-secs 20 --no-cache --no-cooldown | jq '.results[].url'
```

局限：3 条问句、单机单日，指标是启发式域名匹配，不是人工相关性标注；`news` 没有做（缺少可自动判定的时效标签），`general` 的数字只作对照。
