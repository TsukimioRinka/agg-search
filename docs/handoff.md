# 交接说明（给下一个 Agent）

本文件描述：东西放在哪、当前状态、怎么逐层验证、怎么发版、哪些限制别踩。

## 1. 位置

| 东西 | 位置 |
|---|---|
| 本机工作仓库 | `~/projects/agg-search`（git，分支 `main`；2026-11-19 从 `/tmp/agg-search-pub` 迁来，旧路径已不存在） |
| GitHub 仓库 | https://github.com/TsukimioRinka/agg-search |
| Releases | https://github.com/TsukimioRinka/agg-search/releases（最新 `v0.9.0`） |
| CI | https://github.com/TsukimioRinka/agg-search/actions（tests 3.10–3.13 + ruff，publish 已钉 SHA） |
| PyPI | https://pypi.org/project/agg-search/（最新 `0.9.0`） |
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

- 版本 `0.9.0`，281 个离线测试，ruff（F/E9/I001）干净，工作区无未提交改动。
- PyPI `0.9.0`；MCP Registry `0.9.0 active`（0.8.0 及更早已 deprecated）；GitHub `v0.9.0`。
- 能力面：CLI + stdio MCP（2025-06-18）+ streamable HTTP、4 个领域画像、4 个免 key 引擎（openalex/hn/stack/arxiv）、2 个新闻垂直引擎（brave_news/serper_news，复用原 key，opt-in，不在 news 默认里）、56 个 `agent × domain` 门户隔离（`portals.json` / `docs/portals.md` / `agg-search-install --portal`）、按门户隔离 MCP 工具子集/顺序（`academic` 暴露 `search -> fetch -> answer`；`code`/`news`/`general` 暴露 `search -> fetch`）与领域 prompt（`expand`/`answer`/`instructions`/`filter_hint`）、冷却/缺 key 后的 keyless fallback、内容级去重（默认开启、`--no-dedupe` 可关）、权威域软先验（`academic=1.35`、`code=1.25`、`code blend=0.4`）、opt-in `--answer-fetch N`、本地反馈回路、14 客户端配置生成器。
- 安全面（延续 0.4.1 审计）：元数据无条件拦截、3xx 拒绝、容器内最终 URL 复检、`allow_private`/`allow_cidrs` 只允许服务端环境变量、引擎响应 4 MiB 上限、Markdown 转义与控制字符剥离、HTTP 拒绝重复 Content-Length/CL+TE 且有 30s 超时与 64 连接上限、MCP 在飞调用上限 16；非门户工具调用（如 `code` 门户调 `answer`）返回 JSON-RPC `-32602`。

## 3. 逐层验证（按顺序，全部离线优先）

