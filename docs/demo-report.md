# Demo / 验证报告 — `feat/engine-registry-hardening`

日期：2026-11-20 · 分支：`feat/engine-registry-hardening`（基于 `main` @ 0.5.0）
范围：0.5.0 只读评审发现的 4 项问题的修复与验证。**脚本不修改仓库**：生成物写进临时目录后与仓库文件比对。

## 修复项

| # | 问题 | 修复 |
|---|---|---|
| 1 | 引擎元数据分散在 `search.ENGINES` / `CREDENTIAL_ENV` / `DEFAULT_ENGINES` / MCP enum / `portals.ENGINE_ENV_REQUIREMENTS`，新增引擎要改 5–6 处 | 新增 `src/agg_search/engine_specs.py` 单一注册表；上述 5 处全部派生；新增 parity 测试（任一层漂移即失败） |
| 2 | `catalog.json` 内嵌本机绝对路径 `/home/body/...` | 生成物统一 `~/...`（`install.display_path`）；测试断言生成物不含家目录绝对路径 |
| 3 | `answer` 工具 `sources[].text` 无上限（只受 MCP 1 MiB body cap） | schema `maxLength: 20000`（snippet 2000）；运行时单条 20k、总量 60k 截断，并在 `degraded` 标注 |
| 4 | 门户 `required_engines` 运行中失败时不会补位（只在凭据/冷却全过滤时回退） | 新增 `missing_required` + `supplement_keyless`：主引擎失败/冷却后用 keyless fallback 做 8s 有界补召，合并结果与状态；补召结果**不写缓存** |

附带：`--answer` 帮助与 README 明确 `auto` 可能落到计费 DeepSeek；`docs/portals.md` 明确工具顺序是编排建议而非强制流水线（`answer(urls=…)` 预取除外）。

## 验证结果

| 检查 | 结果 |
|---|---|
| 单元测试 | **234 个 OK**（新增 8 个：registry parity、runtime fallback、answer caps、artifact hygiene） |
| ruff `F,E9,I001` | All checks passed |
| 生成物确定性 | `portals.json` / `docs/portals.md` / `catalog.json` / `docs/catalog.md` 与重新生成结果 **逐字节一致** |
| 生成物路径卫生 | `catalog.json`、`portals.json` 中无 `/home/body` |
| 门户隔离 | `claude-code.academic` → `[search, fetch, answer]`；`claude-code.code` → `[search, fetch]`；code 上调 `answer` → `-32602` |
| 本地 CLI 门户面 | `--list-portals` 正常；`--show-config --portal claude-code.academic` 报 `portal/tools` |
| 发布包抽检（可选） | `DEMO_CHECK_PYPI=1` 时用 `uvx` 拉 PyPI 最新版对比门户列表 |

## 复现

```bash
cd ~/projects/agg-search
git switch feat/engine-registry-hardening
bash tools/demo-portals.sh
# 可选：连 PyPI 最新版一起抽检
DEMO_CHECK_PYPI=1 bash tools/demo-portals.sh
```

脚本自清理临时目录（`mktemp -d` + trap），不写入 `portals.json` / `catalog.json` / `docs/`。

## 已知未覆盖

- 补召路径在**真实引擎失败**下未做在线验证（单测用假引擎覆盖逻辑）；合并后建议用 `openalex` 冷却场景实测一次。
- `catalog.json` 的 `path` 现在是 `~/...`，`--write` 仍使用绝对路径展开，行为不变。
- 版本号未在分支内提升（仍 0.5.0，CHANGELOG 为 `Unreleased — target 0.5.1`），由合并方按流程升版发布。

## 合并记录

- **合并日期**：2026-11-20
- **合并与发版提交**：
  - PR #1（`feat/engine-registry-hardening`）squash 合并至 `main`：`70f01c2`
  - `0.5.1` 发版提交：`84976b4`，tag：`v0.5.1`
- **评审确认项**：
  - SSRF 防护、仅服务端 `allow_private` / `allow_cidrs`、3xx 拒绝与请求走私保护均未被修改；实测 `crawl-fetch --url http://169.254.169.254/latest/meta-data/ --no-cache` 拦截（exit 3，`guard`），`https://example.com/` 返回 `200`（`static`）。
  - 产物不含真实家目录绝对路径或密钥；`agg-search --show-config` 仅输出凭据布尔值。
  - 门户运行时回退（`missing_required` + `supplement_keyless`）限定 8s，结果不写缓存（`if pending_cache and not supplemented:`）。
- **合并前发现的边界问题及修复（相对 PR #1 的唯一代码偏差）**：
  - `src/agg_search/mcp.py::_bound_sources` 原实现使用 `elif used + len(text) > total:`，当某条来源同时超过单条 20k 与累计 60k 上限时（例如 `[15k, 15k, 15k, 25k]`），会跳过总量截断导致总字符达到 65k > `MAX_ANSWER_CHARS`；且在循环末尾 `if used >= total: break` 会静默丢弃后续来源而未在 `degraded` 标注。
  - 已在 `84976b4` 中将 `elif` 改为独立 `if`，并在循环入口检查 `used >= total` 时补充 `answer sources truncated at 60000 chars`；同步在 `tests/test_registry_demo.py` 增加 2 个回归测试（测试总数 `234 → 236`）。
  - 备注：`agg-search` CLI 无 `--version` 参数，包版本通过 `importlib.metadata` / `agg_search.__version__` 与 `agg-search --show-config` 核验。
- **发布与一致性验收结果**：
  - 本地复验（`DEMO_CHECK_PYPI=1 bash tools/demo-portals.sh`）：`ruff` clean、**236 tests OK**、4 份生成产物与重生成结果逐字节一致、门户隔离与截断边界通过、PyPI latest 抽检通过。
  - **PyPI**：`https://pypi.org/pypi/agg-search/0.5.1/json` 与 `/pypi/agg-search/json` 均为 `0.5.1`（`bdist_wheel` + `sdist`）；`uvx --refresh --from agg-search@latest` 运行版本为 `0.5.1`，`--list-portals` 共 `52` 个门户。
  - **MCP Registry**：`io.github.TsukimioRinka/agg-search` 最新条为 `0.5.1`（`active`），`0.5.0` 已标为 `deprecated`。
  - **GitHub Release & CI**：`v0.5.1` 已附 `agg_search-0.5.1-py3-none-any.whl` 与 `agg_search-0.5.1.tar.gz`；Actions `tests`（`44739803374`）与 `publish`（`44739955513`）均为 `success`。
- **清理确认**：临时目录与构建目录 `dist/` 已清理，`pgrep` 无 `agg-search` / `mcp-inspector` 残留，`docker ps` 仅保留既有 `searxng`，未打印任何本地密钥文件内容。
