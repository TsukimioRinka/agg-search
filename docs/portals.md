# 门户隔离 — Portal (`agent × domain`)

每个门户是 `agent × domain` 的纯数据描述：绑定固定 domain profile、暴露独立的 MCP 工具子集与调用顺序、使用领域专属 prompt（查询改写 / 答案合成 / 筛选提示），并声明依赖引擎、密钥与降级路径（无 per-agent 分支代码）。

本文件由 `agg-search-install --portals --markdown` 生成，机器可读版本见 [`portals.json`](../portals.json)；客户端矩阵见 [`catalog.md`](catalog.md) / [`catalog.json`](../catalog.json)。

> 工具顺序是**给模型的编排建议**（写进 instructions 与工具描述），不是服务端强制流水线；唯一在工具内部自动执行的是 `answer` 的 opt-in `urls` 预取。门户隔离强制的是**工具子集**（调用未暴露的工具返回 `-32602`）与独立 server 名/instructions。

## 按领域（工具面、顺序、默认窗与依赖）

| 领域 | MCP server 名 | 工具子集与顺序 | 必需引擎（免 key） | 默认时效/参数 | 降级策略 |
|---|---|---|---|---|---|
| `academic` | `agg-search-academic` | `search` → `fetch` → `answer` | `openalex` | top_n=15, blend=0.4, snippet=500 | 缺 key 跳过走无 key 回退；缺 Jina 保留 RRF；缺 LLM 退回无扩展/无答案 |
| `code` | `agg-search-code` | `search` → `fetch` | `stack` | blend=0.4, snippet=600 | 缺 key 跳过走无 key 回退；缺 Jina 保留 RRF；缺 LLM 退回无扩展/无答案 |
| `general` | `agg-search` | `search` → `fetch` | — | balanced | 缺 key 跳过走无 key 回退；缺 Jina 保留 RRF；缺 LLM 退回无扩展/无答案 |
| `news` | `agg-search-news` | `search` → `fetch` | — | recency=`day`, blend=0.3, snippet=300 | 缺 key 跳过走无 key 回退；缺 Jina 保留 RRF；缺 LLM 退回无扩展/无答案 |

## 按 Agent（本地 LLM 假设与回答回退顺序）

