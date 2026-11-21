"""Dual-era MCP: 2026-07-28 stateless requests next to the legacy handshake.

Each test cites the rule it checks from the 2026-07-28 specification
(basic/index, basic/versioning, server/discover, server/utilities/caching,
basic/transports/stdio, basic/transports/streamable-http).
"""

import base64
import http.client
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

from agg_search import http_transport, mcp

V = "2026-07-28"


def modern(method, params=None, rid=1, version=V, caps=True):
    meta = {mcp.META_VERSION: version,
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "0"}}
    if caps:
        meta[mcp.META_CAPABILITIES] = {}
    body = dict(params or {})
    body["_meta"] = meta
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": body}


def fake_call_tool(name, arguments, request_id=None):
    return {"content": [{"type": "text", "text": "ok:%s" % name}],
            "isError": False}


class CoreTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("AGG_SEARCH_PORTAL", None)

    def test_discover_shape(self):  # server/discover: DiscoverResult
        r = mcp.handle(modern("server/discover"))
        self.assertEqual(r["resultType"], "complete")
        self.assertEqual(r["supportedVersions"][0], V)
        self.assertIn("2025-11-25", r["supportedVersions"])
        self.assertEqual(r["capabilities"]["tools"], {"listChanged": False})
        self.assertIn("prompts", r["capabilities"])
        self.assertEqual(r["_meta"][mcp.META_SERVER_INFO]["name"], "agg-search")
        self.assertTrue(r["instructions"])
        self.assertEqual((r["ttlMs"], r["cacheScope"]), (mcp.LIST_TTL_MS, "public"))

    def test_lists_carry_result_type_and_cache_hints(self):  # caching
        for method in ("tools/list", "prompts/list"):
            r = mcp.handle(modern(method))
            self.assertEqual(r["resultType"], "complete")
            self.assertGreaterEqual(r["ttlMs"], 0)
            self.assertEqual(r["cacheScope"], "public")
            self.assertIn(mcp.META_SERVER_INFO, r["_meta"])
        names = [t["name"] for t in mcp.handle(modern("tools/list"))["tools"]]
        self.assertEqual(names, [t["name"] for t in
                                 mcp.handle(modern("tools/list"))["tools"]])

    def test_call_and_prompt_results_are_complete_without_cache_hints(self):
        with mock.patch.object(mcp, "call_tool", fake_call_tool):
            r = mcp.handle(modern("tools/call", {"name": "search",
                                                 "arguments": {"query": "q"}}))
        self.assertEqual((r["resultType"], r["isError"]), ("complete", False))
        self.assertNotIn("ttlMs", r)
        p = mcp.handle(modern("prompts/get", {"name": "web-research",
                                              "arguments": {"topic": "x"}}))
        self.assertEqual(p["resultType"], "complete")

    def test_unsupported_version_is_32022_with_supported_list(self):  # versioning
        with self.assertRaises(mcp.ProtocolError) as cm:
            mcp.handle(modern("tools/list", version="1900-01-01"))
        e = cm.exception
        self.assertEqual((e.code, e.http_status), (-32022, 400))
        self.assertEqual(e.data["requested"], "1900-01-01")
        self.assertIn(V, e.data["supported"])

    def test_legacy_version_in_meta_points_to_initialize(self):
        with self.assertRaises(mcp.ProtocolError) as cm:
            mcp.handle(modern("tools/list", version="2025-06-18"))
        self.assertEqual(cm.exception.code, -32022)
        self.assertIn("initialize", cm.exception.message)

    def test_missing_required_meta_is_32602_400(self):  # basic/index _meta
        with self.assertRaises(mcp.ProtocolError) as cm:
            mcp.handle(modern("tools/list", caps=False))
        self.assertEqual((cm.exception.code, cm.exception.http_status), (-32602, 400))
        with self.assertRaises(mcp.ProtocolError) as cm:
            mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
        self.assertEqual(cm.exception.code, -32602)

    def test_removed_methods_are_not_found_on_modern(self):  # ping removed
        for method in ("ping", "initialize", "resources/list"):
            with self.assertRaises(mcp.ProtocolError) as cm:
                mcp.handle(modern(method))
            self.assertEqual((cm.exception.code, cm.exception.http_status),
                             (-32601, 404))

    def test_portal_isolation_holds_on_modern(self):
        with mock.patch.dict(os.environ, {"AGG_SEARCH_PORTAL": "claude-code.code"}):
            r = mcp.handle(modern("server/discover"))
            self.assertEqual(r["_meta"][mcp.META_SERVER_INFO]["name"], "agg-search-code")
            tools = [t["name"] for t in mcp.handle(modern("tools/list"))["tools"]]
            self.assertEqual(tools, ["search", "fetch"])
            with self.assertRaises(KeyError):
                mcp.handle(modern("tools/call", {"name": "answer",
                                                 "arguments": {"query": "q"}}))

    def test_legacy_handshake_is_unchanged(self):  # dual-era: initialize = legacy
        for asked, got in (("2025-06-18", "2025-06-18"), ("2025-11-25", "2025-11-25"),
                           ("2024-11-05", "2024-11-05"), (V, "2025-11-25"),
                           ("1900-01-01", "2025-11-25")):
            r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": asked}})
            self.assertEqual(r["protocolVersion"], got)
            self.assertNotIn("resultType", r)
        self.assertEqual(mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"}), {})
        self.assertNotIn("resultType", mcp.handle(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}))

    def _call(self, name, args):
        return mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})

    def test_bad_arguments_are_tool_errors_not_protocol_errors(self):  # SEP-1303
        nan = float("nan")
        for name, args in (("search", {}), ("search", {"query": ""}),
                           ("search", {"query": None}), ("search", ["q"]),
                           ("search", {"query": {"a": 1}}),
                           ("search", {"query": "q", "recency": "hour"}),
                           ("search", {"query": "q", "top_n": "abc"}),
                           ("search", {"query": "q", "budget_secs": nan}),
                           ("search", {"query": "q", "engines": 5}),
                           ("fetch", {"urls": []}), ("fetch", {"urls": "http://x"}),
                           ("fetch", {"urls": [5]})):
            r = self._call(name, args)
            self.assertTrue(r["isError"], (name, args))
            self.assertIn("invalid arguments", r["content"][0]["text"])

    def test_calls_that_worked_in_1_0_still_reach_the_cli(self):  # review H1
        seen = []

        def capture(name, arguments, request_id=None):
            seen.append(mcp.build_args(name, arguments))
            return {"content": [], "isError": False}

        ok = ({"query": "x", "recency": None, "top_n": None, "fast": None,
               "profile": None, "engines": None},
              {"query": "x", "engines": "exa,brave", "include_domains": "a.org"},
              {"query": "x", "top_n": 0, "snippet_chars": 0, "expand": 0},
              {"query": "x", "top_n": "5", "budget_secs": 500},
              {"query": 42}, {"query": "x", "answer": False, "fast": False})
        with mock.patch.object(mcp, "call_tool", capture):
            for args in ok:
                r = self._call("search", args)
                self.assertFalse(r["isError"], (args, r))
        self.assertIn("--engines", seen[1])
        self.assertEqual(seen[1][seen[1].index("--engines") + 1], "exa,brave")
        self.assertEqual(seen[3][seen[3].index("--top-n") + 1], "5")

    def test_malformed_params_are_invalid_params_not_internal(self):  # review M2
        for params in ([1], "s", {"_meta": "x"}):
            with self.assertRaises(mcp.ProtocolError) as cm:
                mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "server/discover",
                            "params": params})
            self.assertEqual((cm.exception.code, cm.exception.http_status),
                             (-32602, 400))

    def test_unknown_arguments_stay_ignored_and_never_widen_the_guard(self):
        seen = {}

        def capture(name, arguments, request_id=None):
            seen["args"] = mcp.build_args(name, arguments)
            return {"content": [], "isError": False}

        with mock.patch.object(mcp, "call_tool", capture):
            mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "fetch", "arguments": {
                            "urls": ["http://127.0.0.1/"], "allow_private": True}}})
        self.assertNotIn("--allow-private", seen["args"])


