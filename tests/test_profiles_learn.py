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


if __name__ == "__main__":
    unittest.main()
