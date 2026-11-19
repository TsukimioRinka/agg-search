#!/usr/bin/env python3
"""Domain profiles and the local feedback loop: no network, temp stores."""

import argparse
import json
import os
import pathlib
import tempfile
import unittest

from agg_search import install, learn, mcp, profiles, search


def args(**kw):
    base = {
        "engines": search.DEFAULT_ENGINES, "recency": None,
        "snippet_chars": search.DEFAULT_SNIPPET_CHARS,
        "top_n": search.DEFAULT_TOP_N,
        "per_engine": search.DEFAULT_PER_ENGINE,
        "default_engines": search.DEFAULT_ENGINES, "default_recency": None,
        "default_snippet_chars": search.DEFAULT_SNIPPET_CHARS,
        "default_top_n": search.DEFAULT_TOP_N,
        "default_per_engine": search.DEFAULT_PER_ENGINE,
    }
    base.update(kw)
    return argparse.Namespace(**base)


class ProfileApplyTest(unittest.TestCase):
    def test_academic_presets(self):
        a = args()
        applied = profiles.apply(a, profiles.load("academic"))
        self.assertEqual(a.engines, "openalex,exa,deepseek,brave,tavily")
        self.assertEqual(a.top_n, 15)
        self.assertEqual(a.snippet_chars, 500)
        self.assertIn("engines", applied)

    def test_news_sets_recency(self):
        a = args()
        profiles.apply(a, profiles.load("news"))
        self.assertEqual(a.recency, "day")

    def test_explicit_flag_wins(self):
        a = args(engines="brave", top_n=3)
        profiles.apply(a, profiles.load("academic"))
        self.assertEqual(a.engines, "brave")
        self.assertEqual(a.top_n, 3)

    def test_env_reserved_fields_are_not_touched(self):
        a = args()
        profiles.apply(a, profiles.load("academic"), reserved={"engines"})
        self.assertEqual(a.engines, search.DEFAULT_ENGINES)
        self.assertEqual(a.snippet_chars, 500)

    def test_unknown_profile_names_alternatives(self):
        with self.assertRaises(profiles.UnknownProfile) as ctx:
            profiles.load("nope")
        self.assertIn("academic", str(ctx.exception))

    def test_weights_override_table(self):
        table = {"openalex": (None, 1.0), "brave": (None, 1.0)}
        weights = profiles.weights(profiles.load("academic"), table)
        self.assertEqual(weights["openalex"], 1.8)
        self.assertEqual(weights["brave"], 1.0)


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        for name in ("FEEDBACK_PATH", "RECENT_PATH", "WEIGHTS_PATH"):
            self.addCleanup(setattr, learn, name, getattr(learn, name))
        learn.FEEDBACK_PATH = str(root / "feedback.json")
        learn.RECENT_PATH = str(root / "recent.json")
        learn.WEIGHTS_PATH = str(root / "config" / "weights.json")

    def test_attribution_counts_used_per_engine(self):
        ranked = [("https://a.test/x", 1.0,
                   {"engines": ["openalex", "exa"]}),
                  ("https://b.test/y", 0.5, {"engines": ["openalex"]})]
        learn.record_surfaced("academic", ranked)
        self.assertTrue(learn.record_used("https://a.test/x/"))
        self.assertFalse(learn.record_used("https://never.test/"))
        data = learn.load_feedback()
        counts = data["profiles"]["academic"]
        self.assertEqual(counts["openalex"]["surfaced"], 2)
        self.assertEqual(counts["openalex"]["used"], 1)
        self.assertEqual(counts["exa"]["used"], 1)

    def test_store_holds_hashes_not_urls(self):
        learn.record_surfaced("general",
                              [("https://secret.test/p", 1.0,
                                {"engines": ["brave"]})])
        raw = pathlib.Path(learn.FEEDBACK_PATH).read_text()
        recent = pathlib.Path(learn.RECENT_PATH).read_text()
        self.assertNotIn("secret.test", raw + recent)

    def test_small_samples_keep_weight_one(self):
        learn.record_surfaced("code", [("https://a", 1.0,
                                        {"engines": ["stack"]})])
        self.assertEqual(learn.multipliers()["code"]["stack"], 1.0)

    def test_strong_signal_is_clamped_and_saved(self):
        with open(learn.FEEDBACK_PATH, "w", encoding="utf-8") as f:
            json.dump({"profiles": {"code": {
                "stack": {"surfaced": 200, "used": 80}}},
                "updated": 0}, f)
        data = learn.tune(min_surfaced=30)
        self.assertEqual(data["profiles"]["code"]["stack"], 2.0)
        saved = json.loads(pathlib.Path(learn.WEIGHTS_PATH).read_text())
        self.assertEqual(saved["profiles"]["code"]["stack"], 2.0)
        self.assertEqual(learn.load_multipliers("code"), {"stack": 2.0})
        self.assertEqual(learn.load_multipliers("academic"), {})

    def test_disabled_recording_is_a_noop(self):
        learn.record_surfaced("general", [("https://a", 1.0,
                                           {"engines": ["brave"]})],
                              enabled=False)
        self.assertFalse(os.path.exists(learn.FEEDBACK_PATH))


