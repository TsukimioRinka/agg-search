# 交接说明（给下一个 Agent）

本文件描述：东西放在哪、当前状态、怎么逐层验证、怎么发版、哪些限制别踩。

## 1. 位置

| 东西 | 位置 |
|---|---|
| 本机工作仓库 | `~/projects/agg-search`（git，分支 `main`；2026-11-19 从 `/tmp/agg-search-pub` 迁来，旧路径已不存在） |
| GitHub 仓库 | https://github.com/TsukimioRinka/agg-search |
| Releases | https://github.com/TsukimioRinka/agg-search/releases（最新 `v0.4.1`） |
| CI | https://github.com/TsukimioRinka/agg-search/actions（tests 3.10–3.13 + ruff，publish 已钉 SHA） |
| PyPI | https://pypi.org/project/agg-search/（最新 `0.4.1`） |
| MCP Registry 条目 | `https://registry.modelcontextprotocol.io/v0/servers/io.github.TsukimioRinka%2Fagg-search/versions` |
| 下载门类（人读/机读） | 仓库 `docs/catalog.md` 与 `catalog.json` |

本机运行期文件（**含密钥，不要 cat/打印**）：

| 文件 | 内容 |
|---|---|
| `~/.config/agent-env` (600) | 各引擎 API key + `AGG_SEARCH_ALLOW_CIDRS=198.18.0.0/15`；改动前的备份 `~/.config/agent-env.bak-*` (600) |
| `~/.config/pypi-token` (600) | PyPI API token（`uv publish` 用） |
| `~/.config/mcp-publisher/token.json` (600) | MCP Registry JWT（会过期，`mcp-publisher login github -token "$(gh auth token)"` 重取） |
| `~/.cache/agg-search/` (700) | `results_cache.json`、`cooldown.json`、`fetch_cache.json`、`feedback.json`、`recent.json` |
| `~/.config/agg-search/weights.json` (600) | `agg-search-tune` 写出的引擎权重乘数 |

`AGENT_ENV` 里那行 CIDR 是给 clash TUN 的 fake-IP 用的（`198.18.0.0/15`）；没有它，SSRF 守卫会把所有公网域名当私网拒绝。**云元数据 IP（169.254.169.254 / fd00:ec2::254）不受它影响，永远拒绝。**

## 2. 当前状态（2026-11-19）

- 版本 `0.4.1`，197 个离线测试，ruff（F/E9/I001）干净，工作区无未提交改动。
- PyPI `0.4.1`；MCP Registry `0.4.1 active`（0.4.0 及更早已 deprecated）；GitHub `v0.4.1`。
- 能力面：CLI + stdio MCP（2025-06-18）+ streamable HTTP、4 个领域画像、3 个免 key 引擎（openalex/hn/stack）、本地反馈回路、13 客户端配置生成器、agent×领域 catalog。
- 安全面（独立审计后的 0.4.1）：元数据无条件拦截、3xx 拒绝、容器内最终 URL 复检、`allow_private`/`allow_cidrs` 只允许服务端环境变量、引擎响应 4 MiB 上限、Markdown 转义与控制字符剥离、HTTP 拒绝重复 Content-Length/CL+TE 且有 30s 超时与 64 连接上限、MCP 在飞调用上限 16。

## 3. 逐层验证（按顺序，全部离线优先）

```bash
cd ~/projects/agg-search
uv venv .venv && VIRTUAL_ENV=$PWD/.venv uv pip install -e .
.venv/bin/python -m unittest discover -s tests -v     # 期望 197 OK
ruff check --select F,E9,I001 src tests                # 期望无输出
```

```bash
# 画像 / 生效配置（密钥只出布尔值）
.venv/bin/agg-search --list-profiles
.venv/bin/agg-search --show-config --profile academic | jq '{profile,primary,rerank_blend,weights}'

# 真实搜索（免 key 路径，不烧额度）
.venv/bin/agg-search "speculative decoding" --profile academic \
  --engines openalex --no-jina --top-n 3 --fast --no-cache
```

```bash
# fetch：正常页面（依赖 agent-env 的 CIDR）
.venv/bin/crawl-fetch --url https://example.com/ --no-cache
# 期望 status 200 / title "Example Domain"（DeepSeek 402 时会落容器 static 档）

# 安全不变量
.venv/bin/crawl-fetch --url http://169.254.169.254/latest/meta-data/   # 必须 blocked: metadata address
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"fetch","arguments":{"urls":["http://127.0.0.1:1/"],"allow_private":true}}}' \
  | .venv/bin/agg-search-mcp | jq -r .result.content[0].text   # 必须仍是 blocked（参数不能放大守卫）
```

