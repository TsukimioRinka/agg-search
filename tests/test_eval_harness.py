"""agg-search-eval record/judge/compare: offline, no engine or LLM calls."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from agg_search import eval as ev
from agg_search import profiles, search

FIXTURE = {
    "queries": {
        "q1": {"engines": {
            "brave": [["https://a.test/x", "A", "[2026-11-20] alpha"],
                      ["https://b.test/y", "B", "beta"]],
            "arxiv": [["https://arxiv.org/abs/1", "P", "paper"]],
        }, "status": {}},
    },
}


class VariantTest(unittest.TestCase):
    def test_variant_overrides_profile(self):
        prof = profiles.load("academic")
        cfg = ev.parse_variant(
            "B:engines=+arxiv,-tavily;w.arxiv=1.3;blend=none;boost=1.1;dedupe=0",
            prof)
        self.assertEqual(cfg["name"], "B")
        self.assertIn("arxiv", cfg["engines"])
        self.assertNotIn("tavily", cfg["engines"])
        self.assertEqual(cfg["weights"]["arxiv"], 1.3)
        self.assertIsNone(cfg["blend"])
        self.assertEqual(cfg["boost"], 1.1)
        self.assertFalse(cfg["dedupe"])

    def test_full_engine_list_and_unknown_key(self):
        prof = profiles.load("news")
        cfg = ev.parse_variant("A:engines=brave,serper", prof)
        self.assertEqual(cfg["engines"], ["brave", "serper"])
        self.assertIsNone(ev.parse_variant("current", prof)["blend"])  # news: pure Jina
        with self.assertRaises(SystemExit):
            ev.parse_variant("X:bogus=1", prof)


class MetricTest(unittest.TestCase):
    def test_graded_ndcg(self):
        grades = {"https://a.test/x": 2, "https://b.test/y": 1}
        self.assertAlmostEqual(ev.graded_ndcg(
            ["https://a.test/x", "https://b.test/y"], grades), 1.0)
        self.assertLess(ev.graded_ndcg(
            ["https://b.test/y", "https://a.test/x"], grades), 1.0)
        self.assertEqual(ev.graded_ndcg(["https://z.test"], {}), 0.0)

    def test_legacy_binary_judgments_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "j.json")
            with open(p, "w") as f:
                json.dump({"q1": ["https://www.a.test/x/"]}, f)
            self.assertEqual(ev.load_grades(p),
                             {"q1": {"https://a.test/x": 1.0}})

    def test_pool_strips_news_date_prefix(self):
        pool = ev.fixture_pool(FIXTURE, "q1")
        self.assertEqual(pool["https://a.test/x"], ("A", "alpha"))
        self.assertEqual(len(pool), 3)


class ReplayTest(unittest.TestCase):
    def test_engine_filter_and_rerank_blend(self):
        prof = profiles.load("general")
        only_brave = ev.parse_variant("A:engines=brave", prof)
        out = ev.replay(FIXTURE, only_brave, None, 10)
        self.assertEqual(out["q1"], ["https://a.test/x", "https://b.test/y"])
        pure = ev.parse_variant("B:engines=brave;blend=none", prof)
        scores = {"q1": {"https://b.test/y": 0.9, "https://a.test/x": 0.1}}
        self.assertEqual(ev.replay(FIXTURE, pure, scores, 10)["q1"][0],
                         "https://b.test/y")

    def test_compare_prints_table(self):
        with tempfile.TemporaryDirectory() as d:
            fx, jd = os.path.join(d, "fx.json"), os.path.join(d, "j.json")
            with open(fx, "w") as f:
                json.dump(FIXTURE, f)
            with open(jd, "w") as f:
                json.dump({"q1": {"https://arxiv.org/abs/1": 2}}, f)
            a = SimpleNamespace(fixture=fx, judgments=jd, profile="general",
                                rerank_scores=None, top_n=None,
                                variant=["A:engines=brave",
                                         "B:engines=brave,arxiv"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ev.compare(a)
        text = out.getvalue()
        self.assertIn("| A | 0.000 |", text)
        self.assertIn("| B | 0.631 | 1/5 |", text)  # paper at rank 3: 1/log2(4)
        self.assertIn("1/0", text)


class JudgeRecordTest(unittest.TestCase):
    def test_judge_is_resumable_and_never_regrades(self):
        calls = []

        def fake(q, t, s, rubric, model, today=None):
            calls.append(t)
            seen_today.add(today)
            return 2

        seen_today = set()

        with tempfile.TemporaryDirectory() as d:
            fx, out = os.path.join(d, "fx.json"), os.path.join(d, "j.json")
            with open(fx, "w") as f:
                json.dump(FIXTURE, f)
            with open(out, "w") as f:
                json.dump({"q1": {"https://a.test/x": 1}}, f)
            a = SimpleNamespace(fixture=fx, out=out, judge="qwen",
                                rubric="news", model="m", workers=1)
            with mock.patch.object(ev, "_qwen_grade", fake), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                ev.judge(a)
            with open(out) as f:
                labels = json.load(f)
        self.assertEqual(sorted(calls), ["B", "P"])
        self.assertEqual(len(seen_today), 1)  # one reference date per run
        self.assertRegex(seen_today.pop(), r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(labels["q1"]["https://a.test/x"], 1)

    def test_prompt_carries_reference_date(self):
        seen = {}

        def fake_http(url, **kw):
            seen["prompt"] = kw["body"]["prompt"]
            return {"response": "2"}

        with mock.patch.object(search, "http_json", fake_http):
            self.assertEqual(ev._qwen_grade("q", "t", "s", "news", "m",
                                            "2026-11-20"), 2)
        self.assertIn("The search was made on 2026-11-20.", seen["prompt"])

    def test_record_skips_cooling_engines(self):
        seen = {}

        def fake_recall(q, n, engines, recency):
            seen["engines"], seen["recency"] = list(engines), recency
            return {}, {}

        with tempfile.TemporaryDirectory() as d:
            qp, out = os.path.join(d, "q.json"), os.path.join(d, "fx.json")
            with open(qp, "w") as f:
                json.dump(["q1"], f)
            a = SimpleNamespace(queries=qp, out=out, engines="brave,serper",
                                per_engine=5, recency="day",
                                ignore_cooldown=False)
            with mock.patch.object(search, "load_cooldown",
                                   return_value={"serper": 9e12}), \
                    mock.patch.object(search, "recall", fake_recall), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                ev.record(a)
            with open(out) as f:
                fx = json.load(f)
        self.assertEqual(seen, {"engines": ["brave"], "recency": "day"})
        self.assertEqual(fx["queries"]["q1"]["status"]["serper"],
                         "skipped: cooling")


if __name__ == "__main__":
    unittest.main()
