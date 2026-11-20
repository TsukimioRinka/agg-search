# 交接说明（给下一个 Agent）

本文件描述：东西放在哪、当前状态、怎么逐层验证、怎么发版、哪些限制别踩。

## 1. 位置

| 东西 | 位置 |
|---|---|
| 本机工作仓库 | `~/projects/agg-search`（git，分支 `main`；2026-11-19 从 `/tmp/agg-search-pub` 迁来，旧路径已不存在） |
| GitHub 仓库 | https://github.com/TsukimioRinka/agg-search |
| Releases | https://github.com/TsukimioRinka/agg-search/releases（最新 `v0.5.3`） |
| CI | https://github.com/TsukimioRinka/agg-search/actions（tests 3.10–3.13 + ruff，publish 已钉 SHA） |
| PyPI | https://pypi.org/project/agg-search/（最新 `0.5.3`） |
| MCP Registry 条目 | `https://registry.modelcontextprotocol.io/v0/servers/io.github.TsukimioRinka%2Fagg-search/versions` |
| 下载门类（人读/机读） | 仓库 `docs/catalog.md` 与 `catalog.json` |
| 门户隔离（人读/机读） | 仓库 `docs/portals.md` 与 `portals.json`（56 个 `agent × domain`） |
| 增强评测数据 | 仓库 `docs/benchmarks.md` |

本机运行期文件（**含密钥，不要 cat/打印**）：

| 文件 | 内容 |
|---|---|
| `~/.config/agent-env` (600) | 各引擎 API key + `AGG_SEARCH_ALLOW_CIDRS=198.18.0.0/15`；改动前的备份 `~/.config/agent-env.bak-*` (600) |
| `~/.config/pypi-token` (600) | PyPI API token（`uv publish` 用） |
| `~/.config/mcp-publisher/token.json` (600) | MCP Registry JWT（会过期，`mcp-publisher login github -token "$(gh auth token)"` 重取） |
| `~/.cache/agg-search/` (700) | `results_cache.json`、`cooldown.json`、`fetch_cache.json`、`feedback.json`、`recent.json` |
| `~/.config/agg-search/weights.json` (600) | `agg-search-tune` 写出的引擎权重乘数 |

`AGENT_ENV` 里那行 CIDR 是给 clash TUN 的 fake-IP 用的（`198.18.0.0/15`）；没有它，SSRF 守卫会把所有公网域名当私网拒绝。**云元数据 IP（169.254.169.254 / fd00:ec2::254）不受它影响，永远拒绝。**

## 2. 当前状态（2026-11-20）

- 版本 `0.5.3`，238 个离线测试，ruff（F/E9/I001）干净，工作区无未提交改动。
- PyPI `0.5.3`；MCP Registry `0.5.3 active`（0.5.2 及更早已 deprecated）；GitHub `v0.5.3`。
- 能力面：CLI + stdio MCP（2025-06-18）+ streamable HTTP、4 个领域画像、3 个免 key 引擎（openalex/hn/stack）、56 个 `agent × domain` 门户隔离（`portals.json` / `docs/portals.md` / `agg-search-install --portal`）、按门户隔离 MCP 工具子集/顺序（`academic` 暴露 `search -> fetch -> answer`；`code`/`news`/`general` 暴露 `search -> fetch`）与领域 prompt（`expand`/`answer`/`instructions`/`filter_hint`）、冷却/缺 key 后的 keyless fallback、内容级去重（默认开启、`--no-dedupe` 可关）、权威域软先验（`academic=1.35`、`code=1.25`、`code blend=0.4`）、opt-in `--answer-fetch N`、本地反馈回路、14 客户端配置生成器。
- 安全面（延续 0.4.1 审计）：元数据无条件拦截、3xx 拒绝、容器内最终 URL 复检、`allow_private`/`allow_cidrs` 只允许服务端环境变量、引擎响应 4 MiB 上限、Markdown 转义与控制字符剥离、HTTP 拒绝重复 Content-Length/CL+TE 且有 30s 超时与 64 连接上限、MCP 在飞调用上限 16；非门户工具调用（如 `code` 门户调 `answer`）返回 JSON-RPC `-32602`。

## 3. 逐层验证（按顺序，全部离线优先）

```bash
cd ~/projects/agg-search
uv venv .venv && VIRTUAL_ENV=$PWD/.venv uv pip install -e .
.venv/bin/python -m unittest discover -s tests -v     # 期望 238 OK
ruff check --select F,E9,I001 src tests                # 期望无输出
```

