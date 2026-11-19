#!/usr/bin/env python3
"""Zero-network regression tests for agg_search resilience layer.

Run: python3 tests_offline.py  (stdlib only, no quota, no files touched)
Covers reviewer-caught crashes: hostless-URL AttributeError (P2-3),
Future.result misuse, unknown-engine KeyError, empty-as-source (P2-4).
"""
import unittest

from agg_search import search as m


def rows(*urls):
    return [(u, "t", "s") for u in urls]


class DomainTest(unittest.TestCase):
    def test_etld1(self):
        self.assertEqual(m.domain_of("https://blog.example.com/a"),
                         "example.com")
        self.assertEqual(m.domain_of("https://www.bbc.co.uk/n"), "bbc.co.uk")

    def test_ip_kept(self):
        self.assertEqual(m.domain_of("http://127.0.0.1:8080/x"), "127.0.0.1")

    def test_hostless_never_raises(self):  # P2-3 regression
        for u in ("example.com/x", "https://", "https:///x", "", "#frag",
                  "notaurl"):
            self.assertIsInstance(m.domain_of(u), str)

    def test_diversify_keeps_all(self):
        r = [("https://a.com/%d" % i, 1.0 / (i + 1),
              {"title": "t", "snippet": "s", "engines": ["e"]})
             for i in range(4)]
        r.append(("https://b.org/x", 0.01,
                  {"title": "t", "snippet": "s", "engines": ["e"]}))
        out = m.diversify_ranked(r)
        self.assertEqual(len(out), 5)
        self.assertEqual(
            len({m.domain_of(u) for u, _, _ in out[:2]}), 2)


class ClassifyTest(unittest.TestCase):
    def test_table(self):
        cases = {
            "no EXA_API_KEY": ("no_key", 0),
            "HTTPError: HTTP Error 401: Unauthorized": ("quota", 3600),
            "all Tavily keys failed: x": ("quota", 3600),
            "HTTPError: HTTP Error 429: Too Many Requests": ("antibot", 600),
            "duckduckgo CAPTCHA": ("antibot", 600),
            "TimeoutError: timed out": ("timeout", 90),
            "HTTPError: HTTP Error 503": ("timeout", 90),
            "skipped: min_sources met, not awaited": ("unknown", 0),
            "ok (0)": ("unknown", 0),
        }
        for err, want in cases.items():
            self.assertEqual(m.classify_error(err), want, err)

    def test_empty_never_cools(self):
        for s in ("ok (5)", "ok (0)"):
            _, ttl = m.classify_error(s)
            self.assertEqual(ttl, 0, s)


class FuseTest(unittest.TestCase):
    def test_variant_keys_and_unknown_engines(self):
        res = {"exa": rows("https://a.com/1", "https://b.org/1"),
               "brave#1": rows("https://a.com/1", "https://c.net/1"),
               "nope": rows("https://d.io/1")}
        self.assertTrue(m.rrf_fuse(res, 10))
        self.assertNotEqual(m.rrf_fuse(res, 10, k=10),
                            m.rrf_fuse(res, 10))

    def test_head5_protected(self):
        res = {"e1": rows("https://a.com/1", "https://a.com/2",
                           "https://a.com/3", "https://a.com/4",
                           "https://a.com/5", "https://a.com/6"),
               "e2": rows("https://b.org/1")}
        plain = [u for u, _, _ in m.rrf_fuse(res, 15, k=60)]
        div = [u for u, _, _ in
               m.rrf_fuse(res, 15, k=60, diversify=True)]
        self.assertEqual(div[:5], plain[:5])
        self.assertIn("https://b.org/1", div[:10])


class FlatTest(unittest.TestCase):
    def setUp(self):
        m.ENGINES["t_a"] = (lambda q, n, r: (rows("https://a.com/1"), None),
                            1.0)
        m.ENGINES["t_b"] = (lambda q, n, r: (None, "HTTPError 429"), 1.0)
        m.ENGINES["t_e"] = (lambda q, n, r: ([], None), 1.0)

    def tearDown(self):
        for k in ("t_a", "t_b", "t_e"):
            del m.ENGINES[k]

    def test_min_sources_counts_rows_only(self):  # P2-4 regression
        res, st, note = m.recall_flat(["q"], 5, ["t_e", "t_a", "t_b"], None,
                                      budget_secs=10.0, min_sources=2)
        self.assertIn("t_a#0", res)
        self.assertTrue(note.startswith("sourcesTarget: 2, achieved: 1"),
                        note)

    def test_unmet_target_notes_not_fails(self):
        _, st, note = m.recall_flat(["q"], 5, ["t_b"], None,
                                    budget_secs=5.0, min_sources=1)
        self.assertIn("sourcesTarget", note)
        self.assertNotIn("t_b", [k for k in st if st[k].startswith("ok")])


class CacheKeyTest(unittest.TestCase):
    def test_mode_separation(self):  # P1-2 regression
        base = dict(query="q", engines=["exa"], recency=None, per_engine=5,
                    rrf_top=15, top_n=10, no_jina=False, expand=0,
                    diversify=True)
        full = m.cache_key(**base)
        fast = m.cache_key(**{**base, "fast": True, "budget_secs": 30.0,
                              "min_sources": 2})
        self.assertNotEqual(full, fast)

    def test_engine_order_insensitive(self):
        kw = dict(query="q", recency=None, per_engine=5, rrf_top=15,
                  top_n=10, no_jina=False, expand=0, diversify=False)
        self.assertEqual(m.cache_key(engines=["a", "b"], **kw),
                         m.cache_key(engines=["b", "a"], **kw))


if __name__ == "__main__":
    unittest.main(verbosity=2)
