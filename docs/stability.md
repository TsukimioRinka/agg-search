# 稳定性承诺（1.0 起）

从 1.0.0 开始，agg-search 按 [SemVer](https://semver.org/lang/zh-CN/) 发版：下面「稳定接口」里的东西只在**主版本**（2.0）里做不兼容改动；次版本（1.x）只做新增；补丁版本（1.0.x）只修缺陷。

## 稳定接口

| 接口 | 稳定的内容 |
|---|---|
| 命令名 | `agg-search`、`crawl-fetch`、`agg-search-mcp`、`agg-search-http`、`agg-search-install`、`agg-search-tune`、`agg-search-eval` |
| CLI 参数 | 各命令 `--help` 中列出的参数名、取值和含义；新增参数不算破坏 |
| `agg-search --json` 输出 | 顶层字段 `query`、`engines`、`expand`、`rerank`、`answer`、`answer_note`、`engine_health`、`results`；每条结果的 `rank`、`title`、`url`、`snippet`、`engines`、`rrf`；`engine_health` 中的 `cached`、`profile`、`portal`、`primary`、`degraded`、`latency_ms`、`timing`。可以新增字段，不会删除或改变已有字段的类型 |
| `engines` 状态字符串 | 以 `ok (N)` 开头表示成功（后面可能跟 `; partial: …` 说明），以 `skipped` 开头表示跳过或失败。具体原因文本不稳定 |
| `crawl-fetch` | 每行一个 JSON 对象，字段 `url`、`tier_used`、`status`、`title`、`text`、`markdown`、`blocked`、`error`、`error_class`、`cached`；退出码 `0` 至少一个成功、`1` 全部失败、`2` 启动器或镜像缺失、`3` 全部被拦截 |
| `error_class` 取值 | `docs/fetching.md` 列出的 15 个值。次版本可能**新增**取值，调用方应把不认识的值当作 `other` |
| MCP 工具 | 工具名 `search`、`fetch`、`answer`，各自的必填参数与 `outputSchema` 中的字段；门户未启用的工具返回 JSON-RPC `-32602` |
| MCP 提示 | 名称 `<domain>-research`（无门户时 `web-research`），参数 `topic` |
| MCP 协议版本 | 支持 `2025-06-18`、`2025-03-26`、`2024-11-05`；新版本会在次版本中加入 |
| 画像与门户 | 画像名 `general`、`academic`、`code`、`news`；门户 id `<agent>.<domain>` 与 server 名（`agg-search`、`agg-search-academic`、`agg-search-code`、`agg-search-news`）；`portals.json` 的结构 |
| 引擎名 | `docs/profiles.md` 与 `--engines` 帮助中列出的名字；新增引擎不算破坏 |
| 环境变量 | `docs/environment.md` 列出的变量名与含义 |
| 安全行为 | 云元数据地址永远拒绝；`allow_private`/`allow_cidrs` 只能由服务端环境变量放宽；拒绝 3xx；引擎响应 4 MiB 上限；页面文本剥离控制字符。这些只会更严格，不会放宽 |
| 运行时依赖 | 只用 Python 标准库；支持 Python 3.10–3.13 |

## 不在承诺范围内

- **结果的具体排序与内容**：默认引擎集合、RRF 权重、权威域列表、重排混合系数等质量参数，会在次版本里按评测数据调整（数据写在 `docs/benchmarks.md`，改动写在 `CHANGELOG.md`）。需要固定行为时请显式传 `--engines`、`--profile` 等参数。
- **人读文本**：Markdown 输出格式、错误信息与 `degraded` 说明的措辞、`--help` 的描述文字。
- **`agg-search-eval` 的输出格式**：这是开发用的评测工具。
- **Python 模块内部**：`agg_search.*` 里的函数和类不是公开库接口，可能随时改名。
- **缓存与状态文件格式**：`~/.cache/agg-search/` 下的文件；升级后可能被忽略并重建。
- **第三方服务的可用性**：引擎接口、额度与限流由各服务商决定。

## 弃用流程

需要改变稳定接口时，先在一个次版本里保留旧行为并给出弃用提示（`CHANGELOG.md` 的 Deprecated 段、CLI 在 stderr 打印一次提示），至少再经过一个次版本，才会在下一个主版本中移除。
