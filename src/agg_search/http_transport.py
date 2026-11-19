#!/usr/bin/env python3
"""Streamable HTTP transport for the agg-search MCP server (spec 2025-06-18).

Usage:
    python -m agg_search.http_transport --host 127.0.0.1 --port 8765 --path /mcp

Design notes
------------
* Stdlib only, like the rest of the package.
* POST carries one JSON-RPC message (or a batch array); responses are plain
  ``application/json``. Notifications get ``202`` with an empty body. GET is
  answered with ``405`` unless the client asks for ``text/event-stream``, in
  which case the stream opens and closes at once: this server never initiates
  messages, which the spec permits.
* Sessions are opaque uuid4 values issued on ``initialize`` and kept in a
  bounded FIFO set (1000 ids). A session id we never issued is a hard ``404``.
* The endpoint is unauthenticated. It binds loopback by default and refuses
  requests whose ``Host``/``Origin`` is not loopback or explicitly allowed,
  which blocks DNS rebinding and drive-by browser requests. Binding a
  non-loopback address prints a loud warning; put a proxy in front.
* Every tool call runs in its handler thread; ``mcp.call_tool`` already has
  its own wall clock, so a slow engine cannot wedge the accept loop.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlsplit

from agg_search import mcp

MAX_BODY_BYTES = 1024 * 1024
# bodies up to this size are drained before rejecting with 413 so the client
# sees a clean error instead of a reset; larger ones get the socket closed.
MAX_DRAIN_BYTES = 32 * 1024 * 1024
MAX_SESSIONS = 1000
DEFAULT_PATH = "/mcp"
LOG_LOCK = threading.Lock()


class SessionStore:
    """Thread-safe bounded FIFO set of issued Mcp-Session-Id values."""

    def __init__(self, limit=MAX_SESSIONS):
        self._limit = limit
        self._ids = OrderedDict()
        self._lock = threading.Lock()

    def issue(self):
        session_id = uuid.uuid4().hex
        with self._lock:
            self._ids[session_id] = None
            while len(self._ids) > self._limit:
                self._ids.popitem(last=False)
        return session_id

    def knows(self, session_id):
        with self._lock:
            return session_id in self._ids

    def __len__(self):
        with self._lock:
            return len(self._ids)


class ServerConfig:
    """Immutable request policy plus the session table."""

    def __init__(self, path=DEFAULT_PATH, allow_origins=(), allow_hosts=()):
        self.path = path if path.startswith("/") else "/" + path
        self.allow_origins = {_normalise_origin(o) for o in allow_origins}
        self.allow_hosts = {_hostname_of(h) for h in allow_hosts}
        self.allow_hosts.discard("")
        self.sessions = SessionStore()


def _strip_brackets(host):
    host = host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def is_loopback_host(host):
    """True for localhost, 127.0.0.0/8 and ::1 (never for lookalikes)."""
    host = _strip_brackets(host or "")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _hostname_of(value):
    """Hostname from 'host', 'host:port' or '[::1]:port' ('' when invalid)."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit("//" + value)
        return (parts.hostname or "").lower()
    except ValueError:
        return ""


def _normalise_origin(origin):
    return (origin or "").strip().rstrip("/").lower()


def _is_loopback_origin(origin):
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    return is_loopback_host(parts.hostname or "")


def _jsonrpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _clean_to_log(value):
    """Keep a path safe for one log line (no control characters)."""
    return "".join(ch if " " <= ch <= "~" else "?" for ch in value)[:200]


def _process_message(message):
    """Run one JSON-RPC message.

    Returns ``(response_or_None, saw_initialize)``. A notification has no
    response; its side effects (cancellation) still run.
    """
    if not isinstance(message, dict):
        return _jsonrpc_error(None, -32600, "invalid request"), False
    if message.get("jsonrpc") != "2.0":
        return _jsonrpc_error(message.get("id"), -32600,
                              "invalid request"), False
    method = message.get("method")
    if not isinstance(method, str) or not method:
        return _jsonrpc_error(message.get("id"), -32600,
                              "invalid request"), False

    if "id" not in message:
        # Notification: never answered, even when the method is unknown.
        if method == "notifications/cancelled":
            mcp.cancel_request(message)
        try:
            mcp.handle(message)
        except Exception:  # noqa: BLE001 - notifications are fire-and-forget
            return None, method == "initialize"
        return None, method == "initialize"

    request_id = message.get("id")
    try:
        result = mcp.handle(message)
    except KeyError as exc:
        detail = exc.args[0] if exc.args else "unknown tool"
        return _jsonrpc_error(request_id, -32602, str(detail)), False
    except NotImplementedError as exc:
        return _jsonrpc_error(request_id, -32601, str(exc)), False
    except Exception as exc:  # noqa: BLE001 - a tool must not kill the server
        return _jsonrpc_error(request_id, -32603,
                              "internal error: %s" % exc), False
    return {"jsonrpc": "2.0", "id": request_id, "result": result}, \
        method == "initialize"