```bash
# MCP 协议面
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | .venv/bin/agg-search-mcp | jq '.result.tools[].name'

# HTTP 传输（另开一个终端跑服务）
.venv/bin/agg-search-http --port 18765 &
curl -s -X POST http://127.0.0.1:18765/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' | jq '.result.serverInfo'
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:18765/mcp \
  -H 'Origin: https://evil.example' -H 'Content-Type: application/json' -d '{}'   # 期望 403
```

```bash
# 安装器（用假 HOME，别碰真实配置）
HOME=/tmp/handoff-home .venv/bin/agg-search-install -c cursor --write
HOME=/tmp/handoff-home .venv/bin/agg-search-install -c cursor --check   # 期望 ok
.venv/bin/agg-search-install --catalog | jq '.agents|length'            # 期望 13
```

## 4. 发版流程（与历史一致）

1. 改 `pyproject.toml` 与 `server.json` 的版本（两处：顶层 + packages[0]）。
2. `CHANGELOG.md` 新段落；跑全量测试与 ruff。
3. `uv build` → `uv publish --token "$(cat ~/.config/pypi-token)"`。
4. `mcp-publisher login github -token "$(gh auth token)"` → `mcp-publisher publish`；把上一版 `mcp-publisher status --status deprecated ...`。
5. `git tag -a vX.Y.Z && git push origin main vX.Y.Z`。
6. `gh release create vX.Y.Z dist/*`（`publish` workflow 会跑；`PYPI_PUBLISH` 变量未设时 PyPI job 跳过，属预期）。
7. 验收：`uvx --from agg-search@latest agg-search --version`/`--show-config`、registry `/versions` 第一条为 active。

## 5. 已知限制（不要当成 bug 修一半）

- SSRF 守卫只校验「送出去的 URL」与「容器最终 URL」；DNS 解析两次，具备 DNS 控制权的对手仍可 rebinding。
- DeepSeek Fetch 的出口在服务端，守卫管不到它实际取哪。
- `news` 画像没有自动时效标注，未做基准；academic/code 的 precision@3 只基于 3 条问句（见 `docs/profiles.md`）。
- 反馈回路只在经由本工具 `fetch` 读到页面时记 `used`；纯 CLI 用户乘数停在 1.0。
- GUI 客户端（Cursor/VS Code/Cline/Windsurf/Zed/Continue/Claude Desktop）未实测，只有配置与深链；CLI 实测记录见 `docs/agent-tests.md`。
- `publish.yml` 里 `pip install build` 未做 hash 锁定（项目运行时零依赖，风险低）。

## 6. 给下一个检查者的最小清单

- [ ] `git -C ~/projects/agg-search status` 干净，HEAD 与 `git ls-remote origin main` 一致。
- [ ] 197 测试 + ruff 通过（第 3 节命令）。
- [ ] `crawl-fetch https://example.com` 返回 200（验证 agent-env 的 CIDR 行生效）。
- [ ] 元数据 URL 被 blocked；MCP 参数 `allow_private:true` 不生效。
- [ ] `agg-search --show-config` 不出现任何密钥值，只有布尔。
- [ ] GitHub Actions 最新两次 run 为 success；registry 最新版本 active 且与 PyPI 一致。
- [ ] 如需改 `agent-env`：先 `cp -p` 备份，改完 `chmod 600`，并用 `env -u` 验证 CLI 从文件读到值。

## 7. 过程文件：保留清单与清理规则

本机**保留**（正常工作集）：

| 保留 | 原因 |
|---|---|
| `~/projects/agg-search`（含 `.venv` 196K、`.git` 2.6M） | 唯一工作副本；`.venv` 是 editable 开发环境，已 gitignore |
| `~/.cache/agg-search/` | 运行期缓存与反馈计数（产品数据，删除只会丢缓存） |
| `~/.config/agg-search/weights.json` | 调优权重（`agg-search-tune` 产出） |
| `~/.local/bin/mcp-publisher` (19.7M) | Registry 发布工具，刚需 |
| `~/.config/agent-env` + `agent-env.bak-*` | 密钥与 CIDR；两份都是 600 |
| `searxng` 容器（2026-09-19 创建） | 用户既有的本地搜索服务，**不是测试残留** |

