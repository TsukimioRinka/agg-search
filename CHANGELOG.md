# Changelog

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
