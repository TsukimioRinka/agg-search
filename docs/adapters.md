# 客户端接入 — 把这个 MCP server 装进你的 Agent

`agg-search-mcp` 是一个 **stdio MCP server**：客户端负责拉起进程，双方用换行分隔的 JSON-RPC 2.0 在 stdin/stdout 上通信。所有客户端的配置最后都归结为同一件事——让客户端去执行一条命令：

```bash
uvx --from agg-search agg-search-mcp
```

三种等价启动方式，按你的使用场景挑一个：

| 方式 | 命令 | 适用 |
|---|---|---|
| PyPI 直接跑 | `uvx --from agg-search agg-search-mcp` | 只装了 [uv](https://docs.astral.sh/uv/)；PyPI **0.2.0 起**可用 |
| 常驻安装 | `uv tool install agg-search`，之后命令直接用 `agg-search-mcp` | 想固定版本、少一层 uvx 解析 |
| 仓库内运行 | `uv run --directory /path/to/agg-search agg-search-mcp` | 改代码或未发布提交；路径换成你的 checkout 绝对路径 |

> 下文 JSON 片段里的 `args` 都按 uvx 写法给出。若用 `uv tool install`，把 `command` 改成 `agg-search-mcp`、删掉 `args`；若用仓库内运行，`command` 用 `uv`、`args` 为 `["run", "--directory", "/abs/path/to/agg-search", "agg-search-mcp"]`。

## 总表

| 客户端 | 配置位置 | 注册方式 |
|---|---|---|
| Claude Code | 用户级由 CLI 管理 | `claude mcp add --scope user ...` |
| Claude Desktop | `claude_desktop_config.json`（分平台） | 手写 `mcpServers` |
| Codex CLI | `~/.codex/config.toml` | `codex mcp add ...` 或手写 TOML |
| Cursor | `~/.cursor/mcp.json`（全局）/ `.cursor/mcp.json`（项目） | 手写 `mcpServers` |
| VS Code / Copilot | `.vscode/mcp.json`（`servers` 键） | 手写 或 `code --add-mcp` |
| Cline | `cline_mcp_settings.json` | 面板按钮打开，或手写 `mcpServers` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | 手写 `mcpServers` |
| Zed | `settings.json` → `context_servers` | 手写 |
| Continue | `~/.continue/config.yaml` → `mcpServers` | 手写 YAML |
| Gemini CLI | `~/.gemini/settings.json` → `mcpServers` | 手写 或 `gemini mcp add` |
| Antigravity（App / IDE / CLI） | `~/.gemini/config/mcp_config.json` | 手写，或 CLI 的 `/mcp` |
| OpenCode | `opencode.json` → `mcp.servers`（v2） | 手写 或 `opencode mcp add` |
| 其他客户端 | 通用 `mcpServers` JSON | 手写 |

密钥统一由服务进程的环境变量读取，表格见 [install.md](install.md)。**GUI 客户端（Claude Desktop、Windsurf、Antigravity App）不继承 shell 环境**，要么在客户端配置的 `env` 字段里写，要么把 key 放进 `~/.config/agent-env`（服务端自己会读）。

---

## Claude Code

一条命令，用户级作用域（对所有项目生效）：

```bash
claude mcp add --scope user agg-search -- uvx --from agg-search agg-search-mcp
```

- 作用域：`--scope user` 写用户级；`--scope project` 在当前仓库落 `.mcp.json`（可提交）；`--scope local`（默认）只对当前项目本机生效。
- 带密钥：`claude mcp add --scope user agg-search --env EXA_API_KEY=xxx --env BRAVE_API_KEY=yyy -- uvx --from agg-search agg-search-mcp`
- 核对：`claude mcp list`，会话里用 `/mcp` 看连接状态。

## Claude Desktop

配置文件（文件名固定，目录分平台）：

| 平台 | 路径 |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "agg-search": {
      "command": "uvx",
      "args": ["--from", "agg-search", "agg-search-mcp"],
      "env": {
        "EXA_API_KEY": "your-exa-key",
        "BRAVE_API_KEY": "your-brave-key"
      }
    }
  }
}
```

改完完全退出并重启 Claude Desktop（不是关窗口）。macOS 从 Finder 启动时 `PATH` 很短，找不到 `uvx` 就把 `command` 换成绝对路径，例如 `/Users/<you>/.local/bin/uvx`（`which uvx` 可查）。

## Codex CLI

```bash
codex mcp add agg-search -- uvx --from agg-search agg-search-mcp

