#!/usr/bin/env python3
"""Offline tests for the optional Agent-Reach bridge (no network, no binary)."""

import contextlib
import io
import json
import sys
import unittest
from unittest import mock

from agg_search import engine_specs, fetch, profiles, reach, search


def proc(stdout="", stderr="", code=0):
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=code)


def envelope(items, ok=True, error=None):
    return json.dumps({
        "schema_version": 1, "ok": ok, "channel": "github",
        "action": "search", "backend": "gh",
        "items": items if ok else [],
        "meta": {"elapsed_ms": 1, "attempts": []},
        "error": error,
    })


class ChannelMapTest(unittest.TestCase):
    def test_read_hosts_match_suffix_not_substring(self):
        self.assertEqual(reach.channel_for("https://github.com/o/r"), "github")
        self.assertEqual(reach.channel_for("https://gist.github.com/x"), "github")
        self.assertEqual(reach.channel_for("https://youtu.be/x"), "youtube")
        self.assertIsNone(reach.channel_for("https://notgithub.com/x"))
        self.assertIsNone(reach.channel_for("https://example.com/github.com"))
        # xueqiu has no read action; keeping it mapped would only waste a call.
        self.assertIsNone(reach.channel_for("https://xueqiu.com/S/SH600000"))

    def test_twitter_uses_thread_and_profile_actions(self):
        url = "https://x.com/someone/status/12345"
        self.assertEqual(reach.read_spec("twitter", url), ("thread", url))
        self.assertEqual(reach.read_spec("twitter", "https://x.com/jack"),
                         ("profile", "jack"))
        self.assertEqual(reach.read_spec("twitter", "https://x.com/"),
                         (None, None))
        self.assertEqual(reach.read_spec("github", url), ("read", url))

    def test_engine_names_match_the_registry(self):
        self.assertEqual(set(reach.CHANNEL_ENGINES),
                         {n for n in engine_specs.names()
                          if n.startswith("ar_")})
        for engine in reach.CHANNEL_ENGINES:
            self.assertIn(engine, search.ENGINES)
            self.assertNotIn(engine, search.DEFAULT_ENGINES.split(","))


class SearchTest(unittest.TestCase):
    def test_items_normalize_and_dedupe(self):
        payload = envelope([
            {"title": "T", "url": "https://github.com/o/r", "author": "ann",
             "published_at": "2026-11-20T00:00:00Z", "text": "desc"},
            {"title": "T", "url": "https://github.com/o/r#readme",
             "author": "ann", "text": "dup"},
            {"title": "bad", "url": "ftp://example.com/x"},
            {"title": "Raw", "url": "https://example.com/x",
             "raw": {"description": "raw text"}},
        ])
        with mock.patch.object(reach.subprocess, "run",
                               return_value=proc(payload)):
            rows, err = reach.search("ar_github", "rrf", 5)
        self.assertIsNone(err)
        self.assertEqual(len(rows), 2)
        url, title, snippet = rows[0]
        self.assertEqual(url, "https://github.com/o/r")
        self.assertEqual(title, "T")
        self.assertEqual(snippet, "[ann · 2026-11-20] desc")
        self.assertEqual(rows[1][2], "raw text")

    def test_error_codes_map_to_existing_cooldown_classes(self):
        cases = [("RATE_LIMITED", "quota"), ("TIMEOUT", "timeout"),
                 ("PLATFORM_BLOCKED", "antibot")]
        for code, category in cases:
            payload = envelope([], ok=False,
                               error={"code": code, "message": "m"})
            with mock.patch.object(reach.subprocess, "run",
                                   return_value=proc(payload, code=1)):
                rows, err = reach.search("ar_github", "q", 5)
            self.assertIsNone(rows)
            self.assertEqual(search.classify_error(err)[0], category, code)

    def test_login_setup_and_bad_output_are_plain_skips(self):
        for code in ("NEED_LOGIN", "NEED_SETUP", "UPSTREAM_BROKEN"):
            payload = envelope([], ok=False,
                               error={"code": code, "message": "m"})
            with mock.patch.object(reach.subprocess, "run",
                                   return_value=proc(payload, code=1)):
                rows, err = reach.search("ar_github", "q", 5)
            self.assertIsNone(rows)
            self.assertEqual(search.classify_error(err),
                             ("unknown", 0), code)
        with mock.patch.object(reach.subprocess, "run",
                               return_value=proc("not json", "boom")):
            rows, err = reach.search("ar_github", "q", 5)
        self.assertIsNone(rows)
        self.assertIn("unreadable output", err)

    def test_timeout_flag_precedes_positional_terminator(self):
        run = mock.Mock(return_value=proc(envelope([])))
        with mock.patch.object(reach.subprocess, "run", run):
            reach.search("ar_github", "-q", 5, timeout=7)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[-4:], ["--timeout", "7.0", "--", "-q"])
        run = mock.Mock(return_value=proc(envelope([])))
        with mock.patch.object(reach.subprocess, "run", run):
            reach.read("https://github.com/o/r", timeout=9)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[-3:], ["9.0", "--", "https://github.com/o/r"])

    def test_missing_binary_is_a_skip_not_a_crash(self):
        with mock.patch.object(reach.subprocess, "run",
                               side_effect=FileNotFoundError()):
            rows, err = reach.search("ar_github", "q", 5)
        self.assertIsNone(rows)
        self.assertIn("no agent-reach", err)
        self.assertEqual(search.classify_error(err), ("unknown", 0))


