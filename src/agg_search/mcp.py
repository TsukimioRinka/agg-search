#!/usr/bin/env python3
"""Minimal stdio MCP server exposing aggregate search and the tiered fetcher.

Tools
-----
search  — parallel multi-engine recall, RRF fusion, Jina rerank, optional
          query expansion and answer synthesis (wraps `agg_search.search`).
fetch   — tiered page fetch: DeepSeek Fetch API first, then the crawl-agent
          container ladder (wraps `agg_search.fetch`).

The server speaks newline-delimited JSON-RPC 2.0 on stdin/stdout and shells
out to the CLI entry points with the current interpreter, so it stays
dependency-free and inherits the caller's environment.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version as _dist_version

PROTOCOL_VERSION = "2024-11-05"


def _server_version() -> str:
    try:
        return _dist_version("agg-search")
    except PackageNotFoundError:
        return "0.0.0+source"


SERVER_INFO = {"name": "agg-search", "version": _server_version()}

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query."},
        "top_n": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        "recency": {"type": "string", "enum": ["day", "week", "month", "year"]},
        "engines": {
            "type": "string",
            "description": "Comma-separated subset: exa,brave,serper,deepseek,firecrawl,anysearch,tavily,searxng.",
        },
        "expand": {"type": "integer", "minimum": 2, "maximum": 3, "description": "RAG-Fusion variants (needs local ollama)."},
        "answer": {"type": "boolean", "description": "Add a cited answer section (needs local ollama)."},
        "answer_deepseek": {"type": "boolean", "description": "Use the DeepSeek chat API for the answer section."},
        "fast": {"type": "boolean", "description": "Fan out one pool and return after the first sources."},
        "no_jina": {"type": "boolean", "description": "Skip reranking and keep the RRF order."},
    },
    "required": ["query"],
}

FETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "urls": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "tier": {"type": "string", "enum": ["auto", "deepseek", "static", "dynamic", "stealth"], "default": "auto"},
        "out": {"type": "string", "enum": ["json", "md"], "default": "json"},
        "text_limit": {"type": "integer", "minimum": 500, "default": 8000},
    },
    "required": ["urls"],
}

TOOLS = [
    {
        "name": "search",
        "description": "Parallel multi-engine web search with RRF fusion and Jina reranking. Returns JSON with ranked results, engine provenance and engine health.",
        "inputSchema": SEARCH_SCHEMA,
    },
    {
        "name": "fetch",
        "description": "Fetch page text. Tries the DeepSeek Fetch API first (no container), then a static -> dynamic -> stealth container ladder.",
        "inputSchema": FETCH_SCHEMA,
    },
]

TOOL_MODULES = {"search": "agg_search.search", "fetch": "agg_search.fetch"}


def run_cli(module: str, args: list[str]) -> tuple[str, bool]:
    cmd = [sys.executable, "-m", module, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    output = proc.stdout.strip() or proc.stderr.strip()
    return output, proc.returncode != 0


def build_args(name: str, arguments: dict) -> list[str]:
    if name == "search":
        args = [str(arguments["query"]), "--json"]
        if arguments.get("top_n"):
            args += ["--top-n", str(arguments["top_n"])]
        if arguments.get("recency"):
            args += ["--recency", arguments["recency"]]
        if arguments.get("engines"):
            args += ["--engines", arguments["engines"]]
        if arguments.get("expand"):
            args += ["--expand", str(arguments["expand"])]
        if arguments.get("answer_deepseek"):
            args.append("--answer")
            args.append("deepseek")
        elif arguments.get("answer"):
            args.append("--answer")
        if arguments.get("fast"):
            args.append("--fast")
        if arguments.get("no_jina"):
            args.append("--no-jina")
        return args

    args = []
    for url in arguments["urls"]:
        args += ["--url", url]
    if arguments.get("tier"):
        args += ["--tier", arguments["tier"]]
    if arguments.get("out"):
        args += ["--out", arguments["out"]]
    if arguments.get("text_limit"):
        args += ["--text-limit", str(arguments["text_limit"])]
    return args


def handle(request: dict) -> dict | None:
    method = request.get("method")

    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return {}

    if method == "tools/list":
        return {"tools": TOOLS}

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in TOOL_MODULES:
            raise ValueError(f"unknown tool: {name}")
        output, failed = run_cli(TOOL_MODULES[name], build_args(name, arguments))
        return {"content": [{"type": "text", "text": output}], "isError": failed}

    raise ValueError(f"unknown method: {method}")


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
            continue

        try:
            result = handle(request)
        except Exception as exc:  # noqa: BLE001 - one bad tool call must not kill the server
            send({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32603, "message": str(exc)}})
            continue

        if result is None:
            continue
        send({"jsonrpc": "2.0", "id": request.get("id"), "result": result})


if __name__ == "__main__":
    main()
