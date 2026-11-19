#!/usr/bin/env python3
"""Offline tests for the portal data model, registry sync and installer."""

import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from agg_search import install, portals, profiles

ROOT = pathlib.Path(__file__).resolve().parent.parent


class PortalModelTest(unittest.TestCase):
    def test_matrix_covers_all_agents_and_domains(self):
        self.assertEqual(set(portals.AGENT_SPECS), set(install.CLIENTS))
        self.assertEqual(set(portals.DOMAIN_SPECS), set(profiles.PROFILES))
        ids = portals.portal_ids()
        self.assertEqual(len(ids), len(install.CLIENTS) * len(profiles.PROFILES))
        self.assertEqual(len(ids), 52)

    def test_required_fields_and_tool_isolation(self):
        acad = portals.load("codex.academic")
        self.assertEqual(acad["server_name"], "agg-search-academic")
        self.assertEqual(acad["tools"], ["search", "fetch", "answer"])
        self.assertEqual([s["tool"] for s in acad["tool_order"]],
                         ["search", "fetch", "answer"])
        self.assertEqual(acad["dependencies"]["required_engines"], ["openalex"])
        self.assertEqual(acad["defaults"]["answer_order"], ["local", "deepseek"])

        code = portals.load("claude-code.code")
        self.assertEqual(code["server_name"], "agg-search-code")
        self.assertEqual(code["tools"], ["search", "fetch"])
        self.assertEqual([s["tool"] for s in code["tool_order"]],
                         ["search", "fetch"])
        self.assertEqual(code["dependencies"]["required_engines"], ["stack"])
        self.assertEqual(code["defaults"]["answer_order"], ["deepseek", "local"])

        news = portals.load("gemini.news")
        self.assertEqual(news["server_name"], "agg-search-news")
        self.assertEqual(news["defaults"]["recency"], "day")

    def test_prompts_are_distinct_and_safe_to_render(self):
        p_acad = portals.load("codex.academic")["prompts"]
        p_code = portals.load("codex.code")["prompts"]
        p_news = portals.load("codex.news")["prompts"]
        self.assertNotEqual(p_acad["expand"], p_code["expand"])
        self.assertNotEqual(p_code["answer"], p_news["answer"])
        self.assertIn(portals.UNTRUSTED_CONTENT_NOTICE, p_acad["instructions"])
        # User queries with braces or format specifiers must never fail
        rendered = portals.render_prompt(
            p_code["expand"], count=2, query="std::vector<int>{1}%s {missing}")
        self.assertIn("std::vector<int>{1}%s {missing}", rendered)

    def test_unknown_portal_raises_helpful_error(self):
        with self.assertRaises(portals.UnknownPortal) as ctx:
            portals.load("bogus.academic")
        self.assertIn("codex", str(ctx.exception))

    def test_probe_dependencies_never_leaks_secret_values(self):
        secret = "sk-super-secret-value"
        portal = portals.load("claude-code.academic")
        probe = portals.probe_dependencies(
            portal, lambda k: secret if k == "EXA_API_KEY" else None,
            ollama_ok=False)
        blob = json.dumps(probe)
        self.assertNotIn(secret, blob)
        self.assertIs(probe["engines"]["exa"], True)
        self.assertIs(probe["engines"]["openalex"], True)
        self.assertIs(probe["engines"]["brave"], False)
        self.assertIs(probe["rerank_jina"], False)


class PortalRegistrySyncTest(unittest.TestCase):
    def test_portals_json_and_markdown_match_generator(self):
        disk_json = json.loads(
            (ROOT / "portals.json").read_text(encoding="utf-8"))
        gen_json = install.portals_catalog()
        self.assertEqual(disk_json, gen_json)

        disk_md = (ROOT / "docs" / "portals.md").read_text(encoding="utf-8")
        gen_md = install.portals_markdown(gen_json)
        self.assertEqual(disk_md, gen_md)

    def test_catalog_and_portals_are_consistent(self):
        portals_data = json.loads(
            (ROOT / "portals.json").read_text(encoding="utf-8"))["portals"]
        cat_data = install.catalog()
        for agent_row in cat_data["agents"]:
            agent = agent_row["id"]
            for entry in agent_row["entries"]:
                pid = entry["portal"]
                self.assertEqual(pid, "%s.%s" % (agent, entry["profile"]))
                self.assertIn(pid, portals_data)
                p = portals_data[pid]
                self.assertEqual(entry["name"], p["server_name"])
                self.assertEqual(entry["env"], p["env"])
                self.assertEqual(entry["entry"], p["install"]["entry"])

        disk_cat_md = (ROOT / "docs" / "catalog.md").read_text(encoding="utf-8")
        self.assertEqual(disk_cat_md, install.catalog_markdown(cat_data))


class PortalInstallCliTest(unittest.TestCase):
    def test_portal_command_clients_include_portal_and_profile_env(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = install.main(["--portal", "codex.academic", "--json"])
        self.assertEqual(rc, 0)
        info = json.loads(out.getvalue())
        self.assertEqual(info["portal"], "codex.academic")
        self.assertEqual(info["server"], "agg-search-academic")
        self.assertEqual(info["tools"], ["search", "fetch", "answer"])
        self.assertIn("agg-search-academic", info["command"])
        self.assertIn("AGG_SEARCH_PORTAL=codex.academic", info["command"])
        self.assertIn("AGG_SEARCH_PROFILE=academic", info["command"])
        self.assertIn("[mcp_servers.agg-search-academic]", info["toml"])

    def test_portal_json_write_and_check_coexist(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"HOME": td}), \
                mock.patch.object(install.shutil, "which",
                                  return_value="/bin/uvx"):
            for pid in ("cursor.academic", "cursor.code"):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = install.main(["--portal", pid, "--write"])
                self.assertEqual(rc, 0)
            cfg = pathlib.Path(td, ".cursor", "mcp.json")
            doc = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertIn("agg-search-academic", doc["mcpServers"])
            self.assertIn("agg-search-code", doc["mcpServers"])
            self.assertEqual(
                doc["mcpServers"]["agg-search-academic"]["env"]
                ["AGG_SEARCH_PORTAL"], "cursor.academic")
            self.assertEqual(
                doc["mcpServers"]["agg-search-code"]["env"]
                ["AGG_SEARCH_PORTAL"], "cursor.code")
            for pid in ("cursor.academic", "cursor.code"):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = install.main(["--portal", pid, "--check"])
                self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
