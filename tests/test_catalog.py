#!/usr/bin/env python3
"""Catalog (agent x domain) shape and deeplink payload tests."""

import base64
import json
import unittest
import urllib.parse

from agg_search import install, profiles


class CatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = install.catalog()
        cls.agents = {row["id"]: row for row in cls.data["agents"]}

    def test_every_agent_covers_every_profile(self):
        self.assertEqual(set(self.agents), set(install.CLIENTS))
        for row in self.agents.values():
            self.assertEqual([e["profile"] for e in row["entries"]],
                             profiles.names())

    def test_names_are_profile_scoped(self):
        for profile in profiles.names():
            expected = ("agg-search" if profile == "general"
                        else "agg-search-" + profile)
            self.assertEqual(self.data["servers"][profile]["name"], expected)

    def _entry(self, agent, profile):
        return next(e for e in self.agents[agent]["entries"]
                    if e["profile"] == profile)

    def test_cursor_deeplink_round_trips(self):
        entry = self._entry("cursor", "academic")
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(entry["deeplink"]).query)
        self.assertEqual(query["name"][0], "agg-search-academic")
        cfg = json.loads(base64.b64decode(query["config"][0]))
        self.assertEqual(cfg["command"], "uvx")
        self.assertEqual(cfg["env"]["AGG_SEARCH_PROFILE"], "academic")

    def test_vscode_deeplink_round_trips(self):
        entry = self._entry("vscode", "code")
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(entry["deeplink"]).query)
        cfg = json.loads(query["config"][0])
        self.assertEqual(cfg["type"], "stdio")
        self.assertEqual(cfg["env"]["AGG_SEARCH_PROFILE"], "code")

    def test_claude_command_carries_env(self):
        command = self._entry("claude-code", "news")["command"]
        self.assertIn("agg-search-news", command)
        self.assertIn("--env AGG_SEARCH_PROFILE=news", command)

    def test_json_clients_carry_the_profile_in_their_entry(self):
        for agent in ("gemini", "opencode", "antigravity", "windsurf"):
            entry = self._entry(agent, "academic")
            self.assertEqual(entry["env"]["AGG_SEARCH_PROFILE"], "academic")
            self.assertNotIn("command", entry)

    def test_markdown_lists_agents_and_profiles(self):
        text = install.catalog_markdown(self.data)
        for profile in profiles.names():
            self.assertIn("`%s`" % profile, text)
        for agent in install.CLIENTS:
            self.assertIn("| %s |" % agent, text)
        self.assertIn("name=agg-search&", text)


if __name__ == "__main__":
    unittest.main()
