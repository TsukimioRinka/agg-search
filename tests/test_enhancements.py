#!/usr/bin/env python3
"""Offline tests for benchmark-verified enhancements (dedupe, priors, prefetch)."""

import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

from agg_search import profiles, search


class CanonicalUrlTest(unittest.TestCase):
    def test_arxiv_variants_fold_to_single_id(self):
        urls = [
            "https://arxiv.org/abs/2205.14135",
            "https://arxiv.org/pdf/2205.14135.pdf",
            "https://arxiv.org/html/2205.14135v2",
            "https://export.arxiv.org/abs/2205.14135v1",
            "https://doi.org/10.48550/arXiv.2205.14135",
        ]
        self.assertEqual({search.canonical_url(u) for u in urls},
                         {"arxiv:2205.14135"})

    def test_scheme_www_and_tracking_params_fold(self):
        a = search.canonical_url(
            "http://www.sqlite.org/wal.html?utm_source=x&ref=hn#sec")
        b = search.canonical_url("https://sqlite.org/wal.html/")
        self.assertEqual(a, b)
        # Non-tracking query params stay distinct
        self.assertNotEqual(
            search.canonical_url("https://example.com/item?id=1"),
            search.canonical_url("https://example.com/item?id=2"))


class ContentDedupeTest(unittest.TestCase):
    def test_short_synthetic_rows_never_collapse(self):
        # Synthetic fixtures across tests share "t" / "s" titles; keep them distinct.
        results = {
            "e1": [("https://a.test/1", "t", "s"),
                   ("https://b.test/2", "t", "s"),
                   ("https://c.test/3", "T", "x" * 500)],
        }
        ranked = search.rrf_fuse(results, 10, dedupe=True)
        self.assertEqual(len(ranked), 3)

    def test_distinct_cjk_articles_do_not_false_collapse(self):
        t1 = "財報前夕爆召回 特斯拉近1.3萬輛電池瑕疵車緊急維修"
        s1 = "特斯拉因電池故障隐患在美國召回部分電動汽車並進行檢修"
        t2 = "極端情況下存安全隱患 比亞迪受監管調查後召回逾11.5萬輛車"
        s2 = "比亞迪因電池設計與安裝瑕疵在監管調查後啟動大規模召回"
        self.assertFalse(search.is_content_duplicate(t1, s1, t2, s2))

    def test_mirror_papers_collapse_and_merge_provenance(self):
        title = "FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness"
        snip = "We propose FlashAttention, an IO-aware exact attention algorithm using tiling."
        results = {
            "openalex": [
                ("https://arxiv.org/abs/2205.14135", title, snip),
                ("https://arxiv.org/pdf/2205.14135.pdf", title, snip),
            ],
            "exa": [
                ("https://proceedings.neurips.cc/paper/2022/flash.html",
                 title, snip),
            ],
        }
        ranked = search.rrf_fuse(results, 10, dedupe=True)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0][0], "https://arxiv.org/abs/2205.14135")
        self.assertEqual(set(ranked[0][2]["engines"]), {"openalex", "exa"})

    def test_no_dedupe_preserves_separate_mirrors(self):
        results = {
            "openalex": [("https://arxiv.org/abs/2205.14135", "Paper", "s")],
            "exa": [("https://arxiv.org/pdf/2205.14135", "Paper", "s")],
        }
        ranked = search.rrf_fuse(results, 10, dedupe=False)
        self.assertEqual(len(ranked), 2)


class AuthorityPriorTest(unittest.TestCase):
    def test_academic_prior_promotes_paper_over_blog_without_dropping(self):
        results = {
            "brave": [
                ("https://randomblog.example/post", "Blog Summary", "overview"),
                ("https://arxiv.org/abs/2401.00001", "Original Paper", "proof"),
            ],
        }
        prof = profiles.load("academic")
        base = search.rrf_fuse(results, 10, dedupe=True)
        boosted = search.rrf_fuse(
            results, 10, dedupe=True,
            domain_boost=prof["authority_domains"],
            boost_factor=prof["authority_boost"])
        self.assertEqual(base[0][0], "https://randomblog.example/post")
        self.assertEqual(boosted[0][0], "https://arxiv.org/abs/2401.00001")
        self.assertEqual(len(boosted), 2)

    def test_cache_key_separates_dedupe_and_auth(self):
        a = search.cache_key("q", ["exa"], None, 5, 15, 10, False, 0, True,
                             extra="profile=academic;dedupe=1;auth=1.35")
        b = search.cache_key("q", ["exa"], None, 5, 15, 10, False, 0, True,
                             extra="profile=academic;dedupe=0;auth=1.35")
        self.assertNotEqual(a, b)


class AnswerFetchPipelineTest(unittest.TestCase):
    def test_answer_fetch_prefetches_top_urls_and_passes_ssrf_cidr(self):
        captured_cmd = {}

        def fake_run(cmd, **kw):
            captured_cmd["cmd"] = cmd
            row = json.dumps({
                "url": "https://arxiv.org/abs/2205.14135",
                "tier_used": "static", "status": 200,
                "title": "FlashAttention",
                "text": "IO complexity is O(N^2 d^2 / M).",
                "blocked": False, "error": "",
            })
            return mock.Mock(returncode=0, stdout=row + "\n", stderr="")

        captured_prompt = {}

        def fake_answer(query, ranked, mode="auto", prompt_template=None,
                        fallback_order=("local", "deepseek"),
                        fetched_texts=None):
            captured_prompt["fetched"] = fetched_texts
            return "O(N^2 d^2 / M) [1]", "", "local", []

        orig = search.ENGINES["openalex"]
        search.ENGINES["openalex"] = (
            lambda q, n, r: ([("https://arxiv.org/abs/2205.14135",
                               "FlashAttention", "short snippet")], None),
            orig[1])
        try:
            argv = [
                "agg-search", "flash attention io", "--portal", "codex.academic",
                "--engines", "openalex", "--no-jina", "--no-cache",
                "--no-cooldown", "--answer", "--answer-fetch", "1", "--json",
            ]
            buf = io.StringIO()
            with mock.patch.dict(os.environ,
                                 {"AGG_SEARCH_ALLOW_CIDRS": "198.18.0.0/15"}), \
                    mock.patch.object(search.subprocess, "run", fake_run), \
                    mock.patch.object(search, "answer_with_fallback",
                                      fake_answer), \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(buf):
                search.main()
        finally:
            search.ENGINES["openalex"] = orig

        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["answer"], "O(N^2 d^2 / M) [1]")
        self.assertIn("--allow-cidrs", captured_cmd["cmd"])
        self.assertIn("https://arxiv.org/abs/2205.14135",
                      captured_prompt["fetched"])


if __name__ == "__main__":
    unittest.main()
