"""Regression tests for the pre-1.0 review findings."""

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

from agg_search import eval as ev
from agg_search import install, mcp, profiles, search

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "agg_search"


class PlainScriptTest(unittest.TestCase):
    """-S drops site-packages, so the installed package is not importable:
    the scripts must still run from a checkout (finding 1)."""

    def test_each_cli_runs_as_a_plain_script(self):
        for name in ("search", "fetch", "mcp", "install", "learn", "eval"):
            with self.subTest(script=name):
                p = subprocess.run([sys.executable, "-S", str(SRC / (name + ".py")),
                                    "--version"], capture_output=True, text=True,
                                   timeout=60)
                self.assertEqual(p.returncode, 0, p.stderr[-400:])
                self.assertTrue(p.stdout.strip())


class EntryPortalTest(unittest.TestCase):
    def test_opencode_shape_is_understood(self):  # finding 2
        entry = install.payload("opencode", ["uvx", "--from", "agg-search",
                                             "agg-search-mcp"],
                                {"AGG_SEARCH_PORTAL": "opencode.code"})
        self.assertEqual(install._entry_portal(entry), "opencode.code")
        listed = {"command": ["agg-search-mcp", "--portal", "x.code"]}
        self.assertEqual(install._entry_portal(listed), "x.code")

    def test_portal_argument_wins_like_the_server(self):  # finding 3
        entry = {"command": "agg-search-mcp", "args": ["--portal=b.code"],
                 "env": {"AGG_SEARCH_PORTAL": "a.code"}}
        self.assertEqual(install._entry_portal(entry), "b.code")


class SearxngCooldownTest(unittest.TestCase):
    def test_every_upstream_reason_cools(self):  # finding 4
        for reason in ("upstreams unresponsive: bing=HTTP error",
                       "upstreams unresponsive: x=parsing error",
                       "upstreams unresponsive: unknown"):
            self.assertEqual(search.classify_error(reason)[0], "antibot")


class StackMultiSiteTest(unittest.TestCase):
    def _env(self, k, d=None):
        return "stackoverflow,unix" if k == "STACKEXCHANGE_SITE" else d

    def test_fast_deadline_reaches_site_threads(self):  # finding 6
        seen = []

        def fake(q, n, r, site):
            seen.append(getattr(search._TLS, "deadline", None))
            return []

        search._TLS.deadline = 12345.0
        try:
            with mock.patch.object(search, "getenv", self._env), \
                    mock.patch.object(search, "_stack_site", fake):
                search.eng_stack("q", 5, None)
        finally:
            search._TLS.deadline = None
        self.assertEqual(seen, [12345.0, 12345.0])

    def test_partial_site_failure_is_reported(self):  # finding 7
        def fake(q, n, r, site):
            if site == "unix":
                raise urllib.error.HTTPError("u", 400, "bad", {}, None)
            return [("https://so.test/1", "t", "s")]

        search.PARTIAL_NOTES.clear()
        with mock.patch.object(search, "getenv", self._env), \
                mock.patch.object(search, "_stack_site", fake):
            rows, err = search.eng_stack("q", 5, None)
        self.assertEqual((len(rows), err), (1, None))
        self.assertIn("unix: HTTPError", search.PARTIAL_NOTES.pop("stack"))


class FallbackCooldownTest(unittest.TestCase):
    def test_cooling_fallback_is_skipped_and_failures_are_persisted(self):  # 8
        import time as _time
        calls = []
        saved = {}
        orig = {e: search.ENGINES[e] for e in ("openalex", "arxiv", "searxng")}

        def track(name, result):
            def fn(q, n, r):
                calls.append(name)
                return result
            return (fn, orig[name][1])

        search.ENGINES["openalex"] = track("openalex", (None, "HTTP Error 429"))
        search.ENGINES["arxiv"] = track("arxiv", ([("https://arxiv.org/abs/1", "p", "s")], None))
        search.ENGINES["searxng"] = track("searxng", (None, "upstreams unresponsive: x=CAPTCHA"))
        try:
            argv = ["agg-search", "q", "--portal", "codex.academic",
                    "--engines", "openalex", "--no-jina", "--json", "--no-learn"]
            with mock.patch.object(search, "load_cooldown",
                                   return_value={"arxiv": _time.time() + 300}), \
                    mock.patch.object(search, "save_cooldown", side_effect=saved.update), \
                    mock.patch.object(search, "load_cache", return_value={}), \
                    mock.patch.object(search, "save_cache"), \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                search.main()
        finally:
            search.ENGINES.update(orig)
        self.assertNotIn("arxiv", calls)          # cooling fallback not called
        self.assertIn("searxng", calls)
        self.assertIn("searxng", saved)           # supplement failure cools


class PromptValidationTest(unittest.TestCase):
    def test_bad_arguments_map_to_invalid_params(self):  # finding 9
        for args in (["topic"], "topic", 5):
            with self.assertRaises(KeyError):
                mcp.get_prompt("web-research", args, None)
        for topic in ("\u200b", "\u2060\u2061", "\x00 "):
            with self.assertRaises(KeyError):
                mcp.get_prompt("web-research", {"topic": topic}, None)
        text = mcp.get_prompt("web-research", {"topic": "a\u2060b"},
                              None)["messages"][0]["content"]["text"]
        self.assertIn("Research this topic: ab", text)


class EvalEdgeTest(unittest.TestCase):
    def test_record_with_every_engine_cooling_exits_clearly(self):  # 10a
        with tempfile.TemporaryDirectory() as d:
            qp = os.path.join(d, "q.json")
            with open(qp, "w") as f:
                json.dump(["q"], f)
            a = SimpleNamespace(queries=qp, out=os.path.join(d, "o.json"),
                                engines="brave", per_engine=5, recency=None,
                                ignore_cooldown=False)
            with mock.patch.object(search, "load_cooldown",
                                   return_value={"brave": 9e12}):
                with self.assertRaises(SystemExit) as cm:
                    ev.record(a)
        self.assertIn("all cooling", str(cm.exception.code))

    def test_mixed_engine_edits_are_rejected(self):  # 10b
        with self.assertRaises(SystemExit):
            ev.parse_variant("X:engines=+arxiv,brave", profiles.load("academic"))


if __name__ == "__main__":
    unittest.main()
