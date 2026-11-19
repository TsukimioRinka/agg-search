# Changelog

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
