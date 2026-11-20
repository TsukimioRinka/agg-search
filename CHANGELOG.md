# Changelog

## [0.7.0] - 2026-11-20

Domain refinements for news, academic and code, each gated by an offline A/B
(see `docs/benchmarks.md`, candidate G).

### Added

- `brave_news` and `serper_news` engines: the news verticals of the existing
  providers (same keys, same request count). Snippets start with the
  provider's publication date, e.g. `[2026-11-20]`.
- `arxiv` keyless engine (Atom API, stdlib XML, DTDs refused). It ANDs the
  Latin-script terms of the query; a CJK-only query is a healthy empty result
  without a request. Not in the default engine set.

### Changed

- `news` profile: `brave_news, serper_news, tavily, firecrawl` with
  `rerank_blend=0.3`. On 10 queries (local qwen grading) news reports in the
  top 5 went from 25/50 to 42/50 and items under 72 hours old from 7/50 to
  50/50. Use `--engines brave,serper,...` for the previous behaviour.
- `academic` portal keyless fallback: `openalex, arxiv, searxng`. With
  openalex down and no paid keys, nDCG@10 0.544 -> 0.830.
- `code` portal keyless fallback: `stack, searxng, hn`. With stack down and
  no paid keys, nDCG@10 0.035 -> 0.253.

### Rejected after evaluation

- Stripping CJK from mixed queries before `stack`, a wider `code` authority
  list, `arxiv` on the healthy academic path, and web + news verticals together.

## [0.6.0] - 2026-11-20

Structured fetch failures and stricter portal checks.

### Added

- `crawl-fetch` tags every failed or blocked result with `error_class`, a
  fixed vocabulary (`ssrf_blocked`, `redirect_blocked`, `anti_bot`,
  `rate_limited`, `access_denied`, `http_4xx`, `http_5xx`, `timeout`,
  `tls`, `dns`, `connection`, `thin_content`, `too_large`,
  `no_credentials`, `other`). It never echoes upstream text. The MCP
  `fetch` `outputSchema` declares the same enum. Exit codes are unchanged.

### Fixed

- `agg-search-install --portal X --check` now fails when the entry exists but
  is bound to another portal (or none), instead of reporting `ok`.

### Tests

- Regression test for the same-call keyless supplement when `openalex`
  returns 429 on the first request (cooldown still written, result not cached).

### Rejected after evaluation

- `news` day->week widening on empty results: 0/20 empty `day` recalls on
  niche queries, no measurable benefit (see `docs/benchmarks.md`, candidate F).

## [0.5.3] - 2026-11-20

CLI and packaging hygiene.

### Added

- `--version` on every console entry point (`agg-search`, `crawl-fetch`,
  `agg-search-mcp`, `agg-search-http`, `agg-search-install`,
  `agg-search-tune`, `agg-search-eval`), covered by `tests/test_version_flag.py`.

### Changed

- `pyproject.toml` uses the SPDX `license = "MIT"` expression with
  `license-files` (build requires `setuptools>=77`); the deprecated table
  form and License classifier are gone, so `uv build` is warning-free.
- GitHub Actions pins moved to Node 24 releases: checkout v7.0.1,
  setup-python v7.1.0, upload-artifact v7.0.2, download-artifact v8.0.2.

## [0.5.2] - 2026-11-20

Add dedicated `omp` (oh-my-pi) portal descriptors and installer target.

### Added

- Dedicated `omp` agent and 4 `omp.*` portals (`omp.general`, `omp.academic`,
  `omp.code`, `omp.news`) in `portals.AGENT_SPECS` and `install.CLIENTS`,
  configured with `local_llm=True`, `answer_order=("local", "deepseek")`,
  `default_expand=0`, and JSON config path `~/.omp/agent/mcp.json` (14 agents /
  56 portals total).

### Fixed

- `install.portals_markdown` now derives the total portal count dynamically
  from `len(portal_map)` instead of hardcoding `52`.

## [0.5.1] - 2026-11-20

Hardening pass from the post-0.5.0 review: one engine registry, runtime
fallback, answer caps and portable artifacts.

