#!/usr/bin/env python3
"""Offline tests for the portal data model, registry sync and installer."""

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agg_search import install, mcp, portals, profiles, search

ROOT = pathlib.Path(__file__).resolve().parent.parent


class PortalModelTest(unittest.TestCase):
    def test_matrix_covers_all_agents_and_domains(self):
        self.assertEqual(set(portals.AGENT_SPECS), set(install.CLIENTS))
        self.assertEqual(set(portals.DOMAIN_SPECS), set(profiles.PROFILES))
        ids = portals.portal_ids()
        self.assertEqual(len(ids), len(install.CLIENTS) * len(profiles.PROFILES))
        self.assertEqual(len(ids), 56)

    def test_omp_portal_registered_and_differs_from_generic(self):
        self.assertIn("omp", portals.agent_names())
        self.assertIn("omp", install.CLIENTS)
        for domain in portals.domain_names():
            omp_p = portals.load("omp.%s" % domain)
            gen_p = portals.load("generic.%s" % domain)
            self.assertIs(omp_p["dependencies"]["llm"]["local_llm"], True)
            self.assertEqual(omp_p["defaults"]["expand"], 0)
            self.assertEqual(omp_p["defaults"]["answer_order"],
                             ["local", "deepseek"])
            self.assertEqual(gen_p["defaults"]["answer_order"],
                             ["deepseek", "local"])
            self.assertNotEqual(omp_p["defaults"]["answer_order"],
                                gen_p["defaults"]["answer_order"])

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
        disk_cat = json.loads(
            (ROOT / "catalog.json").read_text(encoding="utf-8"))
        for agent_row in disk_cat["agents"]:
            agent = agent_row["id"]
            for entry in agent_row["entries"]:
                pid = entry["portal"]
                self.assertEqual(pid, "%s.%s" % (agent, entry["profile"]))
                self.assertIn(pid, portals_data)
                p = portals_data[pid]
                self.assertEqual(entry["name"], p["server_name"])
                self.assertEqual(entry["env"], p["env"])
                self.assertEqual(entry["entry"], p["install"]["entry"])

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


