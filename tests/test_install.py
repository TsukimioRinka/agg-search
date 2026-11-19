#!/usr/bin/env python3
"""Tests for the per-client installer: no disk writes outside temp dirs."""

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from agg_search import install

ARGV = ["uvx", "--from", "agg-search", "agg-search-mcp"]


class DescribeTest(unittest.TestCase):
    def test_lists_every_client(self):
        self.assertGreaterEqual(len(install.CLIENTS), 12)
        for spec in install.CLIENTS.values():
            self.assertIn(spec["mode"], ("json", "command", "snippet"))

    def test_cursor_shape(self):
        info = install.describe("cursor", ARGV, {"EXA_API_KEY": "x"}, "user")
        entry = info["config"]["mcpServers"]["agg-search"]
        self.assertEqual(entry["command"], "uvx")
        self.assertEqual(entry["args"], ["--from", "agg-search",
                                         "agg-search-mcp"])
        self.assertEqual(entry["env"], {"EXA_API_KEY": "x"})
        self.assertTrue(info["path"].endswith(".cursor/mcp.json"))

    def test_vscode_uses_servers_key_and_stdio(self):
        info = install.describe("vscode", ARGV, {}, "project")
        entry = info["config"]["servers"]["agg-search"]
        self.assertEqual(entry["type"], "stdio")
        self.assertEqual(info["path"], ".vscode/mcp.json")

    def test_opencode_v2_shape(self):
        info = install.describe("opencode", ARGV, {}, "user")
        entry = info["config"]["mcp"]["servers"]["agg-search"]
        self.assertEqual(entry["type"], "local")
        self.assertEqual(entry["command"], ARGV)
        self.assertIn("environment", entry)

    def test_zed_context_servers(self):
        info = install.describe("zed", ARGV, {}, "user")
        self.assertIn("context_servers", info["config"])
        self.assertEqual(info["config"]["context_servers"]["agg-search"]
                         ["command"], "uvx")

    def test_command_clients(self):
        info = install.describe("claude-code", ARGV, {}, "user")
        self.assertEqual(info["command"][:4],
                         ["claude", "mcp", "add", "--scope"])
        self.assertIn("uvx", info["command"])
        self.assertIn("--from", info["command"])

    def test_generic_snippet(self):
        info = install.describe("generic", ARGV, {}, "user")
        parsed = json.loads(info["snippet"])
        self.assertIn("agg-search", parsed["mcpServers"])

    def test_codex_prints_toml(self):
        info = install.describe("codex", ARGV, {}, "user")
        self.assertEqual(info["mode"], "command")
        toml = install.CLIENTS["codex"]["toml"]
        self.assertIn("[mcp_servers.agg-search]", toml)


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "mcp.json")
        pathlib.Path(self.path).write_text(
            json.dumps({"mcpServers": {"other": {"command": "x"}},
                        "keep": 1}), encoding="utf-8")

    def test_merge_preserves_other_keys_and_backs_up(self):
        written = install.write_config(
            self.path, ("mcpServers",), "agg-search",
            {"command": "uvx", "args": [], "env": {}})
        doc = json.loads(pathlib.Path(written).read_text(encoding="utf-8"))
        self.assertEqual(doc["keep"], 1)
        self.assertIn("other", doc["mcpServers"])
        self.assertEqual(doc["mcpServers"]["agg-search"]["command"], "uvx")
        backups = [p for p in os.listdir(self.tmp.name) if ".bak-" in p]
        self.assertEqual(len(backups), 1)

    def test_refuses_invalid_json(self):
        pathlib.Path(self.path).write_text("{oops", encoding="utf-8")
        with self.assertRaises(SystemExit):
            install.write_config(self.path, ("mcpServers",), "agg-search", {})

    def test_creates_missing_file_and_section(self):
        target = os.path.join(self.tmp.name, "nested", "mcp.json")
        install.write_config(target, ("mcp", "servers"), "agg-search",
                             {"type": "local"})
        doc = json.loads(pathlib.Path(target).read_text(encoding="utf-8"))
        self.assertEqual(doc["mcp"]["servers"]["agg-search"]["type"], "local")

    def test_old_backups_are_pruned(self):
        stamps = iter("20260101-%06d" % i for i in range(20))
        with mock.patch.object(install.time, "strftime",
                               side_effect=lambda *a: next(stamps)):
            for _ in range(8):
                install.write_config(self.path, ("mcpServers",),
                                     "agg-search", {"command": "uvx"})
        backups = [n for n in os.listdir(self.tmp.name) if ".bak-" in n]
        self.assertEqual(len(backups), 5)

    def test_preserves_file_mode_on_write_and_backup(self):
        os.chmod(self.path, 0o600)
        install.write_config(self.path, ("mcpServers",), "agg-search",
                             {"command": "uvx"})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        for name in os.listdir(self.tmp.name):
            if ".bak-" in name:
                backup = os.path.join(self.tmp.name, name)
                self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "mcp.json")

    def _info(self):
        return install.describe("cursor", ["uvx"], {}, "user")

    def test_missing_file_reports_problem(self):
        info = {"mode": "json", "path": self.path,
                "section": ["mcpServers"]}
        with mock.patch.object(install.shutil, "which", return_value="/bin/uvx"):
            self.assertEqual(install.check_client("cursor", info, "uvx"), 1)

    def test_written_config_passes(self):
        info = {"mode": "json", "path": self.path,
                "section": ["mcpServers"]}
        install.write_config(self.path, ("mcpServers",), "agg-search",
                             {"command": "uvx", "args": []})
        with mock.patch.object(install.shutil, "which", return_value="/bin/uvx"):
            self.assertEqual(install.check_client("cursor", info, "uvx"), 0)

    def test_missing_entry_fails(self):
        info = {"mode": "json", "path": self.path,
                "section": ["mcpServers"]}
        pathlib.Path(self.path).write_text(json.dumps({"mcpServers": {}}),
                                           encoding="utf-8")
        with mock.patch.object(install.shutil, "which", return_value="/bin/uvx"):
            self.assertEqual(install.check_client("cursor", info, "uvx"), 1)

    def test_runtime_missing_fails(self):
        info = {"mode": "json", "path": self.path,
                "section": ["mcpServers"]}
        install.write_config(self.path, ("mcpServers",), "agg-search",
                             {"command": "uvx", "args": []})
        with mock.patch.object(install.shutil, "which", return_value=None):
            self.assertEqual(install.check_client("cursor", info, "uvx"), 1)

    def test_list_form_command_passes(self):
        # OpenCode stores the whole argv in `command`, not a string.
        info = {"mode": "json", "path": self.path,
                "section": ["mcp", "servers"]}
        install.write_config(self.path, ("mcp", "servers"), "agg-search",
                             {"type": "local",
                              "command": ["uvx", "--from", "agg-search"],
                              "environment": {}})
        with mock.patch.object(install.shutil, "which", return_value="/bin/uvx"):
            self.assertEqual(install.check_client("opencode", info, "uvx"), 0)


class CliTest(unittest.TestCase):
    def test_env_parsing_reaches_entry(self):
        info = install.CLIENTS["generic"]
        entry = install.payload(info.get("shape", "mcpServers"), ARGV,
                                {"A": "1"})
        self.assertEqual(entry["env"], {"A": "1"})

    def test_main_default_print(self):
        rc = install.main(["--client", "generic"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