本轮**已删除**的过程垃圾（可复查确认不存在）：

```
/tmp/agentlab                  # 1.7G 隔离 agent 实验室（npm 全局前缀/缓存、CODEX_HOME、XDG、各 CLI）
/tmp/venv-lead /tmp/venv-021 /tmp/venv-fetch /tmp/venv-filters
/tmp/mcp-pub                   # 28M，安装 mcp-publisher 时的解压目录
/tmp/domaintune /tmp/learn-cache /tmp/learn-config /tmp/fakehome*
/tmp/reg.json /tmp/mcp-server.schema.json /tmp/times /tmp/t1 /tmp/http.log
/tmp/testadd.py /tmp/agg_files.txt
```

隔离与清理规则（下一轮必须遵守）：

1. 所有 agent/工具装进 `/tmp/<lab>`：独立 `HOME`、`XDG_CONFIG_HOME`、`XDG_DATA_HOME`、`XDG_CACHE_HOME`、`CODEX_HOME`、`npm_config_prefix`、`npm_config_cache`；**禁止** `npm i -g` 到真实前缀。
2. 只测 CLI 客户端；GUI 只给配置/深链，不安装。
3. 测试用容器一律 `--rm`；结束后 `docker ps` 只应看到既有的 `searxng`。
4. 删临时目录前先 `pgrep -af` 确认没有残留进程；删后再 `ls -d /tmp/agentlab /tmp/venv-*` 复查。
5. 任何时刻不得 `cat`/`echo`/提交 `~/.config/agent-env`、`~/.config/pypi-token`、`~/.config/mcp-publisher/token.json`；错误输出也不能带出它们的值。
6. 不把测试用的环境变量（`AGG_SEARCH_*`、`HOME`、`XDG_*`）写进 shell 的持久配置或仓库文件。

## 8. 下一阶段目标：门户隔离 + 领域强化（给下一个 agent 的任务书）

现有基础：`profiles`（engines/weights/recency/snippet/primary/rerank_blend）、`catalog`（agent × profile 的入口）、反馈回路、`agg-search-tune`、MCP stdio/HTTP。

要做的增量（按可验证性排序）：

1. **门户（portal）抽象**：`portal = agent × domain`。每个 portal 是一个数据描述，至少包含
   - `tools`：该门户暴露的 MCP 工具子集与**调用顺序**（例：academic 先 `search` 再 `fetch` 再 `answer`；code 先 `search` 再 `fetch`）；
   - `prompts`：该领域的**内置提示词**（查询改写模板、答案模板、结果筛选提示），与通用模板分开；
   - `dependencies`：所需引擎/密钥，缺失时如何降级（明确列出）；
   - `profile`：绑定到现有 domain profile（引擎集合、权重、recency、blend）。
   生成物：`portals.json`（机读）、`docs/portals.md`（人读）、`agg-search-install --portal <agent>.<domain>` 的入口。
2. **领域提示词与工具顺序落地**：`search --profile` 已换引擎；下一步把 `expand`/`answer` 的 prompt 按 portal 覆盖（当前写死在代码里），并让 MCP 工具描述/instructions 按 portal 不同（同一个 server 名不能同时服务两种语义时，用独立 server 名，如 `agg-search-academic`）。
3. **新增强机制（先评测再合并）**：候选包括按领域的时间窗/权威域先验、查询改写模板化、多查询融合的变体数按领域设定、结果内容级去重、fetch 前预取 top-1 的“读再答”流水线。每个机制要有对照实验（precision@3 或人工抽检 + 延迟/成本），只有达标才进默认路径。
4. **不同 agent 的依赖/降级差异**：Claude Code 可能没有本地 ollama（`expand` 不可用）→ portal 声明 `dependencies`，缺依赖时自动退回无扩展路径并在 `engine_health` 标注；Codex/OpenCode 有本地模型时启用扩展。
5. **门户级测试**：为每个新 portal 写离线测试（prompt 渲染、工具顺序、缺依赖降级），并在隔离环境里用真实 CLI 做一次端到端（按第 7 节规则），结果写进 `docs/agent-tests.md` 对应小节。
6. **发布**：语义变化（新增 portal/默认行为变化）→ 升 minor；纯修复 → patch。发版按第 4 节，catalog/portals 文档一起更新。
