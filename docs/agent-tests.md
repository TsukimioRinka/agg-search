# Agent 实测记录

约束：只装 **CLI** 客户端；全部安装在隔离临时目录（`/tmp/portallab` / `/tmp/agentlab`，独立 `HOME`、`XDG_*`、`CODEX_HOME`、`npm_config_prefix/cache`），测完整个目录删除；全程清空所有真实 API key（不带密钥跑第三方 CLI 在线调用），不触碰真实配置。

---

## 门户隔离实测（0.5.0，2026-11-20）

测试环境：`/tmp/portallab`（独立 venv + npm 前缀 + `AGG_SEARCH_CACHE_DIR`），所有 `EXA/BRAVE/SERPER/FIRECRAWL/TAVILY/DEEPSEEK/JINA/OPENAI/ANTHROPIC/GEMINI` 密钥均 `unset`，`OLLAMA_URL=http://127.0.0.1:1` 验证无本地模型降级路径。

| 客户端 | 版本 | 门户 / Server 名 | 验证方式 | 结果 | 实测证据 |
|---|---|---|---|---|---|
| MCP Inspector | 2.13.0 | `codex.academic` (`agg-search-academic`) | `tools/list`、`tools/call search`、`tools/call answer` | ✅ 真实调用 + 降级 | 工具列表严格为 `["search","fetch","answer"]`；OpenAlex 冷却后自动退回 `searxng`（`arxiv.org/abs/2211.17192`），`engine_health.portal == "codex.academic"`，`degraded` 标注缺 key/冷却/回退；`answer` 在无 ollama 与 DeepSeek key 时返回 `isError:false` 并写明 `degraded` |
| MCP Inspector | 2.13.0 | `claude-code.code` (`agg-search-code`) | `tools/list`、`tools/call search`、非法调用 `answer` | ✅ 工具隔离 + 真实调用 | 工具列表严格为 `["search","fetch"]`；`stack` 免 key 召回 Stack Overflow 首位；调用未暴露的 `answer` 被拦截（`tool_not_found`，JSON-RPC `-32602`） |
| MCP Inspector | 2.13.0 | `gemini.news` (`agg-search-news`) | `tools/list`、`tools/call search` | ✅ 免 key 回退 | 工具列表 `["search","fetch"]`；全部付费新闻引擎缺 key 时自动回退 `hn,searxng`（`ok (5)`），`engine_health.degraded` 记录回退链 |
| stdio 握手核验 | 0.5.0 | `codex.academic` / `claude-code.code` / `gemini.news` | `initialize` + `tools/list` | ✅ 独立 serverInfo 与 instructions | `serverInfo.name` 分别为 `agg-search-academic` / `agg-search-code` / `agg-search-news`，instructions 对应领域工具顺序与 1 天时效窗 |
| Codex CLI | 0.170.0 | `codex.academic` + `codex.code` | `codex mcp add --env …`、`codex mcp list` | ✅ 双门户共存 | `$CODEX_HOME/config.toml` 同时启用 `agg-search-academic` 与 `agg-search-code`（`enabled`） |
| OpenCode | 1.19.6 | `opencode.academic` + `opencode.code` | `agg-search-install --portal … --write`、`--check`、`opencode mcp list` | ✅ 双门户连接 | `--check` 均 `ok`；注：当设置非默认 `XDG_CONFIG_HOME` 时，OpenCode 读 `$XDG_CONFIG_HOME/opencode/opencode.json`（已同步并验证 `✓ agg-search-academic connected` 与 `✓ agg-search-code connected`） |
| Claude Code | 2.1.328 | `claude-code.academic` + `claude-code.code` | `claude mcp add --scope user --env …`、`claude mcp list` | ✅ 双门户连接 | 均输出 `✔ Connected`（写入隔离 `$HOME/.claude.json`） |
| Gemini CLI | 0.69.0 | `gemini.news` (`agg-search-news`) | `agg-search-install --portal gemini.news --write`、`--check`、信任 `$LAB/proj` 后 `gemini mcp list` | ✅ 已连接 | `--check` 输出 `ok`；`✓ agg-search-news ... - Connected` |
| Antigravity CLI (`agy`) | 1.5.12 | `antigravity.code` (`agg-search-code`) | `agg-search-install --portal antigravity.code --write`、`--check`、`agy mcp list` | ✅ 已启用 | `--check` 输出 `ok`；`agg-search-code  stdio  enabled` |

清理校验（测完执行 `pgrep -af` 确认零残留进程，删除 `/tmp/portallab` 与 `/tmp/eval-bench`）：
- `ls -d /tmp/portallab /tmp/eval-bench` 均不存在；
- `docker ps` 仅保留既有 `searxng` 运行；
- 真实 `~/.codex/config.toml`、`~/.gemini/settings.json`、`~/.gemini/config/mcp_config.json` mtime 与大小均未变化，且 `~/.claude.json`、`~/.config/opencode/opencode.json`、`~/.cursor/mcp.json` 仍保持不存在。

---

## 领域画像实测（0.4.0，2026-11-19）

### 结果