class ReadTest(unittest.TestCase):
    def test_read_returns_ladder_shaped_text(self):
        payload = envelope([{"title": "Repo", "url": "https://github.com/o/r",
                             "text": "# README\nbody"}])
        with mock.patch.object(reach.subprocess, "run",
                               return_value=proc(payload)):
            page, err = reach.read("https://github.com/o/r", text_limit=500)
        self.assertIsNone(err)
        self.assertEqual(page["tier_used"], "agent-reach:github")
        self.assertEqual(page["title"], "Repo")
        self.assertIn("README", page["text"])

    def test_read_skips_thin_or_missing_text(self):
        payload = envelope([{"title": "Repo", "url": "https://github.com/o/r",
                             "text": "-"}])
        with mock.patch.object(reach.subprocess, "run",
                               return_value=proc(payload)):
            page, err = reach.read("https://github.com/o/r")
        self.assertIsNone(page)
        self.assertIn("no text", err)

    def test_read_without_channel(self):
        page, err = reach.read("https://example.com/x")
        self.assertIsNone(page)
        self.assertIn("no agent-reach channel", err)


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        reach._READY.clear()
        self.addCleanup(reach._READY.clear)

    @staticmethod
    def doctor(status):
        return json.dumps({
            "schema_version": 1, "ok": True, "action": "doctor",
            "result": {"channels": [{"channel": "xiaohongshu",
                                     "status": status}]},
        })

    def test_default_channel_never_probes_doctor(self):
        payload = envelope([{"title": "Repo", "url": "https://github.com/o/r",
                             "text": "x" * 20}])
        run = mock.Mock(return_value=proc(payload))
        with mock.patch.object(reach.subprocess, "run", run):
            page, err = reach.read("https://github.com/o/r")
        self.assertIsNone(err)
        self.assertEqual(run.call_count, 1)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[1:3], ["github", "read"])

    def test_not_added_channel_skips_the_browser_probe(self):
        run = mock.Mock(return_value=proc(self.doctor("not_added")))
        with mock.patch.object(reach.subprocess, "run", run):
            page, err = reach.read("https://www.xiaohongshu.com/explore/1")
        self.assertIsNone(page)
        self.assertIn("channel not added", err)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0][1], "doctor")

    def test_added_channel_proceeds_to_read(self):
        payload = envelope([{"title": "Note", "text": "x" * 20,
                             "url": "https://www.xiaohongshu.com/explore/1"}])
        run = mock.Mock(side_effect=[proc(self.doctor("unverified")),
                                     proc(payload)])
        with mock.patch.object(reach.subprocess, "run", run):
            page, err = reach.read("https://www.xiaohongshu.com/explore/1")
        self.assertIsNone(err)
        self.assertEqual(page["tier_used"], "agent-reach:xiaohongshu")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args[0][0][1], "xiaohongshu")

    def test_doctor_failure_fails_open(self):
        run = mock.Mock(side_effect=FileNotFoundError())
        with mock.patch.object(reach.subprocess, "run", run):
            page, err = reach.read("https://www.xiaohongshu.com/explore/1")
        self.assertIsNone(page)
        self.assertIn("no agent-reach", err)
        self.assertIs(reach._READY["xiaohongshu"], True)


