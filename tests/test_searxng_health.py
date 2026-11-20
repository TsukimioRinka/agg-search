"""SearXNG answering 0 rows because every upstream is suspended is a failure,
not a healthy empty result (it must cool down and not claim 'too narrow')."""

import contextlib
import io
import json
import sys
import unittest
from unittest import mock

from agg_search import search


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def open(self, req, timeout=None):
        return _Resp(self.body)


SUSPENDED = [["brave", "Suspended: too many requests"],
             ["duckduckgo", "CAPTCHA"]]


class SearxngHealthTest(unittest.TestCase):
    def test_empty_with_unresponsive_upstreams_is_a_failure(self):
        opener = _Opener({"results": [], "unresponsive_engines": SUSPENDED})
        with mock.patch.object(search, "_OPENER", opener):
            rows, err = search.eng_searxng("q", 5, None)
        self.assertIsNone(rows)
        self.assertIn("upstreams unresponsive", err)
        self.assertIn("duckduckgo=CAPTCHA", err)
        self.assertEqual(search.classify_error(err)[0], "antibot")

    def test_rows_with_some_unresponsive_upstreams_stay_ok(self):
        opener = _Opener({"results": [{"url": "https://a.test/", "title": "A",
                                       "content": "c"}],
                          "unresponsive_engines": SUSPENDED})
        with mock.patch.object(search, "_OPENER", opener):
            rows, err = search.eng_searxng("q", 5, None)
        self.assertIsNone(err)
        self.assertEqual(rows, [("https://a.test", "A", "c")])

    def test_empty_with_healthy_upstreams_stays_healthy_empty(self):
        opener = _Opener({"results": [], "unresponsive_engines": []})
        with mock.patch.object(search, "_OPENER", opener):
            self.assertEqual(search.eng_searxng("q", 5, None), ([], None))

    def test_reason_text_is_bounded_and_sanitized(self):
        noisy = [["e%d" % i, "x\u202e" * 100] for i in range(20)]
        opener = _Opener({"results": [], "unresponsive_engines": noisy})
        with mock.patch.object(search, "_OPENER", opener):
            _rows, err = search.eng_searxng("q", 5, None)
        self.assertLessEqual(len(err), 300)
        self.assertNotIn("\u202e", err)

    def test_cli_reports_failure_not_too_narrow(self):
        opener = _Opener({"results": [], "unresponsive_engines": SUSPENDED})
        saved = {}
        argv = ["agg-search", "q", "--engines", "searxng", "--no-jina",
                "--no-cache", "--no-learn"]
        err = io.StringIO()
        with mock.patch.object(search, "_OPENER", opener), \
                mock.patch.object(search, "load_cooldown", return_value={}), \
                mock.patch.object(search, "save_cooldown",
                                  side_effect=saved.update), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as cm:
            search.main()
        msg = str(cm.exception.code)
        self.assertIn("all engines failed", msg)
        self.assertNotIn("too narrow", msg)
        self.assertIn("searxng", saved)


if __name__ == "__main__":
    unittest.main()