class DomainPromptAndFallbackTest(unittest.TestCase):
    def test_expand_uses_domain_prompt(self):
        captured = {}

        def fake_http_json(url, **kw):
            captured["prompt"] = kw["body"]["messages"][0]["content"]
            return {"message": {"content": "speculative decoding draft verification"}}

        with mock.patch.object(search, "http_json", fake_http_json):
            variants, note = search.expand_queries(
                "speculative decoding", 2,
                prompt_template=portals.load("codex.academic")["prompts"]["expand"])
        self.assertEqual(variants, ["speculative decoding draft verification"])
        self.assertIn("academic", captured["prompt"].lower())

    def test_answer_with_fallback_follows_agent_order(self):
        calls = []

        def fake_answer(query, ranked, mode, prompt_template=None,
                        fetched_texts=None):
            calls.append((mode, prompt_template, bool(fetched_texts)))
            if mode == "deepseek":
                return None, "answer failed (no DEEPSEEK_API_KEY), skipped"
            return "root cause [1]", None

        ranked = [("https://stackoverflow.com/q/1", 1.0,
                   {"title": "WAL lock", "snippet": "use busy_timeout",
                    "engines": ["stack"]})]
        portal = portals.load("claude-code.code")
        with mock.patch.object(search, "answer_query", fake_answer):
            text, note, used, degraded = search.answer_with_fallback(
                "sqlite wal", ranked, mode="auto",
                prompt_template=portal["prompts"]["answer"],
                fallback_order=portal["defaults"]["answer_order"],
                fetched_texts={"https://stackoverflow.com/q/1": "PRAGMA busy_timeout=5000"})
        self.assertEqual([c[0] for c in calls], ["deepseek", "local"])
        self.assertTrue(calls[0][2])
        self.assertIn("engineering answer", calls[0][1])
        self.assertEqual(text, "root cause [1]")
        self.assertEqual(used, "local")
        self.assertIn("answer fallback to local", note)
        self.assertEqual(len(degraded), 1)

    def test_cli_portal_degrades_cleanly_when_deps_missing(self):
        def fake_openalex(q, n, r):
            return [("https://arxiv.org/abs/2211.17192", "Speculative",
                     "Draft model verification")], None

        orig = search.ENGINES["openalex"]
        search.ENGINES["openalex"] = (fake_openalex, orig[1])
        try:
            argv = [
                "agg-search", "speculative decoding",
                "--portal", "codex.academic",
                "--expand", "2", "--answer", "--no-jina",
                "--no-cache", "--no-cooldown", "--json",
            ]
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {
                "OLLAMA_URL": "http://127.0.0.1:1",
                "DEEPSEEK_API_KEY": "",
                "EXA_API_KEY": "",
                "BRAVE_API_KEY": "",
                "TAVILY_API_KEY": "",
                "TAVILY_API_KEY_BACKUP": "",
            }), mock.patch.object(search, "getenv",
                                  side_effect=lambda k, d=None: os.environ.get(k) or d), \
                    mock.patch.object(search, "http_json",
                                      side_effect=OSError("connection refused")), \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(buf):
                search.main()
        finally:
            search.ENGINES["openalex"] = orig

        payload = json.loads(buf.getvalue())
        health = payload["engine_health"]
        self.assertEqual(health["portal"], "codex.academic")
        self.assertEqual(health["profile"], "academic")
        self.assertTrue(payload["results"])
        self.assertIn("arxiv.org", payload["results"][0]["url"])
        self.assertTrue(health.get("degraded"))
        joined = " | ".join(health["degraded"])
        self.assertIn("expand failed", joined)
        self.assertIn("local:", joined)

    def test_news_portal_falls_back_to_keyless_when_keys_missing(self):
        orig_hn = search.ENGINES["hn"]
        orig_sx = search.ENGINES["searxng"]
        search.ENGINES["hn"] = (
            lambda q, n, r: ([("https://news.ycombinator.com/item?id=1",
                               "HN Release", "details")], None),
            orig_hn[1])
        search.ENGINES["searxng"] = (
            lambda q, n, r: ([], None), orig_sx[1])
        try:
            argv = [
                "agg-search", "openai release",
                "--portal", "gemini.news", "--no-jina",
                "--no-cache", "--no-cooldown", "--json",
            ]
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {
                "BRAVE_API_KEY": "", "SERPER_API_KEY": "",
                "TAVILY_API_KEY": "", "TAVILY_API_KEY_BACKUP": "",
                "FIRECRAWL_API_KEY": "",
            }), mock.patch.object(search, "getenv",
                                  side_effect=lambda k, d=None: os.environ.get(k) or d), \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(buf):
                search.main()
        finally:
            search.ENGINES["hn"] = orig_hn
            search.ENGINES["searxng"] = orig_sx

        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["engine_health"]["portal"], "gemini.news")
        self.assertEqual(payload["results"][0]["engines"], ["hn"])
        joined = " | ".join(payload["engine_health"]["degraded"])
        self.assertIn("fell back to keyless: hn,searxng", joined)

    def test_academic_portal_falls_back_to_searxng_when_openalex_cooling(self):
        import time as _time
        orig_sx = search.ENGINES["searxng"]
        orig_ax = search.ENGINES["arxiv"]
        search.ENGINES["arxiv"] = (lambda q, n, r: ([], None), orig_ax[1])
        search.ENGINES["searxng"] = (
            lambda q, n, r: ([("https://arxiv.org/abs/2211.17192",
                               "Speculative Decoding", "paper")], None),
            orig_sx[1])
        try:
            argv = [
                "agg-search", "speculative decoding",
                "--portal", "codex.academic", "--no-jina",
                "--no-cache", "--json",
            ]
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {
                "EXA_API_KEY": "", "DEEPSEEK_API_KEY": "",
                "BRAVE_API_KEY": "", "TAVILY_API_KEY": "",
                "TAVILY_API_KEY_BACKUP": "",
            }), mock.patch.object(search, "getenv",
                                  side_effect=lambda k, d=None: os.environ.get(k) or d), \
                    mock.patch.object(search, "load_cooldown",
                                      return_value={"openalex": _time.time() + 300}), \
                    mock.patch.object(search, "save_cooldown"), \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(buf):
                search.main()
        finally:
            search.ENGINES["searxng"] = orig_sx
            search.ENGINES["arxiv"] = orig_ax

        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["engine_health"]["portal"], "codex.academic")
        self.assertEqual(payload["results"][0]["engines"], ["searxng"])
        joined = " | ".join(payload["engine_health"]["degraded"])
        self.assertIn("engine openalex cooling", joined)
        self.assertIn("fell back to keyless: arxiv,searxng", joined)

    def test_academic_first_429_supplements_in_the_same_call(self):
        orig_oa = search.ENGINES["openalex"]
        orig_sx = search.ENGINES["searxng"]
        orig_ax = search.ENGINES["arxiv"]
        search.ENGINES["arxiv"] = (lambda q, n, r: ([], None), orig_ax[1])
        search.ENGINES["openalex"] = (
            lambda q, n, r: (None, "HTTPError: HTTP Error 429: Too Many Requests"),
            orig_oa[1])
        search.ENGINES["searxng"] = (
            lambda q, n, r: ([("https://arxiv.org/abs/2211.17192",
                               "Speculative Decoding", "paper")], None),
            orig_sx[1])
        saved = {}
        try:
            argv = ["agg-search", "speculative decoding",
                    "--portal", "codex.academic", "--engines", "openalex",
                    "--no-jina", "--json"]
            buf = io.StringIO()
            with mock.patch.object(search, "load_cooldown", return_value={}), \
                    mock.patch.object(search, "save_cooldown",
                                      side_effect=saved.update), \
                    mock.patch.object(search, "load_cache", return_value={}), \
                    mock.patch.object(search, "save_cache") as save_cache, \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(buf):
                search.main()
        finally:
            search.ENGINES["openalex"] = orig_oa
            search.ENGINES["searxng"] = orig_sx
            search.ENGINES["arxiv"] = orig_ax

        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["results"][0]["engines"], ["searxng"])
        joined = " | ".join(payload["engine_health"]["degraded"])
        self.assertIn("required engine(s) openalex unavailable; "
                      "supplemented arxiv,searxng", joined)
        self.assertIn("openalex", saved)       # 429 still starts a cooldown
        save_cache.assert_not_called()         # supplemented rows never cached

    def test_show_config_portal_reports_dependencies_without_secrets(self):
        secret = "sk-do-not-leak-999"
        argv = ["agg-search", "--portal", "claude-code.code", "--show-config"]
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"BRAVE_API_KEY": secret}), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(buf):
            search.main()
        raw = buf.getvalue()
        self.assertNotIn(secret, raw)
        cfg = json.loads(raw)
        self.assertEqual(cfg["portal"], "claude-code.code")
        self.assertEqual(cfg["tools"], ["search", "fetch"])
        self.assertIs(cfg["dependencies"]["engines"]["brave"], True)
        self.assertIs(cfg["dependencies"]["engines"]["stack"], True)
        self.assertIs(cfg["dependencies"]["local_llm_expected"], False)


