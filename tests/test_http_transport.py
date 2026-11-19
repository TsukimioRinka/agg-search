#!/usr/bin/env python3
"""Conformance tests for the streamable HTTP transport.

The server runs on an ephemeral loopback port in a thread; ``mcp.call_tool``
is patched so no subprocess or network egress happens. Only 127.0.0.1 is
contacted.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from agg_search import http_transport, mcp

SESSION_HEADER = "Mcp-Session-Id"


def fake_call_tool(name, arguments, _request_id=None):
    return {"content": [{"type": "text", "text": "called %s" % name}],
            "structuredContent": {"name": name, "arguments": arguments},
            "isError": False}


def _header(headers, name):
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


class HTTPTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patcher = mock.patch.object(mcp, "call_tool",
                                         side_effect=fake_call_tool)
        cls._patcher.start()
        cls.server = http_transport.create_server(
            "127.0.0.1", 0, "/mcp",
            allow_origins=["http://allowed.example:9000"],
            allow_hosts=["allowed.example"])
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.01},
                                      daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base = "http://%s:%d" % (host, port)
        cls.url = cls.base + "/mcp"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls._patcher.stop()

    # -- helpers ----------------------------------------------------------

    def _request(self, url=None, method="POST", payload=None, body=None,
                 headers=None):
        if body is None and payload is not None:
            body = json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        request = urllib.request.Request(url or self.url, data=body,
                                         method=method,
                                         headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def _post(self, payload, headers=None):
        return self._request(payload=payload, headers=headers)

    def _initialize(self):
        status, headers, body = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": mcp.PROTOCOL_VERSION}})
        return status, headers, json.loads(body)

    # -- initialize / sessions -------------------------------------------

    def test_initialize_returns_version_and_session(self):
        status, headers, data = self._initialize()
        self.assertEqual(status, 200)
        self.assertEqual(data["result"]["protocolVersion"],
                         mcp.PROTOCOL_VERSION)
        self.assertEqual(data["result"]["serverInfo"], mcp.SERVER_INFO)
        session_id = _header(headers, SESSION_HEADER) or ""
        self.assertTrue(session_id)
        self.assertGreaterEqual(len(session_id), 32)

    def test_issued_session_is_accepted(self):
        _, headers, _ = self._initialize()
        session_id = _header(headers, SESSION_HEADER)
        status, _, body = self._post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={SESSION_HEADER: session_id})
        self.assertEqual(status, 200)
        names = {tool["name"] for tool in json.loads(body)["result"]["tools"]}
        self.assertIn("search", names)

    def test_unknown_session_is_404(self):
        status, _, body = self._post(
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            headers={SESSION_HEADER: "not-a-real-session"})
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

    def test_session_store_is_bounded_fifo(self):
        store = http_transport.SessionStore(limit=3)
        ids = [store.issue() for _ in range(5)]
        self.assertEqual(len(store), 3)
        self.assertFalse(store.knows(ids[0]))
        self.assertFalse(store.knows(ids[1]))
        self.assertTrue(store.knows(ids[-1]))

    # -- plain requests ---------------------------------------------------

    def test_tools_list(self):
        status, _, body = self._post(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["id"], 7)
        self.assertIn("result", data)

    def test_ping(self):
        status, _, body = self._post(
            {"jsonrpc": "2.0", "id": 8, "method": "ping"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"], {})

    def test_tool_call_is_handled_in_process(self):
        status, _, body = self._post(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": "search", "arguments": {"query": "x"}}})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIs(data["result"]["isError"], False)
        self.assertEqual(data["result"]["structuredContent"]["name"],
                         "search")

    def test_unknown_method_is_32601(self):
        status, _, body = self._post(
            {"jsonrpc": "2.0", "id": 10, "method": "does/not/exist"})
        self.assertEqual(status, 200)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], -32601)

    def test_notification_is_202_and_empty(self):
        status, _, body = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(status, 202)
        self.assertEqual(body, b"")

    def test_unknown_tool_is_32602(self):
        status, _, body = self._post(
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
             "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error"]["code"], -32602)

    # -- batches ----------------------------------------------------------

    def test_batch_returns_list(self):
        status, _, body = self._post([
            {"jsonrpc": "2.0", "id": "a", "method": "ping"},
            {"jsonrpc": "2.0", "id": "b", "method": "tools/list"}])
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIsInstance(data, list)
        self.assertEqual({entry["id"] for entry in data}, {"a", "b"})

    def test_batch_of_notifications_is_202(self):
        status, _, body = self._post([
            {"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(status, 202)
        self.assertEqual(body, b"")

    def test_empty_batch_is_invalid_request(self):
        status, _, body = self._post([])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error"]["code"], -32600)

    # -- malformed transport input ---------------------------------------

    def test_bad_json_is_parse_error(self):
        status, _, body = self._request(body=b"{not json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error"]["code"], -32700)

    def test_oversized_body_is_413(self):
        body = b"x" * (http_transport.MAX_BODY_BYTES + 1)
        status, _, _ = self._request(body=body)
        self.assertEqual(status, 413)

    def test_wrong_content_type_is_415(self):
        status, _, _ = self._request(
            body=b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_wrong_path_is_404(self):
        status, _, _ = self._request(url=self.base + "/other",
                                     payload={"jsonrpc": "2.0", "id": 1,
                                              "method": "ping"})
        self.assertEqual(status, 404)

    # -- security ---------------------------------------------------------

    def test_evil_origin_is_403(self):
        status, _, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_loopback_origin_is_allowed(self):
        status, _, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Origin": "http://127.0.0.1:8000"})
        self.assertEqual(status, 200)

    def test_allowed_origin_passes(self):
        status, _, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Origin": "http://allowed.example:9000"})
        self.assertEqual(status, 200)

    def test_evil_host_is_403(self):
        status, _, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_allowed_host_passes(self):
        status, _, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Host": "allowed.example"})
        self.assertEqual(status, 200)

    def test_lookalike_host_is_403(self):
        status, _, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Host": "127.0.0.1.evil.example"})
        self.assertEqual(status, 403)

    # -- other verbs ------------------------------------------------------

    def test_get_without_sse_accept_is_405(self):
        status, headers, _ = self._request(method="GET")
        self.assertEqual(status, 405)
        self.assertEqual(_header(headers, "Allow"), "POST")

    def test_get_with_sse_accept_closes_stream(self):
        status, headers, body = self._request(
            method="GET", headers={"Accept": "text/event-stream"})
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream",
                      _header(headers, "Content-Type") or "")
        self.assertEqual(body, b"")

    def test_get_with_unknown_session_is_404(self):
        status, _, _ = self._request(
            method="GET",
            headers={"Accept": "text/event-stream",
                     SESSION_HEADER: "not-a-real-session"})
        self.assertEqual(status, 404)

    def test_delete_is_405(self):
        status, headers, _ = self._request(method="DELETE")
        self.assertEqual(status, 405)
        self.assertEqual(_header(headers, "Allow"), "POST")


class OriginHelperTest(unittest.TestCase):
    def test_loopback_detection(self):
        for host in ("localhost", "127.0.0.1", "127.0.0.2", "::1",
                     "[::1]"):
            self.assertTrue(http_transport.is_loopback_host(host), host)
        for host in ("evil.example", "localhost.evil.example",
                     "127.0.0.1.evil.example", "", "::2"):
            self.assertFalse(http_transport.is_loopback_host(host), host)


if __name__ == "__main__":
    unittest.main()
