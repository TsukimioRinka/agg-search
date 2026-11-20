"""MCP prompts: one research workflow per portal, isolated like tools."""

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from agg_search import mcp


def call(method, params=None, portal=None):
    env = {"AGG_SEARCH_PORTAL": portal} if portal else {}
    with mock.patch.dict(os.environ, env, clear=False):
        if not portal:
            os.environ.pop("AGG_SEARCH_PORTAL", None)
        return mcp.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params or {}})


class PromptsTest(unittest.TestCase):
    def test_capability_is_advertised(self):
        caps = call("initialize", {"protocolVersion": "2025-06-18"})["capabilities"]
        self.assertEqual(caps["prompts"], {"listChanged": False})
        self.assertIn("tools", caps)

    def test_list_is_per_portal(self):
        self.assertEqual([p["name"] for p in call("prompts/list")["prompts"]],
                         ["web-research"])
        acad = call("prompts/list", portal="codex.academic")["prompts"]
        self.assertEqual([p["name"] for p in acad], ["academic-research"])
        self.assertTrue(acad[0]["arguments"][0]["required"])
        self.assertIn("search -> fetch -> answer", acad[0]["description"])

    def test_get_follows_portal_tool_order(self):
        msg = call("prompts/get", {"name": "academic-research",
                                   "arguments": {"topic": "speculative decoding"}},
                   portal="codex.academic")["messages"][0]
        text = msg["content"]["text"]
        self.assertEqual(msg["role"], "user")
        self.assertIn("speculative decoding", text)
        self.assertLess(text.index("`search`"), text.index("`fetch`"))
        self.assertLess(text.index("`fetch`"), text.index("`answer`"))
        self.assertIn("untrusted", text)

    def test_code_portal_prompt_never_mentions_disabled_answer_tool(self):
        text = call("prompts/get", {"name": "code-research",
                                    "arguments": {"topic": "x"}},
                    portal="claude-code.code")["messages"][0]["content"]["text"]
        self.assertNotIn("`answer`", text)

    def test_cross_portal_and_unknown_names_are_rejected(self):
        with self.assertRaises(KeyError):
            call("prompts/get", {"name": "academic-research",
                                 "arguments": {"topic": "x"}},
                 portal="claude-code.code")
        with self.assertRaises(KeyError):
            call("prompts/get", {"name": "nope", "arguments": {"topic": "x"}})

    def test_topic_is_required_sanitized_and_bounded(self):
        for bad in ({}, {"topic": ""}, {"topic": "   "}, {"topic": 5}):
            with self.assertRaises(KeyError):
                call("prompts/get", {"name": "web-research", "arguments": bad})
        text = call("prompts/get", {"name": "web-research", "arguments": {
            "topic": "a\u202eb\x00c" + "z" * 2000}})["messages"][0]["content"]["text"]
        first = text.splitlines()[0]
        self.assertNotIn("\u202e", first)
        self.assertNotIn("\x00", first)
        self.assertLessEqual(len(first), len("Research this topic: ") + mcp.MAX_PROMPT_ARG)

    def test_no_environment_values_leak(self):
        with mock.patch.dict(os.environ, {"BRAVE_API_KEY": "sk-should-not-leak"}):
            blob = json.dumps([call("prompts/list", portal="codex.news"),
                               call("prompts/get", {"name": "news-research",
                                                    "arguments": {"topic": "t"}},
                                    portal="codex.news")])
        self.assertNotIn("sk-should-not-leak", blob)

    def test_stdio_round_trip_maps_errors(self):
        lines = "\n".join(json.dumps(m) for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "prompts/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "prompts/get",
             "params": {"name": "web-research", "arguments": {"topic": "rrf"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
             "params": {"name": "nope"}},
        )) + "\n"
        env = dict(os.environ)
        env.pop("AGG_SEARCH_PORTAL", None)
        out = subprocess.run([sys.executable, "-m", "agg_search.mcp"],
                             input=lines, capture_output=True, text=True,
                             timeout=30, env=env).stdout
        replies = {r["id"]: r for r in map(json.loads, out.splitlines())}
        self.assertEqual(replies[1]["result"]["prompts"][0]["name"], "web-research")
        self.assertIn("rrf", replies[2]["result"]["messages"][0]["content"]["text"])
        self.assertEqual(replies[3]["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
