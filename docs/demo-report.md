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
