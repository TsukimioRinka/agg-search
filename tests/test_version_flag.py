"""Every console entry point answers --version with the package version."""

import contextlib
import io
import unittest
from unittest import mock

import agg_search
from agg_search import eval as eval_mod
from agg_search import fetch, http_transport, install, learn, mcp, search


class VersionFlagTest(unittest.TestCase):
    CASES = [
        ("search", search.main),
        ("fetch", fetch.main),
        ("mcp", lambda: mcp.main(["--version"])),
        ("http", lambda: http_transport.main(["--version"])),
        ("install", lambda: install.main(["--version"])),
        ("learn", lambda: learn.main(["--version"])),
        ("eval", eval_mod.main),
    ]

    def test_all_entry_points_print_version_and_exit_zero(self):
        for name, fn in self.CASES:
            with self.subTest(entry=name):
                out = io.StringIO()
                with mock.patch("sys.argv", [name, "--version"]), \
                        contextlib.redirect_stdout(out), \
                        self.assertRaises(SystemExit) as cm:
                    fn()
                self.assertEqual(cm.exception.code, 0)
                self.assertIn(agg_search.__version__, out.getvalue())


if __name__ == "__main__":
    unittest.main()
