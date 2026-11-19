# Changelog

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