class McpPortalIsolationTest(unittest.TestCase):
    def test_academic_portal_exposes_three_tools_in_order(self):
        with mock.patch.dict(os.environ, {"AGG_SEARCH_PORTAL": "codex.academic"}):
            init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18"}})
            tools = mcp.handle({"jsonrpc": "2.0", "id": 2,
                                "method": "tools/list"})["tools"]
        self.assertEqual(init["serverInfo"]["name"], "agg-search-academic")
        self.assertIn("1) search -> 2) fetch -> 3) answer",
                      init["instructions"])
        self.assertIn("untrusted data", init["instructions"])
        self.assertEqual([t["name"] for t in tools],
                         ["search", "fetch", "answer"])
        self.assertIn("Step 1 of 3", tools[0]["description"])

    def test_code_portal_hides_and_blocks_answer_tool(self):
        with mock.patch.dict(os.environ, {"AGG_SEARCH_PORTAL": "claude-code.code"}):
            init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {}})
            tools = mcp.handle({"jsonrpc": "2.0", "id": 2,
                                "method": "tools/list"})["tools"]
            self.assertEqual(init["serverInfo"]["name"], "agg-search-code")
            self.assertEqual([t["name"] for t in tools], ["search", "fetch"])
            with self.assertRaises(KeyError):
                mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "answer",
                                       "arguments": {"query": "q"}}})

    def test_news_portal_instructions_and_search_portal_arg(self):
        with mock.patch.dict(os.environ, {"AGG_SEARCH_PORTAL": "gemini.news"}):
            init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {}})
            args = mcp.build_args("search", {"query": "ai policy"})
        self.assertEqual(init["serverInfo"]["name"], "agg-search-news")
        self.assertIn("1-day", init["instructions"])
        self.assertEqual(args[args.index("--portal") + 1], "gemini.news")

    def test_answer_tool_prefetch_urls_pipeline(self):
        calls = []

        def fake_run_cli(name, args, request_id=None, stdin_text=None):
            calls.append((name, args, stdin_text))
            if name == "fetch":
                return (json.dumps({"url": "https://arxiv.org/abs/1",
                                    "tier_used": "static", "title": "Paper",
                                    "text": "Full abstract text",
                                    "blocked": False, "error": ""}) + "\n",
                        "", 0, False)
            return (json.dumps({"query": "q", "portal": "codex.academic",
                                "profile": "academic", "answer": "Summary [1]",
                                "mode_used": "local", "note": None,
                                "degraded": [], "sources_used": 1}),
                    "", 0, False)

        with mock.patch.dict(os.environ, {"AGG_SEARCH_PORTAL": "codex.academic"}), \
                mock.patch.object(mcp, "_run_cli", fake_run_cli):
            res = mcp.call_tool("answer", {
                "query": "q", "urls": ["https://arxiv.org/abs/1"]})
        self.assertFalse(res["isError"])
        self.assertEqual([c[0] for c in calls], ["fetch", "answer"])
        payload = json.loads(calls[1][2])
        self.assertEqual(payload["sources"][0]["text"], "Full abstract text")
        self.assertEqual(res["structuredContent"]["answer"], "Summary [1]")

    def test_stdio_subprocess_portal_isolation(self):
        env_code = dict(os.environ, AGG_SEARCH_PORTAL="claude-code.code")
        msgs = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "answer",
                                   "arguments": {"query": "q"}}}),
        ]) + "\n"
        proc = subprocess.run(
            [sys.executable, "-m", "agg_search.mcp"],
            input=msgs, capture_output=True, text=True, env=env_code,
            timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = {r["id"]: r for r in
                   (json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip())}
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"],
                         "agg-search-code")
        self.assertEqual([t["name"] for t in replies[2]["result"]["tools"]],
                         ["search", "fetch"])
        self.assertEqual(replies[3]["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