### Added

- `agg_search.engine_specs` is now the single source of engine metadata.
  `search.ENGINES`/`CREDENTIAL_ENV`/`DEFAULT_ENGINES`, the MCP `engines` enum
  and the portal dependency table all derive from it, with a parity test that
  fails if the layers drift. Adding an engine no longer means editing five
  registries by hand.
- Portal runtime fallback: when a required engine fails or cools down *after*
  the credential prefilter, the portal recalls its keyless fallbacks (bounded
  to 8s), merges the rows and records the reason in `engine_health.degraded`.
  Supplemented results are not cached, because they depend on the failure.
- Answer input caps: caller-supplied `sources[].text` is limited to 20k
  characters each and 60k total (schema + runtime), enforcing per-item and
  total bounds independently and recording truncation (including dropped
  trailing sources) in `degraded`.

### Changed

- Generated catalog artifacts use `~/...` instead of absolute home paths, so
  `catalog.json` no longer carries this machine's home directory.
- `--answer` help and README state that `auto` can fall through to the billable
  DeepSeek API; use `--answer local` to guarantee zero cost.
- `docs/portals.md` states explicitly that tool order is model guidance, not a
  server-enforced pipeline (only opt-in `answer(urls=...)` prefetch executes
  order internally).

## [0.5.0] - 2026-11-20

Portal isolation (`agent × domain`), domain-tuned prompts and tool ordering,
plus benchmarked fusion improvements.

### Added

- Portal registry (`agg_search.portals`, `portals.json`, `docs/portals.md`):
  52 declarative `agent × domain` descriptors declaring isolated MCP server
  names, exposed tool subsets and workflow order (`academic`: `search -> fetch ->
  answer`; `code`/`news`/`general`: `search -> fetch`), domain-specific prompts,
  dependencies and agent-aware fallback chains (`local -> deepseek` for
  local-LLM agents vs `deepseek -> local` for cloud-first CLI/IDE agents).
- `agg-search-install --portal <agent>.<domain>` (`--list-portals`,
  `--portals [--markdown]`): generates and checks isolated portal entries;
  `catalog.json` and `docs/catalog.md` now carry `AGG_SEARCH_PORTAL` alongside
  `AGG_SEARCH_PROFILE`.
- MCP portal isolation (`agg-search-mcp`, `agg-search-http`, `--portal` /
  `AGG_SEARCH_PORTAL`): portal-scoped `initialize` (`serverInfo.name` and
  `instructions`), ordered `tools/list`, JSON-RPC `-32602` rejection for tools
  outside the portal subset, and the `answer` tool (supporting direct `sources`
  or prefetched `urls`).
- Content-level deduplication in `rrf_fuse` (`dedupe=True` by default,
  `--no-dedupe` to disable): collapses `http`/`https`, `www.`, tracking query
  parameters, arXiv `abs`/`pdf`/`html`/`vN`/`10.48550/arxiv.*` mirrors, and
  substantive exact/near-exact duplicate results while merging engine provenance.
- Soft domain authority priors in `profiles` (`academic`: `1.35`, `code`: `1.25`)
  and opt-in read-then-answer `--answer-fetch N` (CLI) / `answer_fetch` (MCP
  `search`).
- Keyless fallback recovery after credential/cooldown filtering for portals,
  recorded in `engine_health.degraded`.
- `docs/benchmarks.md`: 18-query domain A/B + 10-query general regression
  evaluations across all 5 enhancement candidates.

### Changed

- `expand_queries` and `answer_query` read domain-specific prompt templates from
  `agg_search.portals` instead of hardcoded general strings, and `answer` uses
  agent-ordered fallback (`answer_with_fallback`).
- `code` profile `rerank_blend` tuned from `0.6` to `0.4` based on the 6-query
  code benchmark (`5/18 -> 9/18` authoritative top-3 hits when combined with
  authority priors and deduplication).
- Rejected candidates kept out of default paths after evaluation: automatic
  `--expand` stays `0` (opt-in; 2x-3x call cost + 9B hallucinations at
  `expand=3`) and automatic `fetch` before `answer` stays opt-in (`--answer-fetch`,
  due to container ladder latency on anti-bot sites).

