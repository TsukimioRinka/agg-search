"""docs/stability.md must name every stable surface that actually exists."""

import pathlib
import re
import unittest

from agg_search import fetch, mcp, portals, profiles

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "stability.md").read_text(encoding="utf-8")


class StabilityDocTest(unittest.TestCase):
    def test_every_console_script_is_listed(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        block = pyproject.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
        scripts = re.findall(r"^([a-z-]+) =", block, flags=re.M)
        self.assertEqual(len(scripts), 7)
        for name in scripts:
            self.assertIn("`%s`" % name, DOC)

    def test_mcp_tools_protocols_and_profiles(self):
        for tool in mcp.ALL_TOOLS:
            self.assertIn("`%s`" % tool, DOC)
        for version in mcp.SUPPORTED_PROTOCOLS:
            self.assertIn("`%s`" % version, DOC)
        for name in profiles.names():
            self.assertIn("`%s`" % name, DOC)

    def test_server_names_and_fetch_contract(self):
        servers = {portals.load("codex.%s" % d)["server_name"]
                   for d in profiles.names()}
        for name in servers:
            self.assertIn("`%s`" % name, DOC)
        self.assertIn("15 个值", DOC)
        self.assertEqual(len(fetch.ERROR_CLASSES), 15)
        fetching = (ROOT / "docs" / "fetching.md").read_text(encoding="utf-8")
        for cls in fetch.ERROR_CLASSES:
            self.assertIn("`%s`" % cls, fetching)


if __name__ == "__main__":
    unittest.main()
