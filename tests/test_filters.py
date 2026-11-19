#!/usr/bin/env python3
"""Zero-network tests for domain filtering and snippet rendering.

Run: python3 -m unittest tests.test_filters -v
Pure helpers plus one CLI run against a fake engine: no HTTP, no cache files.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from agg_search import search as m


def rows(*urls):
    return [(u, "t", "s") for u in urls]


class ParseDomainsTest(unittest.TestCase):
    def test_cleans_and_dedupes(self):
        self.assertEqual(m.parse_domains(" A.com, , .edu,a.com "),
                         ("a.com", ".edu"))

    def test_accepts_iterables(self):
        self.assertEqual(m.parse_domains(["b.org", " ", "A.COM"]),
                         ("b.org", "a.com"))


class DomainRuleTest(unittest.TestCase):
    def test_exact_host(self):
        self.assertTrue(m.domain_matches_rule("arxiv.org", "arxiv.org"))
        self.assertFalse(m.domain_matches_rule("arxiv.org.evil.net",
                                               "arxiv.org"))

    def test_subdomain(self):
        self.assertTrue(m.domain_matches_rule("export.arxiv.org",
                                              "arxiv.org"))
        self.assertFalse(m.domain_matches_rule("notarxiv.org", "arxiv.org"))

    def test_dot_edu_suffix(self):
        self.assertTrue(m.domain_matches_rule("cs.mit.edu", ".edu"))
        self.assertFalse(m.domain_matches_rule("notedu.com", ".edu"))
        self.assertFalse(m.domain_matches_rule("edu.example.com", ".edu"))

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(m.domain_matches_rule("ARXIV.org", "  ArXiv.ORG "))
        self.assertTrue(m.domain_matches_rule("arxiv.org", " .arxiv.org "))

    def test_empty_rules_never_match(self):
        for rule in ("", "  ", ".", ".."):
            self.assertFalse(m.domain_matches_rule("example.com", rule))
        self.assertFalse(m.domain_matches_rule("", "example.com"))


class FilterRowsTest(unittest.TestCase):
    def test_no_rules_keeps_everything(self):
        r = rows("https://a.com/1", "https://b.org/2")
        self.assertEqual(m.filter_rows(r, [], []), (r, 0))

    def test_include_restricts_and_exclude_wins(self):
        r = rows("https://arxiv.org/a", "https://evil.arxiv.org/b",
                 "https://example.com/c")
        kept, removed = m.filter_rows(r, "arxiv.org", "evil.arxiv.org")
        self.assertEqual([u for u, _, _ in kept], ["https://arxiv.org/a"])
        self.assertEqual(removed, 2)

    def test_suffix_rule(self):
        r = rows("https://mit.edu/1", "https://example.com/2")
        kept, removed = m.filter_rows(r, [".edu"], [])
        self.assertEqual([u for u, _, _ in kept], ["https://mit.edu/1"])
        self.assertEqual(removed, 1)

    def test_hostless_rows_dropped_by_include(self):
        r = rows("", "notaurl")
        kept, removed = m.filter_rows(r, ["example.com"], [])
        self.assertEqual(kept, [])
        self.assertEqual(removed, 2)


class ApplyFilterTest(unittest.TestCase):
    def test_counts_fold_variants(self):
        res = {"exa": rows("https://a.com/1", "https://keep.io/1"),
               "exa#1": rows("https://b.com/2"),
               "brave": rows("https://keep.io/2")}
        out, counts = m.apply_domain_filter(res, [], ["com"])
        self.assertEqual(counts, {"exa": 2})
        self.assertEqual(sorted(out), ["brave", "exa"])
        self.assertEqual([u for u, _, _ in out["exa"]],
                         ["https://keep.io/1"])

    def test_noop_without_rules(self):
        res = {"exa": rows("https://a.com/1")}
        out, counts = m.apply_domain_filter(res, (), ())
        self.assertEqual(out, res)
        self.assertEqual(counts, {})


class CacheKeyTest(unittest.TestCase):
    KW = dict(query="q", engines=["exa"], recency=None, per_engine=5,
              rrf_top=15, top_n=10, no_jina=False, expand=0,
              diversify=True)

    def test_filter_and_snippet_sensitivity(self):
        base = m.cache_key(**self.KW)
        self.assertEqual(base, m.cache_key(**self.KW))
        self.assertNotEqual(base, m.cache_key(
            **self.KW, include_domains=("arxiv.org",)))
        self.assertNotEqual(base, m.cache_key(
            **self.KW, exclude_domains=("spam.com",)))
        self.assertNotEqual(base, m.cache_key(**self.KW, snippet_chars=200))

    def test_rule_order_insensitive(self):
        self.assertEqual(
            m.cache_key(**self.KW, include_domains=("a.com", "b.com")),
            m.cache_key(**self.KW, include_domains=("b.com", "a.com")))

    def test_clamped_snippet_shares_key(self):
        self.assertEqual(m.cache_key(**self.KW, snippet_chars=10),
                         m.cache_key(**self.KW, snippet_chars=80))


class SnippetTest(unittest.TestCase):
    def test_clip(self):
        self.assertEqual(len(m.clip_snippet("x" * 900, 400)), 400)
        self.assertEqual(m.clip_snippet("short", 400), "short")
        self.assertEqual(m.clip_snippet(None, 100), "")

    def test_clamp_window(self):
        self.assertEqual(m.clamp_snippet_chars(10), 80)
        self.assertEqual(m.clamp_snippet_chars(9999), 2000)
        self.assertEqual(m.clamp_snippet_chars(400), 400)


class MainEndToEndTest(unittest.TestCase):
    """Offline CLI run: filters, health counters and snippet length."""

    @staticmethod
    def fake(q, n, recency):
        return ([("https://arxiv.org/abs/1", "A", "S" * 500),
                 ("https://example.com/x", "B", "x" * 500),
                 ("https://export.arxiv.org/abs/2", "C", "y" * 500)], None)

    def setUp(self):
        m.ENGINES["t_fix"] = (self.fake, 1.0)

    def tearDown(self):
        del m.ENGINES["t_fix"]

    def _run(self, *extra, use_cache=False):
        argv = ["agg-search", "q", "--engines", "t_fix", "--no-cooldown",
                "--no-jina", "--include-domains", "arxiv.org"]
        if not use_cache:
            argv.append("--no-cache")
        argv.extend(extra)
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(buf):
            m.main()
        return buf.getvalue()

    def test_json_filters_and_truncates(self):
        payload = json.loads(self._run("--json", "--snippet-chars", "80"))
        urls = sorted(r["url"] for r in payload["results"])
        self.assertEqual(urls, ["https://arxiv.org/abs/1",
                                "https://export.arxiv.org/abs/2"])
        self.assertEqual(payload["engines"]["t_fix"], "ok (3)")
        health = payload["engine_health"]
        self.assertEqual(health["domain_filter"],
                         {"include": ["arxiv.org"], "exclude": []})
        self.assertEqual(health["filtered"], {"t_fix": 1})
        for r in payload["results"]:
            self.assertEqual(len(r["snippet"]), 80)

    def test_markdown_notes_removals(self):
        out = self._run()
        self.assertIn("domain filter: 1 removed (t_fix=1)", out)
        self.assertNotIn("example.com", out)

    def test_cache_hit_still_filters(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(m, "CACHE_DIR", td), \
                    mock.patch.object(m, "CACHE_PATH",
                                      os.path.join(td, "c.json")):
                first = json.loads(self._run("--json", use_cache=True))
                second = json.loads(self._run("--json", use_cache=True))
        self.assertFalse(first["engine_health"]["cached"])
        self.assertTrue(second["engine_health"]["cached"])
        for payload in (first, second):
            self.assertEqual(sorted(r["url"] for r in payload["results"]),
                             ["https://arxiv.org/abs/1",
                              "https://export.arxiv.org/abs/2"])
            self.assertEqual(payload["engine_health"]["filtered"],
                             {"t_fix": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