## [0.4.1] - 2026-11-19

Security release from an independent audit.

### Security

- Fetch SSRF: cloud metadata addresses (`169.254.169.254`, `fd00:ec2::254`,
  `metadata.google.internal`) are blocked unconditionally, including with
  `--allow-private`.
- Fetch redirects: host-side requests refuse 3xx instead of letting `urllib`
  forward API keys to another origin; the container re-resolves the final URL
  and drops the body when it is private or metadata.
- `allow_private` / `allow_cidrs` are server-side environment policy
  (`AGG_SEARCH_ALLOW_PRIVATE`, `AGG_SEARCH_ALLOW_CIDRS`), no longer writable
  through MCP tool arguments, so a prompt-injected model cannot widen the
  guard. The CIDR option exists for TUN proxies whose fake-IPs
  (`198.18.0.0/15`) would otherwise look private.
- Search engine responses are capped at 4 MiB and 3xx are refused; markdown
  titles are escaped and C0/C1 control characters stripped before output.
- Fetch `title`/`markdown` now pass through the invisible-character
  sanitizer, and the MCP `text_limit` maximum is 40000 characters.
- HTTP transport: duplicate `Content-Length` and `Content-Length` +
  `Transfer-Encoding` are rejected (request smuggling), connections time out
  after 30s and are capped at 64.
- MCP: at most 16 tool calls may be in flight (`-32603 server busy`
  beyond that), and cancellation over HTTP now kills the subprocess.
- `agg-search-install` keeps at most five `.bak-*` copies of a rewritten
  config, so secrets in `env` are not copied indefinitely.
- CI actions are pinned to commit SHAs; the tests workflow declares
  `contents: read`.

## [0.4.0] - 2026-11-19

### Added

- Agent x domain catalog: `agg-search-install --catalog [--markdown]` emits
  `catalog.json` and `docs/catalog.md`; Cursor/VS Code deeplinks carry
  `AGG_SEARCH_PROFILE`, Claude Code/Codex commands add `--env`, and
  non-general profiles register as `agg-search-academic|code|news` so
  several domains coexist.
- Profiles gain a `primary` engine and a `rerank_blend` factor. In `--fast`
  mode the early return now waits for the primary engine (or the budget), so
  a profile is never served by its secondary engines alone; `--json` reports
  `engine_health.primary`.
- Jina reranking blends with the RRF order for academic (0.4) and code (0.6)
  profiles and requests the full relevance vector, so listwise scores no
  longer drop the primary engine's candidates.
- `docs/agent-tests.md`: isolated CLI-agent verification (MCP Inspector,
  Codex, OpenCode, Claude Code, Gemini CLI, Antigravity CLI) with versions,
  evidence and cleanup notes.

### Changed

- Measured domain precision@3 (3 queries per domain, general as control):
  academic 4/9 -> 8/9, code 4/9 -> 7/9; methodology and caveats in
  `docs/profiles.md`.

## [0.3.0] - 2026-11-19

### Added

- Domain profiles (`general`, `academic`, `code`, `news`): a profile sets the
  engine set, RRF weight overrides, default recency and snippet length.
  `--profile`, `--list-profiles`, MCP `profile`, `AGG_SEARCH_PROFILE`, and
  `agg-search-install --profile` to pin a client. Precedence is
  flag > env > profile > built-in.
- Three keyless domain engines: `openalex` (works + reconstructed abstracts,
  optional `OPENALEX_API_KEY`/`OPENALEX_MAILTO`), `hn` (Algolia) and `stack`
  (Stack Exchange, optional `STACKEXCHANGE_SITE`/`STACKEXCHANGE_KEY`).
- Local feedback loop: `search` records which engines surfaced each returned
  URL, `fetch` records pages actually read, and `agg-search-tune` turns the
  counts into clamped engine-weight multipliers (Bayesian shrinkage, samples
  under 30 keep 1.0) stored in `~/.config/agg-search/weights.json`. Weights
  participate in the cache key; `--no-learn`/`AGG_SEARCH_NO_LEARN` disable it.
  Only hashes and counters are stored — no queries, page text or raw URLs.