| Agent | 注册类型 | 本地 Ollama | Answer 回退顺序 | 一行安装（示例 `academic`） |
|---|---|---|---|---|
| `antigravity` | json | 是 | `local` → `deepseek` | `agg-search-install --portal antigravity.academic --write` |
| `claude-code` | command | 否（云端优先） | `deepseek` → `local` | `claude mcp add --scope user agg-search-academic --env AGG_SEARCH_PORTAL=claude-code.academic --env AGG_SEARCH_PROFILE=academic -- uvx --from agg-search agg-search-mcp` |
| `claude-desktop` | json | 否（云端优先） | `deepseek` → `local` | `agg-search-install --portal claude-desktop.academic --write` |
| `cline` | json | 否（云端优先） | `deepseek` → `local` | `agg-search-install --portal cline.academic --write` |
| `codex` | command | 是 | `local` → `deepseek` | `codex mcp add agg-search-academic --env AGG_SEARCH_PORTAL=codex.academic --env AGG_SEARCH_PROFILE=academic -- uvx --from agg-search agg-search-mcp` |
| `continue` | snippet | 是 | `local` → `deepseek` | `agg-search-install --portal continue.academic` |
| `cursor` | json | 否（云端优先） | `deepseek` → `local` | [⬇ 安装](https://cursor.com/en/install-mcp?name=agg-search-academic&config=eyJjb21tYW5kIjogInV2eCIsICJhcmdzIjogWyItLWZyb20iLCAiYWdnLXNlYXJjaCIsICJhZ2ctc2VhcmNoLW1jcCJdLCAiZW52IjogeyJBR0dfU0VBUkNIX1BPUlRBTCI6ICJjdXJzb3IuYWNhZGVtaWMiLCAiQUdHX1NFQVJDSF9QUk9GSUxFIjogImFjYWRlbWljIn19) / `agg-search-install --portal cursor.academic --write` |
| `gemini` | json | 否（云端优先） | `deepseek` → `local` | `agg-search-install --portal gemini.academic --write` |
| `generic` | snippet | 否（云端优先） | `deepseek` → `local` | `agg-search-install --portal generic.academic` |
| `omp` | json | 是 | `local` → `deepseek` | `agg-search-install --portal omp.academic --write` |
| `opencode` | json | 是 | `local` → `deepseek` | `agg-search-install --portal opencode.academic --write` |
| `vscode` | json | 否（云端优先） | `deepseek` → `local` | [⬇ 安装](https://vscode.dev/redirect/mcp/install?name=agg-search-academic&config=%7B%22type%22%3A%20%22stdio%22%2C%20%22command%22%3A%20%22uvx%22%2C%20%22args%22%3A%20%5B%22--from%22%2C%20%22agg-search%22%2C%20%22agg-search-mcp%22%5D%2C%20%22env%22%3A%20%7B%22AGG_SEARCH_PORTAL%22%3A%20%22vscode.academic%22%2C%20%22AGG_SEARCH_PROFILE%22%3A%20%22academic%22%7D%7D) / `agg-search-install --portal vscode.academic --write` |
| `windsurf` | json | 否（云端优先） | `deepseek` → `local` | `agg-search-install --portal windsurf.academic --write` |
| `zed` | json | 是 | `local` → `deepseek` | `agg-search-install --portal zed.academic --write` |

## 全量门户命令索引（56 个 `agent.domain`）

| Portal | Server 名 | 安装 / 写入命令 |
|---|---|---|
| `antigravity.academic` | `agg-search-academic` | `agg-search-install --portal antigravity.academic --write` |
| `antigravity.code` | `agg-search-code` | `agg-search-install --portal antigravity.code --write` |
| `antigravity.general` | `agg-search` | `agg-search-install --portal antigravity.general --write` |
| `antigravity.news` | `agg-search-news` | `agg-search-install --portal antigravity.news --write` |
| `claude-code.academic` | `agg-search-academic` | `agg-search-install --portal claude-code.academic` |
| `claude-code.code` | `agg-search-code` | `agg-search-install --portal claude-code.code` |
| `claude-code.general` | `agg-search` | `agg-search-install --portal claude-code.general` |
| `claude-code.news` | `agg-search-news` | `agg-search-install --portal claude-code.news` |
| `claude-desktop.academic` | `agg-search-academic` | `agg-search-install --portal claude-desktop.academic --write` |
| `claude-desktop.code` | `agg-search-code` | `agg-search-install --portal claude-desktop.code --write` |
| `claude-desktop.general` | `agg-search` | `agg-search-install --portal claude-desktop.general --write` |
| `claude-desktop.news` | `agg-search-news` | `agg-search-install --portal claude-desktop.news --write` |
| `cline.academic` | `agg-search-academic` | `agg-search-install --portal cline.academic --write` |
| `cline.code` | `agg-search-code` | `agg-search-install --portal cline.code --write` |
| `cline.general` | `agg-search` | `agg-search-install --portal cline.general --write` |
| `cline.news` | `agg-search-news` | `agg-search-install --portal cline.news --write` |
| `codex.academic` | `agg-search-academic` | `agg-search-install --portal codex.academic` |
| `codex.code` | `agg-search-code` | `agg-search-install --portal codex.code` |
| `codex.general` | `agg-search` | `agg-search-install --portal codex.general` |
| `codex.news` | `agg-search-news` | `agg-search-install --portal codex.news` |
| `continue.academic` | `agg-search-academic` | `agg-search-install --portal continue.academic` |
| `continue.code` | `agg-search-code` | `agg-search-install --portal continue.code` |
| `continue.general` | `agg-search` | `agg-search-install --portal continue.general` |
| `continue.news` | `agg-search-news` | `agg-search-install --portal continue.news` |
| `cursor.academic` | `agg-search-academic` | `agg-search-install --portal cursor.academic --write` |
| `cursor.code` | `agg-search-code` | `agg-search-install --portal cursor.code --write` |
| `cursor.general` | `agg-search` | `agg-search-install --portal cursor.general --write` |
| `cursor.news` | `agg-search-news` | `agg-search-install --portal cursor.news --write` |
| `gemini.academic` | `agg-search-academic` | `agg-search-install --portal gemini.academic --write` |
| `gemini.code` | `agg-search-code` | `agg-search-install --portal gemini.code --write` |
| `gemini.general` | `agg-search` | `agg-search-install --portal gemini.general --write` |
| `gemini.news` | `agg-search-news` | `agg-search-install --portal gemini.news --write` |
| `generic.academic` | `agg-search-academic` | `agg-search-install --portal generic.academic` |
| `generic.code` | `agg-search-code` | `agg-search-install --portal generic.code` |
| `generic.general` | `agg-search` | `agg-search-install --portal generic.general` |
| `generic.news` | `agg-search-news` | `agg-search-install --portal generic.news` |
| `omp.academic` | `agg-search-academic` | `agg-search-install --portal omp.academic --write` |
| `omp.code` | `agg-search-code` | `agg-search-install --portal omp.code --write` |
| `omp.general` | `agg-search` | `agg-search-install --portal omp.general --write` |
| `omp.news` | `agg-search-news` | `agg-search-install --portal omp.news --write` |
| `opencode.academic` | `agg-search-academic` | `agg-search-install --portal opencode.academic --write` |
| `opencode.code` | `agg-search-code` | `agg-search-install --portal opencode.code --write` |
| `opencode.general` | `agg-search` | `agg-search-install --portal opencode.general --write` |
| `opencode.news` | `agg-search-news` | `agg-search-install --portal opencode.news --write` |
| `vscode.academic` | `agg-search-academic` | `agg-search-install --portal vscode.academic --write` |
| `vscode.code` | `agg-search-code` | `agg-search-install --portal vscode.code --write` |
| `vscode.general` | `agg-search` | `agg-search-install --portal vscode.general --write` |
| `vscode.news` | `agg-search-news` | `agg-search-install --portal vscode.news --write` |
| `windsurf.academic` | `agg-search-academic` | `agg-search-install --portal windsurf.academic --write` |
| `windsurf.code` | `agg-search-code` | `agg-search-install --portal windsurf.code --write` |
| `windsurf.general` | `agg-search` | `agg-search-install --portal windsurf.general --write` |
| `windsurf.news` | `agg-search-news` | `agg-search-install --portal windsurf.news --write` |
| `zed.academic` | `agg-search-academic` | `agg-search-install --portal zed.academic --write` |
| `zed.code` | `agg-search-code` | `agg-search-install --portal zed.code --write` |
| `zed.general` | `agg-search` | `agg-search-install --portal zed.general --write` |
| `zed.news` | `agg-search-news` | `agg-search-install --portal zed.news --write` |

常用示例：`agg-search-install --portal codex.academic`、`agg-search-install --portal claude-code.code`、`agg-search-install --portal cursor.news --write`。
