# Agent 实测记录（2026-11-19）

约束：只装 **CLI** 客户端；全部安装在 `/tmp/agentlab` 下隔离（独立 `HOME`、`XDG_*`、`CODEX_HOME`、`npm_config_prefix/cache`），测完整个目录删除，不触碰真实配置。

## 结果

| 客户端 | 版本 | 验证方式 | 结果 | 证据 |
|---|---|---|---|---|
| MCP Inspector | 2.13.0 | `--config` 指向带 `AGG_SEARCH_PROFILE=code` 的 server，`tools/call search` | ✅ 真实调用 | `structuredContent.engine_health.profile == "code"`，首条为 Stack Overflow |
| Codex CLI | 0.170.0 | `codex mcp add --env …`、`mcp list`、`codex exec` 让模型调用工具 | ✅ 真实调用 | 日志 `mcp: agg-search-code/search started/completed`，返回 SO URL；服务端 `feedback.json` 记 `code → stack surfaced` |
| OpenCode | 1.19.6 | `opencode.json` 的 `mcp.servers`、`opencode mcp list`、`opencode run` | ✅ 真实调用 | `✓ agg-search-code connected`；运行输出 `⚙ agg-search-code_search {"query": …}` |
| Claude Code | 2.1.328 | `claude mcp add --scope user … --env …`、`claude mcp list` | ✅ 连接成功 | `agg-search-code: … - ✔ Connected` |
| Gemini CLI | 0.69.0 | `gemini mcp add --scope user …`、信任工作区后 `mcp list` | ✅ 连接成功 | `✓ agg-search-code: … - Connected` |
| Antigravity CLI (`agy`) | 1.5.11 | `agy mcp add -e …`、`agy mcp list` | ✅ 已启用 | `agg-search-code  stdio  enabled` |

GUI 客户端（Cursor、VS Code、Cline、Windsurf、Zed、Continue、Claude Desktop）按约束未安装，只提供配置与深链（见 [catalog.md](catalog.md)）。

## 一个真实的坑（模型侧，不是服务端）

第一次用本地 `qwen3.5:9b` 驱动 Codex 和 OpenCode 时，模型把工具调用**当成文本**输出（`search(query='…')`、甚至误发 `mcp__agg_search_code.search`），Codex 报 `unsupported call`，服务端一次都没被调用。

换成能力足够的模型（本机 magpie 网关 `auto/deepseek-v4.1-flash`）后，两个客户端都在 6 秒内完成真实调用。结论：MCP 接线与工具 schema 没问题；弱模型的 tool-calling 才是瓶颈。`qwen3.5:9b` 仍可用于 CLI/摘要类任务。

## 复现（隔离，测完删）

```bash
LAB=/tmp/agentlab; mkdir -p $LAB/{home,npm-prefix,npm-cache,proj}
export HOME=$LAB/home npm_config_prefix=$LAB/npm-prefix npm_config_cache=$LAB/npm-cache
npm i -g @modelcontextprotocol/inspector @openai/codex opencode-ai \
          @anthropic-ai/claude-code @google/gemini-cli

# Inspector（第三方客户端，真实 tools/call）
cat > $LAB/inspector.json <<'JSON'
{"mcpServers":{"agg-search-code":{"command":"uvx","args":["--from","agg-search","agg-search-mcp"],"env":{"AGG_SEARCH_PROFILE":"code"}}}}
JSON
$LAB/npm-prefix/bin/mcp-inspector --cli --config $LAB/inspector.json \
  --server agg-search-code --method tools/call --tool-name search \
  --tool-arg query="sqlite wal checkpoint busy"

# Codex
CODEX_HOME=$LAB/codex codex mcp add agg-search-code --env AGG_SEARCH_PROFILE=code -- uvx --from agg-search agg-search-mcp
CODEX_HOME=$LAB/codex codex mcp list

# Claude Code / Gemini / Antigravity
claude mcp add --scope user agg-search-code --env AGG_SEARCH_PROFILE=code -- uvx --from agg-search agg-search-mcp && claude mcp list
gemini mcp add --scope user agg-search-code uvx --from agg-search agg-search-mcp
agy mcp add -e AGG_SEARCH_PROFILE=code agg-search-code uvx --from agg-search agg-search-mcp && agy mcp list

rm -rf $LAB
```

清理校验：真实 `~/.codex/config.toml`、`~/.gemini/settings.json` 的 mtime 未变，且不含 `agg-search` 条目；npm 全局前缀与缓存都在 `$LAB` 内，随目录删除。
