"""STACKEXCHANGE_SITE opt-in multi-site: parsing, interleave, partial failure."""

import unittest
import urllib.error
import urllib.parse
from unittest import mock

from agg_search import search


def env(value):
    return lambda k, d=None: value if k == "STACKEXCHANGE_SITE" else d


def fake_http(failing=()):
    def call(url, **kw):
        site = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["site"][0]
        if site in failing:
            raise urllib.error.HTTPError(url, 502, "bad", {}, None)
        return {"items": [{"link": "https://%s.test/q/%d" % (site, i),
                           "title": "%s %d" % (site, i), "body": "b"}
                          for i in range(2)]}
    return call


class StackSitesTest(unittest.TestCase):
    def test_default_and_validation(self):
        with mock.patch.object(search, "getenv", env(None)):
            self.assertEqual(search.stack_sites(), ["stackoverflow"])
        with mock.patch.object(search, "getenv",
                               env("Unix, serverfault,unix,bad site,&x=1,a")):
            self.assertEqual(search.stack_sites(), ["unix", "serverfault"])
        with mock.patch.object(search, "getenv",
                               env("stackoverflow,unix,serverfault,superuser,askubuntu")):
            self.assertEqual(len(search.stack_sites()), search.MAX_SE_SITES)
        with mock.patch.object(search, "getenv", env(",,&")):
            self.assertEqual(search.stack_sites(), ["stackoverflow"])

    def test_single_site_keeps_one_request(self):
        calls = []
        with mock.patch.object(search, "getenv", env("superuser")), \
                mock.patch.object(search, "http_json",
                                  lambda url, **kw: calls.append(url) or {"items": []}):
            self.assertEqual(search.eng_stack("q", 5, None), ([], None))
        self.assertEqual(len(calls), 1)
        self.assertIn("site=superuser", calls[0])

    def test_interleaves_rank_by_rank(self):
        with mock.patch.object(search, "getenv", env("stackoverflow,unix")), \
                mock.patch.object(search, "http_json", fake_http()):
            rows, err = search.eng_stack("q", 5, None)
        self.assertIsNone(err)
        self.assertEqual([r[1] for r in rows],
                         ["stackoverflow 0", "unix 0", "stackoverflow 1", "unix 1"])

    def test_partial_failure_keeps_other_sites(self):
        with mock.patch.object(search, "getenv", env("stackoverflow,unix")), \
                mock.patch.object(search, "http_json", fake_http({"unix"})):
            rows, _ = search.eng_stack("q", 5, None)
        self.assertEqual([r[1] for r in rows], ["stackoverflow 0", "stackoverflow 1"])

    def test_all_sites_failing_raises_for_cooldown(self):
        with mock.patch.object(search, "getenv", env("stackoverflow,unix")), \
                mock.patch.object(search, "http_json",
                                  fake_http({"stackoverflow", "unix"})):
            with self.assertRaises(urllib.error.HTTPError):
                search.eng_stack("q", 5, None)


if __name__ == "__main__":
    unittest.main()