| 客户端 | 版本 | 验证方式 | 结果 | 证据 |
|---|---|---|---|---|
| MCP Inspector | 2.13.0 | `--config` 指向带 `AGG_SEARCH_PROFILE=code` 的 server，`tools/call search` | ✅ 真实调用 | `structuredContent.engine_health.profile == "code"`，首条为 Stack Overflow |
| Codex CLI | 0.170.0 | `codex mcp add --env …`、`mcp list`、`codex exec` 让模型调用工具 | ✅ 真实调用 | 日志 `mcp: agg-search-code/search started/completed`，返回 SO URL；服务端 `feedback.json` 记 `code → stack surfaced` |
| OpenCode | 1.19.6 | `opencode.json` 的 `mcp.servers`、`opencode mcp list`、`opencode run` | ✅ 真实调用 | `✓ agg-search-code connected`；运行输出 `⚙ agg-search-code_search {"query": …}` |
| Claude Code | 2.1.328 | `claude mcp add --scope user … --env …`、`claude mcp list` | ✅ 连接成功 | `agg-search-code: … - ✔ Connected` |
| Gemini CLI | 0.69.0 | `gemini mcp add --scope user …`、信任工作区后 `mcp list` | ✅ 连接成功 | `✓ agg-search-code: … - Connected` |
| Antigravity CLI (`agy`) | 1.5.11 | `agy mcp add -e …`、`agy mcp list` | ✅ 已启用 | `agg-search-code  stdio  enabled` |

GUI 客户端（Cursor、VS Code、Cline、Windsurf、Zed、Continue、Claude Desktop）按约束未安装，只提供配置与深链（见 [catalog.md](catalog.md) 与 [portals.md](portals.md)）。

### 一个真实的坑（模型侧，不是服务端）

第一次用本地 `qwen3.5:9b` 驱动 Codex 和 OpenCode 时，模型把工具调用**当成文本**输出（`search(query='…')`、甚至误发 `mcp__agg_search_code.search`），Codex 报 `unsupported call`，服务端一次都没被调用。

换成能力足够的模型（本机 magpie 网关 `auto/deepseek-v4.1-flash`）后，两个客户端都在 6 秒内完成真实调用。结论：MCP 接线与工具 schema 没问题；弱模型的 tool-calling 才是瓶颈。`qwen3.5:9b` 仍可用于 CLI/摘要类任务。

### 隔离复现骨架（测完删）

```bash
LAB=/tmp/portallab
mkdir -p $LAB/{home,xdg-config,xdg-data,xdg-cache,codex,npm-prefix,npm-cache,proj}
export HOME=$LAB/home XDG_CONFIG_HOME=$LAB/xdg-config XDG_DATA_HOME=$LAB/xdg-data \
       XDG_CACHE_HOME=$LAB/xdg-cache CODEX_HOME=$LAB/codex \
       npm_config_prefix=$LAB/npm-prefix npm_config_cache=$LAB/npm-cache
npm i -g @modelcontextprotocol/inspector @openai/codex opencode-ai \
          @anthropic-ai/claude-code @google/gemini-cli

# 按门户注册（示例）
agg-search-install --portal codex.academic
agg-search-install --portal claude-code.code
agg-search-install --portal gemini.news --write

rm -rf $LAB
```

## MCP 2026-07-28 双时代实测（2026-11-21，1.1.0）

客户端：官方 MCP Inspector CLI **2.13.0**，装在隔离目录 `/tmp/agg-lab-r24`（独立 `HOME`/`XDG_*`/`npm_config_cache`，`npm install --prefix`，未用 `-g`），测完删除。服务端为仓库开发版。Inspector 的 `--protocol-era` 有三档：`legacy`（只走 `initialize`）、`auto`（先发 `server/discover` 探测，失败才退回 `initialize`）、`modern`（固定 `2026-07-28`，不退回）。

| 传输 | 门户 | 时代 | 协商结果 | `tools/list` | `prompts/get` | 错误路径 |
|---|---|---|---|---|---|---|
| stdio | `omp.academic` | legacy | `2025-11-25` | search, fetch, answer | `academic-research` 正常 | `top_n: 0` → `isError: true`（退出码 5） |
| stdio | `omp.academic` | auto | `2026-07-28` | 同上 | 正常，结果带 `_meta` | 同上 |
| stdio | `omp.academic` | modern | `2026-07-28` | 同上 | 正常，结果带 `_meta` | 同上 |
| HTTP | `omp.code` | legacy | `2025-11-25` | search, fetch | `code-research` 正常 | 调 `answer` → 找不到该工具 |
| HTTP | `omp.code` | auto | `2026-07-28` | search, fetch | 正常 | 同上 |
| HTTP | `omp.code` | modern | `2026-07-28` | search, fetch | 正常 | 同上 |

三档都能连上，门户隔离在两种时代下一致（`code` 门户在新协议下同样看不到 `answer`）。HTTP 实测时发现 Inspector 断开 keep-alive 连接会让服务端打印一段 `ConnectionResetError` 栈追踪，这是 1.0 就有的日志噪音，1.1.0 改为一行日志。
