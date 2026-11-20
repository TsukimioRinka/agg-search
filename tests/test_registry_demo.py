#!/usr/bin/env python3
"""Registry parity, runtime fallback, answer caps and artifact hygiene."""

import pathlib
import time
import unittest

from agg_search import engine_specs, install, mcp, portals, search


class EngineRegistryTest(unittest.TestCase):
    def test_every_layer_uses_the_registry(self):
        names = engine_specs.names()
        self.assertEqual(list(search.ENGINES), names)
        self.assertEqual(list(search.CREDENTIAL_ENV),
                         list(engine_specs.credential_env()))
        self.assertEqual(portals.ENGINE_ENV_REQUIREMENTS,
                         engine_specs.env_requirements())
        self.assertEqual(
            mcp.SEARCH_SCHEMA["properties"]["engines"]["items"]["enum"],
            names)
        self.assertEqual(search.DEFAULT_ENGINES,
                         engine_specs.default_engines())
        for name, (_fn, weight) in search.ENGINES.items():
            self.assertEqual(weight,
                             engine_specs.ENGINE_SPECS[name]["weight"])

    def test_engine_functions_cover_every_spec(self):
        self.assertEqual(set(search._ENGINE_FNS), set(engine_specs.names()))

    def test_keyless_engines_have_no_credentials(self):
        for name in engine_specs.keyless():
            self.assertEqual(engine_specs.ENGINE_SPECS[name]["env"], ())
            self.assertNotIn(name, search.CREDENTIAL_ENV)


class RuntimeFallbackTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: search.ENGINES[k] for k in ("t_fail", "t_fb")
                       if k in search.ENGINES}

    def tearDown(self):
        for name in ("t_fail", "t_fb"):
            search.ENGINES.pop(name, None)
        search.ENGINES.update(self._saved)

    def test_missing_required_detects_failures(self):
        status = {"openalex": "skipped/failed: HTTPError 429",
                  "exa": "ok (5)", "hn": "skipped: cooling"}
        self.assertEqual(search.missing_required(
            ["openalex", "exa", "hn"], status), ["openalex", "hn"])

    def test_supplement_keyless_returns_rows(self):
        def fail(q, n, r):
            return None, "HTTPError 500"

        def fallback(q, n, r):
            return [("https://fallback.test/x", "t", "s")], None

        search.ENGINES["t_fail"] = (fail, 1.0)
        search.ENGINES["t_fb"] = (fallback, 1.0)
        results, status, _note = search.supplement_keyless(
            ["q"], 3, None, ["t_fb"], time.monotonic() + 2)
        self.assertIn("t_fb#0", results)
        self.assertTrue(status["t_fb"].startswith("ok"))


class AnswerBoundsTest(unittest.TestCase):
    def test_source_text_and_totals_are_capped(self):
        sources = [{"title": "t%d" % i, "text": "x" * 30000}
                   for i in range(3)]
        bounded, notes = mcp._bound_sources(sources)
        total = sum(len(row.get("text") or "") for row in bounded)
        self.assertLessEqual(total, mcp.MAX_ANSWER_CHARS)
        self.assertTrue(any("truncated" in note for note in notes))

    def test_schema_declares_the_limits(self):
        text = mcp.ANSWER_SCHEMA["properties"]["sources"]["items"][
            "properties"]["text"]
        self.assertEqual(text["maxLength"], 20000)

    def test_combined_per_item_and_total_cap_enforced(self):
        sources = [{"title": f"t{i}", "text": "x" * n}
                   for i, n in enumerate((15000, 15000, 15000, 25000))]
        bounded, notes = mcp._bound_sources(sources)
        total = sum(len(row.get("text") or "") for row in bounded)
        self.assertEqual(total, mcp.MAX_ANSWER_CHARS)
        self.assertIn("source text truncated to 20000 chars", notes)
        self.assertIn("answer sources truncated at 60000 chars", notes)

    def test_dropped_trailing_source_records_degraded_note(self):
        sources = [{"title": f"t{i}", "text": "x" * 20000}
                   for i in range(4)]
        bounded, notes = mcp._bound_sources(sources)
        total = sum(len(row.get("text") or "") for row in bounded)
        self.assertEqual(len(bounded), 3)
        self.assertEqual(total, mcp.MAX_ANSWER_CHARS)
        self.assertIn("answer sources truncated at 60000 chars", notes)


class ArtifactHygieneTest(unittest.TestCase):
    def test_catalog_has_no_absolute_home_paths(self):
        home = str(pathlib.Path.home())
        data = install.catalog()
        blob = str(data)
        self.assertNotIn(home, blob)
        paths = [entry.get("path") for row in data["agents"]
                 for entry in row["entries"] if entry.get("path")]
        self.assertTrue(any(path.startswith("~/") for path in paths))


if __name__ == "__main__":
    unittest.main()