```bash
# 画像 / 门户 / 生效配置（密钥只出布尔值）
.venv/bin/agg-search --list-profiles
.venv/bin/agg-search --list-portals | wc -l             # 期望 56
.venv/bin/agg-search --show-config --portal codex.academic | jq '{portal,profile,tools,authority_boost,dedupe}'
.venv/bin/agg-search --show-config --profile academic | jq '{profile,primary,rerank_blend,weights}'

# 真实搜索（免 key 路径，不烧额度）
.venv/bin/agg-search "speculative decoding" --portal codex.academic \
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
# MCP 协议面与门户隔离
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | .venv/bin/agg-search-mcp | jq '.result.tools[].name'
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | AGG_SEARCH_PORTAL=codex.academic .venv/bin/agg-search-mcp | jq '.result.tools[].name'    # search, fetch, answer
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | AGG_SEARCH_PORTAL=claude-code.code .venv/bin/agg-search-mcp | jq '.result.tools[].name' # search, fetch

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
HOME=/tmp/handoff-home .venv/bin/agg-search-install --portal cursor.academic --write
HOME=/tmp/handoff-home .venv/bin/agg-search-install --portal cursor.academic --check
.venv/bin/agg-search-install --catalog | jq '.agents|length'            # 期望 14
.venv/bin/agg-search-install --portals | jq '.portals|length'           # 期望 56
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
- `news` 窗口实测（6 条问句）确认 `day` 保持 6/6 非空且不漏入 2023–2025 旧闻，但未做人工金标；academic/code 的 18 条权威源@3 评测基于域名规则与人工抽检，10 条通用回归 `nDCG@10` 基于模型池化标注（见 `docs/benchmarks.md`）。
- 仅基于检索（retrieval-only）时 `academic` 在 `openalex` 首次请求遭遇 429 限速的当次仍会报错并写入 10min 冷却；随后同门户下次请求会自动进入 `searxng` 免 key 回退。
- `--expand` 默认保持 `0`（实测本地 `qwen3.5:9b` 在 `expand=3` 会幻觉实体/年份且多倍消耗引擎额度）；`--answer-fetch` 默认保持 `0`（容器抓反爬页延迟最高 +26.7s）。
- 反馈回路只在经由本工具 `fetch` 读到页面时记 `used`；纯 CLI 用户乘数停在 1.0。
- GUI 客户端（Cursor/VS Code/Cline/Windsurf/Zed/Continue/Claude Desktop）未实测，只有配置与深链；CLI 实测记录见 `docs/agent-tests.md`。
- `publish.yml` 里 `pip install build` 未做 hash 锁定（项目运行时零依赖，风险低）。

## 6. 给下一个检查者的最小清单

- [ ] `git -C ~/projects/agg-search status` 干净，HEAD 与 `git ls-remote origin main` 一致。
- [ ] 238 测试 + ruff 通过（第 3 节命令）。
- [ ] `portals.json` / `docs/portals.md` / `catalog.json` / `docs/catalog.md` 四者同步（`test_portals.py` 已校验）。
- [ ] `crawl-fetch https://example.com` 返回 200（验证 agent-env 的 CIDR 行生效）。
- [ ] 元数据 URL 被 blocked；MCP 参数 `allow_private:true` 不生效。
- [ ] `agg-search --show-config` 不出现任何密钥值，只有布尔。
- [ ] GitHub Actions 最新两次 run 为 success；registry 最新版本 active 且与 PyPI 一致。
- [ ] 如需改 `agent-env`：先 `cp -p` 备份，改完 `chmod 600`，并用 `env -u` 验证 CLI 从文件读到值。

## 7. 过程文件：保留清单与清理规则

本机**保留**（正常工作集）：

| 保留 | 原因 |
|---|---|
| `~/projects/agg-search`（含 `.venv`、`.git`） | 唯一工作副本；`.venv` 是 editable 开发环境，已 gitignore |
| `~/.cache/agg-search/` | 运行期缓存、评测 fixture 与反馈计数（产品数据，删除只会丢缓存） |
| `~/.config/agg-search/weights.json` | 调优权重（`agg-search-tune` 产出） |
| `~/.local/bin/mcp-publisher` (19.7M) | Registry 发布工具，刚需 |
| `~/.config/agent-env` + `agent-env.bak-*` | 密钥与 CIDR；两份都是 600 |
| `searxng` 容器（2026-09-19 创建） | 用户既有的本地搜索服务，**不是测试残留** |