class SurfaceTest(unittest.TestCase):
    def test_mcp_profile_enum_matches_profiles(self):
        schema = mcp.SEARCH_SCHEMA["properties"]["profile"]
        self.assertEqual(schema["enum"], profiles.names())

    def test_mcp_passes_profile_flag(self):
        built = mcp.build_args("search", {"query": "q", "profile": "code"})
        self.assertEqual(built[built.index("--profile") + 1], "code")

    def test_search_engine_enum_covers_every_engine(self):
        enum = set(mcp.SEARCH_SCHEMA["properties"]["engines"]["items"]["enum"])
        self.assertEqual(enum, set(search.ENGINES))

    def test_installer_pins_profile_env(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = install.main(["-c", "generic", "--profile", "academic",
                               "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["entry"]["env"]["AGG_SEARCH_PROFILE"],
                         "academic")

    def test_cache_key_separates_profiles(self):
        a = search.cache_key("q", ["brave"], None, 5, 15, 10, False, 0, False,
                             extra="profile=academic")
        b = search.cache_key("q", ["brave"], None, 5, 15, 10, False, 0, False,
                             extra="profile=code")
        self.assertNotEqual(a, b)


class FastPrimaryTest(unittest.TestCase):
    def test_fast_mode_waits_for_the_primary_engine(self):
        import time as _time

        from agg_search import search as m

        def slow(q, n, r):
            _time.sleep(0.4)
            return [("https://primary.test/x", "p", "")], None

        def fast(q, n, r):
            _time.sleep(0.01)
            return [("https://secondary.test/y", "s", "")], None

        m.ENGINES["t_primary"] = (slow, 1.0)
        m.ENGINES["t_secondary"] = (fast, 1.0)
        try:
            results, status, _note = m.recall_flat(
                ["q"], 3, ["t_secondary", "t_primary"], None,
                budget_secs=5, min_sources=1, must_engines=["t_primary"])
        finally:
            del m.ENGINES["t_primary"]
            del m.ENGINES["t_secondary"]
        self.assertTrue(status["t_primary"].startswith("ok"))
        urls = [u for rows in results.values() for u, _, _ in rows]
        self.assertIn("https://primary.test/x", urls)


class RerankBlendTest(unittest.TestCase):
    def test_blend_zero_keeps_the_rrf_order(self):
        scored = [(0, 0.1), (1, 0.5), (2, 0.9)]
        self.assertEqual(search._blend_order(scored, 3, 0.0), [0, 1, 2])

    def test_blend_one_follows_jina(self):
        scored = [(0, 0.1), (1, 0.5), (2, 0.9)]
        self.assertEqual(search._blend_order(scored, 3, 1.0), [2, 1, 0])

    def test_profiles_declare_a_blend(self):
        self.assertEqual(profiles.PROFILES["academic"]["rerank_blend"], 0.4)
        self.assertEqual(profiles.PROFILES["code"]["rerank_blend"], 0.6)

    def test_jina_requests_the_full_score_vector(self):
        from unittest import mock
        captured = {}

        def fake_http_json(url, **kw):
            captured.update(kw)
            return {"results": [{"index": i, "relevance_score": float(i)}
                                for i in range(3)]}

        ranked = [("u0", 1.0, {"title": "t", "snippet": "s"}),
                  ("u1", 0.9, {"title": "t", "snippet": "s"}),
                  ("u2", 0.8, {"title": "t", "snippet": "s"})]
        with mock.patch.object(search, "http_json", fake_http_json), \
                mock.patch.object(search, "getenv", lambda k, d=None: "key"):
            out, err = search.jina_rerank("q", ranked, 2, blend=0.0)
        self.assertIsNone(err)
        self.assertEqual(captured["body"]["top_n"], 3)
        self.assertEqual([u for u, _, _ in out], ["u0", "u1", "u2"])


if __name__ == "__main__":
    unittest.main()