# 需要固定密钥时：
codex mcp add agg-search --env EXA_API_KEY=xxx -- uvx --from agg-search agg-search-mcp
```

配置文件是 `~/.codex/config.toml`（项目级可放 `.codex/config.toml`，仅受信任项目生效）：

```toml
[mcp_servers.agg-search]
command = "uvx"
args = ["--from", "agg-search", "agg-search-mcp"]
env = { EXA_API_KEY = "your-exa-key" }
```

`codex mcp list` 查看已注册项；会话内 `/mcp` 查看活动连接。

## Cursor

两个位置，格式相同：

- 全局：`~/.cursor/mcp.json`
- 项目（可提交、团队共享）：`<repo>/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "agg-search": {
      "command": "uvx",
      "args": ["--from", "agg-search", "agg-search-mcp"],
      "env": {}
    }
  }
}
```

也在 Cursor 的 Customize → MCP 面板里加（本质是同一文件）。改完在 MCP 面板点刷新/重连。

## VS Code / Copilot

工作区文件 `.vscode/mcp.json`，注意这一版用 **`servers`** 键（不是 `mcpServers`）：

```json
{
  "servers": {
    "agg-search": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "agg-search", "agg-search-mcp"],
      "env": {}
    }
  }
}
```

或命令行写入用户级配置：

```bash
code --add-mcp '{"name":"agg-search","type":"stdio","command":"uvx","args":["--from","agg-search","agg-search-mcp"]}'
```

`type` 取 `stdio`（本地命令）或 `http`（远程 endpoint）。命令面板里的 `MCP: List Servers` 可启停、看输出。

> 新版 Copilot 还有一套"portable"配置：仓库根 `.mcp.json`，顶层是 `mcpServers`、`type` 用 `stdio`。两套文件互不替代，按 `MCP: Add Server` 提示选择目标即可。

## Cline

Cline 面板：MCP Servers 图标 → Configure → **Configure MCP Servers**，按钮会直接打开当前生效的 `cline_mcp_settings.json`。默认位置：

| 形态 | 路径 |
|---|---|
| Cline CLI | `~/.cline/data/settings/cline_mcp_settings.json`（可用 `CLINE_MCP_SETTINGS_PATH` 覆盖） |
| VS Code 扩展（macOS） | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| VS Code 扩展（Linux） | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| VS Code 扩展（Windows） | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json` |

```json
{
  "mcpServers": {
    "agg-search": {
      "command": "uvx",
      "args": ["--from", "agg-search", "agg-search-mcp"],
      "env": {},
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

## Windsurf

用户级配置 `~/.codeium/windsurf/mcp_config.json`（Windows：`%USERPROFILE%\.codeium\windsurf\mcp_config.json`），格式与 Claude Desktop 相同：

```json
{
  "mcpServers": {
    "agg-search": {
      "command": "uvx",
      "args": ["--from", "agg-search", "agg-search-mcp"],
      "env": {}
    }
  }
}
```

改完在 Cascade 的 MCP 面板刷新。Windsurf 从 Dock/开始菜单启动，同样可能读不到 shell 的 `PATH` 和密钥：`command` 用绝对路径，key 写进 `env` 或 `~/.config/agent-env`。

## Zed

`settings.json` 顶层加 `context_servers`。当前版本是扁平结构：

```json
{
  "context_servers": {
    "agg-search": {
      "command": "uvx",
      "args": ["--from", "agg-search", "agg-search-mcp"],
      "env": {}
    }
  }
}
```

旧版 Zed（schema 未扁平化时）用 `source: custom` + 嵌套 `command` 对象；你的 Zed 忽略扁平写法时换成这段：

```json
{
  "context_servers": {
    "agg-search": {
      "source": "custom",
      "command": {
        "path": "uvx",
        "args": ["--from", "agg-search", "agg-search-mcp"],
        "env": {}
      }
    }
  }
}
```

打开 Settings → AI → MCP Servers，指示灯变绿即 "Server is active"。

## Continue

`~/.continue/config.yaml`（VS Code / JetBrains 共用）。注意 `mcpServers` 是**列表**，每项带 `name`：

```yaml
name: agg-search
version: 1.0.0
schema: v1
mcpServers:
  - name: agg-search
    command: uvx
    args:
      - --from
      - agg-search
      - agg-search-mcp
    env: {}
