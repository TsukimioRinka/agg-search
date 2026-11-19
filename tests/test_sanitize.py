#!/usr/bin/env python3
"""Invisible-character sanitisation for text that reaches the model."""

import unittest

from agg_search import fetch, fetch_container, mcp, search

HIDDEN = "safe\u200btext\u202ehere\ufeff"


class SearchSanitizeTest(unittest.TestCase):
    def test_clean_text_drops_zero_width_and_bidi(self):
        self.assertEqual(search.clean_text(HIDDEN), "safetexthere")

    def test_clean_text_keeps_normal_unicode(self):
        text = "中文 العربية 日本語 — dash"
        self.assertEqual(search.clean_text(text), text)

    def test_fuse_cleans_titles_and_snippets(self):
        rows = {"brave": [("https://a", "t\u200b", "s\u202e")]}
        fused = search.rrf_fuse(rows, 1)
        self.assertEqual(fused[0][2]["title"], "t")
        self.assertEqual(fused[0][2]["snippet"], "s")


class EscapeTest(unittest.TestCase):
    def test_markdown_link_breakout_is_escaped(self):
        evil = "x](https://evil.example) [click"
        escaped = search.md_escape(evil)
        self.assertNotIn("](", escaped.replace("\\]", "\\]"))
        self.assertIn("\\]", escaped)
        self.assertIn("\\(", escaped)

    def test_control_characters_are_stripped(self):
        self.assertEqual(search.clean_text("a\x1b[31mb\x07c"), "a[31mbc")


class FetchSanitizeTest(unittest.TestCase):
    def test_host_squash_strips(self):
        self.assertEqual(fetch.squash("a\u200db\u202e c"), "ab c")

    def test_container_squash_strips(self):
        self.assertEqual(fetch_container.squash("x\u2066y\ufeff"), "xy")

    def test_mcp_instructions_warn_about_untrusted_content(self):
        self.assertIn("untrusted", mcp.INSTRUCTIONS.lower())


if __name__ == "__main__":
    unittest.main()