class ProfileTest(unittest.TestCase):
    def test_platform_profile_is_wired_to_the_registry(self):
        prof = profiles.load("platform")
        self.assertEqual(set(prof["engines"]), set(reach.CHANNEL_ENGINES)
                         | {"brave", "searxng"})
        for name in prof["engines"]:
            self.assertIn(name, search.ENGINES)
        for name, weight in (prof.get("weights") or {}).items():
            self.assertEqual(weight, engine_specs.ENGINE_SPECS[name]["weight"])
        self.assertEqual(prof["primary"], list(reach.CHANNEL_ENGINES))


class FetchRungTest(unittest.TestCase):
    def _run(self, argv, read_result):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["crawl-fetch"] + argv), \
                mock.patch.object(fetch, "getenv",
                                  side_effect=lambda k, d=None: ""), \
                mock.patch.object(fetch, "check_url",
                                  return_value=(True, "")), \
                mock.patch.object(fetch, "load_cache", return_value={}), \
                mock.patch.object(fetch, "save_cache"), \
                mock.patch.object(fetch.reach, "channel_for",
                                  return_value="github"), \
                mock.patch.object(fetch.reach, "read",
                                  return_value=read_result), \
                mock.patch.object(fetch, "find_launcher", return_value=None), \
                contextlib.redirect_stdout(out):
            code = fetch.main()
        rows = [json.loads(line) for line in out.getvalue().splitlines()
                if line.startswith("{")]
        return code, rows

    def test_platform_url_uses_reach_before_the_ladder(self):
        page = {"url": "https://github.com/o/r", "tier_used": "agent-reach:github",
                "status": 200, "title": "Repo", "text": "x" * 100,
                "markdown": "", "blocked": False, "error": ""}
        code, rows = self._run(["--url", "https://github.com/o/r"], (page, None))
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["tier_used"], "agent-reach:github")

    def test_thin_reach_page_falls_through(self):
        page = {"url": "https://github.com/o/r", "tier_used": "agent-reach:github",
                "status": 200, "title": "Repo", "text": "-",
                "markdown": "", "blocked": False, "error": ""}
        code, rows = self._run(["--url", "https://github.com/o/r"], (page, None))
        self.assertEqual(code, 2)  # no launcher, nothing emitted
        self.assertEqual(rows, [])

    def test_platform_url_is_not_refetched_by_deepseek(self):
        page = {"url": "https://github.com/o/r", "tier_used": "agent-reach:github",
                "status": 200, "title": "Repo", "text": "x" * 100,
                "markdown": "", "blocked": False, "error": ""}
        calls = []

        def fake_ds(url, a, key):
            calls.append(url)
            return dict(page, url=url, tier_used="deepseek")

        out = io.StringIO()
        with mock.patch.object(
                sys, "argv", ["crawl-fetch", "--url", "https://github.com/o/r",
                              "--url", "https://example.com/x"]), \
                mock.patch.object(
                    fetch, "getenv",
                    side_effect=lambda k, d=None:
                    ("k" if k == "DEEPSEEK_API_KEY" else "")), \
                mock.patch.object(fetch, "check_url", return_value=(True, "")), \
                mock.patch.object(fetch, "load_cache", return_value={}), \
                mock.patch.object(fetch, "save_cache"), \
                mock.patch.object(
                    fetch.reach, "channel_for",
                    side_effect=lambda u: "github"
                    if "github.com" in u else None), \
                mock.patch.object(fetch.reach, "read", return_value=(page, None)), \
                mock.patch.object(fetch, "deepseek_fetch", fake_ds), \
                mock.patch.object(fetch, "find_launcher", return_value=None), \
                contextlib.redirect_stdout(out):
            fetch.main()
        self.assertEqual(calls, ["https://example.com/x"])
        tiers = [json.loads(line)["tier_used"]
                 for line in out.getvalue().splitlines() if line.startswith("{")]
        self.assertEqual(tiers, ["agent-reach:github", "deepseek"])


if __name__ == "__main__":
    unittest.main()