```

已有 `config.yaml` 时只合并 `mcpServers` 这一段（顶层 `name`/`version`/`schema` 保留原值）。工作区也可以在 `.continue/mcpServers/` 放 JSON 片段，Continue 会自动加载；改完重载扩展。

## Gemini CLI

配置文件 `~/.gemini/settings.json`，顶层 `mcpServers`：

```json
{
  "mcpServers": {
    "agg-search": {
      "command": "uvx",
      "args": ["--from", "agg-search", "agg-search-mcp"],
      "env": {},
      "timeout": 30000,
      "trust": false
    }
  }
}
```

或用 CLI 写入（`--scope user` 才写家目录，否则写项目级）：

```bash
gemini mcp add --scope user agg-search uvx --from agg-search agg-search-mcp
gemini mcp list
```

## Antigravity（App / IDE / CLI）

三种形态读同一套 `mcp_config.json`：

| 范围 | 路径 |
|---|---|
| 全局（所有项目） | `~/.gemini/config/mcp_config.json` |
| 工作区 | `<project>/.agents/mcp_config.json` |

```json
{
  "mcpServers": {
    "agg-search": {
      "command": "uvx",
      "args": ["--from", "agg-search", "agg-search-mcp"],
      "env": {}
    }
  }
}
```

- **App**：写入全局文件后重启 App。
- **IDE**：MCP Store 浏览/安装，或 Manage MCP Servers → **View raw config** 直接编辑上面两个文件之一。
- **CLI**：交互式 `/mcp` 查看状态并重载配置；配置迁移后同样落在 `mcp_config.json`，不再写在主设置里。

本服务是 stdio，用 `command`/`args`；远端 HTTP server 才用 `serverUrl`（不是 `url`/`httpUrl`）。文件里的 `~` 不会展开，涉及路径时写绝对路径。

> 别和 Gemini CLI 混：Gemini CLI 读 `~/.gemini/settings.json`，Antigravity 读 `~/.gemini/config/mcp_config.json`，两个文件互不影响。

## OpenCode

项目级 `opencode.json`（或全局 `~/.config/opencode/opencode.json`）。**v2 的 server 名在 `mcp.servers` 下**，`command` 是数组：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "agg-search": {
        "type": "local",
        "command": ["uvx", "--from", "agg-search", "agg-search-mcp"],
        "environment": {}
      }
    }
  }
}
```

v1 把名字直接放在 `mcp` 下（`"mcp": { "agg-search": { "type": "local", ... } }`）；v2 迁移后必须多一层 `servers`。想关掉某个 server 用 `"disabled": true`，v2 没有 `enabled` 字段。也可：

```bash
opencode mcp add agg-search -- uvx --from agg-search agg-search-mcp
opencode mcp list
```

## 通用 `mcpServers` JSON

大多数客户端（Claude Desktop 系、Cursor、Windsurf、Cline、Gemini CLI、LibreChat 等）都吃同一个形状，直接复制：

```json
{
  "mcpServers": {
    "agg-search": {
      "command": "uvx",
      "args": ["--from", "agg-search", "agg-search-mcp"],
      "env": {
        "EXA_API_KEY": "your-exa-key",
        "BRAVE_API_KEY": "your-brave-key",
        "TAVILY_API_KEY": "your-tavily-key"
      }
    }
  }
}
```

接入后客户端应看到两个工具：`search`（并行召回 + RRF + 精排）和 `fetch`（分层抓正文）。排障顺序：先按 [install.md](install.md) 跑冒烟测试确认 `agg-search-mcp` 能起来，再查客户端是否拿到 `uvx` 的绝对路径与密钥。