```bash
cd ~/projects/agg-search
uv venv .venv && VIRTUAL_ENV=$PWD/.venv uv pip install -e .
.venv/bin/python -m unittest discover -s tests -v     # 期望 281 OK
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
- `academic` 门户在 `openalex` 首次遭遇 429 的**当次**就会补召 `searxng`（0.5.1 运行时补召，`degraded` 记 `required engine(s) openalex unavailable; supplemented searxng`），同时写入冷却；补召结果不写缓存。由 `tests/test_portals.py::test_academic_first_429_supplements_in_the_same_call` 锁定。真实 429 未能人为复现，只有离线证据。
- `--expand` 默认保持 `0`（实测本地 `qwen3.5:9b` 在 `expand=3` 会幻觉实体/年份且多倍消耗引擎额度）；`--answer-fetch` 默认保持 `0`（容器抓反爬页延迟最高 +26.7s）。
- 反馈回路只在经由本工具 `fetch` 读到页面时记 `used`；纯 CLI 用户乘数停在 1.0。
- GUI 客户端（Cursor/VS Code/Cline/Windsurf/Zed/Continue/Claude Desktop）未实测，只有配置与深链；CLI 实测记录见 `docs/agent-tests.md`。
- `publish.yml` 里 `pip install build` 未做 hash 锁定（项目运行时零依赖，风险低）。

## 6. 给下一个检查者的最小清单

- [ ] `git -C ~/projects/agg-search status` 干净，HEAD 与 `git ls-remote origin main` 一致。
- [ ] 281 测试 + ruff 通过（第 3 节命令）。
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

## 7.2 第 7–9 轮记录（0.6.0）

- 第 7 轮 `fix(install)`：`--portal X --check` 现在校验条目绑定的门户（`env.AGG_SEARCH_PORTAL` 或 `--portal` 参数），绑错或无绑定返回 1。真实 `~/.omp/agent/mcp.json` 只读实测：omp.general/academic/code 为 ok，omp.news 报未注册。
- 第 8 轮 `feat(fetch)`：失败/拦截结果带 `error_class`（固定词表，见 `docs/fetching.md`），MCP `fetch` 的 `outputSchema` 声明同一枚举。实测曾把不可解析域名误判为 `ssrf_blocked`，已修为 `dns` 并加回归用例。
- 第 9 轮评测：`news` day→week 空结果回退 **拒绝**（20 条冷门查询空结果 0/20，见 `docs/benchmarks.md` 候选 F）；同时补测试锁定 `academic` 首次 429 当次补召。评测中发现 `firecrawl` 额度耗尽（402/429），属账户问题，不是代码缺陷。
- 本机回落：`uv tool` 0.5.3 → 0.6.0；三门户握手 0.6.0、工具 2/3/2；元数据 exit 3（带 `error_class: ssrf_blocked`）；example.com 200；无痕 OMP 会话 `omp.academic` + openalex 返回真实 DOI。`SKILL.md` 增加一行 `error_class` 重试建议。回滚备份：`~/.omp/agent/backups/mcp.json.20261120-215714`、`aggregate-search-skill-20261120-215714.tar.gz`；回滚命令 `uv tool install --force agg-search==0.5.3` 后解包恢复。
- 下一轮优先：给 `firecrawl` 额度耗尽设更长冷却（402 应按「付费失效」而非普通错误处理，需先查 `classify_error` 现状）；其次 `tools/eval-offline.sh` 固化离线评测；再次 arxiv/crossref 免 key 引擎评测。

## 7.3 第 10 轮记录（0.7.0，领域细化）

- 先纠正上一轮的误判：`firecrawl` 402 早已按 `quota` 冷却 3600 s（`search.classify_error`）。上轮评测脚本直接调引擎函数绕过了冷却，才看到 20/20 失败，代码没问题。
- 评测方法：每领域录一次原始引擎返回，之后离线回放 `rrf_fuse` + `_blend_order`。主判据是本地 `qwen3.5:9b` 的 0/1/2 领域分级（零付费），Jina 只做参考（它也是重排器，有偏）。全部数据与拒绝项见 `docs/benchmarks.md` 候选 G。
- 合入：`news` 改用 `brave_news`/`serper_news` + `rerank_blend=0.3`；`arxiv` 免 key 引擎只进 `academic` 补召；`code` 补召加 `searxng`。
- 拒绝：stack 去中文、扩 code 权威域、arxiv 进学术默认路径、新闻 web+垂直并用。
- 本机回落 0.6.0 → 0.7.0；三门户握手 0.7.0，工具 2/3/2；元数据 exit 3；example.com 200；三门户 `--check` ok；无痕 OMP 会话 `omp.academic` 返回真实 DOI。`SKILL.md` 补了新闻日期前缀与补召源说明。回滚备份 `~/.omp/agent/backups/mcp.json.20261120-225224`、`aggregate-search-skill-20261120-225224.tar.gz`；回滚命令 `uv tool install --force agg-search==0.6.0` 后解包恢复。
- 已知外部状态：本机 SearXNG 上游被限流（brave/google cse/wikipedia `Suspended`，duckduckgo `CAPTCHA`），此时 `code` 全降级路径返回 0 条；`firecrawl` 与 `deepseek` 搜索额度 402。
- 下一轮优先：(1) `tools/eval-offline.sh` 把本轮 record → qwen 标注 → 回放流程固化成脚本（本轮脚本已随实验目录删除，方法记录在 benchmarks G）；(2) SearXNG 上游限流时的引擎轮换/健康提示；(3) `general` 画像在中文查询上的去重与重排评测（本轮未覆盖 general）。

## 7.4 第 11–13 轮记录（0.8.0）

- 第 11 轮 `fix(searxng)`：SearXNG 上游全被限流时返回 200 + 0 条，以前当作健康空结果（不冷却、报「查询太窄」）。现在判为失败并按 `antibot` 冷却 600 s，原因列出被暂停的上游。实测本机当前状态即属此情况。
- 第 12 轮 `feat(eval)`：`agg-search-eval` 增加 `judge`（本地 qwen 0/1/2 或 Jina，可断点续跑）与 `compare`（离线回放画像变体）；`record` 默认跳过冷却中的引擎；`tools/eval-offline.sh QUERIES PROFILE RUBRIC [VARIANT...]` 串起全流程。以后做领域 A/B 直接用它，不要再手写一次性脚本。
- 第 13 轮评测（无改动）：`general` 中文 20 条，现有默认最优或持平；内容去重 11 对合并人工核对误杀 0（`docs/benchmarks.md` 候选 H）。
- 本机回落 0.7.0 → 0.8.0；三门户握手 0.8.0、工具 2/3/2；元数据 exit 3；example.com 200；三门户 `--check` ok；无痕 OMP 会话 `omp.academic` 返回真实 DOI。`SKILL.md` 本轮未改。回滚备份 `~/.omp/agent/backups/mcp.json.20261120-231537`；回滚命令 `uv tool install --force agg-search==0.7.0`。
- 下一轮优先：(1) 用 `eval-offline.sh` 扩大 news/code 评测集（目前每领域 10–22 条，同一天录制），确认 0.7.0 的新闻改动跨天稳定；(2) SearXNG 上游限流的根因在本机 SearXNG 配置（brave/google cse 公共接口被限），可考虑调整 `searxng` 容器的上游列表，但那是用户的生产服务，**须先征得同意**；(3) `code` 门户纯免 key 路径仍弱（nDCG 0.253），可评测 Stack Exchange 多站点（unix/superuser/serverfault）。

## 7.5 第 14–16 轮记录（0.9.0）

- 第 14 轮 `feat(stack)`：`STACKEXCHANGE_SITE` 支持逗号列表（opt-in）。有付费引擎时无增益；只有 stack 时 nDCG 0.303 → 0.420（候选 I）。
- 第 15 轮 **撤回 0.7.0 的新闻默认**：两批新查询（各 16 条，事件型）上 0.7.0 默认明显变差，42 条加权 web 0.806 vs 0.776（候选 J）。同时修了 `judge` 提示词缺参照日期的问题。教训：改默认前每个领域至少两批、覆盖不同查询类型。评测数据保存在 `~/.cache/agg-search/eval/`（news-set2/3、code-sites），可直接 `agg-search-eval compare` 复跑。
- 第 16 轮 SearXNG 上游（经用户同意）：启用 `360search` 后上游全在封禁期、验证 0/6，按止损标准回滚，配置与改前逐字节一致（候选 K）。**SearXNG 当前仍被上游限流**，是改前就有的外部状态。
- 本机回落 0.8.0 → 0.9.0；三门户握手 0.9.0、工具 2/3/2；元数据 exit 3；example.com 200；`--check` 三门户 ok；无痕 OMP 会话 `omp.academic` 返回真实 DOI。`SKILL.md` 改正了 0.7.0 时写的新闻说明（改为 opt-in 写法）。回滚备份 `~/.omp/agent/backups/mcp.json.20261120-235757`、`aggregate-search-skill-20261120-235757.tar.gz`；回滚命令 `uv tool install --force agg-search==0.8.0` 后解包恢复。
- 下一轮优先：(1) 隔天、无评测流量时重测 SearXNG `360search`（先用 `engines=` 参数，不改配置）；(2) 新闻 opt-in 并用方案若想进默认，需要成本可接受的证据（例如只在 web 结果新鲜度不足时才追加垂直端点）；(3) 评测流量本身会触发上游限流，以后录制时控制频率。

## 8. 门户隔离与领域强化（0.5.0 已完成及后续可选方向）

0.5.0 已完成：`portals.json` / `docs/portals.md` / `agg-search-install --portal`、按门户隔离工具子集/顺序与领域 prompt、缺失依赖降级与 `engine_health.degraded`、5 项增强机制对照评测（合入内容去重、权威域先验、`code blend=0.4`、opt-in `--answer-fetch`；拒绝默认 `--expand`）及 6 CLI 客户端隔离端到端验证。

后续若继续演进，可考虑：
1. ~~首次 429 当场切到 `keyless_fallback`~~：0.5.1 已实现，0.6.0 补了回归测试。
2. 在更大规模人工标注集上评估通用 `rerank_blend` 与中文同源去重规则。
3. 对 GUI 客户端在独立沙箱内补真实点击深链测试。