- `--show-config` reports the resolved profile, effective weights and learned
  multipliers.

### Changed

- Engine weights are always passed to fusion, so profile and learned weights
  apply to single-query searches too (the `general` profile keeps the old
  defaults: 1.0 per engine, 0.5 for searxng).

## [0.2.1] - 2026-11-19

### Added

- `agg-search-install --check`: verifies the client config parses, holds the
  `agg-search` entry and that the runtime binary resolves (exit 1 with one
  line per problem).

### Fixed

- Text shown to the model (fetched pages, fused titles/snippets) drops
  zero-width and bidi-control characters, the usual hiding place for injected
  instructions; MCP instructions state that page text is untrusted.
- `--check` understands the list-form `command` used by OpenCode.

## [0.2.0] - 2026-11-19

Agent-facing release: a modern MCP surface, an HTTP transport, and the
controls agents actually ask for.

### Added

- MCP: protocol negotiation across 2025-06-18 / 2025-03-26 / 2024-11-05,
  tool `title`/`annotations`/`outputSchema`, `structuredContent`, an
  `instructions` block, and a `fast` default that keeps tool calls interactive.
- `agg-search-http`: streamable HTTP transport (POST JSON-RPC, sessions,
  batches, 1 MiB cap) with loopback-only defaults plus `Host`/`Origin`
  allowlists (DNS-rebinding and drive-by defence).
- `agg-search-install`: prints or merges the registration for 13 clients
  (Claude Code/Desktop, Codex, Cursor, VS Code, Cline, Windsurf, Zed,
  Continue, Gemini CLI, Antigravity, OpenCode, generic) with timestamped
  backups that keep the original file mode.
- Search: `--include-domains` / `--exclude-domains` (applied before RRF, and
  to cache hits), `--snippet-chars`, `--engine-timeout`, `--show-config`,
  per-engine latency and recall/rerank timing in `--json`, and
  `AGG_SEARCH_*` defaults that explicit flags still override.
- Fetch: an SSRF guard (http(s) only, no userinfo, public addresses only,
  `--allow-private` opt-out), a 4 MiB response cap and a 15-minute page cache.
- Packaging: `py.typed`, `__version__`, and tests that fail on any
  third-party runtime import; CI now runs Python 3.10–3.13 plus ruff.

### Fixed

- MCP tool calls have hard wall clocks (120s search / 300s fetch) and run in
  a pool, so a hung engine no longer wedges the transport; cancellation kills
  the process group.
- `install --write` no longer broadens a 0600 config to the umask default.

## [0.1.2] - 2026-11-19

### Fixed

- The MCP `initialize` handshake reports the installed package version instead
  of a hardcoded `0.1.0`.

## [0.1.1] - 2026-11-19

Registry packaging fix.

### Fixed

- `server.json` resolves the MCP server as `uvx --from agg-search
  agg-search-mcp`; the previous form ran the search CLI and passed
  `agg-search-mcp` as its query.
- The PyPI README carries the `mcp-name` marker the official MCP registry
  requires to verify package ownership.

## [0.1.0] - 2026-11-19

First public release.

### Added

- `agg-search` CLI: parallel recall across exa, brave, serper, deepseek,
  firecrawl, anysearch, tavily and searxng; RRF fusion into the top 15, then
  Jina Reranker v3.5 (listwise, fallback v3 → v2).
- Resilience layer: five-minute result cache, failure-class cooldowns (quota
  1h, anti-bot 10min, timeout 90s), engine health in `--json`, missing keys
  skipped before threads start.
- `--expand N` query expansion (RAG-Fusion via local ollama), `--answer` cited
  summaries (local or DeepSeek), `--fast`, `--json`.
- `crawl-fetch`: DeepSeek Fetch API first, then a static → dynamic → stealth
  container ladder.
- `agg-search-mcp`: stdio MCP server exposing `search` and `fetch`.
- `agg-search-eval`: offline RRF weight/K sweep.
- 17 offline tests, CI on Python 3.10–3.12.
