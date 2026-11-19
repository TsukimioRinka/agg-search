# 下载入口 — Agent × 领域

同一个 stdio server，不同 Agent 的注册方式不同；不同领域用 `AGG_SEARCH_PROFILE` 把引擎组合与权重钉死。下表由 `agg-search-install --catalog --markdown` 生成，机器可读版本见仓库根目录的 [`catalog.json`](../catalog.json)。

| profile | 含义 |
|---|---|
| `academic` | papers and preprints: OpenAlex first, then semantic and general engines for context |
| `code` | programming questions, APIs and error messages: Stack Exchange first, docs-friendly engines after |
| `general` | all configured engines, balanced weights |
| `news` | recent events: freshness-first engines with a one-day default window |

## 按 Agent

| Agent | 类型 | 一行入口 |
|---|---|---|
| antigravity | json | `agg-search-install -c antigravity --write` |
| claude-code | command | `claude mcp add --scope user agg-search -- uvx --from agg-search agg-search-mcp` |
| claude-desktop | json | `agg-search-install -c claude-desktop --write` |
| cline | json | `agg-search-install -c cline --write` |
| codex | command | `codex mcp add agg-search -- uvx --from agg-search agg-search-mcp` |
| continue | snippet | `agg-search-install -c continue` |
| cursor | json | [⬇ 安装](https://cursor.com/en/install-mcp?name=agg-search&config=eyJjb21tYW5kIjogInV2eCIsICJhcmdzIjogWyItLWZyb20iLCAiYWdnLXNlYXJjaCIsICJhZ2ctc2VhcmNoLW1jcCJdLCAiZW52Ijoge319) |
| gemini | json | `agg-search-install -c gemini --write` |
| generic | snippet | `agg-search-install -c generic` |
| opencode | json | `agg-search-install -c opencode --write` |
| vscode | json | [⬇ 安装](https://vscode.dev/redirect/mcp/install?name=agg-search&config=%7B%22type%22%3A%20%22stdio%22%2C%20%22command%22%3A%20%22uvx%22%2C%20%22args%22%3A%20%5B%22--from%22%2C%20%22agg-search%22%2C%20%22agg-search-mcp%22%5D%2C%20%22env%22%3A%20%7B%7D%7D) |
| windsurf | json | `agg-search-install -c windsurf --write` |
| zed | json | `agg-search-install -c zed --write` |

## 按领域

| profile | Cursor | VS Code | Claude Code | Codex | 其他客户端 |
|---|---|---|---|---|---|
| `academic` | [安装](https://cursor.com/en/install-mcp?name=agg-search-academic&config=eyJjb21tYW5kIjogInV2eCIsICJhcmdzIjogWyItLWZyb20iLCAiYWdnLXNlYXJjaCIsICJhZ2ctc2VhcmNoLW1jcCJdLCAiZW52IjogeyJBR0dfU0VBUkNIX1BST0ZJTEUiOiAiYWNhZGVtaWMifX0%3D) | [安装](https://vscode.dev/redirect/mcp/install?name=agg-search-academic&config=%7B%22type%22%3A%20%22stdio%22%2C%20%22command%22%3A%20%22uvx%22%2C%20%22args%22%3A%20%5B%22--from%22%2C%20%22agg-search%22%2C%20%22agg-search-mcp%22%5D%2C%20%22env%22%3A%20%7B%22AGG_SEARCH_PROFILE%22%3A%20%22academic%22%7D%7D) | `claude mcp add --scope user agg-search-academic --env AGG_SEARCH_PROFILE=academic -- uvx --from agg-search agg-search-mcp` | `codex mcp add agg-search-academic --env AGG_SEARCH_PROFILE=academic -- uvx --from agg-search agg-search-mcp` | `agg-search-install -c <client> --profile academic --write` |
| `code` | [安装](https://cursor.com/en/install-mcp?name=agg-search-code&config=eyJjb21tYW5kIjogInV2eCIsICJhcmdzIjogWyItLWZyb20iLCAiYWdnLXNlYXJjaCIsICJhZ2ctc2VhcmNoLW1jcCJdLCAiZW52IjogeyJBR0dfU0VBUkNIX1BST0ZJTEUiOiAiY29kZSJ9fQ%3D%3D) | [安装](https://vscode.dev/redirect/mcp/install?name=agg-search-code&config=%7B%22type%22%3A%20%22stdio%22%2C%20%22command%22%3A%20%22uvx%22%2C%20%22args%22%3A%20%5B%22--from%22%2C%20%22agg-search%22%2C%20%22agg-search-mcp%22%5D%2C%20%22env%22%3A%20%7B%22AGG_SEARCH_PROFILE%22%3A%20%22code%22%7D%7D) | `claude mcp add --scope user agg-search-code --env AGG_SEARCH_PROFILE=code -- uvx --from agg-search agg-search-mcp` | `codex mcp add agg-search-code --env AGG_SEARCH_PROFILE=code -- uvx --from agg-search agg-search-mcp` | `agg-search-install -c <client> --profile code --write` |
| `general` | [安装](https://cursor.com/en/install-mcp?name=agg-search&config=eyJjb21tYW5kIjogInV2eCIsICJhcmdzIjogWyItLWZyb20iLCAiYWdnLXNlYXJjaCIsICJhZ2ctc2VhcmNoLW1jcCJdLCAiZW52Ijoge319) | [安装](https://vscode.dev/redirect/mcp/install?name=agg-search&config=%7B%22type%22%3A%20%22stdio%22%2C%20%22command%22%3A%20%22uvx%22%2C%20%22args%22%3A%20%5B%22--from%22%2C%20%22agg-search%22%2C%20%22agg-search-mcp%22%5D%2C%20%22env%22%3A%20%7B%7D%7D) | `claude mcp add --scope user agg-search -- uvx --from agg-search agg-search-mcp` | `codex mcp add agg-search -- uvx --from agg-search agg-search-mcp` | `agg-search-install -c <client> --profile general --write` |
| `news` | [安装](https://cursor.com/en/install-mcp?name=agg-search-news&config=eyJjb21tYW5kIjogInV2eCIsICJhcmdzIjogWyItLWZyb20iLCAiYWdnLXNlYXJjaCIsICJhZ2ctc2VhcmNoLW1jcCJdLCAiZW52IjogeyJBR0dfU0VBUkNIX1BST0ZJTEUiOiAibmV3cyJ9fQ%3D%3D) | [安装](https://vscode.dev/redirect/mcp/install?name=agg-search-news&config=%7B%22type%22%3A%20%22stdio%22%2C%20%22command%22%3A%20%22uvx%22%2C%20%22args%22%3A%20%5B%22--from%22%2C%20%22agg-search%22%2C%20%22agg-search-mcp%22%5D%2C%20%22env%22%3A%20%7B%22AGG_SEARCH_PROFILE%22%3A%20%22news%22%7D%7D) | `claude mcp add --scope user agg-search-news --env AGG_SEARCH_PROFILE=news -- uvx --from agg-search agg-search-mcp` | `codex mcp add agg-search-news --env AGG_SEARCH_PROFILE=news -- uvx --from agg-search agg-search-mcp` | `agg-search-install -c <client> --profile news --write` |

完整路径与 JSON/YAML 片段见 [adapters.md](adapters.md)；领域画像与反馈回路见 [profiles.md](profiles.md)。

