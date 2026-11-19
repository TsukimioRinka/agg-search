#!/usr/bin/env python
"""Protocol-level tests for the MCP server: no network, no subprocess."""

import json
import unittest

from agg_search import mcp


class NegotiationTest(unittest.TestCase):
    def _init(self, version=None):
        params = {} if version is None else {"protocolVersion": version}
        return mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": params})

    def test_echoes_supported_versions(self):
        for version in mcp.SUPPORTED_PROTOCOLS:
            self.assertEqual(self._init(version)["protocolVersion"], version)

    def test_falls_back_on_unknown_version(self):
        self.assertEqual(self._init("1900-01-01")["protocolVersion"],
                         mcp.PROTOCOL_VERSION)

    def test_advertises_instructions(self):
        self.assertIn("search", self._init()["instructions"])


class ToolSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.tools = {t["name"]: t for t in mcp.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["tools"]}

    def test_titles_and_annotations(self):
        for tool in self.tools.values():
            self.assertTrue(tool.get("title"))
            annotations = tool["annotations"]
            self.assertIs(annotations["readOnlyHint"], True)
            self.assertIs(annotations["openWorldHint"], True)

    def test_output_schemas_are_objects(self):
        for tool in self.tools.values():
            self.assertEqual(tool["outputSchema"]["type"], "object")

    def test_search_schema_has_new_filters(self):
        props = self.tools["search"]["inputSchema"]["properties"]
        for key in ("include_domains", "exclude_domains", "snippet_chars",
                    "budget_secs"):
            self.assertIn(key, props)


class ArgumentMappingTest(unittest.TestCase):
    def test_domain_flags_and_snippet(self):
        args = mcp.build_args("search", {
            "query": "q", "include_domains": ["arxiv.org", ".edu"],
            "exclude_domains": "reddit.com", "snippet_chars": 120,
            "budget_secs": 12, "fast": True,
        })
        self.assertEqual(args[args.index("--include-domains") + 1],
                         "arxiv.org,.edu")
        self.assertEqual(args[args.index("--exclude-domains") + 1], "reddit.com")
        self.assertEqual(args[args.index("--snippet-chars") + 1], "120")
        self.assertEqual(args[args.index("--budget-secs") + 1], "12")
        self.assertIn("--fast", args)

    def test_engine_list_is_joined(self):
        args = mcp.build_args("search", {"query": "q",
                                         "engines": ["brave", "exa"]})
        self.assertEqual(args[args.index("--engines") + 1], "brave,exa")

    def test_fast_is_on_unless_disabled(self):
        self.assertIn("--fast", mcp.build_args("search", {"query": "q"}))
        self.assertNotIn("--fast", mcp.build_args(
            "search", {"query": "q", "fast": False}))

    def test_engine_timeout_passes_through(self):
        args = mcp.build_args("search", {"query": "q", "engine_timeout": 12})
        self.assertEqual(args[args.index("--engine-timeout") + 1], "12")

    def test_allow_private_is_a_server_policy_not_a_tool_argument(self):
        # A model-supplied allow_private must be ignored
        args = mcp.build_args("fetch", {"urls": ["https://a.test"],
                                        "allow_private": True})
        self.assertNotIn("--allow-private", args)

    def test_allow_private_env_enables_it_server_side(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ,
                             {"AGG_SEARCH_ALLOW_PRIVATE": "1"}):
            args = mcp.build_args("fetch", {"urls": ["https://a.test"]})
        self.assertIn("--allow-private", args)

    def test_allow_cidrs_env_passes_through(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ,
                             {"AGG_SEARCH_ALLOW_CIDRS": "198.18.0.0/15"}):
            args = mcp.build_args("fetch", {"urls": ["https://a.test"]})
        self.assertEqual(args[args.index("--allow-cidrs") + 1],
                         "198.18.0.0/15")


class ShapingTest(unittest.TestCase):
    def test_search_structured_content_is_compact(self):
        payload = {"query": "q", "engines": {"brave": "ok (1)"},
                   "results": [{"rank": 1, "title": "t", "url": "https://a",
                                "snippet": "s", "engines": ["brave"],
                                "rrf": 0.1}]}
        result = mcp._shape_result("search", json.dumps(payload), "", 0, False)
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["results"][0]["url"],
                         "https://a")
        self.assertNotIn(" \n", result["content"][0]["text"])

    def test_fetch_parses_json_lines(self):
        out = '{"url": "https://a", "tier_used": "deepseek", "blocked": false}\n'
        result = mcp._shape_result("fetch", out, "", 0, False)
        self.assertEqual(result["structuredContent"]["results"][0]["tier_used"],
                         "deepseek")

    def test_timeout_is_an_error_result(self):
        result = mcp._shape_result("search", "", "", None, True)
        self.assertTrue(result["isError"])
        self.assertIn("timed out", result["content"][0]["text"])

    def test_empty_output_is_an_error_result(self):
        result = mcp._shape_result("search", "", "boom", 1, False)
        self.assertTrue(result["isError"])
        self.assertIn("boom", result["content"][0]["text"])


class NotificationTest(unittest.TestCase):
    def test_cancelled_notification_is_silent(self):
        self.assertIsNone(mcp.handle(
            {"jsonrpc": "2.0", "method": "notifications/cancelled",
             "params": {"requestId": 7}}))


if __name__ == "__main__":
    unittest.main()
