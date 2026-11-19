#!/usr/bin/env python
"""Protocol tests for the MCP server: no network, no subprocess."""

import unittest

from agg_search import mcp


class InitializeTest(unittest.TestCase):
    def test_reports_tools_capability(self):
        result = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(result["protocolVersion"], mcp.PROTOCOL_VERSION)
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "agg-search")


class ToolsTest(unittest.TestCase):
    def test_lists_search_and_fetch(self):
        result = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in result["tools"]}
        self.assertEqual(names, {"search", "fetch"})
        for tool in result["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertTrue(tool["inputSchema"]["required"])


class ArgumentsTest(unittest.TestCase):
    def test_search_maps_to_cli_flags(self):
        args = mcp.build_args(
            "search",
            {"query": "q", "top_n": 5, "recency": "week", "engines": "brave,exa", "answer_deepseek": True},
        )
        self.assertEqual(args[0], "q")
        self.assertIn("--json", args)
        self.assertEqual(args[args.index("--top-n") + 1], "5")
        self.assertEqual(args[args.index("--recency") + 1], "week")
        self.assertIn("--answer", args)
        self.assertIn("deepseek", args)

    def test_fetch_repeats_url_flags(self):
        args = mcp.build_args("fetch", {"urls": ["https://a.test", "https://b.test"], "tier": "static"})
        self.assertEqual(args.count("--url"), 2)
        self.assertEqual(args[args.index("--tier") + 1], "static")


class NotificationTest(unittest.TestCase):
    def test_initialized_is_silent(self):
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
