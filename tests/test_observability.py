#!/usr/bin/env python3
"""Zero-network tests for telemetry: per-engine latency, phase timings,
AGG_SEARCH_* env defaults and the --show-config diagnostic.

Run: python3 -m unittest tests.test_observability
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

ENV_KEYS = ("AGG_SEARCH_ENGINES", "AGG_SEARCH_TOP_N",
            "AGG_SEARCH_PER_ENGINE", "AGG_SEARCH_SNIPPET_CHARS",
            "AGG_SEARCH_FAST", "AGG_SEARCH_NO_JINA")


@contextlib.contextmanager
def env(**vals):
    """Run with every AGG_SEARCH_* cleared, then `vals` applied."""
    saved = {k: os.environ.pop(k, None) for k in ENV_KEYS}
    os.environ.update(vals)
    try:
        yield
    finally:
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def urls(n):
    return ["https://a.example/%d" % i for i in range(n)]


class RecallTimingTest(unittest.TestCase):
    """LAST_TIMINGS is written by all three recall entry points."""

    def setUp(self):
        m.LAST_TIMINGS.clear()
        m.ENGINES["t_obs"] = (
            lambda q, n, r: ([(u, "t", "s") for u in urls(2)], None), 1.0)
        m.ENGINES["t_boom"] = (
            lambda q, n, r: (_ for _ in ()).throw(RuntimeError("boom")), 1.0)

    def tearDown(self):
        del m.ENGINES["t_obs"], m.ENGINES["t_boom"]
        m.LAST_TIMINGS.clear()

    def test_recall_records_ms_per_engine(self):
        res, st = m.recall("q", 5, ["t_obs", "t_boom"], None)
        self.assertEqual(st["t_obs"], "ok (2)")
        self.assertTrue(st["t_boom"].startswith("skipped/failed"))
        self.assertEqual(set(m.LAST_TIMINGS), {"t_obs", "t_boom"})
        for ms in m.LAST_TIMINGS.values():
            self.assertIsInstance(ms, float)
            self.assertGreaterEqual(ms, 0.0)

    def test_multi_folds_variants(self):
        m.recall_multi(["q1", "q2", "q3"], 5, ["t_obs"], None)
        self.assertEqual(set(m.LAST_TIMINGS), {"t_obs"})
        self.assertGreaterEqual(m.LAST_TIMINGS["t_obs"], 0.0)

    def test_flat_records_ms(self):
        res, st, note = m.recall_flat(["q1", "q2"], 5, ["t_obs"], None,
                                      budget_secs=10.0)
        self.assertTrue(res)
        self.assertEqual(set(m.LAST_TIMINGS), {"t_obs"})
        self.assertGreaterEqual(m.LAST_TIMINGS["t_obs"], 0.0)


class JsonTimingTest(unittest.TestCase):
    """--json carries latency/timing; markdown stays telemetry-free."""

    def setUp(self):
        self.calls = 0
        m.ENGINES["t_obs"] = (self.fake, 1.0)

    def tearDown(self):
        del m.ENGINES["t_obs"]

    def fake(self, q, n, recency):
        self.calls += 1
        return ([(u, "T", "x" * 500) for u in urls(n)], None)

    def _run(self, *extra, use_cache=False):
        argv = ["agg-search", "q", "--engines", "t_obs", "--no-cooldown",
                "--no-jina"]
        if not use_cache:
            argv.append("--no-cache")
        argv.extend(extra)
        buf = io.StringIO()
        with env(), mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(buf):
            m.main()
        return buf.getvalue()

    def test_latency_and_phase_timings(self):
        payload = json.loads(self._run("--json"))
        health = payload["engine_health"]
        self.assertEqual(set(health["latency_ms"]), {"t_obs"})
        self.assertIsInstance(health["latency_ms"]["t_obs"], int)
        self.assertGreaterEqual(health["latency_ms"]["t_obs"], 0)
        self.assertEqual(set(health["timing"]),
                         {"recall_ms", "rerank_ms", "dedupe_ms"})
        for k, v in health["timing"].items():
            self.assertIsInstance(v, int, k)
            self.assertGreaterEqual(v, 0, k)
        self.assertEqual(health["timing"]["rerank_ms"], 0)  # --no-jina
        self.assertNotIn("cache_age_secs", health)

    def test_cache_hit_reports_age(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(m, "CACHE_DIR", td), \
                mock.patch.object(m, "CACHE_PATH",
                                  os.path.join(td, "c.json")):
            first = json.loads(self._run("--json", use_cache=True))
            second = json.loads(self._run("--json", use_cache=True))
        self.assertFalse(first["engine_health"]["cached"])
        self.assertTrue(second["engine_health"]["cached"])
        self.assertIsInstance(second["engine_health"]["cache_age_secs"], int)
        self.assertGreaterEqual(second["engine_health"]["cache_age_secs"], 0)

    def test_markdown_has_no_telemetry(self):
        out = self._run()
        self.assertIn("engines: t_obs=ok (5)", out)
        for token in ("latency_ms", "recall_ms", "dedupe_ms"):
            self.assertNotIn(token, out)


class EnvDefaultTest(unittest.TestCase):
    """AGG_SEARCH_* fills omitted flags only; explicit flags and bad
    values are unaffected."""

    def setUp(self):
        self.calls = 0
        m.ENGINES["t_obs"] = (self.fake, 1.0)
        m.ENGINES["t_obs2"] = (self.fake, 1.0)

    def tearDown(self):
        del m.ENGINES["t_obs"], m.ENGINES["t_obs2"]

    def fake(self, q, n, recency):
        self.calls += 1
        return ([(u, "T", "s" * 500) for u in urls(n)], None)

    def _run(self, *extra, env_vals=None, engines="t_obs"):
        argv = ["agg-search", "q", "--no-cooldown", "--no-jina", "--no-cache"]
        if engines:
            argv += ["--engines", engines]
        argv.extend(extra)
        buf = io.StringIO()
        with env(**(env_vals or {})), mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(buf):
            m.main()
        return buf.getvalue()

    def test_env_engines_when_flag_omitted(self):
        payload = json.loads(self._run(
            "--json", env_vals={"AGG_SEARCH_ENGINES": " t_obs "},
            engines=""))
        self.assertEqual(set(payload["engines"]), {"t_obs"})
        self.assertEqual(self.calls, 1)

    def test_explicit_engines_beat_env(self):
        payload = json.loads(self._run(
            "--json", env_vals={"AGG_SEARCH_ENGINES": "t_obs"},
            engines="t_obs2"))
        self.assertEqual(set(payload["engines"]), {"t_obs2"})

    def test_env_top_n_and_per_engine(self):
        payload = json.loads(self._run(
            "--json", env_vals={"AGG_SEARCH_TOP_N": "2"}))
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual(len(json.loads(self._run(
            "--json", env_vals={"AGG_SEARCH_PER_ENGINE": "1"}))["results"]), 1)

    def test_explicit_top_n_beats_env(self):
        payload = json.loads(self._run(
            "--json", "--top-n", "4", env_vals={"AGG_SEARCH_TOP_N": "2"}))
        self.assertEqual(len(payload["results"]), 4)

    def test_env_snippet_chars_and_explicit_override(self):
        short = json.loads(self._run(
            "--json", env_vals={"AGG_SEARCH_SNIPPET_CHARS": "80"}))
        for r in short["results"]:
            self.assertEqual(len(r["snippet"]), 80)
        wide = json.loads(self._run(
            "--json", "--snippet-chars", "200",
            env_vals={"AGG_SEARCH_SNIPPET_CHARS": "80"}))
        for r in wide["results"]:
            self.assertEqual(len(r["snippet"]), 200)

    def test_env_fast_and_no_jina(self):
        payload = json.loads(self._run(
            "--json", env_vals={"AGG_SEARCH_FAST": "yes",
                                "AGG_SEARCH_NO_JINA": "TRUE"}))
        self.assertTrue(payload["engine_health"]["fast"])
        self.assertTrue(payload["results"])

    def test_invalid_env_is_ignored(self):
        payload = json.loads(self._run(
            "--json", env_vals={"AGG_SEARCH_TOP_N": "lots",
                                "AGG_SEARCH_PER_ENGINE": "-3",
                                "AGG_SEARCH_SNIPPET_CHARS": "abc",
                                "AGG_SEARCH_ENGINES": "nope,also-nope",
                                "AGG_SEARCH_FAST": "maybe"}))
        health = payload["engine_health"]
        self.assertNotIn("fast", health)         # "maybe" is not truthy
        self.assertEqual(len(payload["results"]), 5)  # --top-n 10, 5 urls
        for r in payload["results"]:
            self.assertEqual(len(r["snippet"]), 400)


class ShowConfigTest(unittest.TestCase):
    """--show-config is a local, key-free diagnostic that stops early."""

    def setUp(self):
        m.ENGINES["t_obs"] = (lambda q, n, r: ([], None), 1.0)

    def tearDown(self):
        del m.ENGINES["t_obs"]

    def _run(self, *extra, env_vals=None):
        argv = ["agg-search", "--show-config"] + list(extra)
        buf = io.StringIO()
        boom = AssertionError("--show-config must not reach the network")
        with env(**(env_vals or {})), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(m, "recall_multi", side_effect=boom), \
                mock.patch.object(m, "recall_flat", side_effect=boom), \
                mock.patch.object(m, "jina_rerank", side_effect=boom), \
                mock.patch.object(m, "load_cache", side_effect=boom), \
                mock.patch.object(m, "load_cooldown", side_effect=boom), \
                contextlib.redirect_stdout(buf):
            m.main()  # returns (exit 0); must not sys.exit either
        return buf.getvalue()

    def test_shape_and_booleans(self):
        cfg = json.loads(self._run("--engines", "t_obs,exa"))
        for key in ("engines", "top_n", "per_engine", "recency",
                    "snippet_chars", "fast", "no_jina", "cache", "cooldown",
                    "credentials"):
            self.assertIn(key, cfg)
        self.assertEqual(cfg["engines"], ["t_obs", "exa"])
        self.assertIs(cfg["fast"], False)
        self.assertIs(cfg["no_jina"], False)
        self.assertIs(cfg["cache"], True)
        self.assertIs(cfg["cooldown"], True)
        self.assertEqual(set(cfg["credentials"]), {"t_obs", "exa"})
        for eng, has in cfg["credentials"].items():
            self.assertIsInstance(has, bool, eng)

    def test_no_query_needed_and_never_leaks_keys(self):
        secret = "sk-test-deadbeef"
        out = self._run(env_vals={
            "EXA_API_KEY": secret,
            "BRAVE_API_KEY": secret + "-2",
            "JINA_API_KEY": secret + "-3",
        })
        self.assertNotIn(secret, out)
        cfg = json.loads(out)
        self.assertEqual(cfg["engines"], m.DEFAULT_ENGINES.split(","))
        self.assertIs(cfg["credentials"]["exa"], True)

    def test_reflects_env_defaults(self):
        cfg = json.loads(self._run(env_vals={
            "AGG_SEARCH_ENGINES": "t_obs",
            "AGG_SEARCH_TOP_N": "3",
            "AGG_SEARCH_SNIPPET_CHARS": "120",
            "AGG_SEARCH_FAST": "1",
            "AGG_SEARCH_NO_JINA": "yes",
        }))
        self.assertEqual(cfg["engines"], ["t_obs"])
        self.assertEqual((cfg["top_n"], cfg["snippet_chars"]), (3, 120))
        self.assertIs(cfg["fast"], True)
        self.assertIs(cfg["no_jina"], True)
        self.assertEqual(cfg["min_sources"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