class StdioTest(unittest.TestCase):
    def _run(self, *messages, portal=None):
        env = dict(os.environ)
        env.pop("AGG_SEARCH_PORTAL", None)
        if portal:
            env["AGG_SEARCH_PORTAL"] = portal
        lines = "".join(json.dumps(m) + "\n" for m in messages)
        out = subprocess.run([sys.executable, "-m", "agg_search.mcp"], input=lines,
                             capture_output=True, text=True, timeout=60, env=env).stdout
        return {r["id"]: r for r in map(json.loads, out.splitlines())}

    def test_dual_era_probe_then_fallback(self):  # stdio: Backward Compatibility
        replies = self._run(
            modern("server/discover", rid=1),
            modern("server/discover", rid=2, version="2099-01-01"),
            {"jsonrpc": "2.0", "id": 3, "method": "initialize",
             "params": {"protocolVersion": "2025-11-25", "capabilities": {}}},
            modern("tools/list", rid=4),
            portal="omp.academic")
        self.assertEqual(replies[1]["result"]["supportedVersions"][0], V)
        self.assertEqual(replies[2]["error"]["code"], -32022)
        self.assertEqual(replies[2]["error"]["data"]["requested"], "2099-01-01")
        self.assertEqual(replies[3]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(len(replies[4]["result"]["tools"]), 3)

    def test_modern_batch_is_rejected_on_stdio_too(self):  # review L7
        env = dict(os.environ)
        env.pop("AGG_SEARCH_PORTAL", None)
        out = subprocess.run([sys.executable, "-m", "agg_search.mcp"],
                             input=json.dumps([modern("tools/list")]) + "\n",
                             capture_output=True, text=True, timeout=60, env=env).stdout
        self.assertEqual(json.loads(out.splitlines()[0])["error"]["code"], -32600)


class HTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("AGG_SEARCH_PORTAL", None)
        cls._p = mock.patch.object(mcp, "call_tool", side_effect=fake_call_tool)
        cls._p.start()
        cls.server = http_transport.create_server("127.0.0.1", 0, "/mcp")
        threading.Thread(target=cls.server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._p.stop()

    def post(self, message, headers=None, auto=True):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if auto and isinstance(message, dict):
            h["MCP-Protocol-Version"] = V
            h["Mcp-Method"] = message["method"]
            name = (message.get("params") or {}).get("name")
            if name is not None:
                h["Mcp-Name"] = name
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/mcp", json.dumps(message), h)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), (json.loads(body) if body else None)

    def test_modern_round_trip_without_session(self):
        st, hdr, body = self.post(modern("server/discover"),
                                  {"Mcp-Session-Id": "never-issued"})
        self.assertEqual(st, 200)  # unknown session id is ignored, not 404
        self.assertEqual(body["result"]["resultType"], "complete")
        self.assertNotIn("Mcp-Session-Id", hdr)
        st, _, body = self.post(modern("tools/call", {"name": "search",
                                                      "arguments": {"query": "q"}}))
        self.assertEqual((st, body["result"]["resultType"]), (200, "complete"))

    def test_header_validation_is_32020_400(self):  # Server Validation
        msg = modern("tools/call", {"name": "search", "arguments": {"query": "q"}})
        for headers in ({"Mcp-Name": "fetch"}, {"Mcp-Method": "tools/list"},
                        {"MCP-Protocol-Version": "2025-06-18"}):
            st, _, body = self.post(msg, headers)
            self.assertEqual((st, body["error"]["code"]), (400, -32020), headers)
        st, _, body = self.post(msg, {"MCP-Protocol-Version": V, "Mcp-Method": "tools/call"},
                                auto=False)
        self.assertEqual((st, body["error"]["code"]), (400, -32020))  # Mcp-Name missing

    def test_base64_sentinel_name_is_decoded(self):  # Value Encoding
        enc = "=?base64?%s?=" % base64.b64encode(b"web-research").decode()
        st, _, body = self.post(modern("prompts/get", {
            "name": "web-research", "arguments": {"topic": "t"}}), {"Mcp-Name": enc})
        self.assertEqual(st, 200, body)
        st, _, body = self.post(modern("prompts/get", {
            "name": "web-research", "arguments": {"topic": "t"}}),
            {"Mcp-Name": "=?base64?!!notb64?="})
        self.assertEqual((st, body["error"]["code"]), (400, -32020))

    def test_status_codes_for_modern_errors(self):  # Protocol Version Header
        st, _, body = self.post(modern("tools/list", version="2099-01-01"),
                                {"MCP-Protocol-Version": "2099-01-01"})
        self.assertEqual((st, body["error"]["code"]), (400, -32022))
        st, _, body = self.post(modern("resources/list"))
        self.assertEqual((st, body["error"]["code"]), (404, -32601))
        st, _, body = self.post(modern("tools/list", caps=False))
        self.assertEqual((st, body["error"]["code"]), (400, -32602))

    def raw_post(self, body, header_items):
        """Send exact header lines (http.client would merge nothing, but a
        dict cannot hold duplicates)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/mcp")
        data = json.dumps(body).encode()
        for k, v in [("Content-Type", "application/json"),
                     ("Content-Length", str(len(data)))] + header_items:
            conn.putheader(k, v)
        conn.endheaders(data)
        resp = conn.getresponse()
        out = json.loads(resp.read() or b"null")
        conn.close()
        return resp.status, out

    def test_repeated_mirrored_header_is_rejected(self):  # review L3
        st, body = self.raw_post(modern("tools/list"), [
            ("MCP-Protocol-Version", V), ("Mcp-Method", "tools/list"),
            ("Mcp-Method", "tools/call")])
        self.assertEqual((st, body["error"]["code"]), (400, -32020))

    def test_malformed_discover_over_http_is_400(self):  # review M2/L5
        for params in ([1], {"_meta": "x"}, {}):
            st, body = self.raw_post(
                {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": params},
                [("MCP-Protocol-Version", V), ("Mcp-Method", "server/discover")])
            self.assertEqual((st, body["error"]["code"]), (400, -32602), params)

    def test_modern_header_without_meta_and_modern_batch_are_rejected(self):
        st, _, body = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                {"MCP-Protocol-Version": V}, auto=False)
        self.assertEqual((st, body["error"]["code"]), (400, -32602))
        st, _, _ = self.post([modern("tools/list")], auto=False)
        self.assertEqual(st, 400)

    def test_peer_reset_logs_one_line_not_a_traceback(self):
        import contextlib
        import io
        import socket
        import struct
        import time
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            s = socket.create_connection(("127.0.0.1", self.port))
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()  # RST
            for _ in range(40):
                if "closed by peer" in err.getvalue():
                    break
                time.sleep(0.05)
        self.assertIn("closed by peer", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_legacy_http_still_issues_sessions(self):
        st, hdr, body = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-11-25"}}, auto=False)
        self.assertEqual((st, body["result"]["protocolVersion"]), (200, "2025-11-25"))
        self.assertTrue(hdr.get("Mcp-Session-Id"))


if __name__ == "__main__":
    unittest.main()
