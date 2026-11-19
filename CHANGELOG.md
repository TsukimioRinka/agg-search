# Changelog

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
