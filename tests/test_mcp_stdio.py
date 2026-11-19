#!/usr/bin/env python3
"""End-to-end stdio conformance tests for `python -m agg_search.mcp`.

Spawns the real server and speaks JSON-RPC over its stdin/stdout. No network:
the one tool call used here fails fast on an invalid engine list.
"""

import json
import queue
import subprocess
import sys
import threading
import unittest


class Server:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "agg_search.mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        self.lines = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)

    def send(self, payload):
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def request(self, payload, timeout=20):
        self.send(payload)
        try:
            return json.loads(self.lines.get(timeout=timeout))
        except queue.Empty:
            self.fail("no response within %ss" % timeout)

    def fail(self, message):
        err = ""
        try:
            self.proc.kill()
            err = self.proc.stderr.read() or ""
        except Exception:  # noqa: BLE001
            pass
        raise AssertionError("%s\nserver stderr:\n%s" % (message, err))

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass


class StdioTest(unittest.TestCase):
    def setUp(self):
        self.server = Server()

    def tearDown(self):
        self.server.close()

    def test_initialize_negotiates_and_responds(self):
        response = self.server.request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "conformance", "version": "0"}},
        })
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "agg-search")
        self.assertIn("instructions", result)

    def test_notification_produces_no_response(self):
        self.server.request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {}})
        self.server.send({"jsonrpc": "2.0",
                          "method": "notifications/initialized"})
        response = self.server.request({"jsonrpc": "2.0", "id": 2,
                                        "method": "ping"})
        self.assertEqual(response["id"], 2)

    def test_tools_list_shape(self):
        response = self.server.request({"jsonrpc": "2.0", "id": 3,
                                        "method": "tools/list"})
        tools = {t["name"]: t for t in response["result"]["tools"]}
        self.assertEqual(set(tools), {"search", "fetch"})
        self.assertIn("outputSchema", tools["search"])
        self.assertTrue(tools["fetch"]["annotations"]["readOnlyHint"])

    def test_unknown_method_is_method_not_found(self):
        response = self.server.request({"jsonrpc": "2.0", "id": 4,
                                        "method": "resources/list"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_unknown_tool_is_invalid_params(self):
        response = self.server.request({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        })
        self.assertEqual(response["error"]["code"], -32602)

    def test_bad_json_is_parse_error(self):
        self.server.proc.stdin.write("{not json\n")
        self.server.proc.stdin.flush()
        response = json.loads(self.server.lines.get(timeout=10))
        self.assertEqual(response["error"]["code"], -32700)

    def test_tool_failure_is_an_error_result(self):
        response = self.server.request({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "search",
                       "arguments": {"query": "x", "engines": ["bogus"]}},
        }, timeout=60)
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertIn("unknown engines", result["content"][0]["text"])

    def test_batch_request_is_answered(self):
        self.server.send([
            {"jsonrpc": "2.0", "id": 8, "method": "ping"},
            {"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
        ])
        seen = {}
        for _ in range(2):
            response = json.loads(self.server.lines.get(timeout=10))
            seen[response["id"]] = response
        self.assertIn(8, seen)
        self.assertIn("tools", seen[9]["result"])


if __name__ == "__main__":
    unittest.main()
