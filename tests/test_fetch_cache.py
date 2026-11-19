#!/usr/bin/env python3
"""Page-cache tests for agg_search.fetch — no network, temp cache dir."""

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from agg_search import fetch


def page(url, text="hello world", **kw):
    d = {"url": url, "tier_used": "deepseek", "status": 200, "title": "t",
         "text": text, "markdown": "", "blocked": False, "error": ""}
    d.update(kw)
    return d


class CacheHelpersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, fetch, "CACHE_DIR", fetch.CACHE_DIR)
        self.addCleanup(setattr, fetch, "FETCH_CACHE_PATH",
                        fetch.FETCH_CACHE_PATH)
        fetch.CACHE_DIR = self.tmp.name
        fetch.FETCH_CACHE_PATH = str(pathlib.Path(self.tmp.name) / "c.json")

    def test_usable_only_for_real_pages(self):
        self.assertTrue(fetch.usable(page("https://a")))
        self.assertFalse(fetch.usable(page("https://a", text="")))
        self.assertFalse(fetch.usable(page("https://a", error="boom")))
        self.assertFalse(fetch.usable(page("https://a", blocked=True)))

    def test_round_trip_and_prune(self):
        store = {"k%d" % i: {"at": i, "result": page("https://a")}
                 for i in range(fetch.MAX_CACHE_ENTRIES + 5)}
        fetch.save_cache(store)
        loaded = fetch.load_cache()
        self.assertEqual(len(loaded), fetch.MAX_CACHE_ENTRIES)
        self.assertNotIn("k0", loaded)

    def test_key_separates_modes(self):
        a = mock.Mock(tier="auto", out="json", text_limit=8000,
                      allow_private=False)
        b = mock.Mock(tier="static", out="json", text_limit=8000,
                      allow_private=False)
        c = mock.Mock(tier="auto", out="json", text_limit=8000,
                      allow_private=True)
        keys = {fetch.cache_key("https://a", a), fetch.cache_key("https://a", b),
                fetch.cache_key("https://a", c)}
        self.assertEqual(len(keys), 3)


class CliCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, fetch, "CACHE_DIR", fetch.CACHE_DIR)
        self.addCleanup(setattr, fetch, "FETCH_CACHE_PATH",
                        fetch.FETCH_CACHE_PATH)
        fetch.CACHE_DIR = self.tmp.name
        fetch.FETCH_CACHE_PATH = str(pathlib.Path(self.tmp.name) / "c.json")
        self.calls = []

    def _run(self, argv):
        out = io.StringIO()

        def fake_fetch(url, a, key):
            self.calls.append(url)
            return page(url, text="cached page body")

        with mock.patch.object(sys, "argv", ["crawl-fetch"] + argv), \
                mock.patch.object(fetch, "getenv",
                                  side_effect=lambda k, d=None: "k"), \
                mock.patch.object(fetch, "check_url",
                                  return_value=(True, "")), \
                mock.patch.object(fetch, "deepseek_fetch", fake_fetch), \
                mock.patch.object(fetch, "find_launcher", return_value=None), \
                contextlib.redirect_stdout(out):
            code = fetch.main()
        return code, [json.loads(line) for line in out.getvalue().splitlines()
                      if line.startswith("{")]

    def test_second_run_is_a_cache_hit(self):
        code, rows = self._run(["--url", "https://a.test/x"])
        self.assertEqual(code, 0)
        self.assertFalse(rows[0].get("cached", False))
        self.assertEqual(len(self.calls), 1)

        code, rows = self._run(["--url", "https://a.test/x"])
        self.assertEqual(code, 0)
        self.assertTrue(rows[0]["cached"])
        self.assertEqual(len(self.calls), 1)  # no second engine call

    def test_no_cache_forces_a_refetch(self):
        self._run(["--url", "https://a.test/x"])
        code, rows = self._run(["--url", "https://a.test/x", "--no-cache"])
        self.assertFalse(rows[0].get("cached", False))
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
