#!/usr/bin/env python3
"""Packaging invariants: stdlib-only runtime imports, py.typed, version sync."""

import ast
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "agg_search"


def imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:  # relative / package-internal
                continue
            names.add(node.module.split(".")[0])
    return names


class StdlibOnlyTest(unittest.TestCase):
    # fetch_container.py runs only inside the crawl-agent image, where
    # scrapling is installed; the host never imports it.
    CONTAINER_ONLY = {"fetch_container.py"}

    def test_no_third_party_imports(self):
        # `import learn` / `import profiles` are this package's own modules
        # (absolute-import fallbacks for direct script execution).
        local = {p.stem for p in SRC.glob("*.py")}
        stdlib = set(sys.stdlib_module_names) | {"agg_search"} | local
        offenders = {}
        for path in sorted(SRC.glob("*.py")):
            if path.name in self.CONTAINER_ONLY:
                continue
            extra = imported_modules(path) - stdlib
            if extra:
                offenders[path.name] = sorted(extra)
        self.assertEqual(offenders, {}, "third-party imports found: %s" % offenders)

    def test_container_module_is_not_imported(self):
        for path in sorted(SRC.glob("*.py")):
            if path.name == "fetch_container.py":
                continue
            self.assertNotIn("fetch_container", imported_modules(path),
                             path.name)


class DistributionTest(unittest.TestCase):
    def test_py_typed_marker(self):
        self.assertTrue((SRC / "py.typed").is_file())

    def test_version_matches_pyproject(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        self.assertIsNotNone(match)
        from agg_search import __version__
        self.assertEqual(__version__, match.group(1))


if __name__ == "__main__":
    unittest.main()