class MCPHandler(BaseHTTPRequestHandler):
    """One JSON-RPC message (or batch) per POST; no server-push stream."""

    protocol_version = "HTTP/1.1"
    server_version = "agg-search-mcp"
    sys_version = ""
    # socketserver applies this to the connection: a client that never
    # finishes a request line (or stalls mid-body) is dropped instead of
    # holding a thread forever.
    timeout = 30

    # -- plumbing ---------------------------------------------------------

    @property
    def config(self):
        return cast(_ThreadingHTTPServer, self.server).config

    def log_message(self, format, *args):
        # default per-request noise is replaced by one structured line
        return

    def _log(self, method, status, started):
        line = "%s %s -> %d (%d ms)\n" % (
            method, _clean_to_log(self.path), status,
            int((time.monotonic() - started) * 1000))
        with LOG_LOCK:
            sys.stderr.write(line)
            sys.stderr.flush()

    def _write(self, status, body=b"", headers=None, content_type=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _send_json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._write(status, body, headers, "application/json")

    def _reject(self, status, message, headers=None, close=True):
        """Error response; close the connection because the body is unread."""
        if close:
            self.close_connection = True
        self._send_json(status, _jsonrpc_error(None, -32600, message), headers)
        return status

    # -- policy -----------------------------------------------------------

    def _host_ok(self):
        hostname = _hostname_of(self.headers.get("Host", ""))
        if not hostname:
            return False
        return hostname in self.config.allow_hosts or \
            is_loopback_host(hostname)

    def _origin_ok(self, origin):
        normalised = _normalise_origin(origin)
        if normalised in self.config.allow_origins:
            return True
        return _is_loopback_origin(origin)

    def _guard(self):
        """Apply Host/Origin checks; 0 when allowed, else the sent status."""
        if not self._host_ok():
            return self._reject(403, "forbidden host")
        origin = self.headers.get("Origin")
        if origin is not None and not self._origin_ok(origin):
            return self._reject(403, "forbidden origin")
        return 0

    def _path_ok(self):
        if urlsplit(self.path).path == self.config.path:
            return True
        self._reject(404, "unknown path")
        return False

    def _session_ok(self):
        """0 when the session header is absent or known, else 404."""
        session_id = self.headers.get("Mcp-Session-Id")
        if session_id is None or self.config.sessions.knows(session_id):
            return 0
        return self._reject(404, "unknown session", close=False)

    # -- body -------------------------------------------------------------

    def _content_length(self):
        """Return (0, length), or (status, 0) after sending an error.

        Duplicate `Content-Length` headers and `Content-Length` together with
        `Transfer-Encoding` are request-smuggling vectors on a keep-alive
        connection; refuse both instead of trusting the first header.
        """
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) > 1:
            return self._reject(400, "duplicate content-length"), 0
        if lengths and self.headers.get("Transfer-Encoding"):
            return self._reject(400, "content-length with transfer-encoding"), 0
        raw = lengths[0] if lengths else None
        if raw is None:
            if self.headers.get("Transfer-Encoding"):
                return self._reject(411, "content-length required"), 0
            return 0, 0
        try:
            length = int(raw)
        except ValueError:
            return self._reject(400, "invalid content-length"), 0
        if length < 0:
            return self._reject(400, "invalid content-length"), 0
        return 0, length

    def _drain(self, length):
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)

    # -- verbs ------------------------------------------------------------

    def do_POST(self):
        started = time.monotonic()
        status = 500
        try:
            status = self._handle_post()
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            try:
                self._send_json(500, _jsonrpc_error(
                    None, -32603, "internal error: %s" % exc))
            except Exception:  # noqa: BLE001 - client already gone
                self.close_connection = True
            status = 500
        finally:
            self._log("POST", status, started)

    def _handle_post(self):
        rejected = self._guard()
        if rejected:
            return rejected
        if not self._path_ok():
            return 404

        ctype = (self.headers.get("Content-Type") or "").split(";")[0]
        if ctype.strip().lower() not in ("", "application/json"):
            return self._reject(415, "expected application/json")

        rejected, length = self._content_length()
        if rejected:
            return rejected
        if length > MAX_BODY_BYTES:
            if length <= MAX_DRAIN_BYTES:
                self._drain(length)
            return self._reject(413, "request body too large")

        body = self.rfile.read(length) if length else b""
        rejected = self._session_ok()
        if rejected:
            return rejected

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_json(200, _jsonrpc_error(None, -32700, "parse error"))
            return 200
        return self._dispatch(payload)

    def _dispatch(self, payload):
        initialized = False
        if isinstance(payload, list):
            if not payload:
                self._send_json(200, _jsonrpc_error(
                    None, -32600, "invalid request: empty batch"))
                return 200
            responses = []
            for message in payload:
                response, is_init = _process_message(message)
                initialized = initialized or is_init
                if response is not None:
                    responses.append(response)
            if not responses:
                self._write(202, b"", self._session_header(initialized))
                return 202
            self._send_json(200, responses, self._session_header(initialized))
            return 200

        response, initialized = _process_message(payload)
        if response is None:
            self._write(202, b"", self._session_header(initialized))
            return 202
        self._send_json(200, response, self._session_header(initialized))
        return 200

    def _session_header(self, initialized):
        if not initialized:
            return None
        return {"Mcp-Session-Id": self.config.sessions.issue()}

    def do_GET(self):
        started = time.monotonic()
        status = 500
        try:
            rejected = self._guard()
            if rejected:
                status = rejected
                return
            if not self._path_ok():
                status = 404
                return
            rejected = self._session_ok()
            if rejected:
                status = rejected
                return
            accept = (self.headers.get("Accept") or "").lower()
            if "text/event-stream" in accept:
                self._write(200, b"", {"Cache-Control": "no-cache"},
                            "text/event-stream")
                status = 200
                return
            self.close_connection = True
            self._write(405, b"", {"Allow": "POST"})
            status = 405
        finally:
            self._log("GET", status, started)

    def do_DELETE(self):
        started = time.monotonic()
        status = 405
        try:
            rejected = self._guard()
            if rejected:
                status = rejected
                return
            self.close_connection = True
            self._write(405, b"", {"Allow": "POST"})
        finally:
            self._log("DELETE", status, started)