本轮**已删除**的过程垃圾（均已 `pgrep` + `ls -d` 复查确认不存在）：

```
# 2026-11-19 上一轮遗留清理：
/tmp/aggvenv /tmp/wheelvenv /tmp/rel-build /tmp/relvenv /tmp/agg-test-venv
/tmp/demo.err /tmp/demo.json /tmp/demo2.err /tmp/demo2.json
/tmp/demo-check.png /tmp/repo-page.png /tmp/readme.err /tmp/readme.html

# 2026-11-20 本轮评测与隔离 CLI 实验室清理：
/tmp/eval-bench                  # Round 4 18-query fixture / Jina 分数 / B&D 对照实验输出
/tmp/portallab                   # Round 5 隔离 CLI 实验室（venv、npm 前缀/缓存、CODEX_HOME、XDG、HOME）
```

实测后宿主机状态复核（2026-11-20）：
- `docker ps` 仅见既有 `searxng`（`Up 12 hours`），无测试容器残留；
- 真实配置未受污染：`~/.codex/config.toml`、`~/.gemini/settings.json`、`~/.gemini/config/mcp_config.json` mtime/大小均与测前完全一致；`~/.claude.json`、`~/.config/opencode/opencode.json`、`~/.cursor/mcp.json` 保持不存在。

隔离与清理规则（下一轮必须遵守）：

1. 所有 agent/工具装进 `/tmp/<lab>`：独立 `HOME`、`XDG_CONFIG_HOME`、`XDG_DATA_HOME`、`XDG_CACHE_HOME`、`CODEX_HOME`、`npm_config_prefix`、`npm_config_cache`；**禁止** `npm i -g` 到真实前缀。
2. 只测 CLI 客户端；GUI 只给配置/深链，不安装。
3. 测试用容器一律 `--rm`；结束后 `docker ps` 只应看到既有的 `searxng`。
4. 删临时目录前先 `pgrep -af` 确认没有残留进程；删后再 `ls -d /tmp/portallab /tmp/eval-bench` 复查。
5. 任何时刻不得 `cat`/`echo`/提交 `~/.config/agent-env`、`~/.config/pypi-token`、`~/.config/mcp-publisher/token.json`；错误输出也不能带出它们的值。
6. 不把测试用的环境变量（`AGG_SEARCH_*`、`HOME`、`XDG_*`）写进 shell 的持久配置或仓库文件。

## 7.1 第 6 轮记录（0.5.3，CLI 与打包债）

- 完成：7 个入口全部支持 `--version`、SPDX license 元数据、Actions 钉到 Node 24 SHA。本轮未起隔离实验目录，无 `/tmp/agg-lab-*` 需清理；`dist/`、`build/`、`egg-info` 已删。
- 本机回落：`uv tool` 升到 0.5.3；`omp.general/academic/code` 握手版本 0.5.3、工具数 2/3/2；元数据 exit 3，example.com 200；无痕 OMP 会话调用 `agg-search-academic` 的 `openalex` 返回真实 DOI 链接且 `engine_health.portal=omp.academic`。回滚备份 `~/.omp/agent/backups/mcp.json.*`（本轮未改 mcp.json）。
- 下一轮优先：`news` 空结果自适应时间窗回退（需 A/B 门禁），其次 `agg-search-install --portal omp.* --check` 单测，再次 `crawl-fetch` 失败分类。

## 8. 门户隔离与领域强化（0.5.0 已完成及后续可选方向）

0.5.0 已完成：`portals.json` / `docs/portals.md` / `agg-search-install --portal`、按门户隔离工具子集/顺序与领域 prompt、缺失依赖降级与 `engine_health.degraded`、5 项增强机制对照评测（合入内容去重、权威域先验、`code blend=0.4`、opt-in `--answer-fetch`；拒绝默认 `--expand`）及 6 CLI 客户端隔离端到端验证。

后续若继续演进，可考虑：
1. 首次搜索即命中 `openalex` 429 时当场切到 `keyless_fallback`（当前是在下一次进入 cooldown 后自动切）。
2. 在更大规模人工标注集上评估通用 `rerank_blend` 与中文同源去重规则。
3. 对 GUI 客户端在独立沙箱内补真实点击深链测试。
