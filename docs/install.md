# 安装与环境变量

本项目是纯 Python 3.10+ 标准库实现，没有第三方运行时依赖。包在 PyPI 上是 `agg-search`，提供七个命令：

| 命令 | 作用 |
|---|---|
| `agg-search` | CLI：多引擎并行召回 + RRF + Jina 精排（`--profile` 选领域画像） |
| `agg-search-mcp` | stdio MCP server（`search` / `fetch` 两个工具） |
| `agg-search-http` | streamable HTTP MCP 端点（多客户端共用） |
| `crawl-fetch` | 分层正文抓取（DeepSeek Fetch → 容器阶梯） |
| `agg-search-install` | 生成/写入各客户端配置，`--check` 自检 |
| `agg-search-tune` | 把本地反馈折算成引擎权重乘数 |
| `agg-search-eval` | 检索质量评测（离线 RRF 参数扫描） |

## 安装

前置条件：装好 [uv](https://docs.astral.sh/uv/)（`curl -LsSf https://astral.sh/uv/install.sh | sh`）。uv 会自动挑选并下载符合 `>=3.10` 的 Python，本机不需要预装对应解释器。

**方式一：uvx，不落盘（PyPI 0.2.0 起）**

```bash
uvx --from agg-search agg-search-mcp
```

**方式二：装成常驻命令**

```bash
uv tool install agg-search
agg-search-mcp          # 之后直接调用，版本固定到安装时
```

升级：`uv tool upgrade agg-search`。想钉版本：`uv tool install 'agg-search==0.2.0'`。

**方式三：从 git checkout 运行（改代码/未发布提交）**

```bash
git clone https://github.com/TsukimioRinka/agg-search
cd agg-search
uv venv && uv pip install -e .
.venv/bin/agg-search-mcp
```

或让 uv 代管环境、不激活 venv：

```bash
uv run --directory /abs/path/to/agg-search agg-search-mcp
```

需要 `python3 -m agg_search.mcp` 之类的手动调用时，把 `src/` 放进 `PYTHONPATH` 即可（`PYTHONPATH=/abs/path/to/agg-search/src python3 -m agg_search.mcp`）。

## 环境变量

所有引擎都是**按 key 选择性启用**：某个 key 缺失就跳过对应引擎，不会让整次搜索失败。没有任何一个变量是硬性必填；但一个引擎都没有时只能返回空结果。

| 变量 | 用途 | 默认 / 必需 |
|---|---|---|
| `EXA_API_KEY` | Exa 语义检索（英文技术文档、官方资源） | 无默认；缺失即跳过 |
| `BRAVE_API_KEY` | Brave Web Search（中英新闻、社区动态） | 无默认；缺失即跳过 |
| `SERPER_API_KEY` | Serper Google SERP 桥（`POST /search`，`X-API-KEY`） | 无默认；缺失即跳过 |
| `FIRECRAWL_API_KEY` | Firecrawl 实时爬取搜索（`POST /v2/search`） | 无默认；缺失即跳过 |
| `TAVILY_API_KEY` | Tavily（中文覆盖 + 摘要） | 无默认；失败跳过 |
| `TAVILY_API_KEY_BACKUP` | Tavily 备用键，主键遇限额类错误（402/429/401 等）自动切换 | 无默认；可选 |
| `MCP_ANYSEARCH_API_KEY` | AnySearch（MCP-over-HTTP 批量检索） | 无默认；缺失即跳过 |
| `DEEPSEEK_API_KEY` | DeepSeek 官方 Search/Fetch（既是引擎，也是抓取第 0 级） | 无默认；缺失即跳过 |
| `DEEPSEEK_BASE_URL` | DeepSeek API 基址 | `https://api.deepseek.com` |
| `SEARXNG_URL` | 自托管 SearXNG 地址 | `http://127.0.0.1:8080` |
| `JINA_API_KEY` | Jina Reranker 精排（listwise）；缺失时保留 RRF 顺序 | 无默认；缺失即降级 |
| `JINA_RERANK_MODEL` | 覆盖精排模型；链式回退 v3.5 → v3 → v2-base-multilingual | `jina-reranker-v3.5` |
| `OLLAMA_URL` | 本地 ollama 基址（`--expand` / `--answer` 用） | `http://127.0.0.1:11434` |
| `EXPAND_MODEL` | `--expand` 的查询改写模型 | `qwen3.5:9b` |
| `ANSWER_MODEL` | `--answer` 的本地答案模型 | `qwen3.5:9b` |
| `ANSWER_DS_MODEL` | `--answer deepseek` 走的 chat 模型 | `deepseek-flash` |

MCP 客户端如果支持 `env` 字段，可以直接把 key 写在客户端配置里（`docs/adapters.md` 有各客户端的片段）；不适配的环境就走下面的文件回退。

## `~/.config/agent-env` 回退链

查一个变量的顺序（先命中先用）：

1. 进程环境变量（`os.environ`）
2. `~/.config/agent-env`
3. `~/.config/ai-keys.env`
4. 代码内置默认值（上表的"默认"列）

两个文件都按行解析，支持 `KEY=value` 与 `export KEY=value`，值两端的引号会被去掉；同一文件里出现多次时**取第一条**。忽略空行和注释。示例：

```bash
# ~/.config/agent-env
export EXA_API_KEY=exa-...
export BRAVE_API_KEY=bsa-...
TAVILY_API_KEY=tvly-...
SEARXNG_URL=http://127.0.0.1:8080
```

这条回退链对 GUI 启动的客户端尤其重要：Claude Desktop、Windsurf、Antigravity App 往往拿不到你 shell 里的 `export`，但服务进程在每次请求时重新读文件，所以把 key 写进 `~/.config/agent-env` 就够了，不用改客户端配置。

## 冒烟测试

不接客户端，直接喂一行 `initialize` 验证安装与协议：

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' | uvx --from agg-search@latest agg-search-mcp
```

期望输出（单行 JSON；`instructions` 字段较长，这里省略）：

```json
{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "agg-search", "version": "0.2.0"}, "instructions": "…"}}
```

`initialize` 时客户端请求哪个受支持的旧版协议，服务端就回哪个（`2025-11-25` / `2025-06-18` / `2025-03-26` / `2024-11-05`）；不认识的版本回 `2025-11-25`。`2026-07-28` 客户端不握手，直接在每个请求的 `_meta` 里带版本（可先调 `server/discover`）。`version` 随安装版本变化。

看到带 `serverInfo` 的 JSON 就说明进程能起、协议能应答；`version` 会随安装版本变化。再验一次工具列表就把 `method` 换成 `tools/list`（`id` 用 2）。若只看到 uv 的下载日志、没有 JSON 响应，通常是 `uvx` 不在 `PATH`（GUI 客户端场景）——换成 `which uvx` 得到的绝对路径。