class _ThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Bound the thread count: idle or slow clients must not be able to spawn
    # an unbounded number of handler threads.
    MAX_CONNECTIONS = 64

    def __init__(self, address, handler_cls, config):
        super().__init__(address, handler_cls)
        self.config = config
        self._slots = threading.BoundedSemaphore(self.MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            except OSError:
                pass
            self.close_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            self._slots.release()


class _ThreadingHTTPServerV6(_ThreadingHTTPServer):
    address_family = socket.AF_INET6


def create_server(host="127.0.0.1", port=8765, path=DEFAULT_PATH,
                  allow_origins=(), allow_hosts=()):
    """Build (but do not start) the HTTP server; port 0 picks a free port."""
    config = ServerConfig(path, allow_origins, allow_hosts)
    server_cls = _ThreadingHTTPServerV6 if ":" in host else _ThreadingHTTPServer
    return server_cls((host, port), MCPHandler, config)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m agg_search.http_transport",
        description="Streamable HTTP (MCP 2025-06-18) for agg-search.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--allow-origin", action="append", default=[],
                        metavar="ORIGIN",
                        help="extra allowed Origin (repeatable)")
    parser.add_argument("--allow-host", action="append", default=[],
                        metavar="HOST",
                        help="extra allowed Host name (repeatable)")
    args = parser.parse_args(argv)

    server = create_server(args.host, args.port, args.path,
                           args.allow_origin, args.allow_host)
    if not is_loopback_host(args.host):
        sys.stderr.write(
            "warning: bound to non-loopback %s; this endpoint is "
            "unauthenticated and must sit behind a proxy\n" % args.host)
    host, port = server.server_address[:2]
    sys.stderr.write("agg-search MCP streamable HTTP on http://%s:%d%s\n"
                     % (host, port, args.path))
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
