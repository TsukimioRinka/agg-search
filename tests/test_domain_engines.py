"""Domain engines added in 0.7.0: arxiv (academic fallback) and the
brave/serper news verticals. Offline: every network call is stubbed."""

import io
import json
import unittest
import urllib.parse
from unittest import mock

from agg_search import engine_specs, portals, profiles, search

ATOM = b"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2211.17192v2</id>
    <title>Fast Inference from Transformers
      via Speculative Decoding</title>
    <summary>  We present speculative decoding.\u200b  </summary>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/cs/0101001v1</id>
    <title>Old style id</title>
    <summary>legacy</summary>
  </entry>
  <entry>
    <id>https://example.com/not-arxiv</id>
    <title>skip me</title>
    <summary>x</summary>
  </entry>
</feed>"""


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, body):
        self.body = body
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        return _Resp(self.body)


class ArxivTest(unittest.TestCase):
    def test_query_ands_latin_terms_and_drops_cjk_and_stopwords(self):
        self.assertEqual(search.arxiv_query("speculative decoding for LLMs"),
                         "all:speculative AND all:decoding AND all:LLMs")
        self.assertEqual(search.arxiv_query("直接偏好优化 DPO 对齐"), "all:DPO")
        self.assertEqual(search.arxiv_query("联邦学习 差分隐私"), "")

    def test_parse_strips_versions_whitespace_and_non_arxiv_ids(self):
        rows = search.parse_arxiv_atom(ATOM, 10)
        self.assertEqual([r[0] for r in rows],
                         ["https://arxiv.org/abs/2211.17192",
                          "https://arxiv.org/abs/cs/0101001"])
        self.assertEqual(rows[0][1],
                         "Fast Inference from Transformers via Speculative "
                         "Decoding")
        self.assertNotIn("\u200b", rows[0][2])
        self.assertEqual(len(search.parse_arxiv_atom(ATOM, 1)), 1)

    def test_parse_refuses_a_dtd(self):
        evil = (b'<?xml version="1.0"?><!DOCTYPE f [<!ENTITY x "y">]>'
                b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
        with self.assertRaises(ValueError):
            search.parse_arxiv_atom(evil, 5)

    def test_cjk_only_query_is_healthy_empty_without_network(self):
        opener = _Opener(ATOM)
        with mock.patch.object(search, "_OPENER", opener):
            self.assertEqual(search.eng_arxiv("联邦学习", 5, None), ([], None))
        self.assertEqual(opener.urls, [])

    def test_request_shape_and_recency_window(self):
        opener = _Opener(ATOM)
        with mock.patch.object(search, "_OPENER", opener):
            rows, err = search.eng_arxiv("speculative decoding", 5, "week")
        self.assertIsNone(err)
        self.assertEqual(len(rows), 2)
        url = opener.urls[0]
        self.assertTrue(url.startswith("https://export.arxiv.org/api/query?"))
        sq = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertIn("submittedDate:[", sq["search_query"][0])
        self.assertEqual(sq["max_results"], ["5"])

    def test_oversized_response_is_rejected(self):
        opener = _Opener(b"x" * (search.MAX_RESPONSE + 1))
        with mock.patch.object(search, "_OPENER", opener):
            with self.assertRaises(ValueError):
                search.eng_arxiv("speculative decoding", 5, None)


class NewsVerticalTest(unittest.TestCase):
    def test_brave_news_dates_snippets_and_uses_news_endpoint(self):
        body = json.dumps({"results": [
            {"url": "https://news.test/a/", "title": "A",
             "description": "body", "page_age": "2026-11-20T07:31:25"},
            {"title": "no url"},
        ]}).encode()
        opener = _Opener(body)
        with mock.patch.object(search, "_OPENER", opener), \
                mock.patch.object(search, "getenv",
                                  lambda k, d=None: "k" if k == "BRAVE_API_KEY" else d):
            rows, err = search.eng_brave_news("OpenAI", 5, "day")
        self.assertIsNone(err)
        self.assertEqual(rows, [("https://news.test/a", "A", "[2026-11-20] body")])
        self.assertIn("/res/v1/news/search?", opener.urls[0])
        self.assertIn("freshness=pd", opener.urls[0])

    def test_serper_news_dates_snippets(self):
        seen = {}

        def fake(url, **kw):
            seen["url"], seen["body"] = url, kw["body"]
            return {"news": [{"link": "https://n.test/x", "title": "X",
                              "snippet": "s", "date": "4 hours ago"},
                             {"title": "no link"}]}

        with mock.patch.object(search, "http_json", fake), \
                mock.patch.object(search, "getenv",
                                  lambda k, d=None: "k" if k == "SERPER_API_KEY" else d):
            rows, err = search.eng_serper_news("OpenAI", 5, "day")
        self.assertEqual(seen["url"], "https://google.serper.dev/news")
        self.assertEqual(seen["body"]["tbs"], "qdr:d")
        self.assertEqual(rows, [("https://n.test/x", "X", "[4 hours ago] s")])

    def test_missing_keys_report_no_key(self):
        with mock.patch.object(search, "getenv", lambda k, d=None: d):
            self.assertEqual(search.eng_brave_news("q", 5, None),
                             (None, "no BRAVE_API_KEY"))
            self.assertEqual(search.eng_serper_news("q", 5, None),
                             (None, "no SERPER_API_KEY"))

    def test_dated_prefix_is_bounded_and_sanitized(self):
        self.assertEqual(search._dated("", "t"), "t")
        out = search._dated("2026\u202e-11", "x" * 500)
        self.assertEqual(len(out), 400)
        self.assertNotIn("\u202e", out)


class DomainWiringTest(unittest.TestCase):
    def test_registry_entries(self):
        specs = engine_specs.ENGINE_SPECS
        self.assertTrue(specs["arxiv"]["keyless"])
        self.assertFalse(specs["arxiv"]["default"])
        self.assertEqual(specs["brave_news"]["env"], ("BRAVE_API_KEY",))
        self.assertEqual(specs["serper_news"]["env"], ("SERPER_API_KEY",))
        self.assertNotIn("arxiv", engine_specs.default_engines().split(","))

    def test_news_profile_keeps_web_endpoints_and_pure_jina(self):
        # 0.7.0's vertical-only default regressed on event queries (J)
        p = profiles.load("news")
        self.assertEqual(p["engines"][:2], ["brave", "serper"])
        self.assertEqual(p["primary"], ["brave"])
        self.assertIsNone(p.get("rerank_blend"))
        self.assertNotIn("brave_news", p["engines"])

    def test_academic_and_code_keyless_fallbacks(self):
        self.assertEqual(
            portals.load("codex.academic")["dependencies"]["keyless_fallback"],
            ["openalex", "arxiv", "searxng"])
        self.assertEqual(
            portals.load("codex.code")["dependencies"]["keyless_fallback"],
            ["stack", "searxng", "hn"])


if __name__ == "__main__":
    unittest.main()
